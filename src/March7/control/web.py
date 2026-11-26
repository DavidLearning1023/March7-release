"""control/web.py — 只读 Web 控制台（标准库 `http.server`，零新依赖）

## 为什么用标准库而不是 Flask/FastAPI

`CONVENTIONS §2` 的依赖表锁死 5+1。加一个 Web 框架要写 ADR + 改 `pyproject.toml`。
而这个页面的需求只是「把几张表渲染成 HTML」——`http.server` 完全够，
且它**没有安装成本**：用户 clone 下来 `uv run` 就能开，不需要多装包。

## 安全边界（三条，缺一条这个页面就是后门）

1. **只绑 `127.0.0.1`**（不是 `0.0.0.0`）：默认不对局域网开放。
   `--host` 存在但**刻意不给** `0.0.0.0` 的简写——想暴露就自己写地址，
   那个动作必须是有意识的。
2. **只读**：只实现 `GET`。任何非 GET 方法返回 `405`，
   页面上没有任何删除/编辑表单（写操作在 CLI，那里有二次确认）。
3. **默认打码**：`root_basename` 与 `reviewed_by` 默认是 `***`，
   加 `--include-reviewer` 才展开。理由同 CLI：这个页面最可能的暴露面是**投屏/截图**。
4. **库不存在就不服务**（S17）：`/` 与 `/health` 在缺 `March7.db` 时回 `503` 并说明，
   **不构造 `MemoryStore`**——它的构造函数会建目录 + 建库，那会让"数据目录错位"的
   用户看到一个空库，以为记忆丢了。
5. **`/__panel` 只回自己的身份**：`{ok, data_dir, pid}`，不读库、不含用户内容，
   供启动器判断"这个端口上跑的是不是这个库"。

## 为什么没有 JS

页面是纯 HTML + 内联 CSS。理由：① 零构建；② 没有 XSS 面（不渲染用户正文到
`innerHTML`，全部做 HTML 转义后放进 `<td>`）；③ 断网可用。
"现代感"不是这个页面的目标——用户来看的是"我记了什么"。

⚠️ **正文必须转义**：记忆正文是用户输入，里面完全可能有 `<script>`。
`html.escape` 是唯一防线，`test_web_escapes_user_content` 钉住。
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import secrets
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from March7 import config, delete_receipt
from March7.control.cli import (
    MASK,
    _known_basenames,
    mask_scope,
    mask_value,
)
from March7.store import MemoryStore

DEFAULT_PORT = 8765
"""默认端口（选一个不常见的，避免和 3000/8000/8080 抢）。"""

DEFAULT_HOST = "127.0.0.1"
"""默认只绑本地回环——**不是** `0.0.0.0`。"""

LIST_LIMIT = 500
"""首页一次渲染的条数上限（S17）。

为什么需要：万条库全量渲染出 1.7 MB HTML，热启要 0.7–0.85s，而卡里的判据是
「消息提交 → 列表可操作 ≤1s」。限量到 500 条后页面约 60 KB，渲染降到几十毫秒。

⚠️ 这不是分页（S18 才做）：页面会写明「共 N 条，显示最近 500 条」，
所以**不会**让用户以为库只有 500 条。
"""

_CSS = """
:root { color-scheme: light; }
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
       margin: 0; padding: 24px 32px; background: #fbfbfd; color: #1c1c1e; line-height: 1.55; }
h1 { font-size: 20px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 15px; margin: 28px 0 8px; font-weight: 600; color: #3a3a3c; }
.sub { color: #6e6e73; font-size: 13px; margin-bottom: 20px; }
nav a { margin-right: 16px; font-size: 13px; color: #0a58ca; text-decoration: none; }
nav a:hover { text-decoration: underline; }
table { border-collapse: collapse; width: 100%; font-size: 13px; background: #fff;
        border: 1px solid #e5e5ea; border-radius: 6px; overflow: hidden; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #f0f0f3; vertical-align: top; }
th { background: #f5f5f7; font-weight: 600; color: #3a3a3c; }
tr:last-child td { border-bottom: none; }
code { font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 12px;
       background: #f5f5f7; padding: 1px 5px; border-radius: 3px; }
.mask { color: #a1a1a6; letter-spacing: 1px; }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 9px;
       background: #eef2ff; color: #3730a3; }
.tag.archived { background: #f3f4f6; color: #6b7280; }
.tag.superseded, .tag.merged { background: #fef3c7; color: #92400e; }
.empty { color: #8e8e93; font-size: 13px; padding: 16px 0; }
.note { background: #fffbea; border: 1px solid #fde68a; border-radius: 6px;
        padding: 10px 14px; font-size: 12.5px; color: #713f12; margin: 16px 0; }
footer { margin-top: 36px; font-size: 12px; color: #a1a1a6; }
"""


def _e(value: object) -> str:
    """HTML 转义（**唯一**出口，所有用户内容都必须过它）。"""
    return html.escape("" if value is None else str(value), quote=True)


def _ts(stamp: object) -> str:
    """Unix 秒 → 可读时间。"""
    if not stamp:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(stamp)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _page(title: str, body: str) -> bytes:
    """包一层 HTML 骨架（纯内联 CSS，无 JS、无外部资源）。"""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head><body>"
        "<h1>March7 控制台</h1>"
        "<div class=\"sub\">只读视图 · 默认只绑 127.0.0.1 · 打码字段用 "
        "<code>--include-reviewer</code> 展开</div>"
        "<nav><a href=\"/\">记忆</a><a href=\"/receipts\">删除回执</a>"
        "<a href=\"/health\">健康</a></nav>"
        f"{body}"
        "<footer>写操作（归档 / 修正 / 删除）在命令行："
        "<code>python -m March7.control --help</code></footer>"
        "</body></html>"
    ).encode("utf-8")


def render_index(
    store: MemoryStore,
    *,
    include_reviewer: bool,
    status: str | None,
    limit: int = LIST_LIMIT,
) -> str:
    """记忆列表页（按 profile / project 分组，`ADR-0040` 步骤 1 的要求）。

    `limit` 只影响**渲染多少条**，不影响 `total`（页面上的总数来自 `stats()`）——
    两者混用会让「共 N 条」变成「渲染了几条」，那是另一种谎报。
    """
    total = store.stats().memory_count
    memories = store.list_recent(scope=None, since=None, limit=limit)
    if status:
        memories = [m for m in memories if m.status.value == status]
    groups: dict[str, list] = {}
    for mem in memories:
        key = "profile（global）" if (mem.scope or "global") == "global" else "project"
        groups.setdefault(key, []).append(mem)
    shown = len(memories)
    if shown < total:
        count_text = f"共 {total} 条，显示最近 {shown} 条"
    else:
        count_text = f"共 {total} 条"
    parts: list[str] = [
        f"<h2>本项目的记忆 <span class=\"sub\">{_e(count_text)}</span></h2>"
    ]
    if not memories:
        parts.append("<div class=\"empty\">（还没有记忆）</div>")
    for group_name in sorted(groups, key=lambda g: (g != "profile（global）", g)):
        rows = groups[group_name]
        parts.append(f"<h2>{_e(group_name)} · {len(rows)} 条</h2>")
        parts.append(
            "<table><thead><tr><th style=\"width:90px\">状态</th>"
            "<th style=\"width:110px\">写入</th><th style=\"width:150px\">作用域</th>"
            "<th>内容</th><th style=\"width:110px\">来源</th></tr></thead><tbody>"
        )
        for mem in rows:
            status_value = mem.status.value
            cls = "" if status_value in ("active", "aging") else f" {status_value}"
            parts.append(
                "<tr>"
                f"<td><span class=\"tag{cls}\">{_e(status_value)}</span></td>"
                f"<td>{_e(_ts(mem.created_at))}</td>"
                f"<td><code>{_e(mask_scope(mem.scope, reveal=include_reviewer))}</code></td>"
                f"<td>{_e(mem.content)}</td>"
                f"<td>{_e(mem.host or '—')}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
    parts.append(
        "<div class=\"note\">删除会同时清掉 FTS 索引、向量、来源、claims、冲突与日志，"
        "并生成一张可查回执。命令：<code>python -m March7.control erase &lt;id&gt;</code></div>"
    )
    return "".join(parts)


def render_receipts_page(data_dir: Path) -> str:
    """删除回执页。"""
    rows = delete_receipt.list_receipts(data_dir)
    if not rows:
        return "<h2>删除回执</h2><div class=\"empty\">（还没有任何删除回执）</div>"
    parts = [f"<h2>删除回执 · {len(rows)} 张</h2>",
             "<table><thead><tr><th style=\"width:230px\">回执 id</th>"
             "<th style=\"width:80px\">范围</th><th style=\"width:130px\">时间</th>"
             "<th>验证</th></tr></thead><tbody>"]
    for row in rows:
        verdict = str(row.get("verdict") or "")
        cls = "" if verdict == "clean" else " superseded"
        parts.append(
            "<tr>"
            f"<td><code>{_e(row.get('receipt_id'))}</code></td>"
            f"<td>{_e(row.get('scope'))}</td>"
            f"<td>{_e(row.get('deleted_at_text'))}</td>"
            f"<td><span class=\"tag{cls}\">{_e(verdict)}</span></td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


def render_health_page(store: MemoryStore, data_dir: Path, *, include_reviewer: bool) -> str:
    """健康页。"""
    stats = store.stats()
    basenames = _known_basenames(store)
    receipts = delete_receipt.list_receipts(data_dir)
    parts = [
        "<h2>健康</h2><table><tbody>",
        f"<tr><th style=\"width:200px\">数据目录</th><td><code>{_e(data_dir)}</code></td></tr>",
        f"<tr><th>schema 版本</th><td>{_e(stats.schema_version)}</td></tr>",
        f"<tr><th>记忆条数</th><td>{_e(stats.memory_count)}（可召回 {_e(stats.recallable_count)}）</td></tr>",
        f"<tr><th>今日写入</th><td>{_e(stats.today_writes)} / 上限 {_e(stats.daily_write_cap or '不限')}</td></tr>",
        f"<tr><th>库大小</th><td>{stats.db_size_bytes / 1024:.1f} KiB</td></tr>",
        f"<tr><th>最近写入</th><td>{_e(_ts(stats.last_write_at))}</td></tr>",
        f"<tr><th>删除回执</th><td>{len(receipts)} 张</td></tr>",
        "</tbody></table>",
        f"<h2>已知项目 · {len(basenames)}</h2>",
    ]
    if basenames:
        parts.append("<table><thead><tr><th style=\"width:140px\">project_id</th>"
                     "<th>目录名（root_basename）</th></tr></thead><tbody>")
        for project_id, basename in basenames:
            shown = mask_value(basename, reveal=include_reviewer) or "—"
            cls = "" if include_reviewer else " class=\"mask\""
            parts.append(
                f"<tr><td><code>{_e(project_id)}</code></td>"
                f"<td{cls}>{_e(shown)}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<div class=\"empty\">（还没有登记任何项目）</div>")
    if not include_reviewer:
        parts.append(
            "<div class=\"note\">目录名已打码（"
            f"<code>{_e(MASK)}</code>）。要看明文：启动时加 "
            "<code>--include-reviewer</code>，但**别在投屏/截图时开着**。</div>"
        )
    return "".join(parts)


def make_handler(
    data_dir: Path, *, include_reviewer: bool = False
) -> type[BaseHTTPRequestHandler]:
    """构造 handler 类（把数据目录绑进去，避免全局状态）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "March7Control/1.0"

        def do_GET(self) -> None:  # noqa: N802 —— 标准库要求这个名字
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            # `/__panel` 是给启动器看的身份端点（S17）：只回自己的数据目录与 pid。
            # 不读库、不建库 ⇒ 库不存在时也能回答"我是谁"，启动器据此判断
            # 「这个端口上跑的是不是**这个库**」（A9 的机械判据）。
            if parsed.path == "/__panel":
                self._json(
                    {"ok": True, "data_dir": str(data_dir), "pid": os.getpid()}
                )
                return
            # ⚠️ 库不存在 ⇒ **不构造 MemoryStore**（它的构造函数会建目录 + 建库）。
            # 这是卡里的「打开时不许初始化未绑定的库」。
            db_path = data_dir / config.DB_FILENAME
            if not db_path.exists():
                self._unavailable(db_path)
                return
            store = MemoryStore(db_path)
            try:
                status = (query.get("status") or [None])[0]
                if parsed.path in ("/", "/index.html"):
                    body, title = render_index(store, include_reviewer=include_reviewer, status=status), "记忆"
                elif parsed.path == "/receipts":
                    body, title = render_receipts_page(data_dir), "删除回执"
                elif parsed.path == "/health":
                    body, title = render_health_page(store, data_dir, include_reviewer=include_reviewer), "健康"
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "no such page")
                    return
            finally:
                store.close()
            payload = _page(title, body)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            # 只读页面也不需要缓存：库随时在变，缓存会让"删了还在列表里"
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, payload: dict) -> None:
            """回一小段 JSON（只给 `/__panel` 用，不含任何用户内容）。"""
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _unavailable(self, db_path: Path) -> None:
            """库不存在 ⇒ 503 + 说明。**绝不**在这里建库（S17 的硬约束）。"""
            body = (
                "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>没有记忆库</title></head><body>"
                "<h1>这个数据目录里没有记忆库</h1>"
                f"<p>找不到 <code>{_e(db_path)}</code>。</p>"
                "<p>面板不会替你新建一个空库——那样你会看到一片空白，"
                "以为记忆丢了。请先在正确的位置完成安装，或用 "
                "<code>--data-dir</code> 指向真正的库。</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _reject(self) -> None:
            """写方法一律 405（**只读**是硬约束，不是"没实现"）。

            ⚠️ 错误消息必须是 **ASCII**：`http.server.send_error` 把 message
            编进状态行，而状态行按 HTTP 规范只能是 latin-1 ⇒ 中文会让
            `send_response_only` 抛 `UnicodeEncodeError`，客户端拿到的是
            **连接重置**而不是 405（实测踩到）。中文说明放在 body 里。
            """
            body = (
                "March7 control web is read-only. "
                "Use the CLI for writes: python -m March7.control --help\n"
                "（本页面只读；归档 / 修正 / 删除请用命令行。）\n"
            ).encode("utf-8")
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Allow", "GET")
            self.end_headers()
            self.wfile.write(body)

        do_POST = _reject  # noqa: N815
        do_PUT = _reject  # noqa: N815
        do_DELETE = _reject  # noqa: N815
        do_PATCH = _reject  # noqa: N815

        def log_message(self, fmt: str, *args: object) -> None:
            """访问日志走 stderr（stdout 留给使用者；与 CONVENTIONS §3 同精神）。"""
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return Handler


def serve(
    data_dir: Path, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, include_reviewer: bool = False,
    manage_key: str | None = None,
) -> ThreadingHTTPServer:
    """起服务（返回 server 对象，调用方负责 `serve_forever()` / `shutdown()`）。

    拆出"建 server"而不直接 `serve_forever()`：测试要能在随机端口起、
    发一次请求、再关掉——直接 `serve_forever()` 会让测试必须开线程杀进程。
    """
    if manage_key is None:
        handler = make_handler(data_dir, include_reviewer=include_reviewer)
    else:
        from March7.control import panel_write

        if host != DEFAULT_HOST or port != 0 or not (data_dir / config.DB_FILENAME).is_file():
            raise ValueError("可写面板仅能在已有本机库与随机回环端口上启动。")
        handler = panel_write.make_handler(data_dir, manage_key)
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    """入口。"""
    parser = argparse.ArgumentParser(
        prog="March7 control web",
        description="March7 只读 Web 控制台（默认只绑 127.0.0.1）",
    )
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 $MARCH7_HOME 或 ~/.March7）")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"绑定地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
    parser.add_argument(
        "--include-reviewer",
        action="store_true",
        help="展开打码字段（投屏/截图时别加）",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="启动后把 pid/端口写进这个文件（S17 的启动器靠它复用实例）",
    )
    parser.add_argument("--log-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--manage", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.manage and not args.state_file:
        parser.error("可写面板请从本机启动器打开。")
    if args.log_file:
        with open(args.log_file, "a", encoding="utf-8", buffering=1) as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                return _serve(args)
    return _serve(args)


def _serve(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else config.resolve_data_dir()
    manage_key = secrets.token_urlsafe(32) if args.manage else None
    httpd = serve(
        data_dir, host=args.host, port=args.port, include_reviewer=args.include_reviewer,
        manage_key=manage_key,
    )
    # ⚠️ 端口必须读 `server_address`（不是 `args.port`）：启动器传的是 0，
    # 真实端口由内核分配；把 0 写进状态文件等于告诉下一次打开"我在 0 端口"。
    port = int(httpd.server_address[1])
    state_file = Path(args.state_file) if args.state_file else None
    if state_file is not None:
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            staged = state_file.with_suffix(f".{os.getpid()}.tmp")
            with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "port": port,
                        "host": args.host,
                        "data_dir": str(data_dir),
                        "started_at": int(time.time()),
                        **({"manage_key": manage_key} if manage_key else {}),
                    },
                    output, ensure_ascii=False,
                )
            staged.replace(state_file)
        except OSError as exc:  # 写不了状态文件不该让面板起不来
            sys.stderr.write(f"state file 写入失败：{exc}\n")
    label = "本机管理" if args.manage else "只读"
    print(f"March7 控制台（{label}）：http://{args.host}:{port}/  —— Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
        if state_file is not None:
            # 只删自己的状态：被新实例覆盖时留新的（否则会把刚起来的记录抹掉）
            try:
                current = json.loads(state_file.read_text(encoding="utf-8"))
                if int(current.get("pid") or 0) == os.getpid():
                    state_file.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "main",
    "make_handler",
    "render_health_page",
    "render_index",
    "render_receipts_page",
    "serve",
]
