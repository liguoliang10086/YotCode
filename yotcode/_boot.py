"""安装后自动配置 PATH 的引导模块（由 site-packages 里的 .pth 在 Python 启动时导入）。

为什么需要它
------------
pip 安装命令行工具时，会把 ``yot.exe`` / ``yot`` 放进当前 Python 环境的
Scripts 目录，但**不会**把这个目录加入系统 PATH。于是用户安装完敲 ``yot``
会提示"不是内部或外部命令"。而 PATH 没配时用户又敲不出 ``yot --setup-path``，
形成死锁——必须有一种"不依赖用户先运行 yot"的自动配置方式。

pip 本身没有标准的"安装后脚本"钩子，但 Python 的 ``.pth`` 文件提供了等价能力：
放在 site-packages 里的 ``.pth``，会在每次 Python 解释器启动时被 ``site`` 模块
执行。我们借此让下面这段代码在用户机器上运行一次，把 Scripts 目录写进用户级
PATH。写完即幂等，之后每次启动读一下注册表发现已存在就直接返回。

设计原则
--------
* **幂等**：注册表里已有该目录就不再写入。
* **安全**：只有当 Scripts 目录里确实存在 yot launcher 时才动手，避免构建隔离
  环境或其他无关的 Python 环境被误写入 PATH。
* **静默**：绝不 print —— .pth 在 Python 启动早期执行，任何输出都会污染 pip、
  python 等命令的标准输出。
* **容错**：所有异常都吞掉，绝不能因为 PATH 配置失败导致 Python 无法启动。
"""
from __future__ import annotations

import os
from pathlib import Path

# 测试专用环境变量：设置后只把检测到的 Scripts 目录写进指定文件，
# 不真的改注册表，方便在隔离环境里验证引导逻辑而不污染真实用户 PATH。
_PROBE_ENV = "YOTCODE_BOOT_PROBE"


def _scripts_dir() -> Path:
    """返回当前 Python 环境安装 console script 的目录（pip 装 yot 的地方）。"""
    import sysconfig
    try:
        path = sysconfig.get_path("scripts")
        if path:
            return Path(path)
    except Exception:
        pass
    import sys
    return Path(sys.executable).parent


def _has_launcher(scripts: Path) -> bool:
    """确认这个目录里真的装了 yot，避免误改别的 Python 环境的 PATH。"""
    return (scripts / "yot.exe").exists() or (scripts / "yot").exists()


def _add_windows_user_path(directory: Path) -> bool:
    """把目录写进 Windows 用户级 PATH（HKCU\\Environment）并广播变更。"""
    import winreg

    dir_str = str(directory)
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ)
    try:
        try:
            current, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""
    finally:
        winreg.CloseKey(key)

    parts = [p for p in current.split(";") if p]
    if any(p.lower().rstrip("\\/") == dir_str.lower().rstrip("\\/") for p in parts):
        return False  # 已存在，幂等返回

    parts.append(dir_str)
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(parts))
    finally:
        winreg.CloseKey(key)

    # 广播环境变量变更：新开的终端会立即看到（已开着的窗口仍需重开）。
    try:
        import ctypes
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment",
            0x0002, 5000, ctypes.byref(result),
        )
    except Exception:
        pass
    return True


def ensure_path() -> None:
    """在 Python 启动时被调用：如需要则自动把 yot 所在目录加入用户 PATH。"""
    try:
        # 类 Unix 环境由用户 shell 启动文件管理，且本工具主要面向 Windows，
        # 这里只在 Windows 上自动改 PATH。
        if os.name != "nt":
            return
        scripts = _scripts_dir()
        if not _has_launcher(scripts):
            return

        probe = os.environ.get(_PROBE_ENV)
        if probe:
            # 测试模式：不碰注册表，只记录检测到的目录。
            Path(probe).write_text(str(scripts), encoding="utf-8")
            return

        _add_windows_user_path(scripts)
    except Exception:
        # .pth 在解释器启动早期执行，任何异常都必须吞掉。
        pass


# 被 .pth 导入时立即执行一次。
ensure_path()
