"""会话持久化：图片块写入 JSONL 并能完整还原。"""
from __future__ import annotations

from yotcode.session import Session
from yotcode.types import ImageBlock, TextBlock, user_message


def test_image_survives_session_roundtrip(tmp_path):
    session = Session.create(tmp_path, tmp_path, "model", "provider")
    img = ImageBlock("image/png", "QUJDREVGRw==", filename="shot.png")
    session.append_message(user_message([TextBlock("看这张"), img]))

    loaded = Session.load(tmp_path, session.meta.id)
    assert loaded.messages
    restored_imgs = [
        b for b in loaded.messages[0].content if isinstance(b, ImageBlock)
    ]
    assert len(restored_imgs) == 1
    assert restored_imgs[0].data == "QUJDREVGRw=="
    assert restored_imgs[0].media_type == "image/png"
    assert restored_imgs[0].filename == "shot.png"


def test_pure_image_message_does_not_crash(tmp_path):
    session = Session.create(tmp_path, tmp_path, "model", "provider")
    img = ImageBlock("image/jpeg", "AAAA", filename="only.jpg")
    # 纯图片消息（没有文本）不应让标题生成逻辑崩溃
    session.append_message(user_message([img]))
    loaded = Session.load(tmp_path, session.meta.id)
    assert loaded.messages[0].images()
