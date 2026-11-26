"""schemas.py — Pydantic 数据模型（引擎核心的契约层）。

字段与《March7-实现规划-v1.md》§3.2（SQL DDL）/ §3.5（MCP 工具面）/ §3.6（Judge 管线）
严格一致，**S0 起冻结**：任何字段增删改必须走 ADR（见 docs/CONVENTIONS.md §8）。

字段名约定：
- L0Profile 保留 camelCase（preferredName/isPinned/updatedAt）——与 profile.json、
  Cyrene memory-types.ts 的 L0Profile 完全一致，人类直接可读可改；
- Memory/Conflict/Episode 用 snake_case——与 §3.2 的 SQL DDL 列名一致；
- MemoryCandidate 为 §3.6 列出的 Judge 输出字段的 Python 化（camelCase → snake_case），
  与 LLM JSON 输出的映射在 judge.py 的 prompt 中完成（S3 定稿）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 枚举（取值与 §3.2 DDL 的字符串完全一致，数据库直存枚举 value）
# ---------------------------------------------------------------------------


class MemoryStatus(str, Enum):
    """memories.status：生命周期 active → aging（久未访问）→ archived；
    冲突裁决后转 superseded / merged（失效不删除，历史永远可查）。"""

    ACTIVE = "active"
    AGING = "aging"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    MERGED = "merged"


class ConflictStatus(str, Enum):
    """conflicts.status"""

    CANDIDATE = "candidate"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ResolutionType(str, Enum):
    """conflicts.resolution_type：冲突四分类 + uncertain（穷观阵的裁决输出）。"""

    UNRELATED = "unrelated"
    CONTEXT_DIFFERENCE = "context_difference"
    PREFERENCE_EVOLUTION = "preference_evolution"
    DIRECT_CONFLICT = "direct_conflict"
    UNCERTAIN = "uncertain"


class ConflictDetector(str, Enum):
    """conflicts.detector：谁发现的冲突。"""

    LOCAL = "local"
    LLM = "llm"
    MANUAL = "manual"


class MemoryLayer(str, Enum):
    """Judge 候选的落层：L0 画像 or L2 记忆库。"""

    L0 = "L0"
    L2 = "L2"


class Durability(str, Enum):
    """候选的**寿命档位**（`ADR-0052`，2026-11-13）。

    为什么要这个字段：`ADR-0028` 曾用一句话挡掉"所有任务相关内容"，结果两头都错——
    既漏掉了"项目做到哪"这类跨会话仍然成立的**状态**，也放过了"这次要第几周课件"
    这类只活一次的参数。判的不是"是不是任务"，而是"这句话活多久"。

    - `DURABLE`：与具体项目无关、跨会话长期成立（身份 / 偏好 / 红线 / 兴趣 / 环境）。
    - `PROJECT_STATE`：某个项目或课程**当前的状态或决定**（做到哪、卡在哪、下一步、放在哪）。
    - `ONE_OFF`：只对这一次请求成立的参数或授权——**下游直接拒收**。
    """

    DURABLE = "durable"
    PROJECT_STATE = "project_state"
    ONE_OFF = "one_off"


class ExportFormat(str, Enum):
    """March7_export 的导出格式。"""

    MD = "md"
    JSON = "json"


# ---------------------------------------------------------------------------
# L0 画像（~/.March7/profile.json，字段照抄 Cyrene L0Profile，人类可直接读写）
# ---------------------------------------------------------------------------


class L0Profile(BaseModel):
    """L0 用户画像——身份锚（内部代号：六相冰）。

    写入门槛（硬约束）：只有 Judge 判定 certainty == "explicit" 且
    attribution == "user_explicit" 的信息才允许写入——AI 推断的一律进不了画像。
    """

    nickname: str = ""
    preferredName: str = ""
    occupation: str = ""
    longTermInterests: str = ""
    language: str = ""
    permanentNote: str = ""
    isPinned: bool = False
    updatedAt: int = 0  # Unix 秒


# ---------------------------------------------------------------------------
# L2 记忆库（memories 表，字段对照 Cyrene L2Memory，主键 UUID）
# ---------------------------------------------------------------------------


class Memory(BaseModel):
    """L2 单条记忆——浓缩结论 + 原话证据链。

    召回时 content 与 source_quote 双注入（Cyrene 精华）：让宿主模型同时看到
    浓缩结论与用户当时的原话，减少无依据推断。
    """

    id: str  # uuid4
    content: str  # 浓缩结论
    source_quote: str | None = None  # 用户当时原话（召回时双注入）
    trigger_text: str | None = None  # 触发写入的原话短引文
    slug: str | None = None  # 精炼标题 ≤20 字（v1 用 id 前 8 位，见 §3.6 简化项）
    scope: str = "global"  # 命名空间：global / project:<name>（抄 PLUR）
    host: str | None = None  # 来源宿主：workbuddy / dsh / claude
    source_conversation_id: str | None = None
    created_at: int  # Unix 秒
    last_accessed_at: int | None = None  # Unix 秒；召回时更新（为 M3 激活度预埋）
    access_count: int = 0
    weight: float = 1.0
    is_pinned: bool = False
    status: MemoryStatus = MemoryStatus.ACTIVE
    conflict_with: list[str] = Field(default_factory=list)  # 冲突记忆 id 列表（JSON 列）
    evidence_ids: list[str] = Field(default_factory=list)  # 证据链（JSON 列）
    keywords: list[str] = Field(default_factory=list)  # 检索/冲突检测用关键词（JSON 列）


# ---------------------------------------------------------------------------
# 冲突日志（conflicts 表，对照 Cyrene ConflictLog）
# ---------------------------------------------------------------------------


class Conflict(BaseModel):
    """一条冲突记录及其裁决状态。

    `should_ask_user` 规则（CONVENTIONS §9）：默认 False（规则级预检的假阳性候选
    不该拿噪音打扰用户）；但 `resolution_type == UNCERTAIN` 时**必须**置 True——
    拿不准时不问用户就是替他做决定，违背「不擅自裁决」。
    """

    id: str  # uuid4
    created_at: int  # Unix 秒
    status: ConflictStatus
    source_id: str  # 新记忆 id
    target_id: str  # 被冲突的既有记忆 id
    reason: str | None = None
    confidence: float | None = None
    detector: ConflictDetector
    resolution_type: ResolutionType | None = None
    should_ask_user: bool = False


# ---------------------------------------------------------------------------
# 会话时间线（episodes 表，PLUR episodes.yaml 思路）
# ---------------------------------------------------------------------------


class Episode(BaseModel):
    """一次会话的时间线记录（host + conversation_id + summary）。"""

    id: str  # uuid4
    started_at: int  # Unix 秒
    ended_at: int | None = None  # Unix 秒
    host: str
    conversation_id: str
    summary: str | None = None


# ---------------------------------------------------------------------------
# Judge 管线（§3.6）：LLM 候选输出与硬过滤结果
# ---------------------------------------------------------------------------


class MemoryCandidate(BaseModel):
    """Judge LLM 输出的单个记忆候选（§3.6）。

    对应 Cyrene memory-judge 输出的 camelCase 字段（layer/field/summary/
    sourceQuote/shouldWrite/forbiddenOverclaims/…）的 Python 化形式。

    注意：certainty / attribution 的完整取值域待 S3 对照
    D:\\Cyrene-Agent-master\\src\\main\\memory\\memory-judge.ts 定稿（路径已于 2026-09-27 实测校正）；
    已知硬过滤只认 certainty == "explicit" 且 attribution == "user_explicit"。
    """

    layer: MemoryLayer
    field: str | None = None  # L0 候选的目标字段名（nickname/occupation/…）；L2 候选为 None
    summary: str  # 浓缩结论
    source_quote: str  # 用户原话（sourceQuote）
    trigger_text: str | None = None  # 触发写入的原话短引文
    should_write: bool  # shouldWrite
    certainty: str | None = None  # 如 "explicit"；取值域 S3 定稿
    attribution: str | None = None  # 如 "user_explicit"；取值域 S3 定稿
    forbidden_overclaims: list[str] = Field(default_factory=list)  # LLM 自报的过度概括
    evidence_quotes: list[str] = Field(default_factory=list)  # 证据原话（黑名单校验对象）
    durability: Durability = Durability.DURABLE
    """寿命档位（`ADR-0052`）。默认 `durable` 是**回放兼容**：旧缓存里没有这个键，
    缺省成 durable 才不会让历史缓存一读就炸或被误杀。"""


class RejectedCandidate(BaseModel):
    """被四条硬过滤之一拒收的候选——拒绝理由让宿主模型可见，形成透明感。"""

    candidate: MemoryCandidate
    rule: str  # 命中的规则标识，如 "rule_1_should_write" / "rule_2_l0_double_condition" / "rule_3_forbidden_overclaims" / "rule_4_absolute_words"
    reason: str  # 统一前缀「宁可漏记，不可误记：<具体理由>」


class JudgeOutput(BaseModel):
    """Judge LLM 一次调用的结构化输出（解析自 response_format json）。"""

    candidates: list[MemoryCandidate] = Field(default_factory=list)  # 纯问候闲聊 → 空


class WriteResult(BaseModel):
    """March7_write 的出参：写了几条、更新了哪些画像字段、拒了几条（§3.5）。

    计数口径（ADR-0003）——**L2 记忆与 L0 画像字段分开呈现，不混算**：
    - `written_count` = **仅**成功写入 L2 的记忆条数（M2/M3 对比实验测的就是这个数）；
    - `updated_l0_fields` = 成功更新的 L0 画像字段名列表（宿主可读成人话：
      「写入 2 条记忆，更新画像字段 nickname」）。

    ⚠️ **构造口径写死（ADR-0015）**：本模型**只能用** `written` / `updated_l0_fields` /
    `rejected` / `conflict_candidates` 四个**列表字段**构造；`written_count` /
    `updated_l0_count` / `rejected_count` 是**只读 `@property`**（由列表长度推导，
    不在 wire 输出里），**禁止当作构造参数传**。
    原因：Pydantic 默认 `extra="ignore"` ⇒ 传错字段名**不报错**、计数静默变 0
    （方向是「谎报漏记」，比漏记更坏）。故本模型加 `extra="forbid"`：
    传错参数立刻抛 `ValidationError`，而不是悄悄返回「写了 0 条」。
    """

    model_config = ConfigDict(extra="forbid")

    written: list[Memory] = Field(default_factory=list)  # 成功写入 L2 的记忆
    updated_l0_fields: list[str] = Field(default_factory=list)  # 成功更新的 L0 字段名
    rejected: list[RejectedCandidate] = Field(default_factory=list)  # 被拒候选及理由
    conflict_candidates: list[str] = Field(default_factory=list)  # 触发冲突预检的既有记忆 id

    @property
    def written_count(self) -> int:
        """成功写入 L2 的记忆条数（**不含** L0 画像字段，见 ADR-0003）。"""
        return len(self.written)

    @property
    def updated_l0_count(self) -> int:
        """成功更新的 L0 画像字段数（与 written_count 分列，不混算）。"""
        return len(self.updated_l0_fields)

    @property
    def rejected_count(self) -> int:
        """拒收条数。"""
        return len(self.rejected)


# ---------------------------------------------------------------------------
# MCP 工具面出参（§3.5）
# ---------------------------------------------------------------------------


class StatusResult(BaseModel):
    """March7_status 的出参：记忆条数 / 库大小 / 最近写入（+ 健康与容量指标）。

    纯出参模型 ⇒ `extra="forbid"`（ADR-0015 决策 1）：传错字段名立刻
    `ValidationError`，不静默忽略。

    `ADR-0026` 追加的四个字段是**只增不改**（都有默认值）：旧消费者读到的
    前三个键语义不变，新消费者能拿到版本/容量/熔断信息。

    S3 追加 `last_receipt` / `last_receipt_summary`（同样**只增不改**）：
    卡里 A4 要求「用户能查到这条为什么被注入」，而画像/记忆的可见性目前只有
    `March7_status` 这一个宿主可调的入口——不回执在这里，用户就只能翻 jsonl。
    """

    model_config = ConfigDict(extra="forbid")

    memory_count: int
    db_size_bytes: int
    last_write_at: int | None = None  # Unix 秒；空库为 None
    schema_version: int = 0  # 库结构版本（ADR-0021）
    recallable_count: int = 0  # status ∈ {active, aging} 的条数（能真正被召回的）
    today_writes: int = 0  # 今日已写入的 L2 条数（按 created_at ≥ 本地零点）
    daily_write_cap: int = 0  # 当前熔断阈值（0 = 不限）
    last_receipt: dict[str, Any] | None = None  # 最近一次注入的凭条（S3 / A4）
    last_receipt_summary: str = ""  # 凭条的一行中文摘要（空 = 没有凭条）

    # S11b（`ADR-0058` 决策 2）追加的**版本三元组**——同样只增不改，都有默认值。
    march7_version: str = ""  # 代码版本（`March7.__version__`）
    db_schema_version: int = 0  # 库结构版本；与 `schema_version` **恒等**，见下
    backup_available: bool = False  # 数据目录里是否存在 `.bak-v*` 备份（可恢复）

    """`db_schema_version` 与 `schema_version` 为什么两个都给（`ADR-0058` 决策 2）：

    ADR 的原文是「已有 `schema_version`（不重复加）」，但同时要求三元组的键名叫
    `db_schema_version`。两者冲突时按**只增不改**（`ADR-0015` 决策 1）处理——
    老消费者读 `schema_version`（语义不变），新的报告模板读 `db_schema_version`。
    两者恒等，并由 `test_s11b_visibility.py::test_db_schema_version_matches_schema_version` 钉住。
    删掉任何一个都会立刻打断一类消费者。"""


class ExportResult(BaseModel):
    """March7_export 的出参（ADR-0003：由裸字符串路径改为结构化结果）。

    两种宿主用法对应两种返回：
    - 程序要文件 → 只给 `path`（默认，省 token）；
    - 用户问「你记得我什么」→ `inline=True` 时额外带 `content`，宿主可直接展示。

    纯出参模型 ⇒ `extra="forbid"`（ADR-0015 决策 1）。
    """

    model_config = ConfigDict(extra="forbid")

    path: str  # 导出文件绝对路径
    memory_count: int  # 导出的记忆条数（含 superseded/merged 历史——失效不删除）
    format: ExportFormat = ExportFormat.MD
    content: str | None = None  # 仅在 inline=True 时填充；默认 None（不塞进上下文）
