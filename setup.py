"""构建期辅助脚本（项目主要配置在 pyproject.toml）。

两个用途：
1. 把 ``yotcode_boot.pth`` 打进 wheel 的顶层（安装后落到 site-packages 根目录，
   Python 每次启动都会执行它，触发 yotcode._boot 的 PATH 配置）。
2. 在 pip install 的**构建阶段**就把 Scripts 目录写进用户 PATH。

为什么第 2 点重要
-----------------
只靠 .pth 有个体验断点：它要在「安装后的下一次 Python 启动」才执行，而用户
常见流程是 `pip install ...` 完直接在同一个 cmd 敲 `yot`——中间没有新的 Python
进程，.pth 没机会跑。pip 构建 wheel 时会在用户真实 Python 上执行 setup.py
（实测此时 sys.executable / sysconfig 都指向真实环境），所以趁这个时机先写一次
PATH，pip install 返回时注册表就已配置好，用户重开终端即可用。

写入逻辑复用 yotcode._boot 里的函数，保证与 .pth 引导行为一致（幂等、静默、
无管理员权限）。写入失败绝不影响安装本身。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


def _configure_path_during_build() -> None:
    """构建阶段把真实 Python 的 Scripts 目录写入用户 PATH（幂等、容错）。"""
    try:
        import sysconfig
        scripts = Path(sysconfig.get_path("scripts"))
        if not scripts:
            return
        # 复用引导模块的注册表写入函数（不检查 launcher：此时 yot.exe 还没装）。
        import importlib.util
        boot_src = Path(__file__).parent / "yotcode" / "_boot.py"
        spec = importlib.util.spec_from_file_location("_yot_boot", boot_src)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.add_user_path(scripts)
    except Exception:
        # PATH 配置失败绝不能拖垮安装。
        pass


class _BuildPyWithPth(_build_py):
    """标准 build_py 之后：打进 .pth，并在构建阶段配置用户 PATH。"""

    def run(self) -> None:
        super().run()
        # 构建期就把 PATH 配好（pip install 过程中执行）。
        _configure_path_during_build()
        source = Path(__file__).parent / "yotcode_boot.pth"
        if not source.exists():
            return
        target = Path(self.build_lib) / "yotcode_boot.pth"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


setup(cmdclass={"build_py": _BuildPyWithPth})
