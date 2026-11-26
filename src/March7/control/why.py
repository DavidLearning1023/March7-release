"""control/why.py — 「为什么没记住」（S11b / `ADR-0058` 决策 1）

## 它解决的是什么

说不出一句话为什么没进库，是 beta 首测里**最常发生**的交互。而在本模块之前，用户只有三条路：

| 路径 | 用户能不能看懂 |
|---|---|
| `~/.March7/logs/writes-*.jsonl` | ❌ 一坨 JSON，字段名是 `precheck_failed` / `l0_updated` |
| `~/.March7/logs/hooks-*.jsonl` 的 `rejected_reasons` | ❌ 同上，还要按 session 对齐 |
| `March7_status` 的 `last_receipt` | ⚠️ 那是**召回侧**凭条，不是写入侧 |

**根因不是没记，是记了但没给出口。** 拒收理由其实一直存在（`Result.rejected` 带
`rule` + `reason`，`stop.py` 也确实写进了 `hooks-*.jsonl`），缺的只是一个用户视角的命令。

## 三条硬约束

1. **只读**：不调 LLM、不建库（`MemoryStore.__init__` 会建文件）、不改账本。
   一个"查为什么"的命令顺手建出一个空库，会让本已数据目录错位的用户更难查。
2. **不改账本格式**（`ADR-0026` 的字段一个不动）：本模块是**读取层**，不是新写入。
3. **输出脱敏**：账本字段是给维护者看的，`why` 是给用户看的。直接把账本字符串透传出去，
   等于把"脱敏只在注入面做"这条纪律绕开了。

## 五档结论（`ADR-0058` 说三类，这里是五档）

| 结论 | 判据 | 用户该做什么 |
|---|---|---|
| `failed` | `failed=True` | 看 key / 余额 / 端点——**要修** |
| `capped` | `capped=True` | 今日触顶。正常保护，明天再来 |
| `rejected` | `rejected > 0` | 读那句「宁可漏记，不可误记：…」，判断是不是设计取舍 |
| `written` | `written > 0` | 已记住（他可能忘了自己已成功） |
| `empty` | 其余 | 本轮没有候选——什么都没发生 |

把"写成功"与"空转"并进"拒收"会让**正常看起来像被拒**，而这两者的处置完全相反。
判定优先级严格照 `ADR-0058`：`failed > capped > rejected > written > 空转`。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONCLUSION_FAILED = "failed"
CONCLUSION_CAPPED = "capped"
CONCLUSION_REJECTED = "rejected"
CONCLUSION_WRITTEN = "written"
CONCLUSION_EMPTY = "empty"
CONCLUSION_DRIFT = "drift"
"""第六档（S14）：这一轮**记到了可疑的项目**里。

它不是"没记住"，也不是"被拒收"——记忆很可能**写成功了**，只是写进了别的项目
（`codex exec resume` 时宿主给的 cwd 会变成"敲命令的那个目录"，`ADR-0063` §3.1）。
把它塞进前五档任何一档都会误导：用户会去查 key / 余额 / 判据，而真正要改的是目录。
"""

EVENT_CWD_DRIFT = "codex_cwd_drift"
"""账本里那一行的 `event` 值（写侧在 `hosts/codex/codex_notify.py`）。

⚠️ 字符串在两处各写一遍（写侧与读侧）：读侧不许 import 宿主脚本
（引擎零宿主依赖）。一致性由 `tests/test_control_why.py::test_drift_event_name_matches_writer` 钉住。
"""


@dataclass
class WhyEntry:
    """一轮的结论。字段刻意少——多一个字段就多一个泄密面。"""

    ts: int
    conclusion: str
    count: int = 0
    """`written` 档里的写入条数 / `rejected` 档里的拒收条数。"""

    detail: str = ""
    """`failed` 档的 `error_type` / `rejected` 档的规则名。**只能是白名单内的短标识**。"""

    reasons: list[str] = field(default_factory=list)
    """拒收理由的**规则名**列表（不是理由全文——全文可能含原文）。"""


def _as_int(value: Any) -> int:
    """宽松转 int（账本是自建 JSONL，历史上出现过 `"3"` 这样的字符串）。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# 「像不像一个短标识」的白名单。**不是路径正则**——它只描述标识该长什么样，
# 路径因为含 `/` `:` 空格或超长，自然过不了（路径识别的唯一实现在 `privacy.PATH_RE`）。
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")


def _safe_detail(raw: Any) -> str:
    """把 `detail` 收成「短标识」——不像标识就整条丢掉，绝不透传。

    为什么不是"把路径替换成 `<路径>`"：`why` 的输出是给人看的一句话，
    里面出现一个 `<路径>` 反而像 bug。标识不像标识 ⇒ 干脆不显示。

    ⚠️ **这里刻意不写路径正则**（`test_no_second_path_regex` 会红）：
    路径识别全仓只许有一份（`privacy.PATH_RE`）。本函数用**白名单正向匹配**
    来判断"它像不像一个短标识"，不需要知道路径长什么样 —— 任何含 `/`、`:`、
    空格或超长的字符串都过不了白名单，路径自然被挡在外面。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if _SAFE_IDENT_RE.match(text):
        return text
    return ""


def _safe_reason(raw: Any) -> str:
    """拒收理由只留**规则名**。理由全文（可能带出用户目录路径）一律不出现。"""
    if isinstance(raw, dict):
        rule = raw.get("rule")
        if rule is not None:
            return _safe_detail(rule)
        return ""
    return _safe_detail(raw)


def classify_row(row: dict[str, Any]) -> WhyEntry:
    """把一行账本判成五档之一（纯函数，判定优先级照 `ADR-0058` 决策 1）。

    Args:
        row: `writes-*.jsonl` 的一行。

    Returns:
        结论 + 计数 + 短标识。
    """
    ts = _as_int(row.get("ts"))
    failed = bool(row.get("failed"))
    capped = bool(row.get("capped"))
    written = _as_int(row.get("written"))
    rejected = _as_int(row.get("rejected"))
    project_rejected = _as_int(row.get("project_rejected"))

    # 事件行（不是管线行）：`ADR-0067` 允许账本里出现 `event` 行，
    # 但它们**没有** written/rejected 语义 ⇒ 必须先判，否则会被判成"空转"。
    if row.get("project_blocked") is True or row.get("event") == "project_session_changed":
        return WhyEntry(ts=ts, conclusion=CONCLUSION_DRIFT, detail="project_blocked")
    if str(row.get("event") or "") == EVENT_CWD_DRIFT:
        return WhyEntry(ts=ts, conclusion=CONCLUSION_DRIFT, detail=_safe_detail(row.get("host")))

    if failed:
        return WhyEntry(ts=ts, conclusion=CONCLUSION_FAILED, detail=_safe_detail(row.get("error_type")))
    if capped:
        return WhyEntry(ts=ts, conclusion=CONCLUSION_CAPPED, count=rejected)
    if project_rejected:
        return WhyEntry(ts=ts, conclusion=CONCLUSION_REJECTED, count=project_rejected, detail="project_identity_required")
    if rejected > 0:
        raw_reasons = row.get("rejected_reasons")
        reasons: list[str] = []
        if isinstance(raw_reasons, list):
            for item in raw_reasons:
                name = _safe_reason(item)
                if name and name not in reasons:
                    reasons.append(name)
        # `detail` 取第一条：用户只需要一个抓手，列全部规则名反而看不出重点
        return WhyEntry(
            ts=ts,
            conclusion=CONCLUSION_REJECTED,
            count=rejected,
            detail=reasons[0] if reasons else "",
            reasons=reasons,
        )
    if written > 0:
        return WhyEntry(ts=ts, conclusion=CONCLUSION_WRITTEN, count=written)
    return WhyEntry(ts=ts, conclusion=CONCLUSION_EMPTY)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读 JSONL，**坏行跳过**（账本是追加写，写到一半断电是正常现象）。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                import json

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError as exc:
        logger.warning("读写入账本失败（按空处理）：%s", exc)
        return []
    return rows


def rows_for_day(
    data_dir: Path,
    *,
    when: float | None = None,
    last_only: bool = False,
    session: str | None = None,
) -> list[WhyEntry]:
    """读某一天的写入账本并判档；按时间**升序**返回。

    Args:
        data_dir: 数据目录（`~/.March7` 或 `~/.March7-dev`）。
        when: 目标时刻（默认现在）。只影响"读哪一天的文件"。
        last_only: 只返回最后一轮。
        session: 只看某个 `conversation_id`（账本里有这个字段）。

    Returns:
        结论列表；没有账本 ⇒ 空列表（新装用户的第一次 `why` 走这一支）。
    """
    from March7 import ledger

    path = ledger.ledger_path(data_dir, when)
    raw_rows = _read_jsonl(path)
    if session:
        raw_rows = [row for row in raw_rows if str(row.get("conversation_id") or "") == session]

    entries = [classify_row(row) for row in raw_rows]
    entries.sort(key=lambda item: item.ts)
    if last_only:
        return entries[-1:]
    return entries


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


_FAILED_HINT = "LLM 调用失败（检查 key / 余额 / 端点）"
_CAPPED_HINT = "今日写入已到上限（保护性熔断，明天自动恢复）"
_EMPTY_HINT = "本轮没有值得记的候选"


def _describe(entry: WhyEntry) -> str:
    """一轮 → 一句中文。**不带任何账本里的自由文本**。"""
    if entry.conclusion == CONCLUSION_WRITTEN:
        return "写入 %d 条 ✓" % entry.count
    if entry.conclusion == CONCLUSION_DRIFT:
        if entry.detail == "project_blocked":
            return "项目身份与会话不一致，已暂停本轮操作。请核对目录、显式指定项目或新开会话。"
        # ⚠️ 必须出现字面量 `codex_cwd_drift`：用户拿它去搜文档 / 贴给维护者
        # （A9 的判据就是"why 能显示这个名字"，不是"能显示一句很像的话"）。
        suffix = f"［{entry.detail}］" if entry.detail else ""
        return (
            "⚠ 项目身份疑似漂移（codex_cwd_drift）%s —— 宿主给的 cwd 与本进程 cwd 不同；"
            "若这是一次 `codex resume`，记忆可能记到了别的项目（ADR-0063 §3.1）" % suffix
        )
    if entry.conclusion == CONCLUSION_FAILED:
        suffix = f"［{entry.detail}］" if entry.detail else ""
        return "没写入 —— %s%s" % (_FAILED_HINT, suffix)
    if entry.conclusion == CONCLUSION_CAPPED:
        return "没写入 —— %s%s" % (
            _CAPPED_HINT,
            f"（本轮另拒收 {entry.count} 条）" if entry.count else "",
        )
    if entry.conclusion == CONCLUSION_REJECTED:
        if entry.detail == "project_identity_required":
            return f"{entry.count} 条项目记忆缺少项目身份，已拒写。请核对目录后重试。"
        which = f"判为「{entry.detail}」" if entry.detail else "被硬过滤拒收"
        return "没写入 —— %s（共 %d 条）" % (which, entry.count)
    return "没写入 —— %s" % _EMPTY_HINT


def render_why(rows: list[WhyEntry], *, day_label: str = "") -> str:
    """把结论列表渲染成用户能读的文本（每轮一行）。

    Args:
        rows: `rows_for_day()` 的结果。
        day_label: 日期标签（如 `2026-11-14`）；空则用"今天"。

    Returns:
        多行文本。没有记录时返回一句人话（**不是空串**——空串看起来像命令坏了）。
    """
    label = day_label or "今天"
    if not rows:
        return (
            f"{label}还没有任何一轮写入记录。\n"
            "  · 全新安装很常见（还没在宿主里说过话）\n"
            "  · 若你刚说过话，可能是钩子没装或没重启宿主：跑 "
            "`python hosts/workbuddy/install.py --status`"
        )

    lines = [f"{label}的写入结论（最近的在下）："]
    for entry in rows:
        clock = time.strftime("%H:%M", time.localtime(entry.ts)) if entry.ts else "--:--"
        lines.append(f"  {clock}  {_describe(entry)}")
    return "\n".join(lines)


def render_for_dir(
    data_dir: Path,
    *,
    when: float | None = None,
    last_only: bool = False,
    session: str | None = None,
) -> str:
    """一步到位：读目录 → 判档 → 渲染（CLI 与测试共用的唯一入口）。"""
    rows = rows_for_day(data_dir, when=when, last_only=last_only, session=session)
    target = when if when is not None else time.time()
    day_label = time.strftime("%Y-%m-%d", time.localtime(target))
    if time.strftime("%Y%m%d", time.localtime(target)) == time.strftime("%Y%m%d"):
        day_label = "今天"
    return render_why(rows, day_label=day_label)


__all__ = [
    "CONCLUSION_CAPPED",
    "CONCLUSION_DRIFT",
    "CONCLUSION_EMPTY",
    "CONCLUSION_FAILED",
    "CONCLUSION_REJECTED",
    "CONCLUSION_WRITTEN",
    "EVENT_CWD_DRIFT",
    "WhyEntry",
    "classify_row",
    "render_for_dir",
    "render_why",
    "rows_for_day",
]
