"""安装后 PATH 自动引导测试（yotcode._boot）。

这些测试不碰真实注册表/shell 配置：add_user_path 被 monkeypatch 掉，
只验证"何时应该配置、目标目录是否正确、是否幂等"这些决策逻辑。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from yotcode import _boot


def test_scripts_dir_from_sysconfig():
    """_scripts_dir 应返回一个绝对路径目录。"""
    d = _boot._scripts_dir()
    assert d.is_absolute()


def test_has_launcher(tmp_path):
    """只有 Scripts 里真的有 yot launcher 才算数。"""
    assert not _boot._has_launcher(tmp_path)
    (tmp_path / "yot.exe").write_bytes(b"x")
    assert _boot._has_launcher(tmp_path)


def test_no_launcher_does_not_write(monkeypatch, tmp_path):
    """Scripts 里没有 yot 时绝不改 PATH（保护其他 Python 环境）。"""
    monkeypatch.setattr(_boot, "_scripts_dir", lambda: tmp_path)
    written = []
    monkeypatch.setattr(_boot, "add_user_path", lambda d: written.append(d))
    _boot.ensure_path()
    assert written == []


def test_with_launcher_writes_scripts_dir(monkeypatch, tmp_path):
    """检测到 yot launcher 时，把该 Scripts 目录写入用户 PATH。"""
    (tmp_path / "yot.exe").write_bytes(b"x")
    monkeypatch.setattr(_boot, "_scripts_dir", lambda: tmp_path)
    written = []
    monkeypatch.setattr(_boot, "add_user_path", lambda d: written.append(d))
    _boot.ensure_path()
    assert written == [tmp_path]


def test_probe_mode_writes_file_not_registry(monkeypatch, tmp_path):
    """probe 模式只写检测到的目录到文件，不碰真实 PATH。"""
    (tmp_path / "yot.exe").write_bytes(b"x")
    probe = tmp_path / "probe.txt"
    monkeypatch.setattr(_boot, "_scripts_dir", lambda: tmp_path)
    monkeypatch.setenv(_boot._PROBE_ENV, str(probe))
    monkeypatch.setattr(
        _boot, "add_user_path", lambda d: pytest.fail("probe 模式不应写 PATH")
    )
    _boot.ensure_path()
    assert probe.read_text(encoding="utf-8") == str(tmp_path)


def test_exceptions_are_swallowed(monkeypatch):
    """引导逻辑抛任何异常都不能向外传播。"""
    def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(_boot, "_scripts_dir", boom)
    _boot.ensure_path()  # 不应抛


def test_add_user_path_windows_writes_and_is_idempotent(monkeypatch, tmp_path):
    """用假 winreg 验证 add_user_path 在 Windows 下写入且幂等，不碰真实注册表。"""
    monkeypatch.setattr(_boot.os, "name", "nt")
    target = str(tmp_path / "Scripts")

    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        KEY_READ = 0
        KEY_SET_VALUE = 1
        REG_EXPAND_SZ = 2
        store = {"Path": "C:\\existing"}

        @staticmethod
        def OpenKey(root, sub, reserved=0, flags=0):
            return object()

        @staticmethod
        def CloseKey(key):
            pass

        @staticmethod
        def QueryValueEx(key, name):
            return FakeWinreg.store[name], 1

        @staticmethod
        def SetValueEx(key, name, reserved, value_type, value):
            FakeWinreg.store[name] = value

    import sys
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg)
    # 屏蔽广播（ctypes 在假环境里无所谓，但保持一致静默）
    monkeypatch.delenv("YOTCODE_BOOT_PROBE", raising=False)

    first = _boot.add_user_path(Path(target))
    assert first is True
    assert target in FakeWinreg.store["Path"]
    second = _boot.add_user_path(Path(target))
    assert second is False  # 幂等
    assert FakeWinreg.store["Path"].count(target) == 1
