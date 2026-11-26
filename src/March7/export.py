"""export.py — 透明化导出（内部代号：相册）
见 docs/NAMING.md —— 相机里的照片，可以拿出来给人看 = 透明化导出。
March7_export 的引擎侧实现：全部记忆导出成人类可读 markdown（或 json）。

**导出必须能查到历史**（CONVENTIONS §9.2：记忆永不物理删除）⇒ 导出**包含**
superseded / merged 的旧记忆，并在文件里标出状态——「这条为什么失效了」要看得见。

⚠️ 本模块**零 MCP 依赖**（CONVENTIONS §2 红线）；SQL 只经 `store.py`（唯一 SQL 出口）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from March7 import privacy
from March7.schemas import ExportFormat, L0Profile, Memory
from March7.store import MemoryStore

logger = logging.getLogger(__name__)

# 导出 markdown 文件头（docs/NAMING.md §4.2 文案落点）
EXPORT_HEADER_TEMPLATE = "> March7 的全部记忆 · 导出即显影 · {exported_at}"

# 导出文件名前缀（docs/NAMING.md §2：March7-memories-YYYY-MM-DD.md）
EXPORT_FILENAME_PREFIX = "March7-memories"

# ---------------------------------------------------------------------------
# 导出白名单（S1 / `ADR-0037`）
# ---------------------------------------------------------------------------
# 为什么改成白名单而不是「`model_dump()` 全给」：
# `model_dump()` 是**自动跟随 schema** 的——将来往 `Memory` 里加一个内部字段
# （调试用的 `embedding`、迁移用的 `_raw`），它会**立刻出现在用户的导出文件里**，
# 而且没有一行代码需要改，code review 也看不出来。导出是"给人看全部记忆"的出口，
# 也是最容易泄露内部字段的出口 ⇒ 必须显式列出"给什么"，新增字段默认**不导出**。

EXPORT_FIELDS: dict[str, tuple[str, ...]] = {
    # L0 画像：8 个字段 == 改造前 `L0Profile.model_dump()` 的字段集（A4 逐一相同）。
    # 前 6 个是"给人看的身份信息"，后 2 个（isPinned / updatedAt）是画像自己的元数据——
    # 它们本来就在导出里，白名单不能顺手删（删了就是静默改了导出契约）。
    "profile": (
        "nickname",
        "preferredName",
        "occupation",
        "longTermInterests",
        "language",
        "permanentNote",
        "isPinned",
        "updatedAt",
    ),
    # L2 记忆：初始集合 == 改造前的 `Memory.model_dump()` 字段集（A4 逐一相同）
    "memory": (
        "id",
        "content",
        "source_quote",
        "trigger_text",
        "slug",
        "scope",
        "host",
        "source_conversation_id",
        "created_at",
        "last_accessed_at",
        "access_count",
        "weight",
        "is_pinned",
        "status",
        "conflict_with",
        "evidence_ids",
        "keywords",
    ),
    # 冲突：初始集合 == 改造前的 `Conflict.model_dump()` 字段集
    "conflict": (
        "id",
        "created_at",
        "status",
        "source_id",
        "target_id",
        "reason",
        "confidence",
        "detector",
        "resolution_type",
        "should_ask_user",
    ),
}
"""`{模型类别: 允许导出的字段}`——导出**只**经过这张表。

⚠️ 加字段时先问一句：这条信息是"用户要看的记忆"，还是"引擎自己要用的实现细节"？
后者留在库里，不进导出。
"""


def _dump(model: object, kind: str) -> dict[str, object]:
    """按白名单 dump（**唯一**的序列化入口，markdown 与 json 共用同一份字段定义）。"""
    return model.model_dump(mode="json", include=list(EXPORT_FIELDS[kind]))  # type: ignore[attr-defined]


def _view(text: str | None, *, verbatim: bool) -> str | None:
    """导出文本的**视图**：默认脱敏（模型会读到这份文件），`verbatim=True` 给原文。

    D6 口径：模型看到的是脱敏后的原话，用户在自己的导出里能看到完整原文。
    `verbatim` 只能由用户侧显式打开（CLI 的逃生舱开关）——默认值必须是安全的那一侧。
    """
    if text is None or verbatim:
        return text
    return privacy.redact_view(text).text

# L0 画像在导出文件里的字段说明（人类可读，不用 camelCase 裸 key）
_PROFILE_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("nickname", "昵称"),
    ("preferredName", "称呼"),
    ("occupation", "职业"),
    ("longTermInterests", "长期兴趣"),
    ("language", "语言"),
    ("permanentNote", "固定备注"),
)


def default_filename(fmt: ExportFormat) -> str:
    """默认导出文件名：March7-memories-YYYY-MM-DD.md / .json（docs/NAMING.md §2）。"""
    suffix = "json" if fmt is ExportFormat.JSON else "md"
    return f"{EXPORT_FILENAME_PREFIX}-{datetime.now().strftime('%Y-%m-%d')}.{suffix}"


def _now_text() -> str:
    """导出时间戳（本地时区，秒级）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_time(unix_seconds: int | None) -> str:
    """Unix 秒 → 可读时间；None 或非法值返回「—」（不抛错，导出不该因一条脏数据失败）。"""
    if unix_seconds is None:
        return "—"
    try:
        return datetime.fromtimestamp(int(unix_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _collect_memories(store: MemoryStore) -> list[Memory]:
    """取**全部**记忆（含 superseded / merged 的历史），按写入时间升序。

    条数用 `store.stats().memory_count`（memories 表全部行数）而不是写死上限——
    导出的完整性不能靠一个魔数保证。排序取「旧的在前」，导出文件读起来是时间线。
    """
    total = store.stats().memory_count
    memories = store.list_recent(scope=None, since=None, limit=max(total, 1))
    return sorted(memories, key=lambda m: (m.created_at, m.id))


def export_markdown(
    store: MemoryStore,
    profile: L0Profile,
    output_dir: Path | None = None,
    *,
    verbatim: bool = False,
) -> Path:
    """导出为人类可读 markdown：文件头（导出即显影）+ L0 画像 + 全部记忆
    （含 superseded/merged 的历史——失效不删除，导出可查）。

    Args:
        store: L2 库。
        profile: 当前画像。
        output_dir: 输出目录；None = 当前工作目录。
        verbatim: True = 保留原话里的绝对路径（**只给用户本人的本地副本**）。
            默认 False：这份文件会被 `March7_export(inline=True)` 读进模型上下文，
            所以默认必须是脱敏视图（D6）。

    Returns:
        导出文件的完整路径。
    """
    exported_at = _now_text()
    memories = _collect_memories(store)
    target_dir = Path(output_dir) if output_dir is not None else Path.cwd()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / default_filename(ExportFormat.MD)

    lines: list[str] = [
        EXPORT_HEADER_TEMPLATE.format(exported_at=exported_at),
        "",
        "# March7 记忆导出",
        "",
        f"- 导出时间：{exported_at}",
        f"- 记忆条数：{len(memories)}",
        "- 失效不删除：下面包含已被冲突裁决标记为 superseded / merged 的历史记忆，"
        "它们仍然可查。",
        "",
        "## L0 画像（六相冰）",
        "",
    ]
    if any(getattr(profile, key, "") for key in EXPORT_FIELDS["profile"]):
        lines.append("| 字段 | 值 |")
        lines.append("| --- | --- |")
        for key, label in _PROFILE_FIELD_LABELS:
            lines.append(f"| {label}（{key}） | {getattr(profile, key, '')} |")
    else:
        lines.append("（画像为空——还没有任何经用户原话背书的身份信息）")
    lines.append("")

    lines.append(f"## L2 记忆（{len(memories)} 条）")
    lines.append("")
    if not memories:
        lines.append("（还没有记忆）")
        lines.append("")
    for index, memory in enumerate(memories, start=1):
        lines.append(f"### {index}. {_view(memory.content, verbatim=verbatim)}")
        lines.append("")
        lines.append(f"- 时间：{_format_time(memory.created_at)}")
        lines.append(f"- 状态：{memory.status.value}")
        if "host" in EXPORT_FIELDS["memory"] and memory.host:
            lines.append(f"- 宿主：{memory.host}")
        if "source_conversation_id" in EXPORT_FIELDS["memory"] and memory.source_conversation_id:
            lines.append(f"- 会话：{memory.source_conversation_id}")
        if "access_count" in EXPORT_FIELDS["memory"] and memory.access_count:
            lines.append(f"- 被召回次数：{memory.access_count}")
        if "source_quote" in EXPORT_FIELDS["memory"] and memory.source_quote:
            lines.append(f"- 原话：{_view(memory.source_quote, verbatim=verbatim)}")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    logger.info("导出完成（markdown）：%s（%d 条记忆）", path, len(memories))
    return path


def export_json(
    store: MemoryStore,
    profile: L0Profile,
    output_dir: Path | None = None,
    *,
    verbatim: bool = False,
) -> Path:
    """导出为 JSON（机器可读备份/迁移格式；结构 = profile + memories + conflicts）。

    Args:
        store: L2 库。
        profile: 当前画像。
        output_dir: 输出目录；None = 当前工作目录。
        verbatim: True = 保留原话里的绝对路径（见 `export_markdown` 的同名参数）。

    Returns:
        导出文件的完整路径。
    """
    exported_at = _now_text()
    memories = _collect_memories(store)
    conflicts = store.list_conflicts(status=None)
    target_dir = Path(output_dir) if output_dir is not None else Path.cwd()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / default_filename(ExportFormat.JSON)

    payload = {
        "exported_at": exported_at,
        "profile": _dump(profile, "profile"),
        "memories": [_dump_memory(memory, verbatim=verbatim) for memory in memories],
        "conflicts": [_dump(conflict, "conflict") for conflict in conflicts],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info(
        "导出完成（json）：%s（%d 条记忆 / %d 条冲突）", path, len(memories), len(conflicts)
    )
    return path


def _dump_memory(memory: Memory, *, verbatim: bool) -> dict[str, object]:
    """记忆的白名单 dump + 文本字段视图（`content` / `source_quote` / `trigger_text`）。"""
    data = _dump(memory, "memory")
    for key in ("content", "source_quote", "trigger_text"):
        if key in data:
            data[key] = _view(data[key], verbatim=verbatim)  # type: ignore[arg-type]
    return data


__all__ = [
    "EXPORT_HEADER_TEMPLATE",
    "default_filename",
    "export_json",
    "export_markdown",
]
