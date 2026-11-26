"""March7 MCP server 入口：python -m March7（stdio 传输）。

S4（T13）实现：组装引擎 → MCPServer 注册 8 工具 → mcp.run()。

⚠️ **stdio 传输的硬约束**：stdout 是**协议通道**，日志绝不能写进 stdout
（会把 JSON-RPC 报文搅坏）。因此日志统一走 stderr（CONVENTIONS §3）。
"""

from __future__ import annotations

import logging
import sys


def main() -> None:
    """启动 stdio MCP server。

    引擎依赖图在**首次调用工具时**才组装（`_build_engine` 惰性单例）：
    进程起来不等于要连真 API、也不等于要加载 BGE-M3 权重——宿主连上来问一句
    「你还记得我吗」不该先付一次模型调用的钱。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,  # stdout 是协议通道，日志只能走 stderr
    )
    from March7.server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
