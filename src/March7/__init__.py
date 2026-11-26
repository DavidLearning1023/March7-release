"""March7 — 宿主无关的长期记忆 MCP server（课题 5「会记住你的 AI 搭子」）。

引擎核心（judge/store/retriever/conflict/l0_profile/export/embedder/llm）零 MCP 依赖；
只有 server.py 与 hosts/ 触碰 MCP。命名规范见 docs/NAMING.md，全局约定见 docs/CONVENTIONS.md。
"""

__version__ = "0.1.0"
