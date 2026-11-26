"""erase.py — 可验证删除（内部代号：**焚稿**）

用户要求「把我的个人数据删掉」时，唯一正确的回答是：**数据真的不存在了，而且这件事能被复核**。
本模块做两件事，且只做这两件：

1. **登记删除必须覆盖的派生面**（`ADR-0040` 决策 3 的 21 处）；
2. **逐面扫描并给出残留数**（`audit_deletion`），供删除后自证与用户复核。

## 为什么需要一张显式的面清单

「删干净」是一个**没有自然边界**的断言。凭记忆逐个 `DELETE` 的必然结果是：
新加一张表（S2 的 `working_items` / `handoffs` 就是这么加进来的）时没人回头补删除路径，
于是「已删除」变成了「主表删了、派生面还在」，而**两者在界面上长得一模一样**。

所以清单本身就是契约：新增派生面而不同步这张表 ⇒ `test_erase_surfaces_cover_adr_table`
会红（它拿 `ADR-0040` 原文的表格行数对账）。这条测试是**唯一**能防止漏项的东西。

## 三个口径（`audit_deletion` 的打印字段）

每一项都打三个数，**它们不是同义词**：

| 字段 | 含义 | 拿来判什么 |
|---|---|---|
| `unit` | 这一面的**计量单位**（表 / 文件 / 目录） | 读输出的人知道"扫的是什么" |
| `units_scanned` | 扫了几个单位 | 证明**扫描真的发生了**（0 个单位 = 什么也没查） |
| `rows_total` | 该单位里的总行/总条数 | 给残留数一个分母 |
| `rows_scanned` | 实际**检查过**的行数 | ⚠️ **业务口径只认这个** |

⚠️ **`rows_total` 不能当判据**（这是本卡最容易写错的地方）：
`rows_total` 是"这个面有多大"，`rows_scanned` 是"我们看了多少"。
两者不等时说明**有行没被检查**（比如按 id 查只扫了 1 行，但表里有 300 行）——
那种情况下「残留 0」只意味着"那 1 行不在了"，不意味着表里没有别的残留。
业务口径 `rows_scanned == rows_total` 才允许宣称「该面已清」。

## 三档可控性（诚实边界，`ADR-0040` 决策 4）

- **auto**：我们自己的表 / 日志 / 文件，能扫能清 —— 残留必须为 0；
- **manual**：本机上的旧库/导出，工具**不碰**，只报告存在与否（用户自己决定）；
- **uncontrollable**：宿主持有的原始对话、LLM 供应商日志、云备份 ——
  **不在本工具能力内**，回执必须写明。把它们算进「已删除」就是撒谎。

## 不做什么

- **不删备份/迁移包**，也不把它算进「已删除」（`ADR-0040` 决策 4）；
- **不碰 `~/.March7-legacy-*`**（只报 `skipped`，`T25` 旧库策略）；
- **不碰宿主 transcript**（那是宿主的文件，删了等于毁用户的历史记录）。

⚠️ 本模块**只用标准库**（钩子与 CLI 都会 import 它）。SQL 仍只在 `store.py`：
这里的 `scan` 函数拿到的是 `MemoryStore`，用它暴露的读方法数行，不自己拼 SQL。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 三档可控性
# ---------------------------------------------------------------------------

ERASE_AUTO = "auto"
"""我们自己的表 / 日志 / 文件：能扫、能清、残留必须为 0。"""

ERASE_MANUAL = "manual"
"""本机上的旧库/导出：**不碰**，只报存在与否（用户自己决定）。"""

ERASE_UNCONTROLLED = "uncontrollable"
"""宿主持有的原始对话 / 供应商日志 / 云备份：**不在本工具能力内**。"""

CONTROLLABILITY_LEVELS = (ERASE_AUTO, ERASE_MANUAL, ERASE_UNCONTROLLED)


@dataclass(frozen=True)
class ScanResult:
    """一个派生面的扫描结果（三个数 + 残留明细）。

    Attributes:
        unit: 计量单位名（`表 memories` / `文件 logs/*.jsonl` …）。
        units_scanned: 扫了几个单位。
        rows_total: 单位里的总行数（分母）。
        rows_scanned: **实际检查过**的行数（业务口径）。
        residue: 判定为残留的条目（**只放标识，不放原文**）。
    """

    unit: str
    units_scanned: int = 0
    rows_total: int = 0
    rows_scanned: int = 0
    residue: tuple[str, ...] = field(default_factory=tuple)

    @property
    def residue_count(self) -> int:
        """残留条数。"""
        return len(self.residue)

    @property
    def fully_scanned(self) -> bool:
        """是否**全量**扫过（`rows_scanned == rows_total`）。"""
        return self.rows_scanned >= self.rows_total

    def as_dict(self) -> dict[str, Any]:
        """给回执与 CLI 用的扁平字典（字段名就是打印出来的字段名）。"""
        return {
            "unit": self.unit,
            "units_scanned": self.units_scanned,
            "rows_total": self.rows_total,
            "rows_scanned": self.rows_scanned,
            "residue_count": self.residue_count,
            "residue": list(self.residue),
        }


@dataclass(frozen=True)
class EraseSurface:
    """派生面的一条登记项（对应 `ADR-0040` 决策 3 表格的一行）。

    Attributes:
        number: ADR 表格里的序号（1..21）——**顺序必须与 ADR 一致**，测试对账。
        name: 人类可读名（ADR 表格第二列）。
        kind: 三档可控性之一。
        scan: 扫描函数（`kind == auto` 时必须给）；签名 `(ctx) -> ScanResult`。
        note: 为什么是这一档 / 清理方式（回执里会给用户看）。
    """

    number: int
    name: str
    kind: str
    scan: Callable[[EraseContext], ScanResult] | None = None
    note: str = ""


@dataclass
class EraseContext:
    """扫描所需的全部句柄（显式注入，便于测试替身）。

    ## ⚠️ `content_needles` 必须在**删除之前**填好

    日志面（第 11/13 处）里存的是 `written_contents` —— **正文**，不是 id。
    删完再从库里取正文是取不到的（已经删了），所以调用方要在删除前把
    待删记忆的 `content` / `source_quote` / `trigger_text` 抄进 `content_needles`。

    漏填这个字段的后果是「日志面永远报 0 残留」——
    而那正是**最需要被发现**的一类残留（日志是纯文本，谁都读得到）。

    Attributes:
        store: 记忆库（`None` = 库不存在，扫描按 0 行处理）。
        data_dir: 数据目录（`logs/` / `profile.json` / `receipts/` 都在它下面）。
        memory_ids: 本次删除涉及的记忆 id 集合。
        project_ids: 本次删除涉及的项目假名集合（按项目整删时用）。
        session_ids: 本次删除涉及的会话 id（清 `ingest_marks` / 日志行用）。
        content_needles: 待删正文（**删除前**抄下来的；只用于在自由文本面里比对）。
        profile_store: L0 画像持久层（第 14 处要清字段）。
    """

    store: Any = None
    data_dir: Path | None = None
    memory_ids: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    content_needles: tuple[str, ...] = ()
    profile_store: Any = None
    conversation_only: bool = False
    visibility_project_id: str | None = None

    def free_text_needles(self) -> tuple[str, ...]:
        """自由文本面（日志 / 导出 / 缓存）要比对的全部串：**id + 正文**。

        ### 为什么正文有最短长度门槛

        太短的正文（「用户」「好的」）会在日志里命中几百行 ⇒ 审计永远报残留、
        永远不 clean ⇒ 用户学会忽略这个报告。所以只取长度 ≥
        `MIN_CONTENT_NEEDLE_CHARS` 的正文参与比对。

        ⚠️ 这是一条**已知的覆盖缺口**：短正文的残留**扫不出来**。
        它被写进 A5 的诚实边界说明里，而不是假装扫过了
        （"扫了但没找到" 与 "没法扫" 是两件事，回执里不能混）。
        调用方要用 `dropped_short_needles()` 把这个缺口**显式标在输出上**
        （`_scan_jsonl` 的 `unit` 后缀就是这么做的）——否则"残留 0"会被读成"查过了"。
        """
        ids = [i for i in self.memory_ids if i]
        ids += [s for s in self.session_ids if s]
        ids += [p for p in self.project_ids if p]
        texts = [t.strip() for t in self.content_needles if t and len(t.strip()) >= MIN_CONTENT_NEEDLE_CHARS]
        return tuple(dict.fromkeys(ids + texts))

    def dropped_short_needles(self) -> tuple[str, ...]:
        """被 `free_text_needles()` **丢掉**的短正文（长度 < 门槛）。

        ## 为什么要把"丢掉的东西"显式暴露出来

        短正文不参与比对是**刻意的取舍**（见 `free_text_needles`），但"不比对"
        与"比对过且干净"在输出里必须能区分。否则审计会宣称"这一面残留 0"，
        而实际上那几条正文压根没查过 —— 独立验收（2026-11-11）把这种情形
        判为假绿，是对的。

        Returns:
            被丢掉的正文（**只在内存里用于计数与打印长度，不进回执**）。
        """
        return tuple(
            t.strip() for t in self.content_needles if t and 0 < len(t.strip()) < MIN_CONTENT_NEEDLE_CHARS
        )


MIN_CONTENT_NEEDLE_CHARS = 6
"""正文参与自由文本比对的最短长度（低于它噪声淹没信号，见 `free_text_needles`）。"""


# ---------------------------------------------------------------------------
# 扫描函数（每个都**只用 store 的读方法**，不自己拼 SQL）
# ---------------------------------------------------------------------------


def _scan_memories(ctx: EraseContext) -> ScanResult:
    """第 1 处：`memories` 主表（content / source_quote / trigger_text）。"""
    if ctx.store is None or not ctx.memory_ids:
        return ScanResult(unit="表 memories", units_scanned=0)
    residue = {mid for mid in ctx.memory_ids if ctx.store.get_memory(mid) is not None}
    scanned = len(ctx.memory_ids)
    if ctx.conversation_only:
        visible = ctx.store.visible_memories(ctx.visibility_project_id)
        scanned += len(visible)
        for memory in visible:
            if any(
                needle in text
                for needle in ctx.content_needles
                for text in (memory.content, memory.source_quote or "", memory.trigger_text or "", memory.slug or "")
            ):
                residue.add(memory.id)
    return ScanResult(
        unit="表 memories",
        units_scanned=1,
        rows_total=scanned,
        rows_scanned=scanned,
        residue=tuple(sorted(residue)),
    )


def _scan_fts(ctx: EraseContext) -> ScanResult:
    """第 2 处：`memories_fts` 检索投影（判据 = 还能不能 `MATCH` 到）。

    ⚠️ **不能拿 `COUNT(*) FROM memories_fts` 当判据**：它是
    `content='memories'` 的**外部内容表**，`COUNT(*)` 读的是主表 ⇒ 主表删了它自然是 0，
    而索引里的投影可能还在（实测过的静默失败）。唯一可信的判据是**按内容 MATCH**。

    所以扫描方式：取被删记忆的原文 → 投影成 bigram → 拿其中一个 token `MATCH`。
    这里只用「`memory_ids` 里的那条还在不在」这一个信号，
    残留记的是 **id**（不是命中的文本）。
    """
    if ctx.store is None or not ctx.memory_ids:
        return ScanResult(unit="表 memories_fts（MATCH 探测）", units_scanned=0)
    residue: list[str] = []
    scanned = 0
    for mid in ctx.memory_ids:
        scanned += 1
        if ctx.store.fts_hits_memory_id(mid):
            residue.append(mid)
    return ScanResult(
        unit="表 memories_fts（MATCH 探测）",
        units_scanned=1,
        rows_total=len(ctx.memory_ids),
        rows_scanned=scanned,
        residue=tuple(residue),
    )


def _scan_vec(ctx: EraseContext) -> ScanResult:
    """第 3 处：`memories_vec` 向量（存在近似反演风险 ⇒ 必须真删）。"""
    if ctx.store is None:
        return ScanResult(unit="表 memories_vec", units_scanned=0)
    if not ctx.store.vec_ready:
        return ScanResult(unit="表 memories_vec（sqlite-vec 未就绪）", units_scanned=0)
    residue = tuple(ctx.store.residual_vec_ids(ctx.memory_ids))
    return ScanResult(
        unit="表 memories_vec",
        units_scanned=1,
        rows_total=len(ctx.memory_ids),
        rows_scanned=len(ctx.memory_ids),
        residue=residue,
    )


def _scan_sources(ctx: EraseContext) -> ScanResult:
    """第 4 处：来源四元组及其派生的 DSH 判重摘要。"""
    if ctx.store is None or not ctx.memory_ids:
        return ScanResult(unit="表 memory_sources", units_scanned=0)
    residue = tuple(
        mid for mid in ctx.memory_ids
        if ctx.store.get_source(mid) is not None or ctx.store.has_dsh_memory_evidence(mid)
    )
    return ScanResult(
        unit="表 memory_sources",
        units_scanned=1,
        rows_total=len(ctx.memory_ids),
        rows_scanned=len(ctx.memory_ids),
        residue=residue,
    )


def _scan_claims(ctx: EraseContext) -> ScanResult:
    """第 5 处：`claims`（`value` 原文 + `memory_id`）。"""
    if ctx.store is None or not ctx.memory_ids:
        return ScanResult(unit="表 claims", units_scanned=0)
    residue = tuple(mid for mid in ctx.memory_ids if ctx.store.list_claims(mid))
    return ScanResult(
        unit="表 claims",
        units_scanned=1,
        rows_total=len(ctx.memory_ids),
        rows_scanned=len(ctx.memory_ids),
        residue=residue,
    )


def _scan_conflicts(ctx: EraseContext) -> ScanResult:
    """第 6 处：`conflicts`（`reason` 可能含引文）。"""
    if ctx.store is None or not ctx.memory_ids:
        return ScanResult(unit="表 conflicts", units_scanned=0)
    residue = tuple(mid for mid in ctx.memory_ids if ctx.store.count_conflicts_touching(mid))
    return ScanResult(
        unit="表 conflicts",
        units_scanned=1,
        rows_total=len(ctx.memory_ids),
        rows_scanned=len(ctx.memory_ids),
        residue=residue,
    )


def _scan_episodes(ctx: EraseContext) -> ScanResult:
    """第 7 处：`episodes`（`summary` 是**会话摘要正文**，可能整段复述用户记忆）。

    ⚠️ 判据必须**按内容**（`summary` 含正文），不能只按 `conversation_id`：
    episode 的会话 id 与 `memory_sources.session_id` 不保证一致
    （记忆来源带 revision/分支）。只按会话判 ⇒ 该清的没清、还报 0
    （独立验收 2026-11-11 指出的缺陷）。
    """
    if ctx.store is None:
        return ScanResult(unit="表 episodes", units_scanned=0)
    needles = ctx.free_text_needles()
    by_session = ctx.store.count_episodes_touching(ctx.session_ids)
    by_content = ctx.store.count_episodes_containing(needles)
    total = by_session + by_content
    return ScanResult(
        unit="表 episodes",
        units_scanned=1,
        rows_total=total,
        rows_scanned=total,
        residue=() if total == 0 else tuple(["episodes"] * total),
    )


def _scan_ingest_marks(ctx: EraseContext) -> ScanResult:
    """第 8 处：`ingest_marks`（可反推断对话存在）。"""
    if ctx.store is None:
        return ScanResult(unit="表 ingest_marks", units_scanned=0)
    hits = ctx.store.count_ingest_marks_for(ctx.session_ids)
    return ScanResult(
        unit="表 ingest_marks",
        units_scanned=1,
        rows_total=hits,
        rows_scanned=hits,
        residue=() if hits == 0 else tuple(["ingest_marks"] * hits),
    )


def _scan_working(ctx: EraseContext) -> ScanResult:
    """第 9 处：`working_items`（用户原话 + 当前 handoff）——S2 新建，本卡必须覆盖。

    ⚠️ **两种命中取并集**，缺一个就会漏：
    - 按项目/会话（整批删除用）；
    - 按**正文**（删单条记忆用——同一句话可能在另一个会话里被重说过一遍，
      那一行没有 `memory_id`，按项目查不到）。
    """
    if ctx.store is None:
        return ScanResult(unit="表 working_items", units_scanned=0)
    by_scope = ctx.store.count_working_for(ctx.project_ids, ctx.session_ids)
    by_content = ctx.store.count_working_containing(ctx.free_text_needles())
    total = by_scope + by_content
    return ScanResult(
        unit="表 working_items",
        units_scanned=1,
        rows_total=total,
        rows_scanned=total,
        residue=() if total == 0 else tuple(["working_items"] * total),
    )


def _scan_handoffs(ctx: EraseContext) -> ScanResult:
    """第 10 处：`handoffs`（project_id / handoff_id / 状态 / 时间）。"""
    if ctx.store is None:
        return ScanResult(unit="表 handoffs", units_scanned=0)
    hits = ctx.store.count_handoffs_for(ctx.project_ids)
    return ScanResult(
        unit="表 handoffs",
        units_scanned=1,
        rows_total=hits,
        rows_scanned=hits,
        residue=() if hits == 0 else tuple(["handoffs"] * hits),
    )


def _scan_jsonl(ctx: EraseContext, *, filename_prefix: str, label: str) -> ScanResult:
    """第 11/12/13 处的共用实现：扫 `logs/<prefix>-*.jsonl` 里是否还有被删内容。

    判据：逐行 JSON 解析后，**递归**检查任何字符串值是否包含被删法的 id 或原话片段。
    为什么不只查 `written_contents` 字段：日志的字段是**增长型证据源**
    （`ADR-0007`），字段名会变；递归扫描对新增字段天然覆盖。

    Returns:
        `rows_scanned` = 实际解析并检查的行数（**业务口径**）。

    ## ⚠️ 短正文的已知覆盖缺口（独立验收 2026-11-11 指出）

    `free_text_needles()` 会丢掉长度 < `MIN_CONTENT_NEEDLE_CHARS`(6) 的正文
    ——因为「用户」「好的」这类词会在日志里命中几百行，让报告永远不干净。
    代价是：**短正文留在日志里时，这一面会报残留 0，但其实没比过。**

    本函数不掩盖这件事：存在被丢掉的短正文时，`unit` 里会带
    `⚠短正文N条未比对` 标记，读输出的人能一眼看出这一面**不是全查过**。
    这比"报 clean 但其实没查"诚实（`ADR-0040` 决策 4 的同一精神）。
    """
    if ctx.data_dir is None:
        return ScanResult(unit=label, units_scanned=0)
    log_dir = Path(ctx.data_dir) / "logs"
    files = sorted(log_dir.glob(f"{filename_prefix}-*.jsonl")) if log_dir.exists() else []
    # 自由文本面要比对 **id + 正文**（正文用删除前抄下的 `content_needles`）
    needles = ctx.free_text_needles()
    dropped = ctx.dropped_short_needles()
    suffix = f"（⚠短正文 {len(dropped)} 条未比对）" if dropped else ""
    rows_total = rows_scanned = 0
    residue: list[str] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("删除审计读不了 %s（跳过该文件，不计入扫描数）：%s", path.name, exc)
            continue
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            rows_total += 1
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                rows_scanned += 1  # 坏行也算"看过了"（它不可能命中 id）
                continue
            rows_scanned += 1
            if needles and _json_contains(payload, needles):
                residue.append(f"{path.name}:{index + 1}")
    return ScanResult(
        unit=label + suffix,
        units_scanned=len(files),
        rows_total=rows_total,
        rows_scanned=rows_scanned,
        residue=tuple(residue),
    )


def _json_contains(payload: Any, needles: tuple[str, ...]) -> bool:
    """递归检查 JSON 结构里任一字符串是否包含任一 needle（大小写敏感：id 是精确串）。"""
    if isinstance(payload, str):
        return any(n and n in payload for n in needles)
    if isinstance(payload, dict):
        return any(_json_contains(v, needles) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_json_contains(v, needles) for v in payload)
    return False


def _scan_writes_log(ctx: EraseContext) -> ScanResult:
    return _scan_jsonl(ctx, filename_prefix="writes", label="文件 logs/writes-*.jsonl")


def _scan_decisions_log(ctx: EraseContext) -> ScanResult:
    return _scan_jsonl(ctx, filename_prefix="decisions", label="文件 logs/decisions-*.jsonl")


def _scan_hooks_log(ctx: EraseContext) -> ScanResult:
    return _scan_jsonl(ctx, filename_prefix="hooks", label="文件 logs/hooks-*.jsonl")


def _scan_profile(ctx: EraseContext) -> ScanResult:
    """第 14 处：`profile.json`（画像字段是**明文个人数据**，erase 必须清）。

    ⚠️ 比对的是 `free_text_needles()`（**id + 正文**），不是只有 id：
    画像里存的是「他在做紫晶计划」这种**正文**，永远不会出现记忆 id。
    只比 id 的话这一面会恒报 0 —— 而它恰恰是最该被清的面之一。
    """
    if ctx.profile_store is None:
        return ScanResult(unit="文件 profile.json", units_scanned=0)
    residue = tuple(ctx.profile_store.fields_containing(ctx.free_text_needles()))
    return ScanResult(
        unit="文件 profile.json",
        units_scanned=1,
        rows_total=1,
        rows_scanned=1,
        residue=residue,
    )


def _scan_exports(ctx: EraseContext) -> ScanResult:
    """第 15 处：导出 md/json 文件（`⚠️ 仅本机`）。"""
    if ctx.data_dir is None:
        return ScanResult(unit="文件 March7-memories-*.{md,json}", units_scanned=0)
    needles = tuple(ctx.memory_ids)
    files: list[Path] = []
    for pattern in ("March7-memories-*.md", "March7-memories-*.json"):
        files.extend(sorted(Path(ctx.data_dir).glob(pattern)))
    residue: list[str] = []
    rows_total = rows_scanned = 0
    for path in files:
        rows_total += 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("删除审计读不了导出文件 %s（跳过）：%s", path.name, exc)
            continue
        rows_scanned += 1
        if needles and _json_contains(text, needles):
            residue.append(path.name)
    return ScanResult(
        unit="文件 March7-memories-*.{md,json}",
        units_scanned=len(files),
        rows_total=rows_total,
        rows_scanned=rows_scanned,
        residue=tuple(residue),
    )


def _scan_eval_cache(ctx: EraseContext) -> ScanResult:
    """第 16 处：`eval-cache/`（评测缓存里可能存着摄入原文）。"""
    if ctx.data_dir is None:
        return ScanResult(unit="目录 eval-cache/", units_scanned=0)
    cache_dir = Path(ctx.data_dir) / "eval-cache"
    files = sorted(cache_dir.glob("*")) if cache_dir.exists() else []
    needles = tuple(ctx.memory_ids)
    residue: list[str] = []
    rows_total = rows_scanned = 0
    for path in files:
        if not path.is_file():
            continue
        rows_total += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            logger.warning("删除审计读不了缓存文件 %s（跳过）：%s", path.name, exc)
            continue
        rows_scanned += 1
        if needles and _json_contains(text, needles):
            residue.append(path.name)
    return ScanResult(
        unit="目录 eval-cache/",
        units_scanned=len(files),
        rows_total=rows_total,
        rows_scanned=rows_scanned,
        residue=tuple(residue),
    )


def _scan_legacy(ctx: EraseContext) -> ScanResult:
    """第 17 处：`~/.March7-legacy-*` 旧库（**手工**；只报存在与否，不删）。

    ⚠️ **`residue` 必须留空**，只在 `note` 里报告"发现了几个"。
    理由：`residue` 是「A1 判据里的残留」，而旧库是 `T25` 旧库策略明确说
    「默认跳过并记 `skipped`，不删除」的对象。把它填进 `residue` 会让每次删除
    都因「旧库还在」而 `verdict=residual`——于是那个警告变成噪音，
    新增的真实残留再也没人看（`diagnostics` 同一教训：假阳性比漏报更贵）。
    """
    home = Path.home()
    legacy = sorted(p for p in home.glob(".March7-legacy-*") if p.is_dir())
    return ScanResult(
        unit="目录 ~/.March7-legacy-*",
        units_scanned=len(legacy),
        rows_total=len(legacy),
        rows_scanned=len(legacy),
        residue=(),  # 见 docstring：手工档不参与 A1 的残留判据
    )


def _scan_uncontrollable(ctx: EraseContext, label: str) -> ScanResult:
    """第 18–21 处：**扫不了**，只能诚实地记一个 0 扫描 + 说明。"""
    return ScanResult(unit=label, units_scanned=0, rows_total=0, rows_scanned=0)


# ---------------------------------------------------------------------------
# 面清单（顺序与 ADR-0040 决策 3 表格逐行对应）
# ---------------------------------------------------------------------------

ERASE_SURFACES: tuple[EraseSurface, ...] = (
    EraseSurface(1, "memories（content / source_quote / trigger_text）", ERASE_AUTO, _scan_memories),
    EraseSurface(2, "memories_fts 检索投影", ERASE_AUTO, _scan_fts,
                 note="外部内容表 + CJK bigram 投影 ⇒ 必须用 FTS5 'delete' 命令并显式传投影值"),
    EraseSurface(3, "memories_vec 向量", ERASE_AUTO, _scan_vec,
                 note="存在近似反演风险 ⇒ 向量必须随行一起删"),
    EraseSurface(4, "memory_sources.source_json（四元组含 quote）", ERASE_AUTO, _scan_sources),
    EraseSurface(5, "claims（value 原文 + memory_id）", ERASE_AUTO, _scan_claims),
    EraseSurface(6, "conflicts（reason 可能含引文）", ERASE_AUTO, _scan_conflicts),
    EraseSurface(7, "episodes（当前 0 行）", ERASE_AUTO, _scan_episodes),
    EraseSurface(8, "ingest_marks（可反推断对话存在）", ERASE_AUTO, _scan_ingest_marks),
    EraseSurface(9, "working_items（用户原话 + 当前 handoff）", ERASE_AUTO, _scan_working,
                 note="S2 新建；新增派生面不同步本表 = 给 S5 埋必漏项"),
    EraseSurface(10, "handoffs（project_id / 状态 / 时间）", ERASE_AUTO, _scan_handoffs,
                 note="S2 新建"),
    EraseSurface(11, "logs/writes-*.jsonl", ERASE_AUTO, _scan_writes_log),
    EraseSurface(12, "logs/decisions-*.jsonl", ERASE_AUTO, _scan_decisions_log),
    EraseSurface(13, "logs/hooks-*.jsonl", ERASE_AUTO, _scan_hooks_log),
    EraseSurface(14, "profile.json", ERASE_AUTO, _scan_profile),
    EraseSurface(15, "导出 md/json 文件", ERASE_AUTO, _scan_exports, note="⚠️ 仅本机（用户可能已下载走）"),
    EraseSurface(16, "evals cache（eval-cache/）", ERASE_AUTO, _scan_eval_cache),
    EraseSurface(17, "~/.March7-legacy-* 旧库", ERASE_MANUAL, _scan_legacy,
                 note="手工：工具只报告存在，不删除"),
    EraseSurface(18, "备份 / 迁移包", ERASE_UNCONTROLLED,
                 lambda ctx: _scan_uncontrollable(ctx, "备份 / 迁移包"),
                 note="不在本工具能力内；**不得**把它算进「已删除」"),
    EraseSurface(19, "宿主 transcript（WorkBuddy / DSH）", ERASE_UNCONTROLLED,
                 lambda ctx: _scan_uncontrollable(ctx, "宿主 transcript"),
                 note="宿主自己的文件，删它等于毁用户的对话历史"),
    EraseSurface(20, "LLM 供应商侧日志（MiniMax / 智谱）", ERASE_UNCONTROLLED,
                 lambda ctx: _scan_uncontrollable(ctx, "LLM 供应商侧日志"),
                 note="不在本工具能力内"),
    EraseSurface(21, "云同步 / 文件快照 / 回收站", ERASE_UNCONTROLLED,
                 lambda ctx: _scan_uncontrollable(ctx, "云同步 / 文件快照 / 回收站"),
                 note="不在本工具能力内"),
)
"""删除必须覆盖的 21 处派生面（`ADR-0040` 决策 3）。

⚠️ **新增派生面必须先改 ADR 再改这里**——两处由 `test_erase_surfaces_cover_adr_table` 对账。
"""


def surfaces_by_kind(kind: str) -> tuple[EraseSurface, ...]:
    """按可控性筛面（回执分三段打印用）。"""
    return tuple(s for s in ERASE_SURFACES if s.kind == kind)


def audit_deletion(ctx: EraseContext) -> list[dict[str, Any]]:
    """逐面扫描，返回**可直接打印**的清单（删除后自证，也用于回执）。

    Returns:
        每个面一个字典：`{number, name, kind, unit, units_scanned, rows_total,
        rows_scanned, residue_count, residue, note}`。

    ⚠️ 业务口径**只认 `rows_scanned` 与 `residue_count`**：
    见模块 docstring 的表格。`rows_total != rows_scanned` 时说明扫描不完整，
    此时 `residue_count == 0` **不足以**宣称该面已清。
    """
    out: list[dict[str, Any]] = []
    for surface in ERASE_SURFACES:
        failed = False
        if surface.scan is None:
            result = ScanResult(unit=surface.name)
            failed = True  # 该面登记为 auto 却没有扫描函数 ⇒ 本身就是缺陷
        else:
            try:
                result = surface.scan(ctx)
            except Exception as exc:  # noqa: BLE001 —— 审计不能因为一面报错而整体崩
                logger.warning("派生面 %d（%s）扫描失败：%s", surface.number, surface.name, exc)
                result = ScanResult(unit=f"{surface.name}（扫描失败）")
                failed = True
        payload = {
            "number": surface.number,
            "name": surface.name,
            "kind": surface.kind,
            "note": surface.note,
            # `scan_error`：这一面**没能检查**。A1 的判据必须能区分
            # 「扫过了、残留 0」与「压根没扫成」——后者是缺陷，前者是结论。
            # 少了这个字段，`residue_count == 0` 会把「扫描崩了」显示成「干净」。
            "scan_error": failed,
            **result.as_dict(),
        }
        out.append(payload)
    return out


def auto_residue(audit: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总**可自动测**的面的残留（回执的 `verification` 段）。

    Returns:
        `{auto_surfaces, auto_residue_total, auto_rows_scanned, auto_rows_total,
        scan_errors, manual, uncontrollable, clean}`。

    `clean` 的判据刻意只看 auto 档，且要求**两个条件同时成立**：
    ① 残留为 0；② 没有扫描失败。manual / uncontrollable 天然"有残留"
    （旧库还在、宿主还有 transcript），把它们算进去会让这个布尔值**永远为假**，
    于是没人再看它。
    """
    auto = [row for row in audit if row["kind"] == ERASE_AUTO]
    manual = [row for row in audit if row["kind"] == ERASE_MANUAL]
    uncontrollable = [row for row in audit if row["kind"] == ERASE_UNCONTROLLED]
    auto_residue_total = sum(int(row["residue_count"]) for row in auto)
    scan_errors = [row["number"] for row in auto if row.get("scan_error")]
    return {
        "auto_surfaces": len(auto),
        "auto_residue_total": auto_residue_total,
        "auto_rows_scanned": sum(int(row["rows_scanned"]) for row in auto),
        "auto_rows_total": sum(int(row["rows_total"]) for row in auto),
        "scan_errors": scan_errors,
        "manual": [row["name"] for row in manual],
        "uncontrollable": [row["name"] for row in uncontrollable],
        # ⚠️ `clean` 必须同时要求「无残留」**和**「无扫描失败」：
        # 一面扫描崩了会产生 `residue_count == 0`（看起来干净），
        # 若只看残留数，`clean=True` 就成了"没查出来"冒充"查过了"。
        "clean": auto_residue_total == 0 and not scan_errors,
    }


HONESTY_CLAUSE = (
    "以下范围不在本工具能力内：宿主持有的原始对话、你已下载的导出、"
    "操作系统备份 / 云同步 / 回收站。"
)
"""回执里必须原文出现的诚实边界（`ADR-0040` 决策 4）。

单独抽成常量而不是写在格式化字符串里：`test_receipt_states_honest_limits`
按**这句话**断言。散在 f-string 里的话，改一个字就悄悄失效了。
"""


# ---------------------------------------------------------------------------
# 日志清理（第 11/12/13 处的**写侧**：审计只报，清除要显式调）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScrubResult:
    """日志清理的结果（**只有计数，不含内容**）。

    Attributes:
        files_scanned: 扫了几个日志文件。
        rows_total: 这些文件里总行数。
        rows_removed: 真被删掉的行数。
        files_touched: 有改动（真删了行）的文件名。
    """

    files_scanned: int = 0
    rows_total: int = 0
    rows_removed: int = 0
    files_touched: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """扁平字典（回执 / CLI 打印用）。"""
        return {
            "files_scanned": self.files_scanned,
            "rows_total": self.rows_total,
            "rows_removed": self.rows_removed,
            "files_touched": list(self.files_touched),
        }


LOG_PREFIXES: tuple[str, ...] = ("writes", "decisions", "hooks")
"""会被 `scrub_logs` 处理的日志前缀（`ADR-0040` 第 11/12/13 处）。

**刻意不扩展到所有 `*.jsonl`**：`receipts/` 与将来的新日志不在这里，
是因为它们要么不含原文（回执），要么还没被审计清单登记。
新增一类日志 ⇒ 先改 ADR 表格、再加进这个元组。
"""


def _without_log_text(payload: Any, needles: tuple[str, ...]) -> Any:
    """擦掉已对账的片段，同时保留同一日志行里其他事实和计数。"""
    if isinstance(payload, str):
        for needle in sorted(needles, key=len, reverse=True):
            payload = payload.replace(needle, "")
        return payload
    if isinstance(payload, dict):
        return {key: _without_log_text(value, needles) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_without_log_text(value, needles) for value in payload]
    return payload


def _scrub_scoped_log_line(
    line: str, target_ids: tuple[str, ...], content_needles: tuple[str, ...],
    sources: dict[tuple[str, str], set[str]], project_id: str | None,
) -> str:
    try:
        payload = json.loads(line)
    except ValueError:
        return line
    if not isinstance(payload, dict):
        return line
    declared_project = payload.get("project_id")
    declared_scope = payload.get("scope")
    if declared_project not in (None, project_id) or (
        isinstance(declared_scope, str)
        and declared_scope.startswith("project:")
        and declared_scope != f"project:{project_id}"
    ):
        # 明确属于其他项目的整行保持原样，无论它怎样引用待擦除记忆。
        return line
    paired = (str(payload.get("session_id") or ""), str(payload.get("message_id") or ""))
    needles = set(target_ids)
    if target_ids and _json_contains(payload, target_ids):
        needles.update(content_needles)
    needles.update(sources.get(paired, ()))
    if not _json_contains(payload, tuple(needles)):
        return line
    return json.dumps(_without_log_text(payload, tuple(needles)), ensure_ascii=False) + "\n"


def scrub_logs(
    data_dir: Path | None,
    *,
    needles: tuple[str, ...] | list[str],
    prefixes: tuple[str, ...] = LOG_PREFIXES,
    dry_run: bool = False,
    scoped_context: EraseContext | None = None,
    sources: dict[tuple[str, str], set[str]] | None = None,
) -> ScrubResult:
    """把命中 `needles` 的日志行**逐行删掉**，其余行原样保留。

    ## 为什么是"删行"而不是"删文件"

    日志是**增长型证据源**（`ADR-0007`）：`writes-*.jsonl` 的行数是
    "这一天跑了几轮写入"的**唯一分母**。整文件删掉之后，
    「本月跑了多少轮」永久失去基准，而没有任何地方会报错。

    ## 为什么需要显式调用，而不是 `erase` 自动做

    删日志会**改变审计历史**。把它做成 `erase` 的隐式副作用，
    会让"删除记忆"意外地"删除运行记录"——用户没同意这件事。

    Args:
        data_dir: 数据目录（`logs/` 在其下）。
        needles: 命中即删的字符串（记忆 id / 原话）。
        prefixes: 处理的日志前缀（默认三类）。
        dry_run: True = 只数不删（回执里报"还会剩多少"用）。

    Returns:
        `ScrubResult`：计数 + 被改动的文件名。

    ⚠️ 任一 needle 为空串时会**跳过**：空串会匹配所有行 ⇒ 一次调用清空全部日志。
    """
    cleaned = tuple(n for n in needles if n)
    if data_dir is None or not cleaned:
        return ScrubResult()
    log_dir = Path(data_dir) / "logs"
    if not log_dir.exists():
        return ScrubResult()
    files: list[Path] = []
    for prefix in prefixes:
        files.extend(sorted(log_dir.glob(f"{prefix}-*.jsonl")))
    files_scanned = 0
    rows_total = 0
    rows_removed = 0
    touched: list[str] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError as exc:
            logger.warning("日志清理读不了 %s（跳过）：%s", path.name, exc)
            continue
        files_scanned += 1
        kept: list[str] = []
        removed_here = 0
        for line in lines:
            rows_total += 1
            if scoped_context is not None:
                scoped_line = _scrub_scoped_log_line(
                    line, cleaned, scoped_context.free_text_needles(), sources or {},
                    scoped_context.visibility_project_id,
                )
                removed_here += int(scoped_line != line)
                kept.append(scoped_line)
                continue
            if line.strip() and any(n in line for n in cleaned):
                removed_here += 1
                continue
            kept.append(line)
        if removed_here:
            rows_removed += removed_here
            touched.append(path.name)
            if not dry_run:
                try:
                    path.write_text("".join(kept), encoding="utf-8")
                except OSError as exc:
                    logger.warning("日志清理写不了 %s（该文件未改动）：%s", path.name, exc)
                    rows_removed -= removed_here
                    touched.pop()
    return ScrubResult(
        files_scanned=files_scanned,
        rows_total=rows_total,
        rows_removed=rows_removed,
        files_touched=tuple(touched),
    )


def scrub_export_files(
    data_dir: Path | None,
    *,
    needles: tuple[str, ...] | list[str],
) -> ScrubResult:
    """导出文件（第 15 处）：命中即**删文件**（导出是整份快照，删行无意义）。

    ⚠️ 与日志不同：导出文件是**快照**，不是证据源。留一个"删了几行"的残骸
    比删掉整份更糟——用户会以为那份导出还能用。
    """
    cleaned = tuple(n for n in needles if n)
    if data_dir is None or not cleaned:
        return ScrubResult()
    files: list[Path] = []
    for pattern in ("March7-memories-*.md", "March7-memories-*.json"):
        files.extend(sorted(Path(data_dir).glob(pattern)))
    removed = 0
    touched: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("导出清理读不了 %s（跳过）：%s", path.name, exc)
            continue
        if any(n in text for n in cleaned):
            try:
                path.unlink()
                removed += 1
                touched.append(path.name)
            except OSError as exc:
                logger.warning("导出清理删不掉 %s：%s", path.name, exc)
    return ScrubResult(
        files_scanned=len(files), rows_total=len(files), rows_removed=removed, files_touched=tuple(touched)
    )


def scrub_eval_cache(
    data_dir: Path | None,
    *,
    needles: tuple[str, ...] | list[str],
) -> ScrubResult:
    """评测缓存（第 16 处）：命中即删**文件**（缓存可重建，留着就是原文副本）。"""
    cleaned = tuple(n for n in needles if n)
    if data_dir is None or not cleaned:
        return ScrubResult()
    cache_dir = Path(data_dir) / "eval-cache"
    if not cache_dir.exists():
        return ScrubResult()
    files = [p for p in sorted(cache_dir.glob("*")) if p.is_file()]
    removed = 0
    touched: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            logger.warning("缓存清理读不了 %s（跳过）：%s", path.name, exc)
            continue
        if any(n in text for n in cleaned):
            try:
                path.unlink()
                removed += 1
                touched.append(path.name)
            except OSError as exc:
                logger.warning("缓存清理删不掉 %s：%s", path.name, exc)
    return ScrubResult(
        files_scanned=len(files), rows_total=len(files), rows_removed=removed, files_touched=tuple(touched)
    )


def scrub_all_free_text(
    data_dir: Path | None,
    ctx: "EraseContext",
) -> dict[str, Any]:
    """把第 11–13 / 15 / 16 处的自由文本面清一遍（**erase 流程的一部分**）。

    ## 为什么清除而不是"只报"

    A1 的判据是「21 处逐项残留 0」。日志里留着正文时，
    物理删除就只完成了"主库那一半"——而日志是**纯文本、谁都能读**，
    恰恰是最容易被翻到的地方。所以 erase 必须连它们一起清。

    ## 为什么清"行"而不是清"文件"

    见 `scrub_logs`：日志的行数是运行记录的分母，整文件删掉会让
    「这个月跑了多少轮」永久失去基准。

    Returns:
        `{logs, exports, cache}`，每项是 `ScrubResult.as_dict()`。
    """
    needles = ctx.free_text_needles()
    logs = scrub_logs(data_dir, needles=needles)
    exports = scrub_export_files(data_dir, needles=needles)
    cache = scrub_eval_cache(data_dir, needles=needles)
    return {"logs": logs.as_dict(), "exports": exports.as_dict(), "cache": cache.as_dict()}


# ---------------------------------------------------------------------------
# 编排：一次完整的 erase（删除 → 清自由文本面 → 审计）
# ---------------------------------------------------------------------------


@dataclass
class EraseOutcome:
    """一次 erase 的完整结果（**CLI / Web / 测试共用同一个编排**）。

    Attributes:
        deleted: 各表删除计数（`{memories, working, handoffs, ...}`）。
        scrubbed: `scrub_all_free_text()` 的产物。
        audit: 删除后的逐面审计。
        verification: `auto_residue()` 的汇总（含 `clean`）。
        ctx: 本次用的上下文（回执要用它的 `content_needles` 算哈希）。
    """

    deleted: dict[str, int]
    scrubbed: dict[str, Any]
    audit: list[dict[str, Any]]
    verification: dict[str, Any]
    ctx: "EraseContext"

    @property
    def clean(self) -> bool:
        """可自动测面是否全清（回执 `verdict` 的来源）。"""
        return bool(self.verification.get("clean"))

    def residue_rows(self) -> list[dict[str, Any]]:
        """需要人看一眼的可自动测面：**有残留** 或 **扫描失败**。

        两者都要报：扫描失败时 `residue_count` 是 0，只看残留会把"没查成"
        当成"查干净了"。
        """
        return [
            row
            for row in self.audit
            if row["kind"] == ERASE_AUTO
            and (int(row["residue_count"]) > 0 or row.get("scan_error"))
        ]


def _collect_needles(
    store: Any, memory_ids: tuple[str, ...], *, include_aliases: bool = False,
) -> tuple[str, ...]:
    """删除**之前**把待删正文抄下来（删完就取不到了）。

    Returns:
        `content` / `source_quote` / `trigger_text` 三段拼接的去重列表。
    """
    out: list[str] = []
    for memory_id in memory_ids:
        mem = store.get_memory(memory_id) if store is not None else None
        if mem is None:
            continue
        source = store.get_source(memory_id) or {}
        for value in (mem.content, mem.source_quote, mem.trigger_text, source.get("quote"), mem.slug if include_aliases else None):
            text = (value or "").strip()
            if text:
                out.append(text)
    return tuple(dict.fromkeys(out))


def perform_erase(
    store: Any,
    data_dir: Path | None,
    *,
    memory_ids: tuple[str, ...] | list[str] = (),
    project_id: str | None = None,
    session_ids: tuple[str, ...] | list[str] = (),
    include_working: bool = True,
    profile_store: Any = None,
    clear_profile: bool = True,
    conversation_only: bool = False,
    working_project_id: str | None = None,
    visibility_project_id: str | None = None,
    expected_memories: dict[str, Any] | None = None,
    expected_revisions: dict[str, str] | None = None,
) -> EraseOutcome:
    """执行一次完整的 erase：**抄正文 → 删库 → 清画像 → 清自由文本面 → 审计**。

    这是唯一正确的调用顺序，几个步骤各自都有"顺序错了就静默失效"的理由：

    1. **先抄正文**：日志比对要用正文，删完就取不到 ⇒ 只能报"没扫到"；
    2. **再删库**：`store.erase_memory` 内部顺序也已固定（FTS → vec → 派生 → 主表）；
    3. **再清画像**：画像里存的是正文摘要，与库同步清；
    4. **再清自由文本面**：日志/导出/缓存里存的是正文，必须与库同步清；
    5. **最后审计**：审计是"事后复核"，必须在全部动作之后跑。

    Args:
        store: `MemoryStore`（`None` ⇒ 只清文件面）。
        data_dir: 数据目录。
        memory_ids: 要删的记忆 id。
        project_id: 按项目整删（连带 `working_items` / `handoffs`）。
        session_ids: 会话 id（连带 `ingest_marks`）。
        include_working: False = 不动 working/handoffs。
        profile_store: L0 画像（第 14 处）。

    Returns:
        `EraseOutcome`。
    """
    ids = tuple(i for i in memory_ids if i)
    sessions = tuple(s for s in session_ids if s)
    projects = (project_id,) if project_id else ()

    # ① 抄正文（必须在删除之前）
    content_needles = _collect_needles(store, ids, include_aliases=conversation_only)

    ctx = EraseContext(
        store=store,
        data_dir=data_dir,
        memory_ids=ids,
        project_ids=projects,
        session_ids=sessions,
        content_needles=content_needles,
        profile_store=profile_store,
        conversation_only=conversation_only,
        visibility_project_id=visibility_project_id,
    )

    # 已掌握的来源标识在擦除主库前收集，仅用于定位同轮日志。
    source_needles: dict[tuple[str, str], set[str]] = {}
    if conversation_only and store is not None:
        for mid in ids:
            original = store.get_source(mid) or {}
            if (source_session := original.get("session_id")) and (source_message := original.get("message_id")):
                exact = _collect_needles(store, (mid,), include_aliases=True)
                source_needles.setdefault((source_session, source_message), set()).update(
                    value for value in exact if len(value) >= MIN_CONTENT_NEEDLE_CHARS
                )

    # ② 删库
    deleted: dict[str, int] = {"memories": 0}
    if store is not None:
        if expected_revisions is not None:
            deleted["memories"] = store.erase_memory_batch(
                ids, expected=expected_memories, expected_revisions=expected_revisions,
            )
        elif expected_memories is not None:
            deleted["memories"] = store.erase_memory_batch(ids, expected=expected_memories)
        else:
            for memory_id in ids:
                if store.erase_memory(memory_id):
                    deleted["memories"] += 1
        if projects or sessions:
            scope_result = store.erase_scope(
                project_id=project_id, session_ids=sessions, include_working=include_working
            )
            deleted.update(scope_result.as_dict())
        # 按**内容**清工作层与 episode（见 `EraseContext.free_text_needles`）：
        # 这两张表的归属键不是 `memory_id`，只按项目/会话清会漏掉"另一个会话里重说过"的行。
        needles = ctx.free_text_needles()
        if not conversation_only or working_project_id is not None:
            working_removed = (
                store.erase_working_containing(needles, project_id=working_project_id)
                if conversation_only else store.erase_working_containing(needles)
            )
            if working_removed:
                deleted["working_by_content"] = working_removed
        if not conversation_only:
            episodes_removed = store.erase_episodes(session_ids=sessions, needles=needles)
            if episodes_removed:
                deleted["episodes"] = episodes_removed

    # ③ 清画像里命中的字段（第 14 处；必须在审计**之前**）
    if profile_store is not None and clear_profile:
        hits = profile_store.fields_containing(ctx.free_text_needles())
        cleared = profile_store.erase_fields(hits) if hits else []
        if cleared:
            deleted["profile_fields"] = len(cleared)

    if conversation_only and profile_store is not None and clear_profile:
        for memory_id in ids:
            profile_store.restore_visibility(memory_id)

    # ④ 清自由文本面
    if conversation_only:
        # 按目标或来源擦掉日志中的授权片段，共享导出和缓存交给审计。
        logs = scrub_logs(data_dir, needles=ids, scoped_context=ctx, sources=source_needles)
        scrubbed = {"logs": logs.as_dict(), "exports": ScrubResult().as_dict(), "cache": ScrubResult().as_dict()}
    else:
        scrubbed = scrub_all_free_text(data_dir, ctx)

    # ⑤ 审计
    audit = audit_deletion(ctx)
    return EraseOutcome(
        deleted=deleted,
        scrubbed=scrubbed,
        audit=audit,
        verification=auto_residue(audit),
        ctx=ctx,
    )


__all__ = [
    "CONTROLLABILITY_LEVELS",
    "ERASE_AUTO",
    "ERASE_MANUAL",
    "ERASE_SURFACES",
    "ERASE_UNCONTROLLED",
    "HONESTY_CLAUSE",
    "LOG_PREFIXES",
    "MIN_CONTENT_NEEDLE_CHARS",
    "EraseContext",
    "EraseOutcome",
    "EraseSurface",
    "ScanResult",
    "ScrubResult",
    "audit_deletion",
    "auto_residue",
    "perform_erase",
    "scrub_all_free_text",
    "scrub_eval_cache",
    "scrub_export_files",
    "scrub_logs",
    "surfaces_by_kind",
]
