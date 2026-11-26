"""retriever.py — 混合检索（内部代号：显影）
见 docs/NAMING.md —— 把底片上的潜影显现出来 = 召回。
管线（§3.3，v1 不上 reranker）：
    query → [FTS5 BM25] ─┐
                          ├→ 每路取可见有效的前 20 条 → RRF 融合（k=60）→ top-k（默认 5）
    query → [BGE-M3 向量] ┘
召回即更新 access_count / last_accessed_at（为 M3 激活度机制预埋数据）。

⚠️ **实测口径（2026-10-31，`evals/reports/retrieval-r5-20261031.md`）**：
BM25 路走 FTS5 的**隐式 AND**，在 324 条自然语言问题上**全部空召回**（100%），
于是 RRF 在实践中退化为「向量路原样输出」（逐条核对 324/324 一致）。
⇒ 对外表述应是 **「向量为主、BM25 兜底」**，不是"双路均衡融合"；
   把 BM25 换成 OR 语义虽然单独有 62%，但等权融合会把整体从 81.5% 拉到 74.4% ——
   **不要在没有重新标定的情况下改融合权重或换语义**（`ADR-0025`）。

当前项目可见且状态为 active 或 aging 的记忆才占用候选名额。否则保留的纠正历史或其他
项目的记忆会挤掉有效结果。作用域按 `ADR-0039` 判定，RRF 同分按 id 升序。

T17（M3 实验变量）：本文件末尾新增 `ResidencyState` —— Cyrene DMAE V5.1 的 Python
简化版激活度状态机。**`Retriever` 本体一行未改、默认召回路径零影响**：驻留状态是
跨轮数据，而钩子每轮是独立进程，必须外置才能开关与序列化（见 `ADR-0035`）。
只有 `--suite residency` 显式持有它，M2 的三条通道（钩子 / SKILL / MCP）都不碰。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from March7 import privacy
from March7.embedder import Embedder, LazyEmbedder
from March7.schemas import Memory
from March7.store import MemoryStore, _tokenize

logger = logging.getLogger(__name__)

RRF_K = 60  # Reciprocal Rank Fusion 标准常数
DEFAULT_TOP_K = 5  # March7_recall 默认返回条数
ACTIVE_RECALL_MAX_L2 = 0.82  # S20 冻结对照，单位为归一化 BGE-M3 向量的 L2 距离。
AUTO_RECALL_MIN_SHARED = 2  # S20 冻结对照，自动注入要求两份词证据或完整短查询。
RECALL_STATUSES = {"active", "aging"}  # archived/superseded/merged 不参与召回

# FTS5 / vec0 各路取的候选数（蓝图 §3.3 规定的 "top-20"）——单测里改它可验证管线
CHANNEL_LIMIT = 20

# ---------------------------------------------------------------------------
# scope 过滤（S3 / ADR-0039 决策 2、3、4）
# ---------------------------------------------------------------------------

SCOPE_GLOBAL = "global"
"""跨项目的用户事实。**任何项目、以及无项目身份时都可见**。"""

SCOPE_PROJECT_PREFIX = "project:"
"""项目作用的唯一合法前缀。`scope` 只允许 `global` 与 `project:<project_id>`
（`ADR-0039` 决策 2）——`project:<目录名>` 这种老写法**不再成立**。"""

def scope_allows(mem: Memory, project_id: str | None) -> bool:
    """这条记忆能不能被当前项目看到（**纯函数，默认拒绝**）。

    Args:
        mem: 候选记忆（只读 `scope`）。
        project_id: 本轮解析出的项目假名（`p-<6 hex>`）；`None` = 没有项目身份。

    Returns:
        `True` 仅当：① `scope == "global"`；或 ② `scope == f"project:{project_id}"`。

    为什么其余一律 `False`（`ADR-0039` 决策 2/3，D2「作用域默认拒绝」）：
    - `project_id is None` ⇒ 不降级成「注入全部 project 记忆」。那等于把 A 项目的事实
      灌进 B 项目，而用户完全看不出来（界面上只是一段"记忆"）。
    - `project:<目录名>` 这类历史写法 ⇒ 也不放行。目录名会变（重命名、换盘符），
      且它和不带项目身份的场景无法安全对齐：放行它意味着**任何**项目都能读到它。
    - 未知形态（拼错、大小写不一致）同理。宁可这一轮少注入一条，
      也不要一次跨项目串味——前者可解释，后者是不可逆的信任损失。
    """
    scope = (getattr(mem, "scope", "") or "").strip()
    if scope == SCOPE_GLOBAL:
        return not privacy.unbound_historical_project(scope, mem.content, mem.source_quote)
    if not project_id:
        return False
    return scope == f"{SCOPE_PROJECT_PREFIX}{project_id}"


def filter_by_scope(memories: list[Memory], project_id: str | None) -> list[Memory]:
    """按作用域过滤，**保持原顺序**。

    顺序不是无关细节：列表顺序就是相关度顺序，重排等于把答案换了。
    """
    return [m for m in memories if scope_allows(m, project_id)]


def _is_recallable(memory: Memory, *, project_id: str | None) -> bool:
    """召回候选必须同时满足状态和当前项目的可见性条件。"""
    return memory.status.value in RECALL_STATUSES and scope_allows(memory, project_id)


_COMMON_QUERY_TOKENS = {"什么", "多少", "怎么", "如何", "是否", "项目", "请问", "这个", "那个", "现在", "我想", "时候"}


def _lexical_relevance(query: str, min_shared: int) -> Callable[[Memory], bool]:
    """允许短而具体的完整查询，其余要求至少两份可分辨的词证据。"""
    tokens = {token for token in _tokenize(query) if len(token) > 1} - _COMMON_QUERY_TOKENS

    def matches(memory: Memory) -> bool:
        shared = tokens.intersection(_tokenize(memory.content + " " + (memory.source_quote or "")))
        return bool(shared) and (shared == tokens or len(shared) >= min_shared)

    return matches


@dataclass(frozen=True, slots=True)
class AutomaticRecall:
    """实际召回和首批候选的过滤原因，供注入与回执共用。"""

    memories: list[Memory]
    candidates: list[Memory]
    out_of_scope: list[str]
    outdated: list[str]
    low_relevance: list[str]


def select_automatic_recall(
    store: MemoryStore, query: str, *, min_shared: int, limit: int = DEFAULT_TOP_K, project_id: str | None = None,
) -> AutomaticRecall:
    """自动注入只走有词证据的可见当前记忆，过滤结果不占据输出名额。"""
    if privacy.short_acknowledgement(query) or not query.strip() or limit <= 0:
        return AutomaticRecall([], [], [], [], [])
    relevant = _lexical_relevance(query, min_shared)
    candidates = store.fts_search_any(query, limit=limit)
    matches, out_of_scope, outdated, low_relevance = [], [], [], []
    for memory in candidates:
        if not scope_allows(memory, project_id):
            out_of_scope.append(memory.id)
        elif memory.status.value not in RECALL_STATUSES:
            outdated.append(memory.id)
        elif not relevant(memory):
            low_relevance.append(memory.id)
        else:
            matches.append(memory)
    if len(candidates) == limit and len(matches) < limit:
        matches = store.fts_search_any(
            query, limit=limit, eligible=lambda memory: _is_recallable(memory, project_id=project_id) and relevant(memory),
        )
    return AutomaticRecall(matches, candidates, out_of_scope, outdated, low_relevance)


def rrf_fuse(
    fts_hits: list[Memory],
    vec_hits: list[Memory],
    k: int = RRF_K,
    top_k: int = DEFAULT_TOP_K,
) -> list[Memory]:
    """RRF 融合两路检索结果：score(d) = Σ 1/(k + rank_i(d))。

    纯函数（不碰数据库与模型），单测直接构造 Memory 列表验证。

    Args:
        fts_hits: BM25 路 top-N（按相关度降序）。
        vec_hits: 向量路 top-N（按相似度降序）。
        k: RRF 常数，默认 60。
        top_k: 融合后保留的条数。

    Returns:
        融合排序后的记忆列表（两路均未命中的记忆不出现）。
        排序键：先 RRF 分数降序；同分按 id 升序（确定性）。
    """
    scores: dict[str, float] = {}
    memories: dict[str, Memory] = {}

    # RRF 公式：score(d) = Σ_i 1/(k + rank_i(d))；rank 从 1 起
    # 同 id 在两路都出现时，"先到先得"——fts 路径先注册，所以 fts 的 Memory 对象胜出
    # （避免 vec 路径覆盖造成 fts 独有字段丢失；测试见 test_rrf_uses_first_occurrence_*）
    for rank, mem in enumerate(fts_hits, start=1):
        mid = mem.id
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
        memories.setdefault(mid, mem)  # 已有则不动，fts 路径先注册赢

    for rank, mem in enumerate(vec_hits, start=1):
        mid = mem.id
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
        memories.setdefault(mid, mem)  # 已有则不动（fts 赢）

    # 同分时按 id 升序（保证 [m.id for m in rrf_fuse(...)] 结果确定）
    sorted_ids = sorted(scores.keys(), key=lambda mid: (-scores[mid], mid))
    return [memories[mid] for mid in sorted_ids[:top_k]]


class Retriever:
    """检索门面：BM25 + 向量 + RRF + 状态过滤 + 召回计数。"""

    def __init__(self, store: MemoryStore, embedder: Embedder | LazyEmbedder) -> None:
        """注入依赖（引擎内部组装，server.py 只透传）。

        Args:
            store: L2 记忆库（提供 fts_search / vector_search / touch）。
            embedder: BGE-M3 编码器（提供 embed_one）。
        """
        self._store = store
        self._embedder = embedder

    def retrieve(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        *,
        project_id: str | None = None,
        max_distance: float | None = None,
    ) -> list[Memory]:
        """混合检索（March7_recall 的引擎侧入口）。

        两路各取前 20 条可见有效记忆，RRF 融合后按 top_k 截断并逐一 touch()。

        Args:
            query: 检索词。
            top_k: 返回条数，默认 5。
            project_id: 本轮项目假名（`p-<6 hex>`）。`None` ⇒ 只召回 `global`
                （`ADR-0039` 决策 3：不降级成「注入全部 project 记忆」）。
                ⚠️ 关键字参数（`*`）是刻意的：位置传参会让「忘了传 scope」静默
                退化成 global-only，而不是一个显眼的 TypeError。

        Returns:
            召回的记忆列表（已按 RRF 分数降序、同分按 id 升序排序），
            status 全部为 active/aging，且 scope 已按 `project_id` 过滤。

        """
        if not query or not query.strip() or (max_distance is not None and privacy.short_acknowledgement(query)):
            return []

        eligible = partial(_is_recallable, project_id=project_id)
        fused = self._fuse(query, eligible=eligible, max_distance=max_distance)
        recalled = fused[:max(top_k, 0)]
        for mem in recalled:
            self._store.touch(mem.id)

        return recalled

    def _fuse(
        self, query: str, *, eligible: Callable[[Memory], bool] | None = None,
        max_distance: float | None = None,
    ) -> list[Memory]:
        """融合两路候选。省略 eligible 可取得未过滤的验收对照。

        召回入口传入可见性条件，使两路各自在候选截断前应用它。
        """
        # ⚠️ **降级路径（ADR-0025，2026-10-31）**：没有 sqlite-vec 时向量路整条不可用，
        # 而 BM25 的 AND 语义在自然语言问题上会**全空**（实测 324/324，见
        # `evals/reports/retrieval-r5-20261031.md`）。所以降级时改用 **OR 语义**的关键词路，
        # 至少还能召回；原来的行为是 `vector_search` 直接抛 RuntimeError ⇒ 召回彻底不可用。
        # 这条分支只在"本来就没有向量"时走，对正常路径零影响。
        or_eligible = eligible
        if max_distance is not None:
            relevant = _lexical_relevance(query, 2)
            or_eligible = lambda memory: (eligible is None or eligible(memory)) and relevant(memory)
        if self._store.vec_ready:
            fts_hits = self._store.fts_search(query, limit=CHANNEL_LIMIT, eligible=eligible)
            vec_hits = self._store.vector_search(
                self._embedder.embed_one(query), limit=CHANNEL_LIMIT, eligible=eligible, max_distance=max_distance,
            )
        else:
            logger.warning(
                "sqlite-vec 不可用 ⇒ 召回降级为纯关键词（OR 语义）。"
                "质量低于向量路，但比整条召回失败好（ADR-0025）。"
            )
            fts_hits = self._store.fts_search_any(query, limit=CHANNEL_LIMIT, eligible=or_eligible)
            vec_hits = []

        # 融合保留两路的并集，最终 top_k 由调用方决定。
        fused = rrf_fuse(fts_hits, vec_hits, k=RRF_K, top_k=CHANNEL_LIMIT * 2)

        # 两路皆空时的兜底：AND 关键词太严，OR 往往还能抢救回一条。
        # 仅在没有合格候选时尝试 OR，并沿用相同的召回条件（ADR-0025）。
        if not fused:
            fused = self._store.fts_search_any(query, limit=CHANNEL_LIMIT * 2, eligible=or_eligible)

        return fused

    def search_keyword(
        self,
        keyword: str,
        limit: int = 20,
        *,
        project_id: str | None = None,
    ) -> list[Memory]:
        """纯 BM25 关键词检索（March7_search 的引擎侧入口；精确找回专有名词/代码）。

        与 retrieve 的区别：不走向量、不做 RRF，用于精确匹配场景。
        同样应用 scope 过滤 + status 过滤 + touch（接口一致性：search_keyword 也是「召回」）。

        Args:
            keyword: 关键词。
            limit: 返回条数上限，默认 20（与 retrieve 的双路候选数对齐）。
            project_id: 本轮项目假名；`None` ⇒ 只返回 `global`（同 `retrieve` 的口径）。
                ⚠️ 不传 `project_id` 的调用方会**看不到任何项目记忆**——这是刻意的
                默认拒绝；要项目记忆就必须显式给身份。

        Returns:
            BM25 命中的记忆列表（按相关度降序），status 全部为 active/aging，
            且 scope 已按 `project_id` 过滤。
        """
        if not keyword or not keyword.strip():
            return []

        eligible = partial(_is_recallable, project_id=project_id)
        recalled = self._store.fts_search(keyword, limit=limit, eligible=eligible)

        for mem in recalled:
            self._store.touch(mem.id)

        return recalled


# ---------------------------------------------------------------------------
# T17 · DMAE 驻留激活度（内部代号：无漏 · 驻留层）
# ---------------------------------------------------------------------------
# 参考：Cyrene `src/main/memory/l2-dmae-manager.ts`（L2 参数与位次 I 梯度）、
#       `src/main/rag/worldbook.ts`（V5.1 主循环）、
#       `docs/specs/2026-08-08-dmae-v5-upgrade-and-l2-working-memory.md`
#       §4.5 Ru* / §4.6 Wake-Up / §4.7 Archived skip / §9.4 关键不变量。
#
# 「Python 简化版」砍掉的是 cascade、持久化、与业务 status 的正交映射；**保留**的是
# 五件真正决定行为的事：Wake-Up 初值改写、Ru* 的饱和/重复双门控、Rm 的 Active
# gating 与 clamp、D=(α·US²+β·MS²)/√I、未命中 Archived 跳过更新。
#
# 三条不变量由代码强制（违反即抛 ResidencyInvariantError）：
#   I1 激活度恒在 [0, max_score] 且为有限数。**下界由 clamp 保证**——Cyrene 原版就这样
#       把「衰减到负数」归零，属正常行为；响亮抛错针对的是公式产出 NaN/inf / 超上界
#       （在 clamp **之前**查未钳制值，否则断言恒真）与外部写坏状态（入口体检）。
#   I2 无用户命中的任何一轮，a_new <= a_old（自动流程不得抬高激活度）；
#   I3 只有用户命中能把 archived 记忆唤醒（本项目口径，见 ADR-0035）；
#   I4 I 只进衰减分母，不进奖励公式（结构上 user_reward 不接收 I）。
# ResidencyStep.raw_a 保留未钳制值，供套件层独立复算「公式有没有算飞」。
#
# ⚠️ 为什么状态机**不挂进 Retriever**：WorkBuddy 钩子每轮是独立进程（T14 实测 Stop
# 冷启动 ≈14.4s），Retriever 实例活不过一轮；跨轮状态必须外置、可序列化、可开关。
# 挂进 Retriever 会把 M2 的无状态契约变成有状态，实验变量也就关不掉了（ADR-0035）。

from collections.abc import Iterable, Sequence  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from math import exp, isfinite, log, sqrt  # noqa: E402


class ResidencyInvariantError(RuntimeError):
    """激活度状态机违反不变量：外部状态被写坏 / 公式产出 NaN·inf·超上界时响亮抛出。

    正常衰减下溢由 clamp 归零（Cyrene 原版数值安全网），不属异常；
    边界与理由见模块 docstring 的 I1 说明（独立验收 P1-2）。
    """


@dataclass(frozen=True, slots=True)
class ResidencyParams:
    """Cyrene V5 的 **L2 专用**参数档（不是 worldbook 的 20/1.5 默认值）。

    L2 档刻意让奖励更小、衰减更慢，才能满足「位次 1 约 4 轮 / 位次 4 约 2 轮
    跌出热层」的驻留目标。改动任何一项都会让参考结论失效，故一律写成常量。
    """

    max_score: float = 100.0
    prompt_threshold: float = 30.0
    user_reward_base: float = 10.0
    wake_gamma: float = 0.5
    model_reward_base: float = 4.0
    wake_lambda: float = 0.3
    decay_alpha: float = 1.0
    decay_beta: float = 0.2
    repeat_rho: float = 0.5
    sat_power: float = 2.0
    repeat_window: int = 6
    wake_bonus: float = 5.0
    intrinsic_by_rank: tuple[float, ...] = (36.0, 8.0, 8.0, 1.0)
    min_intrinsic: float = 1.0
    epsilon: float = 1e-9


def derive_residency_state(
    activation: float, params: ResidencyParams | None = None
) -> str:
    """三态派生（纯函数）：<=0 archived；>=threshold active；中间 dormant。"""
    p = params or ResidencyParams()
    if activation <= 0.0:
        return "archived"
    if activation >= p.prompt_threshold:
        return "active"
    return "dormant"


@dataclass(slots=True)
class ResidencyEntryState:
    """单条记忆的跨轮状态（与业务 status 正交，不落库、不进导出）。"""

    activation: float = 0.0
    user_silence: int = 0
    model_silence: int = 0
    recent_user_hits: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ResidencyStep:
    """一轮更新的逐条账本：Trajectory 日志与不变量断言都读它。"""

    memory_id: str
    a_old: float
    a_new: float
    raw_a: float  # clamp 之前的公式原值（下溢为负属正常衰减，套件据此复核公式）
    state: str
    user_hit: bool
    model_hit: bool
    user_reward: float
    model_reward: float
    decay: float
    woke_up: bool
    skipped: bool
    intrinsic: float


class ResidencyState:
    """跨轮激活度账本：接收「本轮召回位次 + 命中集合」，产出下一轮的驻留槽。

    调用方负责判定 `user_hits` / `model_hits`（关键词或证据覆盖率口径），
    本类只做数学与不变量——这样引擎零依赖，评测可以换命中口径而不改公式。

    **用户独占再激活权**：`recalled_ids` 只按位次设置 I（决定忘得多慢），
    从不直接抬高 activation；`model_hits` 给的 Rm 被 clamp 到 `< D`，净效果单调不增。
    只有 `user_hits` 能触发 Wake-Up / Ru*。
    """

    def __init__(self, params: ResidencyParams | None = None) -> None:
        self.params = params or ResidencyParams()
        self.states: dict[str, ResidencyEntryState] = {}
        self._intrinsic: dict[str, float] = {}

    def activation(self, memory_id: str) -> float:
        """当前激活度（未知记忆视为 0，不隐式创建状态）。"""
        entry = self.states.get(memory_id)
        return entry.activation if entry is not None else 0.0

    def active_ids(self, top_k: int | None = None) -> list[str]:
        """仍在热层（activation >= threshold）的 id，按激活度降序、同分按 id 升序。"""
        ids = [
            mid for mid, entry in self.states.items()
            if entry.activation >= self.params.prompt_threshold
        ]
        ids.sort(key=lambda mid: (-self.states[mid].activation, mid))
        return ids if top_k is None else ids[:top_k]

    def step(
        self,
        *,
        memory_ids: Sequence[str],
        recalled_ids: Sequence[str] = (),
        user_hits: Iterable[str] = frozenset(),
        model_hits: Iterable[str] = frozenset(),
        turn: int = 0,
    ) -> dict[str, ResidencyStep]:
        """推进一轮，返回每个 memory_id 的更新账本。

        Args:
            memory_ids: 本轮参与状态机的全部记忆（未被召回的也要更新沉默计数）。
            recalled_ids: 本轮检索命中的 id，**按 rank 升序**；只用于设置 I 梯度。
            user_hits: 用户本轮主动提及的记忆 id（唯一合法的再激活来源）。
            model_hits: 模型回复里复述到的记忆 id（只能在 Active 给有限维护奖励）。
            turn: 轮次编号（0-based，重复抑制窗口用它）。

        Returns:
            id → ResidencyStep（含 a_old/a_new、奖励、衰减、是否唤醒/跳过）。

        Raises:
            ResidencyInvariantError: 任一不变量被违反（含外部把状态写坏）。
        """
        p = self.params
        u_hits = set(user_hits)
        m_hits = set(model_hits)

        # 入口体检：外部（含变异测试）写坏状态必须当场炸，而不是被 clamp 抹平
        for mid in memory_ids:
            entry = self.states.get(mid)
            if entry is not None and (
                not isfinite(entry.activation)
                or entry.activation < 0.0
                or entry.activation > p.max_score
            ):
                raise ResidencyInvariantError(
                    f"激活度越界：{mid} = {entry.activation}（I1：必须落在 [0, {p.max_score}]）"
                )

        # 召回位次 → I（Cyrene §3.6：I 只进衰减分母，且跨轮保留到下次召回）
        for rank, mid in enumerate(recalled_ids):
            self._intrinsic[mid] = p.intrinsic_by_rank[
                min(rank, len(p.intrinsic_by_rank) - 1)
            ]

        records: dict[str, ResidencyStep] = {}
        for mid in memory_ids:
            entry = self.states.setdefault(mid, ResidencyEntryState())
            a_old = entry.activation
            us_old, ms_old = entry.user_silence, entry.model_silence
            old_state = derive_residency_state(a_old, p)
            user_hit, model_hit = mid in u_hits, mid in m_hits

            # V5 §11.2：未命中的 Archived 跳过本轮（沉默计数也不 unbounded 增长）
            if old_state == "archived" and not user_hit and not model_hit:
                records[mid] = ResidencyStep(
                    memory_id=mid, a_old=a_old, a_new=a_old, raw_a=a_old,
                    state="archived", user_hit=False, model_hit=False,
                    user_reward=0.0, model_reward=0.0, decay=0.0,
                    woke_up=False, skipped=True,
                    intrinsic=max(p.min_intrinsic, self._intrinsic.get(mid, p.min_intrinsic)),
                )
                continue

            # Lifecycle Phase：Wake-Up 是「初值改写」，不是事后加 floor（V5 §7.4）
            a_init = a_old
            woke_up = False
            if user_hit and old_state == "archived":
                a_init = min(p.max_score, p.prompt_threshold + p.wake_bonus)
                woke_up = True

            us_new = 0 if user_hit else us_old + 1
            ms_new = 0 if (user_hit or model_hit) else ms_old + 1
            recent = [t for t in entry.recent_user_hits if t > turn - p.repeat_window]
            if user_hit:
                recent.append(turn)
            entry.recent_user_hits = recent

            # Ru* = Bu × (1 + γ·ln(1+U_old)) × G_sat × G_repeat（不含 I）
            user_reward = 0.0
            if user_hit:
                base = p.user_reward_base * (1.0 + p.wake_gamma * log(1.0 + us_old))
                g_sat = (1.0 - a_init / p.max_score) ** p.sat_power if a_init < p.max_score else 0.0
                g_repeat = 1.0 / (1.0 + p.repeat_rho * len(recent))
                user_reward = base * g_sat * g_repeat

            # D = (α·US² + β·MS²) / √I（I 唯一出现的位置）
            intrinsic = max(p.min_intrinsic, self._intrinsic.get(mid, p.min_intrinsic))
            decay = (
                p.decay_alpha * us_new * us_new + p.decay_beta * ms_new * ms_new
            ) / sqrt(intrinsic)

            # Rm 只在 Active 生效，且恒 < D ⇒ 模型永远无法净抬高激活度（V5 §9.1）
            model_reward = 0.0
            if model_hit and old_state == "active":
                raw = p.model_reward_base * exp(-p.wake_lambda * us_old)
                model_reward = max(0.0, min(raw, decay - p.epsilon))

            raw_a = a_init + user_reward + model_reward - decay

            # ---- 三条运行时不变量（异常值响亮暴露；正常下溢由 clamp 归零）----
            # ⚠️ 必须在 clamp **之前**验未钳制值：先 clamp 再查 `a_new < 0 / > max`
            # 会变成恒真断言（独立验收 P1-2）。下溢（负数）是 Cyrene 原版的正常衰减
            # 归零，不算违规；NaN/inf 与超上界绝不是正常结果，必须当场炸。
            if not isfinite(raw_a) or raw_a > p.max_score:
                raise ResidencyInvariantError(
                    f"激活度公式产出异常值：{mid} raw={raw_a} "
                    f"（I1：必须有限且 <= {p.max_score}）"
                )
            a_new = max(0.0, raw_a)  # 上界已由前置检查保证；下溢按 Cyrene 原版归零

            if not isfinite(a_new) or a_new < 0.0 or a_new > p.max_score:
                raise ResidencyInvariantError(
                    f"激活度越界：{mid} 本轮算出 {a_new}（I1）"
                )
            if not user_hit and a_new > a_old + p.epsilon:
                raise ResidencyInvariantError(
                    f"自动流程抬高了激活度：{mid} {a_old} → {a_new}（I2，turn={turn}）"
                )
            if old_state == "archived" and not user_hit and a_new > 0.0:
                raise ResidencyInvariantError(
                    f"非用户命中唤醒了 archived 记忆：{mid}（I3，turn={turn}）"
                )

            entry.activation = a_new
            entry.user_silence = us_new
            entry.model_silence = ms_new
            records[mid] = ResidencyStep(
                memory_id=mid, a_old=a_old, a_new=a_new, raw_a=raw_a,
                state=derive_residency_state(a_new, p), user_hit=user_hit,
                model_hit=model_hit, user_reward=user_reward,
                model_reward=model_reward, decay=decay, woke_up=woke_up,
                skipped=False, intrinsic=intrinsic,
            )
        return records

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        """可序列化的状态快照（Trajectory 复算 / 换机接力用，不进记忆库）。"""
        return {
            mid: {
                "activation": e.activation,
                "user_silence": e.user_silence,
                "model_silence": e.model_silence,
                "recent_user_hits": len(e.recent_user_hits),
            }
            for mid, e in self.states.items()
        }


def merge_resident(
    resident_ids: Sequence[str], fresh_ids: Sequence[str], top_k: int
) -> list[str]:
    """把「驻留槽」与「本轮新召回」合成一份 ≤top_k 的注入清单。

    驻留优先、按给定顺序、去重。**预算守恒**是 A/B 公平的前提：驻留臂不许靠
    多塞几条取胜，否则命中率与 token 成本两列都失去可比性。

    Args:
        resident_ids: 上轮留下的热层 id（已按激活度降序）。
        fresh_ids: 本轮检索 top-k（已按相关度降序）。
        top_k: 注入槽上限（与引擎 `DEFAULT_TOP_K` 对齐）。

    Returns:
        合并后的 id 列表（长度 ≤ top_k，元素唯一）。
    """
    if top_k <= 0:
        return []
    merged: list[str] = []
    seen: set[str] = set()
    for mid in (*resident_ids, *fresh_ids):
        if mid in seen:
            continue
        seen.add(mid)
        merged.append(mid)
        if len(merged) >= top_k:
            break
    return merged
