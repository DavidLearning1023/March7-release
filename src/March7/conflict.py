"""conflict.py — 冲突预检与四分类消解（内部代号：穷观阵）
见 docs/NAMING.md —— 符玄用来重构记忆、发现失真的阵法 = 冲突检测与消解。

设计红线（§3.2）：失效不删除——裁决后旧记忆只标 superseded / merged，
历史永远可查；四分类 = unrelated / context_difference / preference_evolution /
direct_conflict（+ uncertain 兜底）。

两级判定（`ADR-0018` 后果 A）：
1. **规则筛**（`store.find_claim_conflicts`）：同主体 + 同属性 + 同范围 + 时间重叠，
   **筛不出值不同就没有冲突**。旧方案是「同 scope 近期 + 关键词重叠」——而 `keywords`
   从 T12 起一直是空列表 ⇒ 那条路**从未真正生效过**（不是"效果一般"，是"永远为空"）。
2. **LLM 四分类**（`LLMRole.JUDGE` = 智谱 GLM，异源硬约束）：只对规则筛留下的候选判。
   规则筛宁可多召回，四分类才是精度关口；裁决失败**不猜**（保持未裁决 + 标需要问用户）。

为什么规则筛必须先做：四分类要两次记忆正文 + 提示词，一条就要几百 token；
而「同维度不同取值」是可以用索引在毫秒级压到个位数的。把可判定的规则交给 SQL，
把需要常识的判定留给模型——这也让「换工作」这类场景能在测试里直接查表断言。
"""

from __future__ import annotations

import difflib
import json
import logging
import time
import uuid

from pydantic import BaseModel, ConfigDict

from March7 import config
from March7.claims import (
    SINGLE_VALUED_PREDICATES,
    Claim,
    ClaimExtractionError,
    ClaimExtractor,
)
from March7.llm import LLMClient, LLMError, LLMRole
from March7.schemas import (
    Conflict,
    ConflictDetector,
    ConflictStatus,
    Memory,
    MemoryStatus,
    ResolutionType,
)
from March7.store import MemoryStore

logger = logging.getLogger(__name__)

SIMILAR_VALUE_RATIO = 0.90
"""两个取值「其实是同一个值」的字面相似度下限（低于它才算不同取值）。

与查重的 `_looks_same_fact`（0.80）刻意不同：查重误并会**丢一条事实**，所以那道闸门更严；
这里误判「不同」只是**多报一次冲突**，而冲突会进 `conflicts` 表、由 LLM 再判一次
⇒ 方向是保守（宁可多报，让四分类挡）。0.90 的效果：「软件工程师」vs「软件工程师（后端）」
不算冲突，「软件工程师」vs「产品经理」算。
"""

_STRIP_CHARS = " \t\r\n，。、；：！？,.;:!?\"'“”‘’（）()【】[]《》<>～~—-"


def _squash(text: str | None) -> str:
    """去掉空白、标点与大小写差异（取值比较的前置清洗）。"""
    return "".join(ch for ch in (text or "").lower() if ch not in _STRIP_CHARS)


def _values_differ(new_value: str, old_value: str, *, ratio: float = SIMILAR_VALUE_RATIO) -> bool:
    """两个取值是否**实质不同**（同值换个说法、或一方只是更具体，都不算冲突）。

    为什么不能只比相等：模型抽 value 时会带修饰（「深圳市宝安区」vs「宝安」、
    「产品经理」vs「产品经理（B 端）」）。只比相等会让这些全部变成冲突候选，
    把四分类的调用量和用户的裁决负担一起推上去。

    为什么要额外判「包含即同一事实」：`difflib` 对「香港」vs「香港大围」只给 0.67，
    单靠阈值会把「更具体」判成「冲突」——而更具体**不是**冲突（人还在香港）。
    判据：一方是另一方的子串 ⇒ 视为同一维度上的细化，交给四分类之外的沉默处理。
    """
    a = _squash(new_value)
    b = _squash(old_value)
    if not a or not b:
        return False
    if a == b:
        return False
    if a in b or b in a:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() < ratio


CLASSIFY_SYSTEM_PROMPT = """你是 March7 记忆系统的冲突裁决器（内部代号：穷观阵）。
输入是「新事实」与「旧事实」（都是关于同一个用户的记忆摘要 + 用户原话）。你要判断二者的关系。

## 只能选一个 resolution_type
- unrelated：不是同一件事——主体不同（同事在学吉他 vs 我在学吉他）、维度不同（住在哪 vs 做什么工作），
  或同一个词指的不是同一个对象。字面像、实际互不相干，就选它。
- context_difference：同一件事，但新陈述是**有外在理由的例外或特定场景**（人情场合、职责所在、
  他人安排、场景所迫），两条同时成立、并不矛盾。
- preference_evolution：同一维度上的**状态、取向或偏好**随时间移动（换工作、搬家、改口味、
  以前爱 A 现在更常 B），旧的是过时状态；没有"说过的原则被行为推翻"这一层。
- direct_conflict：旧陈述是**绝对化的立场或原则**（"从不…""坚决不…""绝不…""这辈子不…"），
  新陈述是**与之不相容的具体行为**，且没有外在理由——用户自己把说过的话推翻了。
- uncertain：信息确实不足以判定。

## 判定顺序（按顺序问，前一条成立就停）
1. 说的是同一件事吗（主体、维度、所指对象都相同）？不是 → unrelated。
2. 新陈述是"有外在理由的例外/特定场景"，两条都成立吗？是 → context_difference。
3. 两条说的都是**状态或取向**（住哪、做什么、喜欢什么、习惯如何），新话是它后来的样子，
   而不是一件推翻旧话的具体行为？是 → preference_evolution。
4. 旧话是绝对化立场，新话是**一件与之不相容的具体行为**（办了/去了/喝了/接了/开始了），
   且没有外在理由吗？是 → direct_conflict。
5. 仍然判不了 → uncertain。

## 重要
- 「有外在理由」看的是**有没有外部场合或他人要求**，不是"发生了几次"：
  一次性破例但场合正当（人情、职责）⇒ context_difference；自己主动改主意、图自己的方便 ⇒ direct_conflict。
- 「后来」「现在」这类时间词**不能单独当作 preference_evolution 的依据**——立场反转同样发生在"后来"。
  要问的是：变的是状态/取向，还是被具体行为推翻的原则。
- 不要把"更具体"当成"冲突"：「住在香港」与「住在香港大围」是相容的。
- 两种错都贵：判成冲突会让用户的旧记忆被标失效，判成无关会把真冲突漏掉。
  但**能判就别推**——把可判定的推回给用户，等于这一步没做。

## 输出（严格 JSON，不要代码块、不要解释文字）
{"resolution_type": "preference_evolution", "confidence": 0.86, "reason": "一句话中文理由"}
"""
"""四分类的系统提示词。

**两处口径缺陷在 T16c 修掉**（`ADR-0033`，判据来自 `evals/dataset/DATA.md` §4 的成文规则）：

1. 旧版 `unrelated` 的定义里写了"主体、维度或**语境**都不同"，而 `context_difference`
   的定义正是"语境或适用范围不同" ⇒ **提示词自相矛盾**，且判定顺序把 `unrelated` 放第一位。
   实测后果：13 条 `context_difference` 里 5 条被判成 `unrelated`（模型照着第一条规则走）。
   现在 `unrelated` 只管主体/维度/所指对象，语境归 `context_difference`。
2. 旧版把"有时间先后、有变化动词（换/改/搬/停/戒…）"直接判 `preference_evolution`，
   并把 `direct_conflict` 定义成"同一语境下互相否定、不可能同时成立" ⇒ **时间性反转被
   结构性地排除在 `direct_conflict` 之外**。而 `DATA.md` §4 的 `direct_conflict` 恰恰就是
   "主动让步 / 立场反转"，必然带时间先后。实测后果：开发集 10 条 `direct_conflict` 判对 **0 条**，
   其中 5 条被吞成 `preference_evolution`（**全量 108 条没有旧提示词读数**，不能外推）。

现在改按 `DATA.md` §4 的判据排序：**有没有外在理由**（决定 context vs 其余）→
**旧话是不是绝对化原则 + 新话是不是具体行为**（决定 direct vs evolution）。
提示词里的例子全部是**通用构造**，没有一条取自评测集——否则就是拿答案调提示词。

⚠️ **一处有意收窄（`ADR-0033`「操作化登记」）**：§4 只说"立场反转"，本实现要求旧话**绝对化**
（从不/坚决不/绝不/这辈子不）。理由：§4 的三个例子全是绝对化句式，放宽会把"更喜欢一个人待着"
这类取向变化吸进 `direct_conflict`。代价：非绝对化表述的立场反转判成演化——两者对产品动作
相同（都 supersede 旧记忆），只差指标与"要不要问用户"。
"""


class _ClassifyOutput(BaseModel):
    """四分类裁决的 LLM 输出（宽进严出：字段缺失走默认值，非法取值由代码兜底）。"""

    model_config = ConfigDict(extra="ignore")

    resolution_type: str = ""
    confidence: float | None = None
    reason: str = ""


def _parse_resolution(raw: str) -> ResolutionType | None:
    """把模型给的字符串解析成枚举；认不出返回 None（由调用方降级为 uncertain）。"""
    text = (raw or "").strip().lower()
    for member in ResolutionType:
        if text == member.value:
            return member
    return None


class ConflictChecker:
    """冲突检测器与裁决器：规则筛 → LLM 四分类 → 失效不删除。

    依赖注入（`ADR-0004` 同一套风格）：`store` 必填；`llm` 缺省时**只做规则筛**
    （不抽取 claim、不裁决，但**向量候选筛仍可用**——它不需要 LLM）。
    `claim_extractor` 可单独替换：harness 用它接上"带缓存的抽取器"，避免重复付费。
    """

    def __init__(
        self,
        store: MemoryStore,
        llm: LLMClient | None = None,
        *,
        claim_extractor: ClaimExtractor | None = None,
        vector_min_cosine: float | None = None,
        vector_top_k: int | None = None,
    ) -> None:
        """注入 L2 库与 LLM client（纯预检场景可传 llm=None，跳过 claim 抽取与 LLM 裁决）。

        Args:
            store: L2 库。
            llm: 裁决/抽取用 LLM；None ⇒ 只做规则筛（不抽 claim）。
            claim_extractor: 可替换的抽取器（harness 用它接带缓存的实现）。
            vector_min_cosine: 向量候选筛的余弦下限；None ⇒ 读 `MARCH7_CONFLICT_VEC_MIN_COSINE`
                （默认 0.57）；`0` ⇒ 关闭向量通道（回到纯 claim 筛，用于复现 T16 口径）。
            vector_top_k: 单次写入最多取几个近邻；None ⇒ 读 `MARCH7_CONFLICT_VEC_TOP_K`（默认 1）。
        """
        self._store = store
        self._llm = llm
        self._vec_min = (
            config.conflict_vector_min_cosine() if vector_min_cosine is None else vector_min_cosine
        )
        self._vec_top_k = (
            config.conflict_vector_top_k() if vector_top_k is None else vector_top_k
        )
        if claim_extractor is not None:
            self._claims = claim_extractor
        elif llm is not None:
            self._claims = ClaimExtractor(llm)
        else:
            self._claims = None
        # 只读观测位（与 `Judge.extraction_failed` 同一套做法）：写入管线据此把
        # 「claim 抽取失败」记进账本（`ADR-0018` 决策 7），而不是只在日志里留一行。
        # 用**累计计数**而不是布尔：ConflictChecker 是进程内单例，布尔位无法区分
        # 「本轮的失败」与「上一轮留下的痕迹」；管线取前后差值即可得到本轮次数。
        self.claims_failed_total = 0

    # ---- 规则筛 ----

    def pre_check(
        self, new_memory: Memory, claims: list[Claim] | None = None
    ) -> list[Conflict]:
        """规则级冲突预检：先按 claim 筛候选，命中即生成 `candidate` 冲突落库。

        流程：
        1. 取新记忆的 claim（未传入则**现抽**并落库——claim 是后续所有轮次的比对基础，
           即使本轮没冲突也要存下来）；
        2. **只对单值维度**（`claims.SINGLE_VALUED_PREDICATES`）做规则筛，
           多值维度（兴趣 / 技能 / 观点…）按设计跳过——人可以同时有两个爱好，
           等值比对只会制造噪声候选（实测理由见该常量的 docstring）；
        3. 逐条 claim 走 `store.find_claim_conflicts`（索引扫描）；
        4. 值实质不同 ⇒ 生成 `Conflict(detector=local, status=candidate)`；
        5. **一条都没筛出来时**才跑向量近邻补召回（`_screen_by_vector`，T16b/`ADR-0032`）：
           claim 筛是"同维度等值"的高精度档，跨维度对只能靠向量通道；
        6. 回填新记忆的 `conflict_with`（并集，幂等）。

        **一条记忆对一个既有记忆只记一条冲突**：同一对记忆在「职业」「居住地」两个维度
        同时冲突时，合成一条交给人/模型判——拆成两条会让用户对同一件事被问两遍。

        Args:
            new_memory: 刚通过 Judge 硬过滤、已（或即将）入库的新记忆。
            claims: 该记忆的 claim；None ⇒ 内部抽取（失败则降级为空，**不猜**）。

        Returns:
            本次生成的冲突候选列表（可能为空）。

        Raises:
            sqlite3.Error: 落库失败（基础设施错误必须响亮；判定逻辑本身从不抛）。
        """
        if claims is None:
            claims = self._extract_and_store_claims(new_memory)
        if claims is None:
            # 抽取**失败**（不是「没有稳定事实」）⇒ 按 ADR-0018 的「不猜」直接结束。
            # 不能继续走向量通道：那等于用一条没有 claim 的记忆去做判定，
            # 与「抽取失败不做冲突预检」的承诺冲突，也让失败率在指标上隐身。
            return []
        if not claims:
            logger.info(
                "claims_missing：记忆 %s 没有稳定事实（正常结果），claim 筛跳过，"
                "改走向量候选通道（T16b）",
                new_memory.id,
            )

        created: list[Conflict] = []
        seen_targets: set[str] = set()
        now = int(time.time())
        skipped_multi: list[str] = []
        for claim in claims:
            if claim.predicate not in SINGLE_VALUED_PREDICATES:
                # 多值维度不参与规则筛（见 claims.SINGLE_VALUED_PREDICATES 的实测理由）：
                # 同一个人完全可以同时有两个爱好，等值比对只会生成噪声候选。
                skipped_multi.append(claim.predicate)
                continue
            hits = self._store.find_claim_conflicts(
                claim.subject,
                claim.predicate,
                claim.scope,
                exclude_memory_id=new_memory.id,
            )
            for other in hits:
                if other.memory_id in seen_targets:
                    continue
                if not _values_differ(claim.value, other.value):
                    logger.debug(
                        "规则筛命中但取值实质相同，不算冲突：%s「%s」≈「%s」",
                        claim.predicate,
                        claim.value,
                        other.value,
                    )
                    continue
                conflict = Conflict(
                    id=str(uuid.uuid4()),
                    created_at=now,
                    status=ConflictStatus.CANDIDATE,
                    source_id=new_memory.id,
                    target_id=other.memory_id,
                    reason=(
                        f"「{claim.predicate}」维度取值不同：新「{claim.value}」"
                        f" vs 旧「{other.value}」"
                    ),
                    confidence=None,
                    detector=ConflictDetector.LOCAL,
                    resolution_type=None,
                    should_ask_user=False,
                )
                self._store.insert_conflict(conflict)
                seen_targets.add(other.memory_id)
                created.append(conflict)

        if not created:
            # claim 筛没筛出东西（多数情况）⇒ 交给向量补召回（T16b，`ADR-0032`）。
            # 为什么只在"空手"时才跑：claim 命中的候选已是**同维度取值不同**的高置信候选，
            # 再叠一层低阈值近邻只会把裁决调用翻倍，而召回的边际收益接近 0。
            created = self._screen_by_vector(new_memory)

        if created:
            target_ids = [c.target_id for c in created]
            merged = self._store.update_conflict_with(new_memory.id, target_ids)
            new_memory.conflict_with = merged  # 同步内存态：调用方拿到的是同一条对象
            logger.info(
                "冲突预检：记忆 %s 命中 %d 条既有记忆（%s）",
                new_memory.id,
                len(created),
                "、".join(target_ids),
            )
        elif skipped_multi:
            logger.debug(
                "记忆 %s 只有多值维度的 claim（%s），按设计不参与规则筛",
                new_memory.id,
                "、".join(skipped_multi),
            )
        return created

    def _screen_by_vector(self, memory: Memory) -> list[Conflict]:
        """向量近邻补召回：与既有记忆主题够近的，生成 `candidate` 冲突（T16b）。

        claim 等值筛只比**单值维度**，跨维度对（「不办健身卡」vs「办了健身卡」）永远
        进不了同一个桶——这是 T16 实测的结构性缺口（`ADR-0031` 后果 A）。向量通道不看
        predicate，只看「两条记忆是不是在说同一件事附近」，因此能补上这一类。

        代价控制三件套：阈值 `_vec_min`（默认 0.57）＋ top-k（默认 1）＋
        **只在 claim 筛空手时**跑。实测每写新增裁决调用 ≤0.5 次
        （`scripts/calibrate_vector_channel.py`，108 条 `cr` 语料）。

        失败**不抛**：向量表缺失 / 距离不可比只让本条退回「只有 claim 筛」，
        与 `pre_check` 的降级语义一致（宁可不判，不能判错）。
        """
        if self._vec_min <= 0 or self._vec_top_k <= 0:
            return []
        try:
            neighbours = self._store.find_similar_memories(
                memory.id, scope=memory.scope, min_cosine=self._vec_min, limit=self._vec_top_k
            )
        except Exception as exc:  # noqa: BLE001 — 候选筛失败不得阻断写入（记忆已入库）
            logger.warning(
                "向量候选筛失败（memory_id=%s）：%s ⇒ 本条只剩 claim 筛", memory.id, exc
            )
            return []
        created: list[Conflict] = []
        now = int(time.time())
        for other, cosine in neighbours:
            conflict = Conflict(
                id=str(uuid.uuid4()),
                created_at=now,
                status=ConflictStatus.CANDIDATE,
                source_id=memory.id,
                target_id=other.id,
                reason=(
                    f"向量近邻（cos={cosine:.2f}）：主题接近，需判定是矛盾还是各说各话"
                ),
                confidence=None,
                detector=ConflictDetector.LOCAL,
                resolution_type=None,
                should_ask_user=False,
            )
            self._store.insert_conflict(conflict)
            created.append(conflict)
            logger.info(
                "向量候选：记忆 %s ↔ %s（cos=%.3f，阈值 %.2f）",
                memory.id,
                other.id,
                cosine,
                self._vec_min,
            )
        return created

    def _extract_and_store_claims(self, memory: Memory) -> list[Claim] | None:
        """抽取并落库一条记忆的 claim；失败返回 `None`（**不抛**），由调用方决定降级。

        为什么失败不抛（`ADR-0018` 决策 7）：claim 是判定用的辅助结构，
        它失败不该让「用户的原话已经记住了」变成一次写失败。但也不能静默——
        `claims_failed_total` 会被写入账本记账，日志留 ERROR。

        Returns:
            claim 列表（可能为空 = 模型明确说"没有稳定事实"）；
            `None` = **抽取失败**。两者必须区分：空列表可以继续走向量候选，
            失败则按「宁可不判」直接结束（独立验收 2026-11-05 指出的混同）。
        """
        if self._claims is None:
            logger.info(
                "未注入 claim 抽取器（llm=None）：记忆 %s 只入库、不建 claim、不做冲突预检",
                memory.id,
            )
            return []
        try:
            batches = self._claims.extract([(memory.content, memory.source_quote or "")])
        except ClaimExtractionError as exc:
            self.claims_failed_total += 1
            logger.error(
                "claim 抽取失败（memory_id=%s）：%s。本条不做冲突预检——宁可不判，不能判错。",
                memory.id,
                exc,
            )
            return None
        claims = batches[0] if batches else []
        for claim in claims:
            claim.memory_id = memory.id
            claim.scope = memory.scope  # 记忆的 scope 是权威，模型不给 scope
        if claims:
            self._store.insert_claims(memory.id, claims)
        return claims

    # ---- 四分类裁决 ----

    def classify(self, conflict: Conflict) -> Conflict:
        """LLM 四分类裁决：判定 `resolution_type` 与 `should_ask_user`，回写并返回。

        失败语义（`CONVENTIONS` §9.3 + §3）：**裁决失败不猜**——
        `resolution_type` 保持 None、`should_ask_user=True`、状态留在 pending，
        让「拿不准」以「问用户」的形式暴露，而不是以「默认不算冲突」的形式消失。

        Raises:
            KeyError: 冲突引用的记忆已不存在（数据不一致，必须响亮）。
        """
        source = self._store.get_memory(conflict.source_id)
        target = self._store.get_memory(conflict.target_id)
        if source is None or target is None:
            missing = conflict.source_id if source is None else conflict.target_id
            raise KeyError(f"冲突 {conflict.id} 引用的记忆不存在：{missing}")

        if self._llm is None:
            logger.warning(
                "JUDGE 未配置：冲突 %s 保持未裁决，并标记 should_ask_user=True",
                conflict.id,
            )
            conflict.status = ConflictStatus.PENDING
            conflict.should_ask_user = True
            self._store.update_conflict(conflict)
            return conflict

        payload = {
            "new": _memory_payload(source, self._store.list_claims(source.id)),
            "old": _memory_payload(target, self._store.list_claims(target.id)),
            "rule_hint": conflict.reason,
        }
        try:
            raw = self._llm.complete_json(
                LLMRole.JUDGE,
                CLASSIFY_SYSTEM_PROMPT,
                json.dumps(payload, ensure_ascii=False),
                _ClassifyOutput,
            )
        except LLMError as exc:
            logger.error(
                "冲突裁决 LLM 失败（conflict_id=%s）：%s ⇒ 保持未裁决并标记需询问用户",
                conflict.id,
                exc,
            )
            conflict.should_ask_user = True
            self._store.update_conflict(conflict)
            return conflict

        out = raw if isinstance(raw, _ClassifyOutput) else _ClassifyOutput.model_validate(raw)
        resolution = _parse_resolution(out.resolution_type)
        if resolution is None:
            logger.warning(
                "冲突裁决返回未知取值 %r（conflict_id=%s），按 uncertain 处理",
                out.resolution_type,
                conflict.id,
            )
            resolution = ResolutionType.UNCERTAIN

        conflict.resolution_type = resolution
        conflict.confidence = _clamp_confidence(out.confidence)
        if out.reason:
            # reason 同时保留规则筛的原始依据与模型的裁决理由：后者解释"为什么这么判"，
            # 前者是"为什么会被筛进来"，用户看 `March7_conflicts` 时两样都需要。
            conflict.reason = f"{conflict.reason or ''}｜裁决依据：{out.reason.strip()[:200]}"
        if resolution is ResolutionType.UNCERTAIN:
            conflict.status = ConflictStatus.PENDING
            conflict.should_ask_user = True
        elif resolution is ResolutionType.UNRELATED:
            # 判成「不是同一件事」⇒ 结束，而不是留给用户看：它没有任何动作要做。
            # 记录仍在（审计"这条候选被查过"），但不再出现在 March7_conflicts 里。
            # 这条是 T16 实测后加的：规则筛必然带噪声，把噪声做成"待你裁决"是最坏的体验。
            conflict.status = ConflictStatus.DISMISSED
            conflict.should_ask_user = False
        else:
            conflict.status = ConflictStatus.CONFIRMED
            conflict.should_ask_user = False
        self._store.update_conflict(conflict)
        logger.info(
            "冲突裁决：%s → %s（confidence=%s, ask_user=%s）",
            conflict.id,
            resolution.value,
            conflict.confidence,
            conflict.should_ask_user,
        )
        return conflict

    def classify_all(self, conflicts: list[Conflict]) -> list[Conflict]:
        """对一批候选逐条裁决；**单条失败不打断整批**（各自降级为「待问用户」）。

        为什么需要批入口：写入管线一轮可能命中多条既有记忆（多维度冲突），
        而「一条裁决失败就把整轮写成异常」会让已入库的记忆变成一次失败返回（谎报漏记）。
        逐条 try 的代价是可能的多次调用——它由规则筛的 limit 兜住（默认每维度 20 条，
        实际每轮通常 0–1 条）。
        """
        judged: list[Conflict] = []
        for conflict in conflicts:
            try:
                judged.append(self.classify(conflict))
            except KeyError as exc:
                # 引用的记忆不存在（数据不一致）：留痕后跳过，不让整轮写入失败。
                logger.error("跳过无法裁决的冲突 %s：%s", conflict.id, exc)
            except Exception as exc:  # noqa: BLE001 — 单条失败必须降级，见上
                logger.error("冲突 %s 裁决异常，保持未裁决：%s", conflict.id, exc)
                conflict.should_ask_user = True
                try:
                    self._store.update_conflict(conflict)
                except Exception:  # noqa: BLE001 — 落库也失败时只留痕（已经是降级路径）
                    logger.error("冲突 %s 的降级标记也写不进去", conflict.id)
        return judged

    # ---- 裁决落库 ----

    def resolve(self, conflict_id: str, resolution: ResolutionType) -> Conflict:
        """裁决落库（`March7_resolve` 的引擎侧入口）。

        语义：`conflict.status → resolved`；被判失效的一方记忆 `status → superseded`
        （**不物理删除**，导出仍可查），其 claim 一并失效（`ADR-0018` 决策 6）。

        哪些裁决会让旧记忆失效——**只有两种**：
        - `direct_conflict`：同一语境互相否定，旧的必须退场；
        - `preference_evolution`：用户后来变了，旧的是过时的历史。
        `unrelated` / `context_difference` 两条记忆**都保留**（各说各话 / 各管一个语境），
        这正是「四分类」比「冲突/不冲突」二分更有用的地方：判成 unrelated 也不必删东西。
        `uncertain` 一律不自动失效，转成「问用户」。

        v1 不产出 `merged`：合并同一件事由查重（`ADR-0024`，cos ≥0.90 且字面 ≥0.80）负责，
        冲突裁决只负责「谁退场」。两者若同时生效会把一条记忆既并掉又标失效。

        Args:
            conflict_id: 冲突 id。
            resolution: 人工/自动给出的裁决类型。

        Returns:
            落库后的冲突记录。

        Raises:
            KeyError: 冲突 id 不存在。
        """
        conflict = self._store.get_conflict(conflict_id)
        if conflict is None:
            raise KeyError(f"冲突记录不存在：{conflict_id}")

        conflict.resolution_type = resolution
        if resolution is ResolutionType.UNCERTAIN:
            conflict.status = ConflictStatus.PENDING
            conflict.should_ask_user = True
            self._store.update_conflict(conflict)
            logger.warning(
                "冲突 %s 被标为 uncertain ⇒ 保持 pending 并要求询问用户（不擅自裁决）",
                conflict_id,
            )
            return conflict

        conflict.status = ConflictStatus.RESOLVED
        conflict.should_ask_user = False
        if resolution in (ResolutionType.DIRECT_CONFLICT, ResolutionType.PREFERENCE_EVOLUTION):
            target = self._store.get_memory(conflict.target_id)
            if target is None:
                logger.warning(
                    "冲突 %s 的旧记忆 %s 不存在（可能已被合并），跳过失效标记",
                    conflict_id,
                    conflict.target_id,
                )
            else:
                self._store.update_status(target.id, MemoryStatus.SUPERSEDED)
                superseded_claims = self._store.supersede_claims(target.id)
                logger.info(
                    "冲突 %s 裁决为 %s ⇒ 旧记忆 %s 标 superseded（claim 失效 %d 条），未删除",
                    conflict_id,
                    resolution.value,
                    target.id,
                    superseded_claims,
                )
        self._store.update_conflict(conflict)
        return conflict

    def list_pending(self) -> list[Conflict]:
        """列出**尚未结束**的冲突（`March7_conflicts` 的引擎侧入口）。

        「待裁决」= `candidate`（规则筛刚筛出、四分类还没跑）/ `pending`（裁决说拿不准）
        / `confirmed`（类型已判定，等用户拍板要不要让旧的退场）——三者都在等动作。
        只有 `resolved` / `dismissed` 算结束。

        为什么不是「只列 pending」（接口名沿用 `server.py` 既有签名）：只列 pending 会让
        `confirmed` 的冲突**从用户的视野里消失**——系统明明判定「你换了工作」，用户却
        永远看不到、也就永远无法确认要不要失效旧记忆。那等于把「不擅自裁决」
        做成了「悄悄攒着不告诉你」。
        """
        active = (ConflictStatus.CANDIDATE, ConflictStatus.PENDING, ConflictStatus.CONFIRMED)
        return [c for c in self._store.list_conflicts() if c.status in active]


def _memory_payload(memory: Memory, claims: list[Claim]) -> dict[str, object]:
    """裁决提示词里「一条记忆」的最小表示：摘要 + 原话 + claim 的**语义字段**。

    为什么只带 `predicate/value/status`，不带 `id/created_at/memory_id`：后者是每次
    写入都会变的随机值，混进 prompt 只会让**同一对记忆的输入每次都不同**——
    调用缓存永远命不中（实测：同一批 12 条条目重跑仍有 3 次新调用），
    而裁决结果还会因此不可复现。证据纪律要求"同一个输入得到同一个结论"，先保证输入相同。
    """
    return {
        "summary": memory.content,
        "quote": memory.source_quote,
        "claims": [
            {"predicate": c.predicate, "value": c.value, "status": c.status.value}
            for c in claims
        ],
    }


def _clamp_confidence(value: float | None) -> float | None:
    """把置信度夹到 [0, 1]；非数值/NaN 一律返回 None（宁可显示"未给"，不给假精度）。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return min(1.0, max(0.0, number))
