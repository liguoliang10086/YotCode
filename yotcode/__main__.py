"""当用户输入 `python -m yotcode` 时，Python 会执行这个文件。

它本身不实现任何功能，只是一个"跳板"：
  1. 从 cli.py 导入 main 函数（真正的主入口）
  2. 调用它，并把它返回的数字当作程序的退出码
     （0 表示正常结束，非 0 表示出错，见 cli.py 里 main() 的各处 return）
"""
from .cli import main

raise SystemExit(main())
