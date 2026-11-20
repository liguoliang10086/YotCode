"""Agent 附件测试：run(list[Block])、@图片展开、序列化往返。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from yotcode.agent import Agent
from yotcode.config import build_config
from yotcode.providers.mock import MockProvider
from yotcode.types import (
    ImageBlock,
    TextBlock,
    block_from_dict,
    block_to_dict,
)


def _agent(tmp: Path) -> Agent:
    cfg = build_config(tmp, mock=True)
    return Agent(cfg, MockProvider())


def test_expand_mentions_image_becomes_imageblock():
    tmp = Path(tempfile.mkdtemp())
    img = tmp / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nxxx")
    agent = _agent(tmp)

    blocks = agent.expand_mentions("看 @shot.png")
    assert any(isinstance(b, ImageBlock) for b in blocks)
    imgs = [b for b in blocks if isinstance(b, ImageBlock)]
    assert imgs[0].filename == "shot.png"


def test_expand_mentions_text_keeps_file_wrapper():
    tmp = Path(tempfile.mkdtemp())
    code = tmp / "a.py"
    code.write_text("print(1)\n", encoding="utf-8")
    agent = _agent(tmp)

    blocks = agent.expand_mentions("读 @a.py")
    assert any(isinstance(b, TextBlock) and "<referenced-files>" in b.text for b in blocks)
    assert not any(isinstance(b, ImageBlock) for b in blocks)


def test_expand_mentions_keeps_existing_imageblock():
    tmp = Path(tempfile.mkdtemp())
    agent = _agent(tmp)
    img = ImageBlock("image/png", "AAAA", filename="pasted.png")

    blocks = agent.expand_mentions([TextBlock("看这张"), img])
    assert img in blocks
    assert len([b for b in blocks if isinstance(b, ImageBlock)]) == 1


def test_run_accepts_list_of_blocks():
    import asyncio

    tmp = Path(tempfile.mkdtemp())
    agent = _agent(tmp)
    img = ImageBlock("image/png", "AAAA", filename="shot.png")

    result = asyncio.run(agent.run([TextBlock("描述一下"), img]))
    assert isinstance(result, str)

    # 用户消息已持久化，含图片块
    user_msgs = [m for m in agent.session.messages if m.role == "user"]
    assert user_msgs, "应该存下用户消息"
    assert any(isinstance(b, ImageBlock) for b in user_msgs[0].content)


def test_imageblock_serialization_roundtrip_preserves_filename():
    img = ImageBlock("image/png", "QUJD", filename="shot.png")
    restored = block_from_dict(block_to_dict(img))
    assert isinstance(restored, ImageBlock)
    assert restored.filename == "shot.png"
    assert restored.data == "QUJD"
    assert restored.media_type == "image/png"
