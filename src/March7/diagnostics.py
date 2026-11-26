"""diagnostics.py — 三个「项目身份错乱」诊断事件（内部代号：**验伤**）

## 为什么需要它们

S1 把项目身份压成 `p-<6 hex>`（`privacy.project_id_for`），S3 用它做 scope 过滤。
这套机制有一个共同的失效模式：**同一个人做出两个项目身份**，于是记忆被劈成两半，
而用户在界面上只会觉得"它好像忘了我上周说的话"——没有任何报错。

三个事件分别覆盖三种根因，**混成一个就修不动**：

| 事件 | 症状 | 根因 | 该改什么 |
|---|---|---|---|
| `duplicate_cwd_hash` | 两宿主把同一 cwd 算成两个项目 | 归一化规则漂了 | 统一规则 + 重新登记 |
| `norm_mismatch` | 同 root_hash 但 `norm_version` 不同 | 两宿主代码版本不同 | 升级其中一侧 |
| `project_split_suspected` | 同一目录名在 24h 内出现第二个 project_id | 临时目录被当项目 / 路径不同 | 修判据，**绝不自动合并** |

## 三条硬约束

1. **只报警、不自动合并**（`ADR-0039` 决策 2）：合并 = 改写 `Memory.scope`，
   错了拆不开。合并属用户确认范围（T25）。
2. **不带路径、不带 hash 前缀**（`ADR-0037` 红线 + 蓝图 T20d）：
   只允许 `project_id` + `host` + 计数/布尔。11 位 hex 在日志里已经够被 grep 拼回项目。
   `ALLOWED_FIELDS` 是这条约束的**静态**实现，`build_event` 会在运行时校验它。
3. **假阳性比漏报更贵**：doctor 里一个长期红点会被忽略，然后真出事时没人看。
   所以每个检测函数都有一个"不该报"的用例（见 `tests/test_diagnostics.py`）。

## 为什么本模块只用标准库

钩子（UPS）会在每轮调用它，而钩子进程每轮都付一次 import 成本。
`emit` 走 `hosts/workbuddy/hooks/_common.log_event` 的同一份日志约定
（`<data_dir>/logs/hooks-YYYYMMDD.jsonl`），但不 import 钩子模块——钩子是宿主拉起的
独立进程，不能反向依赖。
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from March7.store import MemoryStore

logger = logging.getLogger(__name__)

EVENT_DUPLICATE_CWD_HASH = "duplicate_cwd_hash"
EVENT_NORM_MISMATCH = "norm_mismatch"
EVENT_PROJECT_SPLIT_SUSPECTED = "project_split_suspected"
RECALL_TRACE_PREFIX = "recall-diagnostics"
_PROJECT_TAG = re.compile(r"p-[a-f0-9]{6}")
_WRITE_MESSAGES = {
    "project_identity_required": "候选属于项目，但写入时缺少可信项目身份。请核对目录后重试。",
    "project_changed": "会话项目与当前目录不一致，已暂停该轮记忆操作。",
    "failed": "记忆判定服务执行失败，可稍后重试。",
    "capped": "今日长期记忆写入已达预算上限。",
    "rejected": "候选未通过记忆写入校验。",
    "deduped": "这条事实已有记录，本轮加固了旧记录。",
    "written": "这一轮有记忆写入。",
    "empty": "这一轮没有发现可写入的事实。",
    "unobserved": "今日没有这个范围内的写入记录。",
}
_RECALL_MESSAGES = {
    "active_match": "该范围最近的主动检索有匹配，依据是关键词或向量距离。结果同时带记忆原话及来源。",
    "automatic_match": "自动注入使用了词面匹配、项目权限和当前状态均通过的记录。",
    "filtered": "候选因相关性、项目权限、版本状态或注入预算被过滤。",
    "empty": "当前范围没有足够相关的记忆。",
    "failed": "召回执行失败，可稍后重试。",
    "unobserved": "今日没有这个范围内的召回记录。",
}


def trace_recall(
    data_dir: Path, project_id: str | None, *, match_count: int = 0, chars: int = 0,
    channel: str, host: str | None = None, session_id: str | None = None,
    filtered: bool = False, failed: bool = False,
) -> None:
    """记召回的结果状态，不存查询文字、原话、错误详情或候选 ID。"""
    from March7 import ledger

    ledger.append_decision({
        "project_id": project_id,
        "host": host, "conversation_id": session_id,
        "channel": channel, "match_count": match_count, "injection_chars": chars,
        "filtered": filtered, "failed": failed,
    }, data_dir, prefix=RECALL_TRACE_PREFIX)


def _number(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _write_state(row: dict[str, Any]) -> str:
    from March7 import privacy

    event = row.get("event")
    if event == privacy.EVENT_PROJECT_SESSION_CHANGED or (event == "codex_cwd_drift" and row.get("project_blocked") is True):
        return "project_changed"
    for field, state in (
        ("failed", "failed"), ("capped", "capped"), ("project_rejected", "project_identity_required"),
        ("written", "written"), ("l0_updated", "written"), ("rejected", "rejected"), ("deduped", "deduped"),
    ):
        value = row.get(field)
        if value is True or _number(value):
            return state
    return "empty"


def library_identifier(data_dir: Path) -> str:
    """提供可在宿主和面板之间核对的库编号。"""
    from March7 import privacy

    directory = privacy.normalize_path(str(data_dir.resolve()))
    return hashlib.blake2s(directory.encode("utf-8"), digest_size=6).hexdigest()


def runtime_snapshot(
    store: MemoryStore, project_id: str | None, *, host: str | None = None, session_id: str | None = None,
) -> dict[str, Any]:
    """只投影固定状态、统计、库和项目假名。会话诊断按宿主、会话和项目核对。"""
    from March7 import config, ledger, privacy
    from March7.injection import MAX_INJECTED_CHARS, MAX_WORKING_BLOCK_CHARS

    public_id = project_id if isinstance(project_id, str) and _PROJECT_TAG.fullmatch(project_id) else None
    library_id = library_identifier(store.data_dir)
    def same_session(row: Any) -> bool:
        return (
            isinstance(row, dict) and (not host or row.get("host") == host)
            and (not session_id or row.get("conversation_id") == session_id)
        )

    latest_write, latest_recall, chars = "unobserved", "unobserved", None
    for row in filter(same_session, reversed(ledger.read_day(store.data_dir))):
        event = row.get("event")
        if event is None and row.get("project_id") == public_id:
            latest_write = _write_state(row)
            break
        if session_id and event in (privacy.EVENT_PROJECT_SESSION_CHANGED, "codex_cwd_drift") and _write_state(row) == "project_changed":
            latest_write = "project_changed"
            break

    traces = ledger.read_decisions(store.data_dir, prefix=RECALL_TRACE_PREFIX)
    for row in reversed(traces):
        if not isinstance(row, dict) or row.get("project_id") != public_id:
            continue
        active = row.get("channel") == "active"
        # 无会话标识的 MCP 只提供同项目检索的摘要，不能说是本会话的结果。
        if not same_session(row) and not (active and row.get("conversation_id") is None and row.get("host") is None):
            continue
        if chars is None and not active:
            chars = _number(row.get("injection_chars"))
        if latest_recall == "unobserved":
            if row.get("failed") is True:
                latest_recall = "failed"
            elif _number(row.get("match_count")):
                latest_recall = "active_match" if active else "automatic_match"
            else:
                latest_recall = "filtered" if row.get("filtered") is True else "empty"
        if chars is not None:
            break
    return {
        "library_id": library_id, "project_id": public_id,
        "latest_write": latest_write, "latest_recall": latest_recall,
        "today_writes": store.count_since(config.start_of_local_day()), "daily_write_cap": config.daily_write_cap(),
        "injection_chars": chars or 0, "long_term_budget_chars": MAX_INJECTED_CHARS,
        "working_budget_chars": MAX_WORKING_BLOCK_CHARS,
    }


def explain_runtime(report: dict[str, Any], *, include: str = "all") -> str:
    """用固定中文说明诊断原因，调用方不直接渲染账本字段。"""
    details = [
        f"当前记忆库 {report['library_id']}。项目 {report['project_id'] or '未确认'}。",
    ]
    if include in ("all", "write"):
        details.append(_WRITE_MESSAGES[report["latest_write"]])
        cap = report["daily_write_cap"] or "不限"
        details.append(f"今日写入 {report['today_writes']} 条，上限 {cap}。")
    if include in ("all", "recall"):
        details.append(_RECALL_MESSAGES[report["latest_recall"]])
        details.append(
            f"最近自动注入 {report['injection_chars']} 字。长期记忆预算 {report['long_term_budget_chars']} 字，"
            f"工作记忆预算 {report['working_budget_chars']} 字。"
        )
    return " ".join(details)


def visible_receipt(built: dict[str, Any] | None, project_id: str | None) -> dict[str, Any] | None:
    """凭条只展示当前项目可见的结构化字段，不回传账本中的自由文本。"""
    from March7.injection import CATEGORY_LABELS, RECALL_OMIT_REASONS, WORKING_KIND_LABELS
    from March7.receipt import RECEIPT_INJECT_REASONS

    if not isinstance(built, dict) or built.get("project_id") not in (None, project_id):
        return None

    def entries(key: str) -> list[dict[str, Any]]:
        items = built.get(key)
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def safe_id(item: dict[str, Any]) -> bool:
        value = item.get("id")
        return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9-]{1,36}", value) is not None

    return {
        "injected": [
            {"id": item["id"], "scope": item["scope"], "category": item.get("category"), "reason": item["reason"]}
            for item in entries("injected")
            if safe_id(item) and item.get("scope") in ("global", f"project:{project_id}")
            and item.get("category") in (*CATEGORY_LABELS, None) and item.get("reason") in RECEIPT_INJECT_REASONS
        ],
        "omitted": [
            {"id": item["id"], "reason": item["reason"]} for item in entries("omitted")
            if safe_id(item) and item.get("reason") in RECALL_OMIT_REASONS
        ],
        "working": [
            {"id": item["id"], "kind": item["kind"]} for item in entries("working")
            if project_id and built.get("project_id") == project_id and safe_id(item)
            and item.get("kind") in tuple(WORKING_KIND_LABELS)
        ],
        "chars": _number(built.get("chars")), "prompt_chars": _number(built.get("prompt_chars")),
        "project_id": project_id if built.get("project_id") == project_id and project_id and _PROJECT_TAG.fullmatch(project_id) else None,
    }

DIAGNOSTIC_EVENTS = (
    EVENT_DUPLICATE_CWD_HASH,
    EVENT_NORM_MISMATCH,
    EVENT_PROJECT_SPLIT_SUSPECTED,
)
"""三个事件的**全集**（`doctor` 按它 grep；改名等于统计失效）。"""

ALLOWED_FIELDS = frozenset(
    {
        "project_id",
        "host",
        "conflict_count",
        "sibling_count",
        "norm_version",
        "recorded_norm_version",
    }
)
"""允许出现在诊断事件里的字段（**白名单**）。

刻意不含 `cwd_hash` / `root_hash` 这类"能还原到路径"的键：
诊断事件的用途是进日志，而日志里的 hash 前缀等于把脱敏开口挪到了日志面。
`test_allowed_fields_exclude_anything_path_like` 静态钉住这条。
"""

SPLIT_WINDOW_SECONDS = 24 * 3600
"""`project_split_suspected` 的时间窗（24h，蓝图 T20d）。"""

_ENV_DATA_DIR = "MARCH7_HOME"
_DEFAULT_DATA_DIR_NAME = ".March7"
"""与 `config.ENV_DATA_DIR` / `config.DEFAULT_DATA_DIR` 同值（刻意复制，不 import：
钩子进程一旦 import 引擎就会拉起 pydantic，把「进程启动」本身变成被测量对象）。"""


def build_event(event: str, *, project_id: str, **fields: Any) -> dict[str, Any]:
    """组一条诊断事件（**唯一**构造入口，字段白名单在这里生效）。

    Args:
        event: 三个事件名之一。
        project_id: 项目假名（`p-<6 hex>`）——必填，否则事件无法归因。
        **fields: 其余字段，必须在 `ALLOWED_FIELDS` 内。

    Returns:
        `{"event": ..., "project_id": ..., "at": <unix>, **fields}`。

    Raises:
        ValueError: 事件名不在闭集内 / `project_id` 为空 / 出现了白名单外的字段。
            刻意**响亮失败**：静默丢掉一个字段，等于"诊断写了一半"而看日志的人不知道。
    """
    if event not in DIAGNOSTIC_EVENTS:
        raise ValueError(
            f"未知诊断事件 {event!r}——只允许 {list(DIAGNOSTIC_EVENTS)}（新增事件先更新 doctor）"
        )
    if not project_id:
        raise ValueError("诊断事件必须带非空 project_id（否则无法归因到项目）")
    illegal = set(fields) - ALLOWED_FIELDS
    if illegal:
        raise ValueError(
            f"诊断事件字段 {sorted(illegal)} 不在白名单内（{sorted(ALLOWED_FIELDS)}）："
            "诊断只允许写别名 + 计数，任何路径/hash 字段都不许进来"
        )
    return {"event": event, "project_id": project_id, "at": int(time.time()), **fields}


def _data_dir() -> Path:
    """诊断日志目录：`MARCH7_HOME` → `~/.March7`（与钩子同一口径，但不 import 钩子）。"""
    raw = (os.environ.get(_ENV_DATA_DIR) or "").strip()
    return Path(raw) if raw else Path.home() / _DEFAULT_DATA_DIR_NAME


def emit(event: dict[str, Any]) -> None:
    """把诊断事件写进钩子日志（`<data_dir>/logs/hooks-YYYYMMDD.jsonl`）。

    ⚠️ **失败绝不抛**：诊断是观测手段，不能因为"日志写不进去"而影响注入。
    写失败只记 WARNING（在宿主侧看不到，但开发者 `doctor` 时能看到）。
    """
    try:
        log_dir = _data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"hooks-{time.strftime('%Y%m%d')}.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 —— 观测失败不得影响主流程
        logger.warning("诊断事件写入失败（不影响注入）：%s", exc)


def detect_duplicate_cwd_hash(
    store: Any, *, host: str, cwd_hash: str, project_id: str
) -> dict[str, Any] | None:
    """① 同一 `cwd_hash` 在两宿主映射到不同 `project_id`？

    为什么在这里"预检"而不是靠 `record_host_cwd` 抛异常：那个异常会沿调用栈
    往上冒，而钩子的红线是**恒 exit 0**。所以生产路径的写法是
    「先预检 → 记事件 → 再决定是否写入」，异常只用于单测与脚本。

    Returns:
        事件字典，或 `None`（没有冲突）。**不写库、不落日志**——`emit` 是调用方的事。
    """
    try:
        for other_host in ("workbuddy", "dsh"):
            if other_host == host:
                continue
            row = store.get_host_registry(other_host)
            if not row:
                continue
            mapping = row.get("cwd_hashes_json") or {}
            found = mapping.get(cwd_hash)
            if found is not None and found != project_id:
                return build_event(
                    EVENT_DUPLICATE_CWD_HASH,
                    project_id=project_id,
                    host=host,
                    conflict_count=1,
                )
    except Exception as exc:  # noqa: BLE001 —— 诊断失败不得影响写入
        logger.warning("duplicate_cwd_hash 预检失败（按无冲突处理）：%s", exc)
        return None
    return None


def detect_norm_mismatch(
    store: Any,
    *,
    root_hash: str,
    project_id: str,
    norm_version: int,
) -> dict[str, Any] | None:
    """② 同一个 `root_hash` 但两侧归一化规则版本不同？

    `norm_version` 是**检测位，不是迁移方案**（蓝图 T20d）：查到版本不同就记事件、
    并**登记为独立项目**，绝不自动合并——把两条记忆链嫁接到一起，错了拆不开。

    版本号从两侧各取一份，**都必须来自真实数据源**：
    - `norm_version`：当前代码的 `privacy.NORM_VERSION`（调用方传）；
    - `recorded_norm_version`：库里 `projects.norm_version`（本函数自己读）。

    ⚠️ 这里刻意**由函数自己读库**而不是让调用方两边都传进来：
    首轮独立验收指出「没有任何数据源能产出 `norm_version`」，
    根因就是"两个值都由调用方给"⇒ 调用方随便传两个相同的数就能让它永不触发。
    读库这一半钉死了，报不报就取决于"库里到底记了什么"。

    Returns:
        事件字典，或 `None`（未登记 / 版本一致）。**不写库、不落日志**。
    """
    try:
        recorded = store.get_project_norm_version(root_hash)
    except Exception as exc:  # noqa: BLE001 —— 诊断失败不得影响写入
        logger.warning("norm_mismatch 预检读库失败（按无风险处理）：%s", exc)
        return None
    if recorded is None or int(recorded) == int(norm_version):
        return None
    return build_event(
        EVENT_NORM_MISMATCH,
        project_id=project_id,
        norm_version=int(norm_version),
        recorded_norm_version=int(recorded),
    )


def record_basename(store: Any, *, basename: str, project_id: str, now: int | None = None) -> None:
    """登记 `(basename, project_id)`（诊断用的最小事实：只有名字与假名）。

    单独包一层而不是让调用方直接调 `store.record_basename`：
    诊断模块负责"失败不影响主流程"这条纪律，store 负责数据。
    """
    try:
        store.record_basename(basename, project_id, at=now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("basename 登记失败（不影响注入）：%s", exc)


def detect_project_split(
    store: Any, *, basename: str, project_id: str, now: int | None = None
) -> dict[str, Any] | None:
    """③ 同一目录名在 24h 内出现了**第二个** project_id？

    判据可成立（蓝图 T20d）：同一 `basename` + 同一 `norm_version` 在 24h 内出现
    第二个 `project_id`。这里用 `first_seen_at >= now - 24h` 近似"24h 内"。

    ⚠️ **只报警、不自动合并**：合并会改写 `Memory.scope`，属用户确认范围（T25）。

    Returns:
        事件字典（含 `sibling_count`），或 `None`。**不登记自己**——登记与检测分开，
        因为"先登记再检测"会让新项目永远看不到自己的兄弟（它已把自己算进去了）。
    """
    stamp = int(now if now is not None else time.time())
    try:
        siblings = store.sibling_basenames(
            basename, project_id, since=stamp - SPLIT_WINDOW_SECONDS
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("project_split 预检失败（按无风险处理）：%s", exc)
        return None
    if not siblings:
        return None
    return build_event(
        EVENT_PROJECT_SPLIT_SUSPECTED,
        project_id=project_id,
        sibling_count=len(siblings),
    )


__all__ = [
    "ALLOWED_FIELDS",
    "DIAGNOSTIC_EVENTS",
    "EVENT_DUPLICATE_CWD_HASH",
    "EVENT_NORM_MISMATCH",
    "EVENT_PROJECT_SPLIT_SUSPECTED",
    "SPLIT_WINDOW_SECONDS",
    "build_event",
    "detect_duplicate_cwd_hash",
    "detect_norm_mismatch",
    "detect_project_split",
    "emit",
    "record_basename",
]
