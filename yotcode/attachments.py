"""附件转换核心：把用户提供的图片 / 文件统一转成内容块（Block）。

设计目标
--------
项目的数据层（types.ImageBlock）与两个 provider 的协议转换都已经支持图片，
真正缺的是"输入端"。本模块负责所有输入入口的共通逻辑：

  * 文件路径 / 图片路径 -> Block（图片为 ImageBlock，文本为 TextBlock）
  * 系统剪贴板里的截图 -> ImageBlock
  * 粘贴文本中夹带的文件路径 -> Block 列表
  * 把用户输入文字与待发送附件合并成一份 list[Block]

所有对外函数都遵循"出错返回可读提示、绝不抛未捕获异常"的原则，
并统一做大小 / 类型校验，避免超大 base64 撑爆会话 JSONL 与 API 请求体。

为什么这样设计
--------------
1. 复用已有的 ImageBlock / Block 类型，不新增并行的数据模型；
   这样会话序列化、provider 转换、工具结果回填全部自动生效。
2. 图片统一走 base64 data URL；Anthropic provider 会转成 source.base64，
   OpenAI 兼容 provider 会转成 image_url，两条链路都已验证可用。
3. 文本附件沿用 agent.expand_mentions 的 <file> 包装风格，保持提示词一致。
"""
from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

from .types import Block, ImageBlock, TextBlock

# ---- 限制常量 -------------------------------------------------------------
# 与 tools/file_tools.py 里 Read 工具读图片的上限保持一致（5MB）。
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# 单个文本文件读进 prompt 的最大字符数，与 agent.expand_mentions 一致。
MAX_TEXT_CHARS = 60_000
# 一次最多附加多少个文件，避免请求体膨胀。
MAX_ATTACHMENTS = 10

# 常见图片扩展名 -> MIME。mimetypes 在少数系统上不全，这里兜底。
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def is_image_path(path: str | Path) -> bool:
    """按扩展名判断一个路径是否图片文件（大小写不敏感）。"""
    return Path(path).suffix.lower() in _IMAGE_MIME


def image_mime_type(path: str | Path) -> str:
    """返回图片的 MIME 类型；未知扩展名回退到 image/png。"""
    return _IMAGE_MIME.get(Path(path).suffix.lower(), "image/png")


def _resolve(path: str | Path, base_dir: Path | None) -> Path:
    """把相对路径按 base_dir（默认 cwd）解析成绝对路径。"""
    candidate = Path(os.path.expanduser(str(path)))
    if not candidate.is_absolute():
        candidate = (base_dir or Path.cwd()) / candidate
    try:
        return candidate.resolve()
    except OSError:
        return candidate


def _read_image_block(path: Path) -> Block:
    """读取图片文件并构造 ImageBlock；超限 / 出错返回提示性 TextBlock。"""
    if not path.is_file():
        return TextBlock(f"[附件] 找不到文件：{path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return TextBlock(f"[附件] 无法读取文件信息：{path}（{exc}）")
    if size > MAX_IMAGE_BYTES:
        return TextBlock(
            f"[附件] 图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限，已跳过：{path}"
        )
    try:
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
    except OSError as exc:
        return TextBlock(f"[附件] 读取图片失败：{path}（{exc}）")
    # 带上文件名，UI 筹码与会话回放时能显示来源。
    return ImageBlock(
        media_type=image_mime_type(path), data=encoded, filename=path.name
    )


def _read_text_block(path: Path) -> Block:
    """读取文本文件并构造带 <file> 包装的 TextBlock（与其他引用方式一致）。"""
    if not path.is_file():
        return TextBlock(f"[附件] 找不到文件：{path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return TextBlock(f"[附件] 无法读取文件信息：{path}（{exc}）")
    if size > MAX_IMAGE_BYTES:
        # 文本文件也设一个粗上限，避免把超大文件整段塞进 prompt。
        return TextBlock(
            f"[附件] 文件超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限，已跳过：{path}"
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return TextBlock(f"[附件] 读取文件失败：{path}（{exc}）")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n... (truncated)"
    return TextBlock(f'<file path="{path.name}">\n{text}\n</file>')


def path_to_block(path: str | Path, base_dir: Path | None = None) -> Block:
    """把单个文件或图片路径转成内容块。

    图片（png/jpg/...）-> ImageBlock；其他文件 -> 文本包装的 TextBlock。
    这是四种发送入口（CLI / 粘贴 / 拖放 / @文件）共用的转换函数。
    """
    resolved = _resolve(path, base_dir)
    if is_image_path(resolved):
        return _read_image_block(resolved)
    return _read_text_block(resolved)


def paths_to_blocks(paths: list[str | Path], base_dir: Path | None = None) -> list[Block]:
    """批量转换路径，超过 MAX_ATTACHMENTS 的部分丢弃并给出提示块。"""
    blocks: list[Block] = []
    for index, raw in enumerate(paths):
        if index >= MAX_ATTACHMENTS:
            blocks.append(
                TextBlock(f"[附件] 一次最多附加 {MAX_ATTACHMENTS} 个文件，其余已忽略。")
            )
            break
        blocks.append(path_to_block(raw, base_dir))
    return blocks


def clipboard_image_block() -> ImageBlock | None:
    """从系统剪贴板读取截图并转成 ImageBlock；没有图片或环境不支持时返回 None。

    终端里的普通 Ctrl+V 只会传来文本，不会携带剪贴板位图，因此这里直接读
    操作系统剪贴板。Pillow 已是必需依赖，但这里仍做防御性处理：环境异常时
    返回 None，由调用方给出友好提示，不让剪贴板问题拖垮整个附件功能。
    """
    try:
        from PIL import ImageGrab  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        image = ImageGrab.grabclipboard()  # type: ignore[attr-defined]
    except Exception:
        return None
    if image is None:
        return None
    # grabclipboard 可能返回文件名列表（复制的是文件而非位图），这里只处理位图。
    if isinstance(image, list):
        return None
    try:
        import io
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
    except Exception:
        return None
    if len(data) > MAX_IMAGE_BYTES:
        return None
    return ImageBlock(media_type="image/png", data=base64.b64encode(data).decode("ascii"))


def attachment_candidates(pasted_text: str, base_dir: Path | None = None) -> list[Block]:
    """从粘贴的文本里识别文件路径并转成附件块。

    常见场景：用户从资源管理器复制了文件，粘贴到输入框得到的是一段路径文本
    （可能带引号、换行，或每行一个）。这里逐行尝试识别为存在的文件，
    识别成功的转成附件；没有识别到任何文件时返回空列表。
    """
    if not pasted_text or not pasted_text.strip():
        return []
    blocks: list[Block] = []
    seen: set[str] = set()
    for line in pasted_text.splitlines():
        token = line.strip().strip('"').strip("'").strip()
        # 只处理看起来像路径的行，避免把普通一句话当文件。
        if not token or "\n" in token or len(token) > 1024:
            continue
        if "://" in token:
            continue
        resolved = _resolve(token, base_dir)
        if not resolved.is_file() or str(resolved) in seen:
            continue
        seen.add(str(resolved))
        blocks.append(path_to_block(resolved, base_dir))
        if len(blocks) >= MAX_ATTACHMENTS:
            break
    return blocks


def merge_prompt_blocks(text: str, attachments: list[Block]) -> list[Block]:
    """把用户输入的文字与待发送附件合并成一份内容块列表。

    顺序：文字在前，附件在后；文字为空时只发附件。
    这样模型先看到指令，再看到附件内容，符合直觉。
    """
    blocks: list[Block] = []
    if text and text.strip():
        blocks.append(TextBlock(text))
    blocks.extend(attachments)
    return blocks


def describe_block(block: Block) -> str:
    """为 UI 筹码展示生成一句简短描述（文件名 / 类型 / 文本摘要）。"""
    if isinstance(block, ImageBlock):
        name = getattr(block, "filename", None)
        return f"图片 {name}" if name else "图片"
    if isinstance(block, TextBlock):
        first = block.text.strip().splitlines()[0] if block.text.strip() else ""
        return (first[:28] + "…") if len(first) > 28 else (first or "文件")
    return "附件"


def attach_filename(block: Block, filename: str) -> Block:
    """给图片块附加文件名元数据（用于 UI 展示），非图片块原样返回。"""
    if isinstance(block, ImageBlock):
        try:
            block.filename = filename  # type: ignore[attr-defined]
        except Exception:
            pass
    return block
