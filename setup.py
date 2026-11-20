"""构建期辅助脚本（项目主要配置在 pyproject.toml）。

唯一的用途：把 ``yotcode_boot.pth`` 打进 wheel 的**顶层**。

为什么需要它
------------
Python 只会执行 site-packages **根目录下**的 ``.pth`` 文件。默认的打包配置
不会把项目根目录的 ``.pth`` 放到 wheel 顶层，所以这里用一个 build_py 钩子，
在构建时手动把它复制进 build_lib 根目录。这样用户 pip install 后，该文件
会落到 site-packages/yotcode_boot.pth，Python 每次启动都会执行它，从而触发
yotcode._boot 里的 PATH 自动配置逻辑。

pyproject.toml 负责声明依赖、入口等元数据；本文件只补充这个构建期动作。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class _BuildPyWithPth(_build_py):
    """在标准 build_py 之后，把引导用的 .pth 复制到 wheel 的顶层。"""

    def run(self) -> None:
        super().run()
        source = Path(__file__).parent / "yotcode_boot.pth"
        if not source.exists():
            return
        target = Path(self.build_lib) / "yotcode_boot.pth"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


setup(cmdclass={"build_py": _BuildPyWithPth})
