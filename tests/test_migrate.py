"""改名迁移：老目录（.claudepy）自动搬到新目录（.yotcode）。"""
from __future__ import annotations

from yotcode.config import (
    home_dir,
    legacy_project_config_dir,
    migrate_legacy_dirs,
    project_config_dir,
)


def test_project_dir_is_migrated(tmp_path):
    """项目里的 .claudepy 会被改名成 .yotcode，内容原样带过去。"""
    old = legacy_project_config_dir(tmp_path)
    old.mkdir(parents=True)
    (old / "settings.json").write_text('{"model": "x"}', encoding="utf-8")
    (old / "commands").mkdir()
    (old / "commands" / "a.md").write_text("hello", encoding="utf-8")

    notes = migrate_legacy_dirs(tmp_path)

    new = project_config_dir(tmp_path)
    assert new.is_dir()
    assert (new / "settings.json").read_text(encoding="utf-8") == '{"model": "x"}'
    assert (new / "commands" / "a.md").read_text(encoding="utf-8") == "hello"
    assert not old.exists()
    assert any(str(old) in note for note in notes)


def test_existing_new_dir_is_not_overwritten(tmp_path):
    """新目录已经存在时不覆盖用户的东西。"""
    old = legacy_project_config_dir(tmp_path)
    old.mkdir(parents=True)
    (old / "settings.json").write_text("old", encoding="utf-8")

    new = project_config_dir(tmp_path)
    new.mkdir(parents=True)
    (new / "settings.json").write_text("new", encoding="utf-8")

    migrate_legacy_dirs(tmp_path)

    assert (new / "settings.json").read_text(encoding="utf-8") == "new"
    assert old.is_dir()  # 老目录保留，没被动


def test_no_legacy_dir_is_noop(tmp_path):
    """没有老目录就什么都不做，也不该报错。"""
    notes = migrate_legacy_dirs(tmp_path)
    assert notes == []
    assert not project_config_dir(tmp_path).exists()


def test_user_home_is_untouched_when_home_env_set(tmp_path, monkeypatch):
    """用户显式指定了 HOME 时，绝不能去动真正的 ~/.claudepy。"""
    monkeypatch.setenv("CCPY_HOME", str(tmp_path / "custom-home"))
    notes = migrate_legacy_dirs(tmp_path)
    assert notes == []
    assert home_dir() == tmp_path / "custom-home"
