"""claims.py — 记忆 claim 层的抽取与 predicate 归一（内部代号：穷观阵·解扣）

「解扣」= 把一段浓缩结论拆成几个可以互相比较的扣：谁的、哪一维、值是什么。
冲突判定必须先能回答「这两条说的是不是同一维度的同一件事」，否则只能靠关键词重叠
猜——那就是 T16 之前的状态（`keywords` 恒空，规则预检永远筛不出东西）。

落点由 `ADR-0018` 定死：**独立 `claims` 表**，`Memory` 不加字段、`schemas.py` 零改动。
本模块只负责「把摘要变成 claim」这一件事：模型、词表、抽取器。
持久化在 `store.py`，判定在 `conflict.py`。

**为什么抽取是独立一次调用，而不是搭 Judge 那趟车**（David 2026-11-05 拍板，`ADR-0031`）：
1. T15b 刚把 Judge 提示词校准到 0.796 / κ=0.92（v2b=0.919），往同一个 prompt 里塞 claim 规格
   会让候选生成本身漂移 ⇒ 已付过钱的质量基线要重验；
2. 摄入缓存的文件名含**提示词指纹**（`evals/harness/ingest.cache_path`）⇒ 改 Judge prompt
   等于 214 个会话全部重抽：既再付一次钱，也把「T15b 那套已复现的候选集」整体换掉，
   0.796 与新版不再可比。独立抽取器有自己的提示词，A/B 可以继续复用旧候选；
3. 独立抽取器可被回填脚本与线上路径**共用**，predicate 归一只有一处可调。
代价：有候选的轮次多一次结构化调用（`ADR-0031` 记了口径与量级）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from March7.llm import LLMClient, LLMError, LLMRole

logger = logging.getLogger(__name__)


class ClaimStatus(str, Enum):
    """claims.status：与 `memories.status` 的失效语义对齐（失效不删除）。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    MERGED = "merged"


class Claim(BaseModel):
    """一条结构化事实：`(subject, predicate, value)` + 作用范围与有效期。

    字段与 `ADR-0018` 决策 1 的 DDL 一一对应（`id` / `memory_id` / `subject` /
    `predicate` / `value` / `scope` / `valid_from` / `valid_to` / `status` / `created_at`）。

    ⚠️ **不进 `schemas.py`**（`ADR-0018` 后果 B）：它是 T16 的内部结构，
    不是 MCP 工具面 / 冻结契约的一部分。放进宪法级文件会让「T16 改个字段」变成宪法变更。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = ""  # uuid4；空表示尚未落库，由 store 生成
    memory_id: str = ""
    subject: str = "用户"  # v1 恒为「用户」（本系统的记忆主体只有一个）
    predicate: str = "其他"  # 维度（闭集，见 PREDICATE_VOCAB）
    value: str = ""  # 该维度上的取值
    scope: str = "global"  # 与 Memory.scope 对齐（project:<name> 不跟 global 比）
    valid_from: int | None = None  # Unix 秒；None = 不设下界（v1 恒 None）
    valid_to: int | None = None  # Unix 秒；None = 当前仍有效
    status: ClaimStatus = ClaimStatus.ACTIVE
    created_at: int = 0


# ---------------------------------------------------------------------------
# predicate 闭集与别名归一
# ---------------------------------------------------------------------------

PREDICATE_VOCAB: tuple[str, ...] = (
    "职业",
    "雇主",
    "居住地",
    "家乡",
    "学历",
    "专业",
    "学校",
    "兴趣",
    "饮食偏好",
    "作息",
    "目标",
    "计划",
    "项目",
    "技能",
    "语言",
    "家庭",
    "健康",
    "财务",
    "观点",
    "其他",
)
"""predicate 的**闭集**（`ADR-0018` 后果 C 要求的「同义 predicate 归一」的落点）。

为什么要闭集而不是让模型自由生成：规则筛的第一条就是 `predicate` 等值比较。
自由词表下「职业 / 工作 / 职位 / occupation」会各成一体，索引命中率归零，
规则筛形同虚设——这与「关键词重叠」当主判据是同一类失败（看着在工作，实际全筛空）。
闭集 + 别名归一，把这一类漂移收敛到一张 20 行的表里，**可审、可测**。
"""

PREDICATE_ALIASES: dict[str, str] = {
    # 职业类
    "工作": "职业",
    "职位": "职业",
    "岗位": "职业",
    "职务": "职业",
    "occupation": "职业",
    "job": "职业",
    # 雇主类
    "公司": "雇主",
    "单位": "雇主",
    "就职单位": "雇主",
    "employer": "雇主",
    # 居住地类
    "住址": "居住地",
    "居住城市": "居住地",
    "所在地": "居住地",
    "住在": "居住地",
    "城市": "居住地",
    "location": "居住地",
    # 家乡类
    "老家": "家乡",
    "故乡": "家乡",
    "籍贯": "家乡",
    # 学历 / 专业 / 学校
    "学位": "学历",
    "最高学历": "学历",
    "degree": "学历",
    "研究方向": "专业",
    "专业方向": "专业",
    "major": "专业",
    "院校": "学校",
    "大学": "学校",
    "school": "学校",
    # 兴趣 / 饮食
    "爱好": "兴趣",
    "喜好": "兴趣",
    "hobby": "兴趣",
    "爱好偏好": "兴趣",
    "饮食": "饮食偏好",
    "口味": "饮食偏好",
    # 作息 / 财务
    "作息习惯": "作息",
    "睡眠": "作息",
    "理财": "财务",
    "收入": "财务",
    "投资": "财务",
    # 目标 / 计划
    "职业规划": "目标",
    "愿望": "目标",
    "打算": "计划",
}
"""别名 → 闭集成员。**只做单向归一，不做实体归一**（`ADR-0018` 后果 C 的 v1 口径）。

归一失败的后果是「漏判冲突」（两条本该比的 claim 没进同一个桶），**不是误判**——
方向上与 `CONVENTIONS` §9.3「拿不准就问用户」一致：宁可漏判，交给 LLM 兜底。
"""


SINGLE_VALUED_PREDICATES: frozenset[str] = frozenset(
    {"职业", "雇主", "居住地", "家乡", "学历", "专业", "学校", "语言"}
)
"""**单值维度**：同一时刻只能有一个取值，才可能"新的推翻旧的"。

这是 T16 实测出来的硬约束（2026-11-05，`ADR-0031` 决策 2）。第一版对**所有** predicate
一律做等值比对，结果在 12 条 `cr` 条目上筛出 4–12 条冲突/条目，逐条看全是噪声：
「钓鱼」「游泳」「去公园跑步」「组织周末聚会」「用某键盘」全落在「兴趣」桶里，
两两组合都被判成"同维度取值不同"⇒ 每条都要一次 LLM 裁决。**兴趣天生是多值的**
（人当然可以同时爱钓鱼和游泳），拿单值语义去比，等于把"多值"当"互斥"。

同一批数据里，真冲突（换工作）恰恰全在单值维度上 ⇒ 收窄不会削弱本任务的验收场景，
反而让"候选"重新变得可信、把每次写入的裁决调用从 4–12 次降到 0–1 次。

**不在表里的维度（兴趣 / 技能 / 观点 / 计划 / 目标 / 项目 / 家庭 / 健康 / 饮食偏好 /
财务 / 作息 / 其他）一律**不参与**规则筛**：它们可以并存，冲突与否只能靠语义判断，
而语义判断的入口是 LLM 四分类——但在没有高精度候选之前，把全库两两配给模型是烧钱。
这条取舍记在 `ADR-0031` 后果 A。
"""


def normalize_predicate(raw: str | None) -> str:
    """把模型给的 predicate 归一到闭集成员；认不出的一律落到「其他」。

    Args:
        raw: 模型输出（可能带空格 / 大小写 / 同义词）。

    Returns:
        闭集成员之一。**永不返回空串**：空 predicate 会让规则筛按空值等值匹配，
        把所有「其他」混成一个巨型桶，比归一到「其他」更糟。
    """
    text = (raw or "").strip().lower()
    if not text:
        return "其他"
    if raw is not None and raw.strip() in PREDICATE_VOCAB:
        return raw.strip()
    if text in PREDICATE_ALIASES:
        return PREDICATE_ALIASES[text]
    # 模型偶尔会输出「职业（job）」这类带注解的形式：取分隔符前的主体再试一次
    for sep in ("（", "(", "/", "|", ":", "："):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if head in PREDICATE_VOCAB:
                return head
            if head in PREDICATE_ALIASES:
                return PREDICATE_ALIASES[head]
    logger.info("claim predicate %r 不在闭集内，归一到「其他」", raw)
    return "其他"


# ---------------------------------------------------------------------------
# LLM 输出模型
# ---------------------------------------------------------------------------


class ClaimDraft(BaseModel):
    """LLM 返回的单条 claim（只有它该管的三样：下标 / 维度 / 取值）。"""

    model_config = ConfigDict(extra="ignore")

    index: int
    predicate: str = "其他"
    value: str = ""

    @field_validator("value")
    @classmethod
    def _strip_value(cls, value: str) -> str:
        """去首尾空白并截断：value 进 SQL 等值比较前必须干净（否则「产品经理 」≠「产品经理」）。"""
        return (value or "").strip()[:120]


class ClaimBatch(BaseModel):
    """一次抽取调用的结构化输出容器。"""

    model_config = ConfigDict(extra="ignore")

    claims: list[ClaimDraft] = Field(default_factory=list)


EXTRACT_SYSTEM_PROMPT = """你是 March7 记忆系统的 claim 抽取器（内部代号：穷观阵·解扣）。
你的唯一任务：把一条「记忆摘要」拆成可比较的结构化事实（claim），供冲突检测做规则筛。

## 只抽用户本人的稳定事实
- subject 固定是「用户」，你不要输出 subject。
- 第三方的事实（同事、老板娘、师兄、家人）**不抽**——那是别人的信息，不是记忆主体。
- 一次性状态（这周在考试、今天很累、刚吃完饭、上周陪甲方吃饭）**不抽**。
- 任务指令与过程叙述（「把模型换成 X」「已完成 T16」「重启了宿主」）**不抽**。

## predicate 只能取下列闭集之一，不许自创
职业 / 雇主 / 居住地 / 家乡 / 学历 / 专业 / 学校 / 兴趣 / 饮食偏好 / 作息 /
目标 / 计划 / 项目 / 技能 / 语言 / 家庭 / 健康 / 财务 / 观点 / 其他

能落到具体项就**不要**用「其他」；确实无处可归时才用「其他」。

## value 取最具体的形态
- 「产品经理」优于「换了工作」；「香港大围」优于「香港」；「手冲咖啡」优于「咖啡」。
- 不要把整句摘要抄进 value：value 要能独立回答「这一维度上是什么」。

## 输出格式（严格 JSON，不要 markdown 代码块，不要任何解释文字）
{"claims": [{"index": 0, "predicate": "职业", "value": "产品经理"}]}

- index = 输入条目的下标（从 0 开始）。
- **抽不出就整条不出现**，不要输出空 value 占位。
- 一条记忆最多 2 条 claim，且同一记忆内不许重复 predicate。
"""


MAX_CLAIMS_PER_MEMORY = 2
"""一条记忆最多挂几条 claim。

上限的理由是**规则筛的精度**而不是成本：一条记忆拆得越碎，同一条记忆自己的多个 claim
越容易互相撞上（同一记忆内已按 predicate 去重），而冲突检测要的是「这条记忆在某一维度上的
取值」，不是全文的三元组分解。2 条覆盖「职业 + 城市」这类常见复合句，再多就是噪声。
"""


class ClaimExtractionError(LLMError):
    """claim 抽取失败（重试耗尽 / JSON 不合规）。

    单独一个异常类型是为了让调用方能**区分**「模型说这条没有稳定事实」与
    「抽取这一步挂了」——前者是正常结果（空列表），后者必须留痕（`ADR-0018` 决策 7）。
    """


class ClaimExtractor:
    """把记忆摘要批量拆成 claim：一次调用处理一批，失败抛 `ClaimExtractionError`。

    **批式而不是逐条**：写入管线通常一轮产生 1–3 条记忆，逐条调用会把延迟乘上去、
    也会让 token 花在重复的 system prompt 上。批式的代价是「一条坏掉整批失败」，
    所以调用方必须按「整批失败 ⇒ 本轮记 claims_failed、不猜」来处理——
    与 `CONVENTIONS` §3「LLM 失败 ≠ 崩溃」同一套降级语义。
    """

    def __init__(self, llm: LLMClient) -> None:
        """注入 LLM client（`LLMRole.JUDGE`：与 `ADR-0018` 决策 5 的回填口径一致）。"""
        self._llm = llm

    def extract(self, items: list[tuple[str, str]]) -> list[list[Claim]]:
        """抽取一批 claim。

        Args:
            items: 每项 `(summary, source_quote)`——摘要是主体，原话只作佐证
                （模型常需要原话判断「换工作了」到底是变了还是只是一句抱怨）。

        Returns:
            与 `items` **等长且对齐**的列表；第 i 项是该条记忆的 claim 列表（可能为空）。
            列表内 claim 的 `memory_id` / `scope` 仍为空，由调用方按记忆补齐。

        Raises:
            ClaimExtractionError: LLM 调用或解析失败（重试由 `LLMClient` 内部完成一次）。
        """
        if not items:
            return []
        payload = [
            {"index": i, "summary": (summary or "").strip(), "quote": (quote or "").strip()}
            for i, (summary, quote) in enumerate(items)
        ]
        try:
            result = self._llm.complete_json(
                LLMRole.JUDGE,
                EXTRACT_SYSTEM_PROMPT,
                json.dumps({"items": payload}, ensure_ascii=False),
                ClaimBatch,
            )
        except LLMError as exc:
            raise ClaimExtractionError(f"claim 抽取失败（{len(items)} 条批次）：{exc}") from exc

        batch = result if isinstance(result, ClaimBatch) else ClaimBatch.model_validate(result)
        drafts: list[list[Claim]] = [[] for _ in items]
        for draft in batch.claims:
            if not 0 <= draft.index < len(drafts):
                # 越界下标 = 模型没有按输入对齐。静默丢弃会让「本该有的 claim 消失」，
                # 但把整批判失败又会让这一轮的记忆全部没有 claim ⇒ 留痕后丢这一条。
                logger.warning("claim 抽取返回越界下标 %d（批次长度 %d），已丢弃", draft.index, len(items))
                continue
            if not draft.value:
                continue
            predicate = normalize_predicate(draft.predicate)
            bucket = drafts[draft.index]
            if any(existing.predicate == predicate for existing in bucket):
                continue  # 同一记忆内同维度只留第一条（提示词已要求，这里是代码侧兜底）
            if len(bucket) >= MAX_CLAIMS_PER_MEMORY:
                logger.warning(
                    "claim 抽取超过每条 %d 条上限（index=%d），多余部分已丢弃",
                    MAX_CLAIMS_PER_MEMORY,
                    draft.index,
                )
                continue
            bucket.append(
                Claim(
                    id=str(uuid.uuid4()),
                    predicate=predicate,
                    value=draft.value,
                    created_at=int(time.time()),
                )
            )
        return drafts
