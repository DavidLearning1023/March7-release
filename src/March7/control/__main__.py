"""`python -m March7.control` 的入口（等价于 `python -m March7.control.cli`）。

为什么单独一个 `__main__.py` 而不是让用户记住 `control.cli`：
`python -m March7.control` 是最自然的写法，而少了这个文件时 Python 会去
`control/__main__.py` 找、找不到就报 `No module named March7.control.__main__`
——那个报错看起来像"包坏了"，不像"少了个入口文件"。

⚠️ 退出码要透传（`sys.exit(main())`）：`erase` 的"有残留"是 1，
脚本化调用要靠这个退出码判断，不能恒 0。
"""

from __future__ import annotations

import sys

from March7.control.cli import main

if __name__ == "__main__":
    sys.exit(main())
