"""附件转换核心测试：路径转块、大小校验、粘贴路径识别、合并。"""
from __future__ import annotations

import base64

from yotcode.attachments import (
    MAX_IMAGE_BYTES,
    attachment_candidates,
    is_image_path,
    merge_prompt_blocks,
    path_to_block,
    paths_to_blocks,
)
from yotcode.types import ImageBlock, TextBlock


def _make_image(path, size: int = 100) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * size)


def test_is_image_path():
    assert is_image_path("shot.PNG")
    assert is_image_path("a.jpg")
    assert is_image_path("b.webp")
    assert not is_image_path("code.py")
    assert not is_image_path("note.txt")


def test_image_path_to_block(tmp_path):
    img = tmp_path / "shot.png"
    _make_image(img)
    block = path_to_block(img)
    assert isinstance(block, ImageBlock)
    assert block.media_type == "image/png"
    assert block.filename == "shot.png"
    # base64 能解回原字节
    assert base64.b64decode(block.data).startswith(b"\x89PNG")


def test_text_path_to_block(tmp_path):
    code = tmp_path / "a.py"
    code.write_text("print('hi')\n", encoding="utf-8")
    block = path_to_block(code)
    assert isinstance(block, TextBlock)
    assert '<file path="a.py">' in block.text
    assert "print('hi')" in block.text


def test_missing_file_returns_readable_text(tmp_path):
    block = path_to_block(tmp_path / "nope.png")
    assert isinstance(block, TextBlock)
    assert "找不到文件" in block.text


def test_oversized_image_returns_warning(tmp_path):
    img = tmp_path / "big.png"
    img.write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))
    block = path_to_block(img)
    assert isinstance(block, TextBlock)
    assert "上限" in block.text


def test_paths_to_blocks_limit(tmp_path):
    code = tmp_path / "a.txt"
    code.write_text("x", encoding="utf-8")
    blocks = paths_to_blocks([str(code)] * 15, tmp_path)
    # 超过 10 个会追加一条提示块
    assert any(isinstance(b, TextBlock) and "最多" in b.text for b in blocks)


def test_attachment_candidates_from_pasted_paths(tmp_path):
    img = tmp_path / "shot.png"
    _make_image(img)
    code = tmp_path / "a.py"
    code.write_text("print(1)", encoding="utf-8")
    pasted = f'这里是一句话\n"{img}"\n{code}\nhttps://example.com/x'
    blocks = attachment_candidates(pasted, tmp_path)
    # 只识别真实存在的文件路径，URL / 普通句子忽略
    assert len(blocks) == 2
    assert any(isinstance(b, ImageBlock) for b in blocks)
    assert any(isinstance(b, TextBlock) and "<file" in b.text for b in blocks)


def test_merge_prompt_blocks():
    img = ImageBlock("image/png", "AAAA")
    blocks = merge_prompt_blocks("看这张图", [img])
    assert len(blocks) == 2
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "看这张图"
    assert blocks[1] is img


def test_merge_empty_text_only_attachments():
    img = ImageBlock("image/png", "AAAA")
    blocks = merge_prompt_blocks("", [img])
    assert len(blocks) == 1
    assert blocks[0] is img
