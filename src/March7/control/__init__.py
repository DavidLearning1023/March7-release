"""control/ — 用户控制面（本地 CLI + 只读 Web）

## 为什么是一个包而不是一个模块

两个入口共用同一份「读库 → 渲染」逻辑，但依赖极不对称：

- `cli.py`：零额外依赖（argparse + 标准库），能在任何终端跑；
- `web.py`：用标准库的 `http.server`，**不引入任何 Web 框架**
  （`CONVENTIONS §2` 的依赖表锁死 5+1，加 Flask 要写 ADR）。

放在一个包里是为了让"哪两个文件构成控制面"一眼可见——
散成两个顶层模块时，`web.py` 会看起来像"又一个没用的脚本"，
而它其实是用户**唯一**的可视入口。

## 权限边界（写操作只在 CLI）

- **CLI**：看 / 改 / 删（删除有二次确认 + 回执）；
- **Web**：只读（列表 / 详情 / 健康）。写操作不做成网页——
  "在浏览器里点一下就把记忆删了"是最容易误触的形态，
  而 `ADR-0040` 的核心是「删除必须可证明」，不是「删除必须方便」。

⚠️ 本包**不 import MCP**（`CONVENTIONS §2` 红线），也**不 import `server.py`**。

## 怎么跑

```bash
uv run python -m March7.control list
uv run python -m March7.control show <id>
uv run python -m March7.control erase <id>          # 会二次确认
uv run python -m March7.control receipts
uv run python -m March7.control --help
```

Web：`uv run python -m March7.control.web --port 8765`（默认只绑 `127.0.0.1`）
"""

from __future__ import annotations

__all__: list[str] = []
