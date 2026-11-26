"""control/cli.py — 用户控制面（本地 CLI，内部代号：**明镜台**）

用户**不看说明书**就能「看 → 改 → 删」自己的记忆。本模块是那条路径的唯一入口。

## 五条命令（对应 `ADR-0040` 的三个概念）

| 命令 | 做什么 | 语义 |
|---|---|---|
| `list` | 列记忆 / 来源 / 状态 | 只读 |
| `show <id>` | 一条记忆的全部派生面 | 只读 |
| `archive <id>` | 标 `archived`（不删行） | **软归档**：无后继的失效 |
| `fix <id>` | 标旧条 `superseded` 并写入新条 | **修正**：用户提供更正内容 |
| `erase <id> \\| --project \\| --all` | 物理删除 + 出回执 | **删除**：用户要求不再保留 |
| `receipts` | 列 / 看删除回执 | 只读 |
| `health` | 库路径哈希 / schema / 双宿主 / 写入预算 | 只读 |

## 三条硬约束

1. **删除只由用户显式触发**：本模块是**唯一**能调 `store.erase_*` 的地方
   （`server.py` 的 8 个工具里**没有**删除工具，`ADR-0040` 决策 4 明令）。
   `test_control_cli.py::test_no_erase_tool_in_server` 静态钉住。
2. **默认打码 `root_basename` 与 `reviewed_by`**，`--include-reviewer` 才展开明文。
   理由：用户在终端里看自己的记忆，不需要每个字段都是明文；而**截图/投屏/录屏**
   是这个 CLI 最常见的暴露面（`--include-reviewer` 是显式的逃生舱）。
3. **修正保留历史**：`fix` 在同一事务中标旧条失效、写入新条及其索引和来源
   （`ADR-0040` 决策 1）。

## 为什么 CLI 先行、Web 只读

写操作要**二次确认**，而"在浏览器里点一下就把记忆删了"是最容易误触的形态。
所以 Web 只出列表/来源/健康页（只读），写操作全部留在 CLI——那里有
`--yes` 才能跳过的确认提示，用户看得见自己要删什么。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from March7 import config, delete_receipt, erase, privacy
from March7.embedder import Embedder
from March7.l0_profile import ProfileStore
from March7.schemas import MemoryStatus
from March7.store import MemoryStore

# ---------------------------------------------------------------------------
# 打码
# ---------------------------------------------------------------------------

MASK = "***"
"""打码后的占位符（三字符，与项目其它占位符 `<路径>` 风格一致：短、明显、不误导）。"""


def mask_value(value: str | None, *, reveal: bool) -> str:
    """默认打码，`reveal=True` 才给明文（`--include-reviewer`）。

    为什么这两个字段要单独打码：`root_basename` 是**目录名**（项目名往往就是
    公司名/课题名），`reviewed_by` 是**人名**。它们出现在截图里的概率远高于
    记忆正文——而正文是用户自己写的、他本来就知道，人名和目录名不是。
    """
    if reveal:
        return value or ""
    if not value:
        return ""
    return MASK


def mask_scope(scope: str | None, *, reveal: bool) -> str:
    """scope 里的项目部分打码（`project:p-abc123` → `project:***`）。

    `project:<id>` 的 id 已经是假名（`p-<6 hex>`），本身不泄露路径；
    但 `ADR-0039` 之前的老数据可能有 `project:<目录名>` 这种写法（历史遗留），
    所以按"可能是目录名"处理 ⇒ 一并打码。
    """
    text = scope or "global"
    if reveal or not text.startswith("project:"):
        return text
    return f"project:{MASK}"


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------


def _format_ts(stamp: int | None) -> str:
    """Unix 秒 → 本地可读时间；None → `—`。"""
    if stamp is None:
        return "—"
    import time as _time

    try:
        return _time.strftime("%Y-%m-%d %H:%M", _time.localtime(int(stamp)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _truncate(text: str | None, limit: int = 60) -> str:
    """单行截断（列表页用；详情页不截）。"""
    raw = (text or "").replace("\n", " ").strip()
    return raw if len(raw) <= limit else raw[: limit - 1] + "…"


def render_project_line(
    project: privacy.ProjectResolution, *, include_reviewer: bool = False
) -> str:
    """`项目：p-xxxxxx（<目录名>）` 一行；没解析出来就说清为什么。

    为什么 `project_id` 一律明文、目录名默认打码：id 是假名（`p-<6 hex>`），
    打码反而让它没法被核对；而目录名往往就是公司/课题名，是截图里最容易泄露的东西
    （同 `mask_value` 的口径）。要明文就加 `--include-reviewer`。

    ⚠️ **解析不出来不是错误**：`ADR-0039` 决策 3 规定那时只读写 `global`。
    这行的作用是让人一眼看出"我现在被当成了没有项目"——而不是以为记忆丢了。
    """
    if not project.project_id:
        reason = project.event or privacy.EVENT_PROJECT_UNRESOLVED
        return f"项目：—（{reason} ⇒ 本轮只读写 global）"
    root = project.root or ""
    basename = Path(root).name if root else ""
    shown = mask_value(basename, reveal=include_reviewer) or "—"
    return f"项目：{project.project_id}（{shown}）"


def render_preamble(
    data_dir: Path,
    project: privacy.ProjectResolution | None = None,
    *,
    include_reviewer: bool = False,
) -> str:
    """所有 `control` 子命令的**输出头**：先告诉你"我在看哪个库"。

    为什么固定打这一行（`ADR-0066` §6.1）：Codex 实测踩过一次——`why --last`
    在**默认目录**上显示了旧的成功结论，而用户以为那是"指定目录的成功"。
    `why` 本身没错（空目录它会说没有记录），错的是**它从不告诉你读的是哪个目录**。
    这一行是一次性消灭那类误判的最便宜办法。
    """
    lines = [f"数据目录：{data_dir}"]
    if project is not None:
        lines.append(render_project_line(project, include_reviewer=include_reviewer))
    return "\n".join(lines)


def render_list(
    store: MemoryStore,
    *,
    include_reviewer: bool = False,
    status: str | None = None,
    limit: int = 50,
    project_id: str | None = None,
) -> str:
    """`list` 的输出（**人读**，不是 JSON）。

    列顺序刻意是「状态 → 时间 → 内容 → scope」：用户扫一眼先看"这条还活着吗"，
    再看"什么时候记的"，最后才看内容（内容他大多记得）。

    Args:
        project_id: 给了 ⇒ 只列**这个项目看得见**的记忆（`global` + `project:<id>`，
            判据是 `retriever.filter_by_scope`，与读写两侧同一份）。
            `None`（默认）⇒ 不过滤，保持历史行为——「没指定项目」不等于「只看 global」，
            否则用户会以为记忆丢了（`ADR-0039` 决策 3 只约束**召回**，不约束**盘点**）。
    """
    total = store.stats().memory_count
    memories = store.list_recent(scope=None, since=None, limit=max(total, 1))
    if project_id is not None:
        from March7.retriever import filter_by_scope

        memories = filter_by_scope(memories, project_id)
    if status:
        memories = [m for m in memories if m.status.value == status]
    memories = memories[:limit]
    if not memories:
        return "（没有记忆）"
    lines = [
        f"共 {total} 条（显示 {len(memories)} 条）",
        "",
        f"{'状态':<10} {'时间':<16} {'scope':<18} 内容",
        "-" * 78,
    ]
    for mem in memories:
        scope = mask_scope(mem.scope, reveal=include_reviewer)
        lines.append(
            f"{mem.status.value:<10} {_format_ts(mem.created_at):<16} "
            f"{scope:<18} {_truncate(mem.content)}"
        )
        lines.append(f"{'':<10} id={mem.id}")
    return "\n".join(lines)


def render_show(
    store: MemoryStore, memory_id: str, *, include_reviewer: bool = False
) -> str:
    """`show` 的输出：一条记忆的**全部**派生面（用户要能核对"删了会删掉什么"）。"""
    mem = store.get_memory(memory_id)
    if mem is None:
        return f"找不到记忆 {memory_id}（可能已被删除）"
    lines = [
        f"记忆 {mem.id}",
        f"  状态：{mem.status.value}",
        f"  作用域：{mask_scope(mem.scope, reveal=include_reviewer)}",
        f"  宿主：{mem.host or '—'}",
        f"  写入：{_format_ts(mem.created_at)}",
        f"  最后召回：{_format_ts(mem.last_accessed_at)}（{mem.access_count} 次）",
        "",
        f"  结论：{mem.content}",
        f"  原话：{mem.source_quote or '—'}",
        f"  触发：{mem.trigger_text or '—'}",
    ]
    source = store.get_source(memory_id)
    if source:
        lines += [
            "",
            "  来源四元组：",
            f"    会话：{source.get('session_id') or '—'}",
            f"    消息：{source.get('message_id') or '—'}",
            f"    版本：{source.get('revision') or '—'}",
        ]
    claims = store.list_claims(memory_id)
    if claims:
        lines += ["", f"  claims（{len(claims)} 条）："]
        for claim in claims:
            lines.append(f"    {claim.predicate} = {claim.value}（{claim.status.value}）")
    conflicts = store.count_conflicts_touching(memory_id)
    if conflicts:
        lines += ["", f"  冲突记录：{conflicts} 条"]
    lines += ["", "提示：删除这条会同时清掉上面列出的全部派生面。"]
    return "\n".join(lines)


def render_receipts(rows: list[dict[str, Any]]) -> str:
    """`receipts` 列表输出。"""
    if not rows:
        return "（没有任何删除回执）"
    lines = [
        f"共 {len(rows)} 张删除回执",
        "",
        f"{'回执 id':<26} {'范围':<8} {'时间':<18} 验证",
        "-" * 72,
    ]
    for row in rows:
        lines.append(
            f"{str(row.get('receipt_id')):<26} {str(row.get('scope')):<8} "
            f"{str(row.get('deleted_at_text')):<18} {row.get('verdict')}"
        )
    return "\n".join(lines)


def render_audit(audit: list[dict[str, Any]]) -> str:
    """逐面打印 `unit + units_scanned + rows_total + rows_scanned`（A1 要求的形态）。

    ⚠️ 业务口径**只认 `rows_scanned`**：所以每行都把它单独列出来，
    并在两种异常时打标记：
    - `rows_scanned < rows_total` ⇒ `⚠未扫全`（这一面没查完，残留 0 不算数）；
    - `scan_error` ⇒ `✗扫描失败`（压根没查成，**不是**"干净"）。
    """
    lines = [
        f"{'#':<3} {'档位':<14} {'单位':<34} {'units':>5} {'total':>6} {'scanned':>7} {'残留':>4}",
        "-" * 86,
    ]
    for row in audit:
        scanned = int(row["rows_scanned"])
        total = int(row["rows_total"])
        if row.get("scan_error"):
            flag = " ✗扫描失败"
        elif scanned < total:
            flag = " ⚠未扫全"
        else:
            flag = ""
        lines.append(
            f"{row['number']:<3} {row['kind']:<14} {row['unit'][:34]:<34} "
            f"{row['units_scanned']:>5} {total:>6} {scanned:>7} "
            f"{row['residue_count']:>4}{flag}"
        )
    return "\n".join(lines)


def render_health(store: MemoryStore, data_dir: Path, *, include_reviewer: bool = False) -> str:
    """`health` 页：库路径哈希 / schema / 双宿主 / 写入预算 / 回执数。

    `root_basename` 与 `reviewed_by` 默认打码（`--include-reviewer` 才展开）。
    """
    stats = store.stats()
    basenames = _known_basenames(store)
    lines = [
        "March7 健康",
        f"  数据目录：{data_dir}",
        f"  schema 版本：{stats.schema_version}",
        f"  记忆条数：{stats.memory_count}（可召回 {stats.recallable_count}）",
        f"  今日写入：{stats.today_writes} / 上限 {stats.daily_write_cap or '不限'}",
        f"  库大小：{stats.db_size_bytes / 1024:.1f} KiB",
        f"  最近写入：{_format_ts(stats.last_write_at)}",
        "",
        f"  已知项目（{len(basenames)}）：",
    ]
    if basenames:
        for project_id, basename in basenames:
            lines.append(
                f"    {project_id}  {mask_value(basename, reveal=include_reviewer)}"
            )
    else:
        lines.append("    （还没有登记任何项目）")
    hosts = _host_rows(store, include_reviewer=include_reviewer)
    lines += ["", f"  宿主登记（{len(hosts)}）："]
    if hosts:
        for host, reviewed_by in hosts:
            lines.append(f"    {host}  reviewed_by={mask_value(reviewed_by, reveal=include_reviewer)}")
    else:
        lines.append("    （还没有宿主登记）")
    receipts = delete_receipt.list_receipts(data_dir)
    lines += ["", f"  删除回执：{len(receipts)} 张"]
    if receipts:
        lines.append(f"    最近：{receipts[0].get('receipt_id')}（{receipts[0].get('verdict')}）")
    return "\n".join(lines)


def _known_basenames(store: MemoryStore) -> list[tuple[str, str]]:
    """`(project_id, basename)` 列表（`project_basenames` 表）。"""
    try:
        rows = store._conn.execute(
            "SELECT DISTINCT project_id, basename FROM project_basenames ORDER BY project_id"
        ).fetchall()
    except Exception:  # noqa: BLE001 —— 健康页不能因为一张可选的表而崩
        return []
    return [(str(r[0]), str(r[1])) for r in rows]


def _host_rows(store: MemoryStore, *, include_reviewer: bool) -> list[tuple[str, str]]:
    """`(host, reviewed_by)` 列表（`host_registry` 表）。

    `host_registry` 本身没有 `reviewed_by` 列（那是 `handoffs` 的列），
    所以这里取两处：登记过的宿主 + handoffs 里出现过的人名。
    """
    out: list[tuple[str, str]] = []
    try:
        rows = store._conn.execute("SELECT host FROM host_registry ORDER BY host").fetchall()
        for row in rows:
            out.append((str(row[0]), ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = store._conn.execute(
            "SELECT DISTINCT started_by_host, reviewed_by FROM handoffs "
            "WHERE reviewed_by IS NOT NULL ORDER BY started_by_host"
        ).fetchall()
        for row in rows:
            out.append((f"{row[0]}（handoff 复核人）", str(row[1])))
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# 写操作
# ---------------------------------------------------------------------------


def _confirm(prompt: str, *, assume_yes: bool, for_erase: bool = True) -> bool:
    """二次确认（`--yes` 跳过）。非交互环境下**默认拒绝**。

    ⚠️ **`input()` 必须包 try**（2026-11-11 A6 试用实测）：`isatty()` 返回 True
    并不保证能读到一行——Git Bash 的 `< /dev/null` 重定向、某些 CI 的伪终端、
    以及宿主把 stdin 接成管道但保留了 TTY 标志时，`input()` 会抛 `EOFError`，
    于是 `erase` **整条命令崩在堆栈里**，用户看到的是 Python traceback 而不是
    「需要 --yes」。删除是高风险操作，**崩在堆栈上会让人以为数据坏了**，
    必须收敛成一句可读的拒绝。

    Returns:
        `True` 仅当用户**明确**输入了 `y` / `yes` / `是`；其余一律 `False`。
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        if for_erase:
            print("非交互环境：需要显式加 --yes 才会执行（这是删除，不做静默确认）。")
        else:
            print("非交互环境：需要显式加 --yes 才会执行修正。")
        return False
    cancelled = "没有删除任何东西。" if for_erase else "没有修正任何记忆。"
    try:
        answer = input(f"{prompt} 输入 yes 确认：").strip().lower()
    except EOFError:
        # 读到空 → 当作拒绝（**不是**当作同意），并说明为什么
        print(
            f"\n读不到输入（stdin 已结束）。已按「取消」处理，{cancelled}\n"
            "若要执行，请在能交互的终端里重跑，或显式加 --yes。"
        )
        return False
    except KeyboardInterrupt:
        print(f"\n已取消（Ctrl+C）。{cancelled}")
        return False
    return answer in ("y", "yes", "是")


def cmd_archive(store: MemoryStore, memory_id: str) -> int:
    """软归档：标 `archived`，**不删行**（导出仍可见，注入不再出现）。"""
    mem = store.get_memory(memory_id)
    if mem is None:
        print(f"找不到记忆 {memory_id}")
        return 1
    store.update_status(memory_id, MemoryStatus.ARCHIVED)
    print(f"已归档 {memory_id}（status=archived，行仍在、导出可见、不再注入）")
    return 0


def cmd_fix(
    store: MemoryStore,
    data_dir: Path,
    memory_id: str,
    new_content: str,
    *,
    assume_yes: bool,
) -> int:
    """重新编码用户输入，并在一个事务里失效旧条、写入新条。"""
    old = store.get_memory(memory_id)
    if old is None:
        print(f"找不到记忆 {memory_id}")
        return 1
    if old.status not in (MemoryStatus.ACTIVE, MemoryStatus.AGING):
        print(f"记忆 {memory_id} 已失效，请选择当前有效的记忆。")
        return 1
    if not _confirm(
        f"将旧条 {memory_id} 标记为失效并保留历史，同时写入新内容。",
        assume_yes=assume_yes,
        for_erase=False,
    ):
        print("已取消。")
        return 1

    new_id = _new_memory_id()
    try:
        # 编码可能需要加载模型，拿到向量后才开启写事务。
        embedding = Embedder().embed_one(new_content)
        fixed = store.fix_memory(old, new_id, new_content, embedding)
    except Exception as exc:
        print(f"修正失败：{exc}", file=sys.stderr)
        return 1
    if not fixed:
        print(f"修正失败：记忆 {memory_id} 已变化，请重新查看当前记忆。", file=sys.stderr)
        return 1
    print(f"已修正：旧条 {memory_id} 已标为 superseded，新条 {new_id}")
    return 0


def _new_memory_id() -> str:
    """新记忆 id（uuid4，与 `judge.py` 同一规则）。"""
    import uuid

    return str(uuid.uuid4())


def cmd_erase(
    store: MemoryStore,
    data_dir: Path,
    *,
    memory_id: str | None = None,
    project_id: str | None = None,
    everything: bool = False,
    assume_yes: bool = False,
) -> int:
    """物理删除 + 出回执（**唯一**的 erase 入口）。

    四个范围（三者互斥，`memory_id` 优先）：
    - `memory_id`：删一条（`scope=single`）；
    - `project_id`：删该项目的 working/handoffs + 该 scope 的全部记忆（`scope=project`）；
    - `everything`：删全库记忆 + working + handoffs（`scope=all`）。

    Returns:
        0 = 删除且验证通过；1 = 删除但仍有残留 / 失败；2 = 用户取消。
    """
    if memory_id:
        mem = store.get_memory(memory_id)
        if mem is None:
            print(f"找不到记忆 {memory_id}（可能已删除）")
            return 1
        if not _confirm(f"将**物理删除**记忆 {memory_id}：{_truncate(mem.content, 60)}", assume_yes=assume_yes):
            print("已取消。")
            return 2
        targets: tuple[str, ...] = (memory_id,)
        sessions: list[str] = []
        source = store.get_source(memory_id)
        if source and source.get("session_id"):
            sessions.append(str(source["session_id"]))
        scope = delete_receipt.SCOPE_SINGLE
        outcome = erase.perform_erase(
            store,
            data_dir,
            memory_ids=targets,
            session_ids=tuple(sessions),
            include_working=False,  # 单条删除不动项目级 working（那是 --project 的范围）
            profile_store=ProfileStore(data_dir / config.PROFILE_FILENAME),
        )
        deleted = outcome.deleted
    elif project_id:
        ids = _memory_ids_in_scope(store, project_id)
        if not ids:
            print(f"项目 {project_id} 下没有记忆")
            return 1
        if not _confirm(
            f"将**物理删除**项目 {project_id} 下的 {len(ids)} 条记忆与工作层数据",
            assume_yes=assume_yes,
        ):
            print("已取消。")
            return 2
        targets = tuple(ids)
        scope = delete_receipt.SCOPE_PROJECT
        outcome = erase.perform_erase(
            store,
            data_dir,
            memory_ids=targets,
            project_id=project_id,
            profile_store=ProfileStore(data_dir / config.PROFILE_FILENAME),
        )
        deleted = outcome.deleted
    elif everything:
        ids = store.all_memory_ids()
        sessions_all = tuple(store.all_session_ids())
        if not ids and not sessions_all:
            print("库里没有任何记忆")
            return 1
        if not _confirm(
            f"将**物理删除全库** {len(ids)} 条记忆 + 工作层 + 水位线（不可恢复）",
            assume_yes=assume_yes,
        ):
            print("已取消。")
            return 2
        targets = tuple(ids)
        scope = delete_receipt.SCOPE_ALL
        outcome = erase.perform_erase(
            store,
            data_dir,
            memory_ids=targets,
            session_ids=sessions_all,
            profile_store=ProfileStore(data_dir / config.PROFILE_FILENAME),
        )
        deleted = outcome.deleted
    else:
        print("需要指定 --id / --project / --all 之一（见 --help）")
        return 1

    audit = outcome.audit
    receipt = delete_receipt.build_receipt(
        scope=scope,
        targets=targets,
        deleted=deleted,
        audit=audit,
        verification=outcome.verification,
    )
    path = delete_receipt.write_receipt(receipt, data_dir)
    print(render_audit(audit))
    print()
    print(_render_scrub(outcome.scrubbed))
    print()
    print(receipt.summary())
    print(f"  回执文件：{path}")
    if not receipt.verified:
        residual = outcome.residue_rows()
        print()
        print("⚠️ 仍有残留（下面每一行都要人看一眼）：")
        for row in residual:
            print(f"  第 {row['number']} 处 {row['unit']}：{row['residue']}")
        return 1
    return 0


def _render_scrub(scrubbed: dict[str, Any]) -> str:
    """把自由文本面的清理结果压成两行（日志/导出/缓存各删了几行）。"""
    parts: list[str] = []
    for key, label in (("logs", "日志"), ("exports", "导出"), ("cache", "评测缓存")):
        data = scrubbed.get(key) or {}
        removed = int(data.get("rows_removed") or 0)
        scanned = int(data.get("files_scanned") or 0)
        parts.append(f"{label}：扫 {scanned} 个文件，删 {removed} 行/份")
    return "自由文本面清理 —— " + "；".join(parts)


def _memory_ids_in_scope(store: MemoryStore, project_id: str) -> list[str]:
    """某个 `project:<id>` scope 下的全部记忆 id。"""
    rows = store.list_recent(scope=f"project:{project_id}", since=None, limit=100_000)
    return [m.id for m in rows]


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """CLI 参数（`python -m March7.control` 与 `March7 admin` 共用）。"""
    parser = argparse.ArgumentParser(
        prog="March7 control",
        description="March7 用户控制面：看、改、删自己的记忆（删除会出可查回执）。",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="数据目录（默认 $MARCH7_HOME 或 ~/.March7）",
    )
    parser.add_argument(
        "--project",
        default=None,
        metavar="路径",
        help=(
            "人工核对项目身份：给出项目根，输出头会打印解析出的 p-xxxxxx；"
            "`list` 同时只列这个项目看得见的记忆（解析不出来则不改变 list 范围，头部写明原因）"
        ),
    )
    parser.add_argument(
        "--include-reviewer",
        action="store_true",
        help="展开被删除打码的字段（root_basename / reviewed_by）。**截图前别加**",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出记忆")
    p_list.add_argument("--status", default=None, help="按状态筛选（active/archived/…）")
    p_list.add_argument("--limit", type=int, default=50, help="最多显示几条（默认 50）")

    p_show = sub.add_parser("show", help="看一条记忆的全部派生面")
    p_show.add_argument("memory_id")

    p_archive = sub.add_parser("archive", help="软归档（标 archived，不删行）")
    p_archive.add_argument("memory_id")

    p_fix = sub.add_parser("fix", help="保留旧条历史并写入修正后的内容")
    p_fix.add_argument("memory_id")
    p_fix.add_argument("--content", required=True, help="新的结论文本")
    p_fix.add_argument("--yes", action="store_true", help="跳过二次确认")

    p_erase = sub.add_parser("erase", help="物理删除 + 出回执")
    p_erase.add_argument("memory_id", nargs="?", help="要删的记忆 id")
    p_erase.add_argument("--project", default=None, help="按项目假名整批删（p-xxxxxx）")
    p_erase.add_argument("--all", action="store_true", help="删除全库")
    p_erase.add_argument("--yes", action="store_true", help="跳过二次确认")

    p_receipts = sub.add_parser("receipts", help="列出删除回执")
    p_receipts.add_argument("receipt_id", nargs="?", help="看某一张回执的全文")

    sub.add_parser("health", help="健康页")
    sub.add_parser("audit", help="对当前库做一次全量残留审计（不删任何东西）")

    # S11b / ADR-0058 决策 1：用户视角的「为什么没记住」。
    # 为什么不做成 MCP 工具：工具面已经 8 个，再加一个会让模型误以为该主动调；
    # 而"我为什么没记住"是**用户自查**动作，不是模型动作。
    p_why = sub.add_parser("why", help="看某一轮为什么没记住（只读账本，不调 LLM）")
    p_why.add_argument("--last", action="store_true", help="只看最近一轮")
    p_why.add_argument("--session", default=None, help="只看某个会话（conversation_id）")

    # `web`（S14 补）：只读控制台。此前只能 `python -m March7.control.web` 启动，
    # 而 `control --help` 里看不到它 ⇒ 用户不知道有这个东西（手册里写了却敲不出来）。
    # 与 `why` 一样**在 ``MemoryStore(...)`` 之前返回**：一个"看记忆"的命令
    # 不该顺手造出一个空库（理由见下面 why 的注释）。
    p_web = sub.add_parser("web", help="打开只读网页版（本机 http://127.0.0.1:8765/）")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址（默认只绑本机）")
    p_web.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")

    return parser


def main(argv: list[str] | None = None) -> int:
    """入口（返回退出码）。"""
    args = build_parser().parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else config.resolve_data_dir()

    # `--project` 只做**核对与显示**：给一个项目根，把它解析成 `p-xxxxxx` 打印出来。
    # 解析失败不是错误（`ADR-0039` 决策 3：那时只读写 global），由输出头写明原因。
    project = (
        privacy.resolve_active_project(payload_value=args.project) if args.project else None
    )
    project_id = project.project_id if project is not None else None

    # ⚠️ `why` 必须在 `MemoryStore(...)` **之前**返回。
    # `MemoryStore.__init__` 会建库文件 ⇒ 一个"查为什么没记住"的命令顺手造出一个空库，
    # 会让本已"数据目录错位"的用户看到空记忆列表，以为记忆丢了（ADR-0058 决策 1 的只读约束）。
    if args.command == "why":
        from March7.control.why import render_for_dir

        print(render_preamble(data_dir, project, include_reviewer=args.include_reviewer))
        print(render_for_dir(data_dir, last_only=args.last, session=args.session))
        return 0

    # `web` 同样在任何建库动作之前：它是只读控制台，且设计上**不依赖库存在**
    # （`web.py` 自己会处理"库不存在"的情况并显示提示）。
    # ⚠️ 调 `web.main()` 而不是 `web.serve()`：后者只构造 server 对象不启动
    # （那是给测试用的，`docstring` 里写明"调用方负责 serve_forever"）。
    if args.command == "web":
        from March7.control import web

        return web.main(["--data-dir", str(data_dir), "--host", args.host, "--port", str(args.port)])

    store = MemoryStore(data_dir / config.DB_FILENAME)
    store.init_schema()
    try:
        command = args.command
        if command == "list":
            print(render_preamble(data_dir, project, include_reviewer=args.include_reviewer))
            print(
                render_list(
                    store,
                    include_reviewer=args.include_reviewer,
                    status=args.status,
                    limit=args.limit,
                    project_id=project_id,
                )
            )
            return 0
        if command == "show":
            print(render_show(store, args.memory_id, include_reviewer=args.include_reviewer))
            return 0
        if command == "archive":
            return cmd_archive(store, args.memory_id)
        if command == "fix":
            return cmd_fix(store, data_dir, args.memory_id, args.content, assume_yes=args.yes)
        if command == "erase":
            return cmd_erase(
                store,
                data_dir,
                memory_id=args.memory_id,
                project_id=args.project,
                everything=args.all,
                assume_yes=args.yes,
            )
        if command == "receipts":
            if args.receipt_id:
                data = delete_receipt.load_receipt(args.receipt_id, data_dir)
                if data is None:
                    print(f"找不到回执 {args.receipt_id}")
                    return 1
                print(json.dumps(data, ensure_ascii=False, indent=2))
                return 0
            print(render_receipts(delete_receipt.list_receipts(data_dir)))
            return 0
        if command == "health":
            # `health` 自己已经打印数据目录（在本卡之前就有）⇒ 这里只补项目那一行，
            # 不重复打第二遍目录（两行一样的路径会让人以为是两个库）。
            if project is not None:
                print(render_project_line(project, include_reviewer=args.include_reviewer))
            print(render_health(store, data_dir, include_reviewer=args.include_reviewer))
            return 0
        if command == "audit":
            ids = tuple(store.all_memory_ids())
            ctx = erase.EraseContext(
                store=store,
                data_dir=data_dir,
                memory_ids=ids,
                project_ids=tuple(store.all_project_ids()),
                session_ids=tuple(store.all_session_ids()),
                profile_store=ProfileStore(data_dir / config.PROFILE_FILENAME),
            )
            audit = erase.audit_deletion(ctx)
            print(render_audit(audit))
            print()
            print(json.dumps(erase.auto_residue(audit), ensure_ascii=False, indent=2))
            return 0
        return 1
    finally:
        store.close()


__all__ = [
    "MASK",
    "build_parser",
    "cmd_archive",
    "cmd_erase",
    "cmd_fix",
    "main",
    "mask_scope",
    "mask_value",
    "render_audit",
    "render_health",
    "render_list",
    "render_preamble",
    "render_project_line",
    "render_receipts",
    "render_show",
]
