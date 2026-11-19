#!/usr/bin/env python3
"""YotCode 一键安装脚本（跨平台）。

用法：
    python install.py                # 安装 YotCode + 配置 PATH + 生成全局模板
    python install.py --path-only    # 只配置 PATH（已 pip 安装过时用）
    python install.py --no-install   # 跳过 pip 安装，只做 PATH 与模板

也可以完全不用本脚本，直接：
    python -m pip install "git+https://github.com/liguoliang10086/YotCode.git"
    python -m yotcode --setup-path
"""
from __future__ import annotations

import argparse
import subprocess
import sys


REPO = "https://github.com/liguoliang10086/YotCode.git"


def pip_install() -> bool:
    print("[1/3] 安装 YotCode ...")
    cmd = [sys.executable, "-m", "pip", "install", f"git+{REPO}"]
    print("      " + " ".join(cmd))
    try:
        subprocess.check_call(cmd)
        print("      安装完成。")
        return True
    except subprocess.CalledProcessError as exc:
        print(f"      安装失败（退出码 {exc.returncode}）。")
        print("      可尝试镜像源：")
        print(f'      {sys.executable} -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "git+{REPO}"')
        return False


def setup_path() -> None:
    print("[2/3] 配置命令行 PATH ...")
    try:
        from yotcode.setup import setup_path as do_setup_path
        _changed, message, directory = do_setup_path()
        print(f"      命令目录：{directory}")
        print(f"      {message}")
    except Exception as exc:
        print(f"      跳过 PATH 配置：{exc}")


def setup_templates() -> None:
    print("[3/3] 生成全局配置模板 ...")
    try:
        from yotcode.config import home_dir
        from yotcode.setup import ensure_user_templates
        created = ensure_user_templates()
        print(f"      全局配置目录：{home_dir()}")
        for path in created:
            print(f"      已生成：{path}")
        if not created:
            print("      模板已存在，未覆盖。")
    except Exception as exc:
        print(f"      跳过模板生成：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="YotCode 一键安装脚本")
    parser.add_argument("--path-only", action="store_true", help="只配置 PATH，不安装")
    parser.add_argument("--no-install", action="store_true", help="跳过 pip 安装")
    args = parser.parse_args()

    ok = True
    if not args.path_only and not args.no_install:
        ok = pip_install()
    if args.path_only:
        setup_path()
        return 0
    if ok:
        setup_path()
        setup_templates()
        print("\n完成。请重新打开终端，然后运行：yot --version")
        print("配置模型：编辑 ~/.yotcode/.env 取消注释需要的 API Key，")
        print("或用 yot --setup 查看完整配置指引。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
