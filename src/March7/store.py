"""store.py — 记忆库读写（内部代号：忆庭）
见 docs/NAMING.md —— 流光忆庭，记忆命途的收藏者。
SQLite 单文件（+ FTS5 全文索引 + sqlite-vec 向量索引）的唯一 SQL 出口：
其他模块不允许手拼 SQL（docs/CONVENTIONS.md §6）。
数据模型严格照《March7-实现规划-v1.md》§3.2 的 DDL。

设计红线（写进代码，不靠记性）：
- **失效不删除**：本模块不提供任何 `DELETE FROM memories`。冲突裁决只改 `status`
  （superseded / merged），历史永远可查（CONVENTIONS §9.2）。

⚠️ **`memories_fts` 存的是「检索投影」，不是原文**（T8 实测决定）：
SQLite FTS5 自带的 `unicode61` 分词器把整段连续中文当成**一个 token**
（实测 `MATCH '黄金'` 对「用户做黄金 ETF 交易」返回 **0 行**），而 `trigram`
分词器又要求查询串 ≥ 3 字符（实测 2 字查询 `黄金` 返回 0 行）。
中文是本项目的主场，故采用**与评测 harness 同一套 CJK bigram 分词**
（`evals/harness/baselines.py` 的 `tokenize`，Lucene CJK 惯例，免分词器、零新依赖）：
入库与查询**两侧**都把文本投影成「空格分隔的 bigram 串」再交给 FTS5。

由此带来一条**必须遵守**的纪律：
- `memories_fts` 是 `content='memories'` 的**外部内容表**，但索引里存的是投影串；
  对它执行 `('rebuild')` / `('integrity-check')` 会拿 memories 的**原文字面**重建索引
  ⇒ 中文索引立刻失效（且**静默**：不报错，只是搜不到）。
  **本模块永不调用它们，其他模块也不得调用。**
- `memories_fts` 只索引 `content` / `source_quote` 两列，且**只在插入时写入**；
  `update_status` / `touch` 不动 FTS 列，故无需触发器同步。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import struct
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import closing
from dataclasses import asdict, dataclass
from itertools import islice, takewhile
from pathlib import Path
from uuid import uuid4

from March7 import config, maintenance, privacy
from March7.claims import Claim, ClaimStatus
from March7.schemas import (
    Conflict,
    ConflictDetector,
    ConflictStatus,
    Episode,
    Memory,
    MemoryStatus,
    ResolutionType,
    StatusResult,
)

logger = logging.getLogger(__name__)

_VEC_KNN_LIMIT = 4096  # sqlite-vec 单次 KNN 查询允许的最大 k


@dataclass(frozen=True)
class ManagementDetail:
    """供控制面核对的条目、来源及适用期限。"""

    memory: Memory
    source: dict[str, str | None] | None
    expires_at: int | None
    revision: str


_MANAGEMENT_ROW = (
    "SELECT m.rowid AS management_rowid, m.*, s.source_json, e.expires_at "
    "FROM memories m LEFT JOIN memory_sources s ON s.memory_id = m.id "
    "LEFT JOIN memory_expiries e ON e.memory_id = m.id WHERE m.id = ?"
)


def _package_version() -> str:
    """`March7.__version__`（**运行时读**，不硬编码）。

    为什么不在 schemas 里给一个默认字符串：默认值会与真实版本漂移，
    而"用户报 bug 时报的版本号"是最不能漂的一个字段（`ADR-0058` 决策 2 的动机）。
    """
    try:
        import March7

        return str(getattr(March7, "__version__", "") or "")
    except Exception:  # noqa: BLE001 —— 版本读不到不该拖垮健康检查
        return ""


_BACKUP_GLOB = "*.bak-v*"
"""升级前备份的命名形状：`March7.db.bak-v<from>-<YYYYMMDDHHMMSS>`（`install.py --upgrade`）。"""


def _has_backup(data_dir: Path) -> bool:
    """数据目录里是否存在升级备份（有 ⇒ 用户在 `SchemaVersionError` 时可以照它恢复）。

    为什么用 glob 而不是维护一张备份清单：清单会漂（用户手工复制、备份被移走）。
    这里回答的是一个很窄的问题——"现在有没有一份能拿来回滚的东西"。
    """
    try:
        return any(Path(data_dir).glob(_BACKUP_GLOB))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 库结构版本（ADR-0021）
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 8
"""当前库结构版本，落在 SQLite 的 `PRAGMA user_version` 上。

**每次改 `_DDL` / `_VEC_DDL` 的字段或语义，必须 +1 并补一条迁移**（见 `_MIGRATIONS`）。
为什么必须有这个东西：T16 的 claim 层（`ADR-0018`）**必然**要加列或加表，
而在此之前库是"无版本"的——旧代码开新库、新代码开旧库，两种错配都不会报错，
只会静默读错字段（最坏情况：把 superseded 当 active 召回）。

版本历史：v1 = S0 五表 + `memory_sources` / `ingest_marks`（T14）；
v2 = 新增 `claims` 表与两个索引（T16，`ADR-0018` 决策 4）——纯建表，既有列一个不动；
v3 = 新增 `claim_backfill_marks`（T16b）——回填脚本的**尝试记录**，解决「抽不出 claim 的
记忆每次重跑都重新付费」的队列排不空问题。纯建表，既有列一个不动。
v4 = 新增 `projects` 表（S1，`ADR-0037`）——`root_hash → project_id` 的**库里映射**。
`root_hash` 只在这一张表里做等值比较（`WHERE root_hash = ?`），绝不进日志/导出/注入；
对外一律只出现 `project_id = p-<6 hex>`。纯建表，既有列一个不动。
v5 = 新增 `handoffs` / `working_items` / `memory_meta` / `host_registry` 四表（S2，`ADR-0041`）。
`working_items.seq` 用 `AUTOINCREMENT`（注入水位线的正确性，见 `ADR-0041` 决策 3）。
纯建表，既有列一个不动 ⇒ 老库升上来不需要回写，历史记忆不被追溯打标签。
v6 = 新增 `project_basenames`（S3）——`project_split_suspected` 的判据依据。
**只存目录末段（basename）与 project_id，不存 root_hash、不存路径**：
这个事件要能回答"同一个名字在 24h 内出现了第二个项目身份吗"，
而多存一个 hash 就等于把脱敏开口挪到日志面。纯建表，既有列一个不动。
v7 = `projects` 加列 `norm_version`（S3 / A5 补）——`norm_mismatch` 的**唯一**数据源。
加列必须走迁移（`ALTER TABLE ADD COLUMN`）：老行回填 0，于是"老行 vs 新代码"天然构成
一次可观测的不一致，第一次升级就会被看见。这不是"迁移方案"，是检测位（`ADR-0039` 决策 2）。
"""


class SchemaVersionError(RuntimeError):
    """库结构版本**高于**本代码支持的版本（降级打开）。

    刻意**不自动兼容**：旧代码不认识新字段，静默跑下去等于用错误的语义读取用户记忆。
    正确处置 = 升级代码，或用新版引擎导出后重建。与 `PRAGMA user_version` 的
    "前向不兼容" 语义一致（SQLite 官方也建议应用自己解释这个值）。
    """


# ---------------------------------------------------------------------------
# 并发写重试（ADR-0064）
# ---------------------------------------------------------------------------

_MAX_WRITE_ATTEMPTS = 6
"""写入失败后的最大尝试次数（含首次）。

为什么是 6：指数退避 50/100/200/400/800ms，总预算约 1.5s。
再长会拖住钩子（钩子有 120s 超时，但那是最后防线，不该靠它）。
"""

_RETRY_BASE_DELAY = 0.05
"""退避基数（秒）。第 n 次失败后睡 `base * 2**n`。"""

_TRANSIENT_ERROR_MARKERS = ("locked", "busy", "readonly")
"""判定"这是锁竞争、可以重试"的**错误文案**片段（`ADR-0064` §3.2）。

⚠️ **只认这三种**。其它 `OperationalError`（`no such table` / `no such column` /
`disk I/O error`）是真 bug，重试只会掩盖它并拖慢 6 倍。

原文依据（实测，Windows + WAL + 8 进程并发写）：
- `sqlite3.OperationalError: database is locked`
- `sqlite3.OperationalError: attempt to write a readonly database`
"""


def _is_transient_write_error(exc: BaseException) -> bool:
    """这个异常是不是"再抢一次锁就好了"的瞬态错误。

    为什么按文案判断而不是按错误码：Python 的 `sqlite3` 暴露 `sqlite_errorcode` /
    `sqlite_errorname` 是 3.11+ 的特性，但**同一个错误码会覆盖多种语义**
    （`SQLITE_BUSY` 与 `SQLITE_READONLY` 在 WAL 并发下都表现为可重试）。
    文案判断更窄，宁可少重试也不要把真 bug 吞掉。
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


def _is_connection_poisoned(exc: BaseException) -> bool:
    """这个错误是否把**这条连接**弄成了永久只读（必须重建连接）。

    ## 为什么需要这个（实测，`ADR-0064` §2.2）

    只重试**同一个连接**是没用的：

    | 策略 | 5 轮 × 8 进程结果 |
    |---|---|
    | 重试同一个连接 | 4/5 轮全成功 |
    | **每次重试重建连接** | **5/5 轮全成功** |

    原因：SQLite 在 WAL 并发下某次写失败后，会把**这条连接**标记成只读；
    之后在同一条连接上怎么重试都还是 `attempt to write a readonly database`
    （错误是**连接级状态**，不是一次性失败）。

    ⇒ `readonly` 出现时必须 `close()` 重开。`locked` / `busy` 只是锁竞争，
    同一连接再试就行（重开反而更慢）。
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    return "readonly" in str(exc).lower()


def _retry_write(operation: Callable[[], None], reconnect: Callable[[], None]) -> None:
    """执行一次写操作，遇到锁竞争按指数退避重试；耗尽则**上抛**。

    Args:
        operation: 无参可调用对象，内部自己完成"写 + commit"。
        reconnect: 无参可调用对象，**重建 self._conn**（见 `_is_connection_poisoned`）。

    Raises:
        sqlite3.OperationalError: 非瞬态错误立刻上抛；瞬态错误重试耗尽后上抛。
            调用方（钩子 / MCP 工具）负责记日志；本函数**不吞异常**。

    ⚠️ 为什么是"重试"而不是"进程间锁"：前者每个连接自己负责，不依赖所有调用方
    遵守同一把锁（`control` 命令 / 三个宿主的钩子 / MCP server 都得改才有效）。
    详见 `ADR-0064` §3.3。

    ## 反证开关 `MARCH7_DISABLE_WRITE_RETRY`

    设成 `1` 时**只执行一次、不重试也不重建连接**——那是修复前的行为。

    ⚠️ **它只为反证而存在**（`scripts/verify_concurrent_test.py`）：一个抓不住 bug 的
    测试比没有测试更糟（它给的是虚假安全感）。要让并发用例"在关掉修复后变红"，
    就必须有一个**能被外部关掉**的开关——而子进程是独立 Python 进程，
    父进程里的 `monkeypatch` 传不过去，只能靠环境变量。

    正常运行时**不要设它**；设了等于关掉这个 bug 的修复。
    """
    if os.environ.get("MARCH7_DISABLE_WRITE_RETRY") == "1":
        operation()
        return

    for attempt in range(_MAX_WRITE_ATTEMPTS):
        try:
            operation()
            return
        except sqlite3.OperationalError as exc:
            if not _is_transient_write_error(exc) or attempt == _MAX_WRITE_ATTEMPTS - 1:
                raise
            delay = _RETRY_BASE_DELAY * (2**attempt)
            poisoned = _is_connection_poisoned(exc)
            logger.warning(
                "写入遇到锁竞争%s，%.0fms 后重试（第 %d/%d 次）：%s",
                "（连接已只读，将重开）" if poisoned else "",
                delay * 1000,
                attempt + 1,
                _MAX_WRITE_ATTEMPTS - 1,
                exc,
            )
            time.sleep(delay)
            if poisoned:
                # 必须重建连接：`readonly` 是连接级状态，同一条连接重试永远不会好。
                reconnect()

@dataclass(frozen=True)
class EraseScopeResult:
    """按项目/会话整批删除的**计数**结果（S5）。

    只放计数，**不放任何内容**：这个对象会进回执与 CLI 输出，
    带一个 content 字段就等于把要删的东西又写了一遍。
    """

    deleted_working: int = 0
    deleted_handoffs: int = 0
    deleted_ingest_marks: int = 0

    @property
    def total(self) -> int:
        """三张表合计删了多少行。"""
        return self.deleted_working + self.deleted_handoffs + self.deleted_ingest_marks

    def as_dict(self) -> dict[str, int]:
        """扁平字典（回执 / CLI 用）。"""
        return {
            "deleted_working": self.deleted_working,
            "deleted_handoffs": self.deleted_handoffs,
            "deleted_ingest_marks": self.deleted_ingest_marks,
        }


MIGRATIONS: dict[int, "Callable[[sqlite3.Connection], None]"] = {}
"""迁移表：`{源版本: 从该版本升到 +1 的处理函数}`。

为什么大多数情况是空表也能工作：`_DDL` 全是 `CREATE TABLE IF NOT EXISTS`
⇒ **新增表/新增索引**由建表语句自动补齐；迁移函数只负责 DDL 表达不了的事
（给已有表加列、回填数据、重建索引）。

**T16 的 claim 层已定形态**（`ADR-0018`，2026-11-03）：**新建 `claims` 表**，
不给 `memories` 加列 ⇒ 在这里注册 `1: _migrate_v2_claims`（建表 + 索引，幂等），
并把 `SCHEMA_VERSION` 改成 2。`Memory` 与 `schemas.py` 都不动。
"""


# ---------------------------------------------------------------------------
# CJK 检索分词（投影用）
# 与 evals/harness/baselines.py 的 tokenize() 保持同一套规则：
# CJK 连续段切 bigram（单字成段则保留单字），ASCII 连续段取词并小写。
# 引擎不许 import 评测 harness（CONVENTIONS §2），故此处独立实现同一算法。
# ---------------------------------------------------------------------------

_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 统一表意文字
    (0xF900, 0xFAFF),  # CJK 兼容表意文字
)
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+")


def _is_cjk(ch: str) -> bool:
    """该字符是否属 CJK 表意文字区（决定是否切 bigram）。"""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _bigrams(chars: list[str]) -> list[str]:
    """CJK 连续段切 bigram（Lucene CJK 惯例，与 baselines.py 一致）；单字成段则保留该单字。

    注意：长段内的字**只以 bigram 形式入索引**（如「广东菜」→「广东」「东菜」），
    故孤立的单字查询（如「菜」）**命不中**该段。这是 bigram 方案的固有取舍
    （换来的是「2 字词可查 + 零新依赖」），**不是 bug**。
    """
    if len(chars) == 1:
        return [chars[0]]
    return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]


def _tokenize(text: str) -> list[str]:
    """检索分词：CJK 连续段切 bigram，ASCII 连续段取词并小写。"""
    tokens: list[str] = []
    run: list[str] = []
    for ch in text:
        if _is_cjk(ch):
            run.append(ch)
        elif run:
            tokens.extend(_bigrams(run))
            run = []
    if run:
        tokens.extend(_bigrams(run))
    tokens.extend(w.lower() for w in _ASCII_WORD.findall(text))
    return tokens


def _fts_projection(text: str | None) -> str:
    """把文本投影成「空格分隔的 token 串」，交给 FTS5 的 unicode61 分词器落索引。"""
    if not text:
        return ""
    return " ".join(_tokenize(text))


def _fts_query(text: str) -> str:
    """把用户查询投影成 FTS5 MATCH 表达式（每个 token 加引号 = 只当字面词，隐式 AND）。

    每个 token 都被双引号包住，且 token 只含汉字/字母/数字，
    不含 FTS5 保留字（AND/OR/NOT/NEAR）与特殊符号，故不会被误解析。
    """
    return " ".join(f'"{t}"' for t in _tokenize(text))


# ---------------------------------------------------------------------------
# §3.2 DDL（唯一出处）
# ---------------------------------------------------------------------------

_MEMORY_COLUMNS = (
    "id, content, source_quote, trigger_text, slug, scope, host, "
    "source_conversation_id, created_at, last_accessed_at, access_count, weight, "
    "is_pinned, status, conflict_with, evidence_ids, keywords"
)
_MEMORY_PLACEHOLDERS = ", ".join(["?"] * 17)

_CLAIMS_DDL = """
-- claim 层（ADR-0018 决策 1，T16 落地）：一行一个 claim，一条记忆可挂多行。
-- **独立表而不是给 memories 加 JSON 列**：T16 的规则筛是「主体同 + 属性同 + 范围同 +
-- 时间重叠」的等值/区间比较，JSON 列只能全表扫、也只能把 JSON 拉回 Python 再比
-- （等于把可单测的规则又写回一段只有作者看得懂的代码）。而 `Memory` 加字段会触发
-- 宪法级变更（CONVENTIONS §8），出参也已冻结（ADR-0003）。
CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL,        -- 外键 → memories.id（不建 FK 约束：失效不删除，历史要可查）
  subject TEXT NOT NULL,          -- 事实主体（v1 恒为「用户」）
  predicate TEXT NOT NULL,        -- 维度（闭集，见 claims.PREDICATE_VOCAB）
  value TEXT NOT NULL,            -- 该维度上的取值
  scope TEXT NOT NULL DEFAULT 'global',   -- 与 memories.scope 对齐
  valid_from INTEGER,             -- Unix 秒；NULL = 不设下界（v1 恒 NULL）
  valid_to INTEGER,               -- Unix 秒；NULL = 当前仍有效
  status TEXT NOT NULL DEFAULT 'active',  -- active|superseded|merged
  created_at INTEGER
);

-- 索引按「规则筛」设计（ADR-0018 决策 2）：主查询 = 同主体 + 同属性 + 同范围 + 仍有效，
-- 一次索引扫描即可；另一个索引供「按记忆取 claim」（回填脚本、导出、单测）。
CREATE INDEX IF NOT EXISTS idx_claims_screen ON claims(subject, predicate, scope, status);
CREATE INDEX IF NOT EXISTS idx_claims_memory ON claims(memory_id);
"""

_BACKFILL_OUTCOMES = frozenset({"filled", "empty", "failed"})
"""回填结果的三种取值（校验放在 store 层：写错 outcome 会让队列永远排不空）。"""

_BACKFILL_DDL = """
-- 回填尝试记录（T16b，库 v3）：一行 = 一条记忆被回填脚本处理过。
-- **为什么必须有**：`_pending_memories` 原先只筛「active 且无 claim」，而抽取器按提示词
-- 拒掉「任务流水账」是**正常结果**（dev 库 41 条里 35 条如此）⇒ 这些记忆永远留在队列里，
-- 每次重跑都重新付一遍 LLM 调用。记下 outcome 后，脚本可以只重试 `failed` 的批次。
-- 表独立于 `claims`：抽不出 claim 的记忆**不能**因为「没写成功」而被当成没试过。
CREATE TABLE IF NOT EXISTS claim_backfill_marks (
  memory_id TEXT PRIMARY KEY,
  attempted_at INTEGER NOT NULL,
  outcome TEXT NOT NULL           -- filled | empty | failed
);
"""

_PROJECTS_DDL = """
-- 项目身份映射（S1，库 v4 / ADR-0037）：`root_hash → project_id`。
-- **为什么需要一张表**：路径归一化之后要能判断「同一个项目」——跨会话、跨工具。
-- 判断必须发生在**库里**（等值比较），因为 `root_hash` 是路径的指纹，一旦出现在
-- 日志/导出/注入里就等于把路径泄了出去 ⇒ 表是它的唯一容身处。
-- **为什么 root_hash 不进内存比较**：字符串比较可以散落在十来处，每一处都是一次泄露机会；
-- 只有 `WHERE root_hash = ?` 这一种用法，grep 就能审计完。
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,      -- p-<6 hex>，对外假名
  root_hash TEXT NOT NULL UNIQUE,   -- 归一化根的散列，只在本表内比较
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  -- 归一化规则的版本位（S3 / v7）：`norm_mismatch` 的**唯一**数据源。
  -- 0 = 本列落地前写下的行（v4/v5 时代），迁移时回填 0；
  -- 于是"老行 vs 新代码（`privacy.NORM_VERSION`）"天然构成一次可观测的不一致。
  norm_version INTEGER NOT NULL DEFAULT 0
);
"""

_HANDOFFS_DDL = """
-- 显式接力窗口（S2，库 v5 / ADR-0038 决策 5）。一行 = 一次「开始 handoff … 结束」。
-- **为什么要显式窗口而不是时间窗**：working 层的内容是「当前正在做的事」，
-- 「多久算当前」没有可验收的答案（7 天？下班？）——用显式开关替代，它是**可验证**的：
-- `open` 才注入、关闭即不注入（S2 的 A3 就是这么判的）。
CREATE TABLE IF NOT EXISTS handoffs (
  project_id TEXT,                    -- p-<6 hex>；NULL = 只做 global（P2 不产生）
  handoff_id TEXT PRIMARY KEY,
  started_by_host TEXT NOT NULL,      -- workbuddy / dsh
  state TEXT NOT NULL CHECK(state IN ('open','pending_close','closed')),
  started_at INTEGER NOT NULL,
  closed_at INTEGER,
  reviewed_by TEXT                    -- 必须有值才能 closed（谁确认的）
);

-- **单 open 唯一性交给数据库**：应用层判「已经有一个 open 了」在并发下必然漏
-- （两个宿主同时开 ⇒ 两次 SELECT 都看到 0 行）⇒ 用部分唯一索引，第二次 INSERT 直接失败。
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_handoff
  ON handoffs(project_id) WHERE state = 'open';
"""

_WORKING_ITEMS_DDL = """
-- 工作层（S2，库 v5 / ADR-0039 决策 1）。一行 = 一条「当前项目正在发生的事」。
-- **与 memories 分表而不是加 category**：两者生命周期相反——长期记忆无自动过期、
-- 经 Judge 双条件；工作层随 handoff 关闭即失效、由 UPS 规则抽取（不走 LLM）。
-- 塞进同一张表就必须给每个查询都加 `WHERE category != 'working'`，漏一处就串味。
CREATE TABLE IF NOT EXISTS working_items (
  -- ⚠️ **AUTOINCREMENT 不是风格问题，是注入水位线的正确性**（ADR-0041 决策 3）：
  -- 裸 INTEGER PRIMARY KEY 会复用 max(rowid)+1 ⇒ 删掉最大 seq 后新条目拿到旧号
  -- ⇒ `seq <= watermark` 把「本轮刚写的」误判成「之前写的」⇒ 当轮自我注入。
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  host TEXT NOT NULL,
  session_id TEXT NOT NULL,
  message_id TEXT,
  handoff_id TEXT NOT NULL,           -- 当前活跃 handoff
  project_id TEXT,                    -- NULL = global-only，不注入项目块
  kind TEXT NOT NULL,                 -- constraint / state / scratch（ADR-0039 闭集）
  content TEXT NOT NULL,
  source_quote TEXT,
  content_hash TEXT NOT NULL,         -- sha256(NFKC + strip)；只做「同文本」去重
  event_ts INTEGER NOT NULL,          -- 宿主时间优先
  status TEXT NOT NULL DEFAULT 'active',  -- active / closed（P2 只有两个生产者）
  created_at INTEGER NOT NULL,
  -- 幂等：同 handoff 内完全相同的文本只留一条（A4）
  UNIQUE(handoff_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_working_project
  ON working_items(project_id, status, seq DESC);
CREATE INDEX IF NOT EXISTS idx_working_handoff
  ON working_items(handoff_id, status, seq DESC);
"""

_MEMORY_META_DDL = """
-- 长期记忆的旁挂元数据（S2，库 v5 / ADR-0039 决策 1）。
-- **独立表而不是给 memories 加列**：`schemas.py` 是宪法级，加列会改 `Memory` 出参
-- （ADR-0003 已冻结）；而这张表**全部字段可空/有默认值** ⇒ 老库升上来是空表，不需要回填，
-- 也不会因为"猜"给历史记忆打错标签。
CREATE TABLE IF NOT EXISTS memory_meta (
  memory_id TEXT PRIMARY KEY,         -- 外键 → memories.id（不建 FK：失效不删除，历史要可查）
  category TEXT,                      -- **可空**：profile / project（ADR-0039 闭集）；NULL = 未分类
  subtype TEXT,                       -- decision / constraint / preference / fact（**不作判据**）
  verification TEXT NOT NULL DEFAULT 'unverified',   -- unverified / confirmed
  sensitivity TEXT NOT NULL DEFAULT 'normal',
  salience_reason TEXT,               -- 为什么预计未来会用到
  last_confirmed_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meta_category ON memory_meta(category, verification);
"""

_HOST_REGISTRY_DDL = """
-- 宿主登记（S2，库 v5 / ADR-0038 决策 1）。回答一个问题：
-- **两个宿主是不是指向同一个库？** 这是对 MARCH7_HOME 分叉事故的结构性防线。
CREATE TABLE IF NOT EXISTS host_registry (
  host TEXT PRIMARY KEY,              -- workbuddy / dsh
  data_dir_key TEXT NOT NULL,         -- 数据目录的**哈希**（不存绝对路径）
  schema_version INTEGER NOT NULL,    -- 该宿主上次见到时库的 user_version
  -- `{cwd_hash: project_id}` **映射**，不是单一哈希值：
  -- 每宿主一行、每行一个值的话，「同一 cwd 在两宿主映射到不同 project」永远比不出来
  -- （duplicate_cwd_hash 就成了纸糊的检测）。
  cwd_hashes_json TEXT,
  project_source TEXT,                -- 最近一次解析来源：git / cwd / transcript / manual
  last_seen_at INTEGER NOT NULL
);
"""

_MEMORY_V5_DDL = _HANDOFFS_DDL + _WORKING_ITEMS_DDL + _MEMORY_META_DDL + _HOST_REGISTRY_DDL

_PROJECT_BASENAMES_DDL = """
-- 项目目录末段登记（S3，库 v6）。`project_split_suspected` 的判据依据。
--
-- **为什么需要它**：判断"一个项目被拆成两个"必须有一个**与路径无关**的共同标识。
-- basename（目录末段）是唯一能当这个标识而又不泄露路径的东西：
-- `D:\\work\\march7` 与 `E:\\backup\\march7` 的 basename 都是 `march7`，
-- 而一旦把完整路径存进来，这张表就变成了"路径 → 项目"的明文索引。
--
-- ⚠️ **只存 basename**，不存 root_hash、不存路径前缀。理由与 `projects` 相反：
-- `projects` 表里 root_hash 的**唯一**用法是 `WHERE root_hash = ?` 等值比较；
-- 而这张表的用途是"按名字找兄弟"，存 hash 只会多一个能被 grep 拼回去的指纹。
CREATE TABLE IF NOT EXISTS project_basenames (
  basename TEXT NOT NULL,           -- 目录末段（大小写已归一）
  project_id TEXT NOT NULL,         -- p-<6 hex>（假名）
  first_seen_at INTEGER NOT NULL,   -- 首次见到该 (basename, project_id) 的时刻
  PRIMARY KEY (basename, project_id)
);
CREATE INDEX IF NOT EXISTS idx_basenames_seen ON project_basenames(basename, first_seen_at);
"""

_MEMORY_V6_DDL = _PROJECT_BASENAMES_DDL

_MAINTENANCE_DDL = """
CREATE TABLE IF NOT EXISTS maintenance_scopes (
  scope TEXT PRIMARY KEY,
  automatic INTEGER NOT NULL DEFAULT 0,
  version TEXT,
  checked_at INTEGER NOT NULL DEFAULT 0,
  projection TEXT,
  findings TEXT
);
CREATE TABLE IF NOT EXISTS maintenance_changes (
  scope TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  dirty INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(scope, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_dirty ON maintenance_changes(scope, dirty);
CREATE TABLE IF NOT EXISTS maintenance_batches (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  owner TEXT NOT NULL,
  binding TEXT NOT NULL,
  version TEXT NOT NULL,
  kind TEXT NOT NULL,
  parent_id TEXT,
  deadline INTEGER NOT NULL,
  state TEXT NOT NULL,
  targets TEXT NOT NULL,
  applied_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_maintenance_receipts ON maintenance_batches(scope, applied_at);
"""


def _maintenance_triggers() -> str:
    """只给已经检查或启用的范围登记变动，召回计数不触发检查。"""
    columns = (
        "id, scope, content, source_quote, trigger_text, slug, host, source_conversation_id, "
        "created_at, weight, is_pinned, status, conflict_with, evidence_ids, keywords"
    )
    statements: list[str] = []
    for table in ("memories", "memory_sources", "memory_expiries", "claims", "conflicts"):
        events = (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",)))
        for event, rows in events:
            action = f"{event} OF {columns}" if table == "memories" and event == "UPDATE" else event
            selects: list[str] = []
            for row in rows:
                if table == "memories":
                    selects.append(f"SELECT {row}.scope AS scope, {row}.id AS id")
                elif table == "conflicts":
                    selects.append(f"SELECT scope, id FROM memories WHERE id IN ({row}.source_id, {row}.target_id)")
                else:
                    selects.append(f"SELECT scope, id FROM memories WHERE id = {row}.memory_id")
            selection = " UNION ".join(selects)
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS maintenance_{table}_{event.lower()} AFTER {action} ON {table} BEGIN "
                "INSERT INTO maintenance_changes(scope, memory_id) "
                f"SELECT affected.scope, affected.id FROM ({selection}) affected "
                "JOIN maintenance_scopes tracked ON tracked.scope = affected.scope "
                "ON CONFLICT(scope, memory_id) DO UPDATE SET generation = generation + 1, dirty = 1; END;"
            )
    return "\n".join(statements)


_DDL = """
-- 主表（字段对照 Cyrene L2Memory，去 Electron 耦合，主键 UUID）
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,            -- uuid4
  content TEXT NOT NULL,          -- 浓缩结论
  source_quote TEXT,              -- 用户当时原话（召回时双注入，防 AI 替你回忆）
  trigger_text TEXT,              -- 触发写入的原话短引文
  slug TEXT,                      -- 精炼标题 ≤20 字（导出文件名/锚点）
  scope TEXT DEFAULT 'global',    -- 命名空间：global / project:<name>（抄 PLUR）
  host TEXT,                      -- 来源宿主：workbuddy / dsh / claude
  source_conversation_id TEXT,
  created_at INTEGER, last_accessed_at INTEGER,
  access_count INTEGER DEFAULT 0, weight REAL DEFAULT 1.0,
  is_pinned INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',   -- active|aging|archived|superseded|merged
  conflict_with TEXT,             -- JSON: 冲突记忆 id 列表
  evidence_ids TEXT,              -- JSON: 证据链
  keywords TEXT                   -- JSON: 检索/冲突检测用关键词
);

-- 全文检索（BM25）。索引内容 = _fts_projection() 的检索投影，见模块 docstring。
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content, source_quote, content='memories', content_rowid='rowid'
);

-- 冲突日志（对照 Cyrene ConflictLog）
CREATE TABLE IF NOT EXISTS conflicts (
  id TEXT PRIMARY KEY, created_at INTEGER,
  status TEXT,                    -- candidate|pending|confirmed|resolved|dismissed
  source_id TEXT, target_id TEXT,
  reason TEXT, confidence REAL,
  detector TEXT,                  -- local|llm|manual
  resolution_type TEXT,           -- unrelated|context_difference|preference_evolution|direct_conflict|uncertain
  should_ask_user INTEGER
);

-- 会话时间线（episode，PLUR 的 episodes.yaml 思路）
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY, started_at INTEGER, ended_at INTEGER,
  host TEXT, conversation_id TEXT, summary TEXT
);

-- 来源四元组（ADR-0016 决策 3，T14 落地）：{session_id, message_id, revision, quote}
-- **独立表而不是给 memories 加列** ——「JSON 存，不改表结构」是 ADR-0016 的硬约束；
-- 加列会改 `memories` 的字段模型，而 `schemas.py` 是宪法级（字段增删要走 ADR）。
-- `source_json` 是四元组的**唯一真相源**（JSON 文本，ensure_ascii=False 保持人类可读）；
-- `session_id` / `message_id` 两列是从中派生的**查询键**，只为免依赖 SQLite 的 JSON1
-- 扩展就能按「哪条宿主消息」查，不是第二份真相源。
CREATE TABLE IF NOT EXISTS memory_sources (
  memory_id TEXT PRIMARY KEY,
  session_id TEXT,
  message_id TEXT,
  source_json TEXT NOT NULL,
  created_at INTEGER
);

-- 写入水位线（宿主每轮 Stop 的幂等判据）：记「哪些宿主消息已经被处理过」。
-- **为什么独立于 memory_sources**：一轮里若**只**产生 L0 画像更新（没有 L2 记忆），
-- 来源表里就一行都没有 ⇒ 用来源表判重会失效 ⇒ 同一轮被反复处理
-- （重复烧 LLM 调用 + 重复写画像）。水位线与「是否产生了记忆」无关，故独立成表。
-- **为什么是集合而不是「最后一条」**：一轮里用户可能连发多条消息（宿主只触发一次 Stop），
-- 只记最后一条会漏掉前面的；而集合天然覆盖「多消息」「编辑后新 id」两种情况。
CREATE TABLE IF NOT EXISTS ingest_marks (
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  created_at INTEGER,
  PRIMARY KEY (session_id, message_id)
);

-- 只有用户设置时才有明示期限，老记忆没有默认寿命。
CREATE TABLE IF NOT EXISTS memory_expiries (
  memory_id TEXT PRIMARY KEY,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expiries_due ON memory_expiries(expires_at);


-- DSH 只登记用户消息摘要，支持来源对账及短期保留。
CREATE TABLE IF NOT EXISTS dsh_source_proofs (
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  project_id TEXT,
  digest TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_dsh_proofs_expiry ON dsh_source_proofs(expires_at);

-- 已核验的每条记忆保存判重摘要，和短期的全消息鉴权见证分开。
CREATE TABLE IF NOT EXISTS dsh_memory_evidence (
  memory_id TEXT PRIMARY KEY,
  digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dsh_memory_evidence_digest ON dsh_memory_evidence(digest);

-- 宿主重放同一事件时，不能按变化后的条件再删除一次。
CREATE TABLE IF NOT EXISTS conversation_operations (
  event_key TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL
);

-- 宿主每条原生用户轮次提供本机授权上下文，只保存摘要与计划引用。
CREATE TABLE IF NOT EXISTS dialogue_turns (
  session_key TEXT PRIMARY KEY,
  project_id TEXT,
  event_key TEXT NOT NULL,
  nonce TEXT NOT NULL UNIQUE,
  binding TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  prompt_key TEXT NOT NULL,
  operation TEXT,
  reference TEXT
);

-- 原生确认的回执快照随事务提交，迟到的重放不会拿到后来改过的状态。
CREATE TABLE IF NOT EXISTS dialogue_results (
  event_key TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  project_id TEXT,
  binding TEXT NOT NULL,
  prompt_key TEXT NOT NULL,
  batch TEXT NOT NULL
);

-- 宿主会话初次观察到的项目身份。只存会话和假名，跨进程比较时无需读取正文。
CREATE TABLE IF NOT EXISTS session_project_bindings (
  host TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  PRIMARY KEY(host, session_id)
);
CREATE TABLE IF NOT EXISTS session_project_drifts (
  host TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  PRIMARY KEY(host, session_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_session_project_drifts_project ON session_project_drifts(project_id);

-- 查询加速索引（不改变 §3.2 的字段模型，只加访问路径）
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, created_at);
CREATE INDEX IF NOT EXISTS idx_conflicts_status ON conflicts(status);
CREATE INDEX IF NOT EXISTS idx_memory_sources_session ON memory_sources(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_sources_message ON memory_sources(message_id);
""" + _CLAIMS_DDL + _BACKFILL_DDL + _PROJECTS_DDL + _MEMORY_V5_DDL + _MEMORY_V6_DDL


def _migrate_v2_claims(conn: sqlite3.Connection) -> None:
    """v1 → v2：建 `claims` 表与索引（`ADR-0018` 决策 4）。

    为什么 `_DDL` 里已经有同一段 DDL，这里还要再执行一次：`_DDL` 的
    `CREATE TABLE IF NOT EXISTS` 只在**打开库时**跑，而迁移表负责「任何旧版本升上来
    都落到同一个结构」。重复执行是幂等的，代价是十几行字符串（共用 `_CLAIMS_DDL`
    常量，不存在两份会漂移的字面量），换来的是**版本路径可审计**。

    ⚠️ **用 `execute` 逐句执行，不用 `executescript`**：`executescript` 会先隐式
    COMMIT，把 `_migrate` 开的 `BEGIN IMMEDIATE` 事务切断 ⇒ 迁移失败时无法回滚。
    """
    for statement in _CLAIMS_DDL.split(";"):
        if statement.strip():
            conn.execute(statement)


MIGRATIONS[1] = _migrate_v2_claims
"""v1 → v2 的迁移已注册（`ADR-0018` 决策 4）。

为什么在函数定义之后注册，而不是写在顶部 `MIGRATIONS` 字面量里：函数必须先存在。
表本身仍是「源版本 → 目标版本」的映射，语义不变。
"""


def _migrate_v3_backfill(conn: sqlite3.Connection) -> None:
    """v2 → v3：建 `claim_backfill_marks`（T16b）。

    同 `_migrate_v2_claims` 的理由与写法：复用 `_BACKFILL_DDL` 字面量、逐句 `execute`
    （不用 `executescript`，否则会切断 `_migrate` 的事务）。
    纯建表——既有表一列不动，旧数据因此不需要任何回写。
    """
    for statement in _BACKFILL_DDL.split(";"):
        if statement.strip():
            conn.execute(statement)


MIGRATIONS[2] = _migrate_v3_backfill
"""v2 → v3 已注册（T16b）。"""


def _migrate_v4_projects(conn: sqlite3.Connection) -> None:
    """v3 → v4：建 `projects` 表（S1，`ADR-0037`）。

    同前两条迁移的写法：复用 `_PROJECTS_DDL` 字面量、逐句 `execute`（不用 `executescript`）。
    纯建表——既有表一列不动 ⇒ 老库升上来不需要任何回写，历史记忆也**不**会被
    追溯打上项目标签（那是猜；项目归属只对**新写入**成立）。
    """
    for statement in _PROJECTS_DDL.split(";"):
        if statement.strip():
            conn.execute(statement)


MIGRATIONS[3] = _migrate_v4_projects
"""v3 → v4 已注册（S1 / `ADR-0037`）。"""


def _migrate_v5_working(conn: sqlite3.Connection) -> None:
    """v4 → v5：建 `handoffs` / `working_items` / `memory_meta` / `host_registry`（S2）。

    同前三条迁移的写法：复用 `_MEMORY_V5_DDL` 字面量、逐句 `execute`（不用 `executescript`）。
    **纯建表** —— 既有五张表一列不动 ⇒ 老库升上来不需要任何回写，历史记忆既不会被
    追溯打上 `category`，也不会被追溯归进某个 handoff（那是猜）。

    降级路径见 `scripts/downgrade_schema.py`（`ADR-0041` 决策 2）：`DROP` 这四张表
    不减少任何一条用户记忆的可召回性，因为 v4 代码根本不读它们。
    """
    for statement in _MEMORY_V5_DDL.split(";"):
        if statement.strip():
            conn.execute(statement)


MIGRATIONS[4] = _migrate_v5_working
"""v4 → v5 已注册（S2 / `ADR-0041`）。"""


def _migrate_v6_basenames(conn: sqlite3.Connection) -> None:
    """v5 → v6：建 `project_basenames`（S3）。

    同前几条迁移的写法：复用 `_PROJECT_BASENAMES_DDL` 字面量、逐句 `execute`。
    **纯建表** —— 既有六张表一列不动 ⇒ 老库升上来是空表；
    它不参与任何注入判据，只是诊断事件的数据源，空表意味着"还没有可比较的兄弟"。
    """
    for statement in _PROJECT_BASENAMES_DDL.split(";"):
        if statement.strip():
            conn.execute(statement)


MIGRATIONS[5] = _migrate_v6_basenames
"""v5 → v6 已注册（S3）。"""


def _migrate_v7_norm_version(conn: sqlite3.Connection) -> None:
    """v6 → v7：`projects` 加列 `norm_version`（S3 / A5 补）。

    为什么这一条**必须有迁移函数**（而前面几条都是"纯建表"）：
    `CREATE TABLE IF NOT EXISTS` 对已存在的表**完全无效**——它不会补列。
    v4 建出来的 `projects` 里没有 `norm_version`，不 `ALTER TABLE` 就永远读不到。

    回填 0 而不是当前版本：0 的语义是"本列落地前写下的行"，于是它和
    `privacy.NORM_VERSION`（从 1 起）天然不一致 ⇒ 第一次跑就会记一条 `norm_mismatch`，
    提醒"这些历史映射是按哪一版规则算的没人知道"。回填成当前版本会把这个信息**抹掉**。

    ⚠️ 加列是幂等的（先查 `PRAGMA table_info`），重复执行不会报错；
    但**版本号只升一次**，所以正常路径不会走到第二次。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    if "norm_version" not in columns:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN norm_version INTEGER NOT NULL DEFAULT 0"
        )


MIGRATIONS[6] = _migrate_v7_norm_version
"""v6 → v7 已注册（S3 / A5）。"""

HANDOFF_OPEN = "open"
HANDOFF_PENDING_CLOSE = "pending_close"
HANDOFF_CLOSED = "closed"

_HANDOFF_STATES = frozenset({HANDOFF_OPEN, HANDOFF_PENDING_CLOSE, HANDOFF_CLOSED})
"""handoff 三态（`ADR-0038` 决策 5）。`open` 才注入 working；另两态 0 注入。"""

WORKING_KINDS = ("constraint", "state", "scratch")
"""working 层的 kind 闭集（`ADR-0039` 决策 1）。

**闭集的意义**：每个值必须有可验收的注入差异或生命周期差异。这三个的差异在
「注入条数上限」与「重要度」上（constraint/state ≤2 条、scratch ≤2 条且只有命中才注入）。
加第四个值之前先回答「它和现有三个在注入行为上差在哪」。"""

_WORKING_KINDS = frozenset(WORKING_KINDS)


def working_content_hash(text: str) -> str:
    """working 条目的去重键：`sha256(NFKC + strip)`。

    **只做「完全相同文本」去重**，不做语义去重（`ADR-0042` §2 取舍 1）：
    语义去重要嵌入 ⇒ UPS 每轮加载 BGE-M3（实测 7.5s），代价远大于收益。

    NFKC 是必要的：全角/半角数字与标点在中文输入法下会混用，
    「１５分」与「15分」在用户眼里是同一条，`strip` 之后再哈希才不会写两条。
    """
    import hashlib
    import unicodedata

    normalized = unicodedata.normalize("NFKC", text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

_VEC_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
  memory_id TEXT PRIMARY KEY, embedding float[1024]
);
"""


def _json_list(value: object) -> list[str]:
    """把 SQLite 里的 JSON 文本列读成 `list[str]`（坏值记日志后降级为空列表）。"""
    if not value:
        return []
    try:
        data = json.loads(str(value))
    except (TypeError, ValueError):
        logger.warning("JSON 列解析失败，按空列表处理（原始值前 80 字符：%r）", str(value)[:80])
        return []
    if not isinstance(data, list):
        logger.warning("JSON 列不是列表（%s），按空列表处理", type(data).__name__)
        return []
    return [str(x) for x in data]


def _json_dump(values: list[str]) -> str:
    """把 `list[str]` 写成 JSON 文本列（ensure_ascii=False —— 库里保持人类可读）。"""
    return json.dumps(list(values), ensure_ascii=False)


def _json_dict(value: object) -> dict[str, str]:
    """把 SQLite 里的 JSON 文本列读成 `dict[str, str]`（坏值 / 非字典 ⇒ 空字典）。

    `host_registry.cwd_hashes_json` 用这个：它是 `{cwd_hash: project_id}` **映射**，
    不是列表。坏值降级成空字典而不是抛错——丢了映射最坏是「重复登记一次」，
    抛错会让整个钩子轮次失败（`ADR-0042` R15 的反面）。
    """
    if not value:
        return {}
    try:
        data = json.loads(str(value))
    except (TypeError, ValueError):
        logger.warning("JSON 列解析失败，按空字典处理（原始值前 80 字符：%r）", str(value)[:80])
        return {}
    if not isinstance(data, dict):
        logger.warning("JSON 列不是字典（%s），按空字典处理", type(data).__name__)
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _row_to_memory(row: sqlite3.Row) -> Memory:
    """memories 行 → `Memory`（JSON 列反序列化，INTEGER 布尔转 bool）。"""
    return Memory(
        id=row["id"],
        content=row["content"],
        source_quote=row["source_quote"],
        trigger_text=row["trigger_text"],
        slug=row["slug"],
        scope=row["scope"],
        host=row["host"],
        source_conversation_id=row["source_conversation_id"],
        created_at=row["created_at"],
        last_accessed_at=row["last_accessed_at"],
        access_count=row["access_count"] if row["access_count"] is not None else 0,
        weight=row["weight"] if row["weight"] is not None else 1.0,
        is_pinned=bool(row["is_pinned"]),
        status=MemoryStatus(row["status"]),
        conflict_with=_json_list(row["conflict_with"]),
        evidence_ids=_json_list(row["evidence_ids"]),
        keywords=_json_list(row["keywords"]),
    )


def _row_to_conflict(row: sqlite3.Row) -> Conflict:
    """conflicts 行 → `Conflict`（NULL 的 resolution_type 保持 None）。"""
    resolution = row["resolution_type"]
    return Conflict(
        id=row["id"],
        created_at=row["created_at"],
        status=ConflictStatus(row["status"]),
        source_id=row["source_id"],
        target_id=row["target_id"],
        reason=row["reason"],
        confidence=row["confidence"],
        detector=ConflictDetector(row["detector"]),
        resolution_type=ResolutionType(resolution) if resolution is not None else None,
        should_ask_user=bool(row["should_ask_user"]),
    )


def _row_to_claim(row: sqlite3.Row) -> Claim:
    """claims 行 → `Claim`（status 字符串转枚举；坏值记日志后按 active 处理）。

    为什么坏值不抛：claim 是**判定用的辅助结构**，一条 claim 的 status 脏了不该让
    整个召回 / 导出让路。降级方向取 active 是刻意的——把一条脏记录当有效，
    最坏是多报一次冲突（用户可裁决）；当失效则会让真冲突静默消失。
    """
    raw_status = row["status"]
    try:
        status = ClaimStatus(raw_status)
    except ValueError:
        logger.warning("claims.status 取值未知（%r），按 active 处理", raw_status)
        status = ClaimStatus.ACTIVE
    return Claim(
        id=row["id"],
        memory_id=row["memory_id"],
        subject=row["subject"],
        predicate=row["predicate"],
        value=row["value"],
        scope=row["scope"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        status=status,
        created_at=row["created_at"] or 0,
    )


class MemoryStore:
    """L2 记忆库的持久层：建库 / CRUD / FTS5 / vec0 / 冲突 / claim 与时间线表。

    约束：
    - 失效不删除——任何操作都不提供物理 DELETE（冲突裁决只改 status）；
    - 每次写入同步维护 memories_fts 与 memories_vec 两个虚拟表；
    - sqlite-vec 扩展的加载在连接层统一处理（S1 实现，Windows 路径见 CONVENTIONS §5）。
    """

    def __init__(self, db_path: Path, *, existing_only: bool = False) -> None:
        """打开（必要时创建）数据库并加载扩展。开发/测试传 tmp_path 或 DEV_DATA_DIR。

        - 目录不存在则创建（`~/.March7-dev/` 首次运行时不存在）；
        - 开启 WAL：钩子脚本（独立进程）与 MCP server 会并发读写同一个库；
        - sqlite-vec **延迟导入**并在连接上加载：轻量环境（未装 sqlite-vec）下
          `import March7.store` 仍可用，但向量能力记为未就绪（`vec_ready is False`）。

        ⚠️ **`check_same_thread=False` 必需**（2026-11-12 真机事故，`ADR-0047`）：
        MCP server（`mcp.server.mcpserver`）把**同步工具函数**派发到
        `anyio.to_thread.run_sync` 的**工作线程**执行（`func_metadata.py:163`），
        而 `sqlite3.connect()` 默认 `check_same_thread=True` ⇒ 连接绑死在**构造函数
        所在线程**，跨线程一用就抛
        `ProgrammingError: SQLite objects created in a thread can only be used in
        that same thread`。更糟的是 MCP 框架用 `except Exception` 把它统一包成
        `UnexpectedToolError("Error executing tool <name>")` 并**丢掉原始消息**
        （`tools/base.py:212`）⇒ 宿主只看到「工具坏了」，无法归因。

        事故现象很有辨识度：`March7_profile`（纯 JSON 文件读写）**一直正常**，而
        `March7_status` / `search` / `conflicts` / `write` / `recall`（全部碰 SQLite）
        **全部失败**——分界线就是「有没有摸 DB」。

        关掉线程检查是安全的：① 本模块每个方法都用 `with self._conn:` 或单条
        `execute()` 自成事务，不存在跨线程共享游标的用法；② SQLite 自身的
        `SQLITE_THREADSAFE=1`（Python 默认构建）保证单连接多线程安全；
        ③ 写并发由 WAL + `busy_timeout=5000` 兜住（与钩子进程并发读写同一库本来
        就靠这两条）。
        """
        self._db_path = Path(db_path)
        self._existing_only = existing_only
        if not existing_only:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：MCP 工具跑在工作线程，见上面 ⚠️（ADR-0047）
        self._conn = self._open_connection()
        self._vec_ready = self._load_vec_extension()
        self._warned_no_vec = False  # 降级写入只在首次记 WARNING，避免钩子每轮刷屏

    def _open_connection(self) -> sqlite3.Connection:
        """按本类约定的 PRAGMA 顺序新开一条连接。

        抽成方法是为了让 `_reconnect()`（`ADR-0064`）能复用同一套 PRAGMA——
        两处各写一份迟早会漂（例如有人只改了构造里的 `busy_timeout`）。
        """
        location = str(self._db_path)
        if self._existing_only:
            location = self._db_path.absolute().as_uri() + "?mode=rw"
        conn = sqlite3.connect(location, uri=self._existing_only, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _reconnect(self) -> None:
        """丢弃当前连接、重开一条（只给 `_retry_write` 在 `readonly` 时用）。

        为什么必须重开而不是 `rollback()`：实测 `attempt to write a readonly database`
        是**连接级状态**——同一条连接上重试永远不会恢复（`ADR-0064` §2.2）。

        ⚠️ 重开**不影响** `_vec_ready`：它记的是"本环境有没有 sqlite-vec"，
        与新连接无关；扩展会在 `_load_vec_extension()` 里重新挂到新连接上。
        """
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - 关不上也要继续重开，不能因清理失败而放弃写入
            logger.warning("重开连接前关闭旧连接失败，继续重开", exc_info=True)
        self._conn = self._open_connection()
        if self._vec_ready:
            self._vec_ready = self._load_vec_extension()

    def _load_vec_extension(self, conn: sqlite3.Connection | None = None) -> bool:
        """在连接上加载 sqlite-vec 扩展；失败不抛（由调用方决定是否降级）。

        Returns:
            True = 扩展已加载，向量表可用；False = 环境缺 sqlite-vec（已记 ERROR）。
        """
        try:
            import sqlite_vec  # 延迟导入：轻量环境不保证装上（CONVENTIONS §2 同精神）
        except ImportError:
            logger.error(
                "sqlite-vec 未安装 —— 向量检索不可用（FTS5 关键词检索仍可用）。"
                "请在完整环境（uv sync）下运行；本次不会创建 memories_vec。"
            )
            return False
        conn = self._conn if conn is None else conn
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        except (AttributeError, OSError, sqlite3.OperationalError) as exc:
            logger.error("sqlite-vec 扩展加载失败（%s），向量检索不可用", exc)
            return False
        finally:
            conn.enable_load_extension(False)  # 加载完立刻关掉开关（最小权限）
        return True

    @property
    def vec_ready(self) -> bool:
        """sqlite-vec 是否就绪。

        False 时：`init_schema` 不建 `memories_vec`；`insert_memory` **降级**为只写 FTS5
        （记 WARNING）；`vector_search` **抛 RuntimeError**（读侧必须响亮）。
        """
        return self._vec_ready

    # ---- 建库与生命周期 ----

    def init_schema(self) -> None:
        """建表（幂等）+ 结构版本校验与迁移（`ADR-0021`）。

        顺序刻意是「先查版本 → 再建表 → 再迁移」：
        - **先查**：降级打开（库比代码新）必须在**动任何东西之前**失败，
          否则会先建出一堆表再报错，留下半截状态；
        - **再建**：`CREATE TABLE IF NOT EXISTS` 自动补齐新增的表与索引；
        - **再迁移**：只有 DDL 表达不了的变更（加列 / 回填 / 重建）才需要迁移函数。

        7 张表：memories / memories_fts / memories_vec / conflicts / episodes /
        memory_sources（+ ingest_marks，T14 追加；+ claims，T16 / `ADR-0018` 追加）。

        memories_vec 只在 sqlite-vec 就绪时创建；缺扩展时记 ERROR 并跳过该表
        （此后 `insert_memory` 降级为只写 FTS5 并记 WARNING，`vector_search` 抛 RuntimeError）。

        Raises:
            SchemaVersionError: 库的 `user_version` 高于 `SCHEMA_VERSION`（代码太旧）。
        """
        current = self._check_schema_version()
        self._conn.executescript(_DDL)
        if self._vec_ready:
            self._conn.executescript(_VEC_DDL)
        else:
            logger.error("跳过 memories_vec 建表：sqlite-vec 未就绪（向量检索不可用）")
        self._migrate(current)
        self._conn.commit()
        self.init_maintenance()
        self.refresh_maintenance_safely()

    def init_maintenance(self) -> None:
        """为已绑定的旧库补齐维护派生表，无需迁移记忆记录。"""
        if not self._maintenance_ready(self._conn):
            self._conn.executescript(_MAINTENANCE_DDL + _maintenance_triggers())

    # ---- 库结构版本与迁移（ADR-0021） ----

    @property
    def schema_version(self) -> int:
        """当前库的 `PRAGMA user_version`（0 = 本机制落地前创建的无版本库）。"""
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _check_schema_version(self) -> int:
        """读版本并在「库比代码新」时响亮失败；返回当前版本。"""
        current = self.schema_version
        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"记忆库结构版本 {current} 高于本代码支持的 {SCHEMA_VERSION}"
                f"（{self._db_path}）。旧代码读新库会把新字段当默认值、把新状态当未知状态，"
                "语义会静默出错 ⇒ 请升级 March7 代码后再打开；确需回退，先用新版导出再重建库。"
            )
        return current

    def _set_schema_version(self, version: int) -> None:
        """写 `PRAGMA user_version`（PRAGMA 不支持占位符；值来自本模块的 int，不是外部输入）。"""
        self._conn.execute(f"PRAGMA user_version = {int(version)}")

    def _migrate(self, current: int) -> None:
        """把库从 `current` 逐级升到 `SCHEMA_VERSION`（每级一个事务）。

        **逐级而不是一步到位**：用户可能跳过好几个版本（很久没开），
        每级单独提交 ⇒ 中途失败时库停在一个**已知版本**上，下次可从那里继续，
        而不是停在一个谁也说不清的中间态。
        """
        if current >= SCHEMA_VERSION:
            return
        for version in range(current, SCHEMA_VERSION):
            step = MIGRATIONS.get(version)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if step is not None:
                    logger.info(
                        "库结构迁移 v%s → v%s（%s）",
                        version,
                        version + 1,
                        getattr(step, "__name__", repr(step)),
                    )
                    step(self._conn)
                self._set_schema_version(version + 1)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                logger.error("库结构迁移 v%s → v%s 失败，已回滚", version, version + 1)
                raise

    def close(self) -> None:
        """关闭连接（钩子脚本与 server 退出路径必须调用）。

        WAL 模式下最后一个连接关闭时会自动 checkpoint 并删除 -wal/-shm，
        保持「整库一个文件可拷走」的产品叙事（§2 差异化第 4 点）。
        """
        self._conn.close()

    # ---- memories 主表 CRUD ----

    def insert_memory(
        self, memory: Memory, embedding: list[float], *,
        dsh_evidence: str | None = None, source: dict[str, str | None] | None = None,
    ) -> None:
        """写入一条记忆，同步维护 FTS5 与 vec0 索引（embedding 为 BGE-M3 1024 维）。

        三写同事务：① memories 主表；② memories_fts（写入 `_fts_projection` 检索投影）；
        ③ memories_vec（BGE-M3 向量）。rowid 由 SQLite 自增，作为 FTS 的外部内容锚点。

        ⚠️ **缺 sqlite-vec 时降级但不静默**：只写 ①+②（FTS5 关键词仍可召回），并记
        WARNING 明示「向量索引缺失」；而 `vector_search` 依旧**抛错**（读侧必须响亮）。
        取舍理由（T8 复验修订）：写入侧若直接拒绝，等于**丢掉用户的原话**（T14 的钩子
        每轮都会写，代价不可逆）；写入降级 + 读侧报错 = 数据不丢、缺陷可见
        （CONVENTIONS §3「记日志 + 语义化降级」）。

        Raises:
            sqlite3.IntegrityError: 主键重复（同一 id 重复写入）。
            sqlite3.OperationalError: 锁竞争重试耗尽（`ADR-0064`）。**不静默**：
                上抛给调用方，由钩子记 stderr / MCP 工具转成可读错误。
        """

        def _write() -> None:
            with self._conn:
                self._insert_memory(self._conn, memory, embedding)
                if dsh_evidence is not None:
                    self._conn.execute(
                        "INSERT INTO dsh_memory_evidence(memory_id, digest) VALUES (?, ?)",
                        (memory.id, dsh_evidence),
                    )
                if source is not None:
                    self._attach_source(self._conn, memory.id, source)

        # ADR-0064：多进程并发写时 WAL 会偶发 `database is locked` /
        # `attempt to write a readonly database`。重试把这类瞬态失败吃掉；
        # 非瞬态错误（真 bug）立刻上抛。readonly 还要重开连接（见 `_reconnect`）。
        _retry_write(_write, self._reconnect)
        self._log_memory_insert(memory)

    def _insert_memory(
        self, conn: sqlite3.Connection, memory: Memory, embedding: list[float]
    ) -> None:
        """写记忆与检索投影，由调用方管理事务。"""
        cur = conn.execute(
            f"INSERT INTO memories ({_MEMORY_COLUMNS}) VALUES ({_MEMORY_PLACEHOLDERS})",
            (
                memory.id,
                memory.content,
                memory.source_quote,
                memory.trigger_text,
                memory.slug,
                memory.scope,
                memory.host,
                memory.source_conversation_id,
                memory.created_at,
                memory.last_accessed_at,
                memory.access_count,
                memory.weight,
                int(memory.is_pinned),
                memory.status.value,
                _json_dump(memory.conflict_with),
                _json_dump(memory.evidence_ids),
                _json_dump(memory.keywords),
            ),
        )
        conn.execute(
            "INSERT INTO memories_fts (rowid, content, source_quote) VALUES (?, ?, ?)",
            (cur.lastrowid, _fts_projection(memory.content), _fts_projection(memory.source_quote)),
        )
        if self._vec_ready:
            conn.execute(
                "INSERT INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
                (memory.id, self._serialize_embedding(embedding)),
            )

    def _log_memory_insert(self, memory: Memory) -> None:
        """只在提交成功后报告写入结果。"""
        if self._vec_ready:
            logger.debug(
                "写入记忆 %s（scope=%s, status=%s）", memory.id, memory.scope, memory.status.value
            )
        elif not self._warned_no_vec:
            self._warned_no_vec = True
            logger.warning(
                "sqlite-vec 未就绪：记忆 %s 只写入了 FTS5 关键词索引，**缺少向量索引**"
                "（语义召回取不到它；`vector_search` 也会抛错）。请在完整环境（uv sync）重跑。",
                memory.id,
            )
        else:
            logger.debug("写入记忆 %s（降级：无向量索引）", memory.id)

    def fix_memory(
        self, old: Memory, new_id: str, content: str, embedding: list[float], *,
        expected_revision: str | None = None,
        host: str | None = None,
    ) -> bool:
        """将确认时的活跃旧条标为失效，并原子写入用户手动提供的新条。

        目标不存在、已失效或状态、正文、作用域等已变化时返回 False。旧条的检索投影和
        来源仍可用于历史与删除审计，活跃召回按状态过滤。
        """
        if old.status not in (MemoryStatus.ACTIVE, MemoryStatus.AGING):
            return False
        new = Memory(
            id=new_id,
            content=content,
            source_quote=content,
            scope=old.scope,
            host=host,
            created_at=int(time.time()),
        )
        # 独立连接防止同一个 store 上的其他写入意外提交这笔事务。
        with closing(self._open_connection()) as conn, conn:
            if self._vec_ready and not self._load_vec_extension(conn):
                raise RuntimeError("无法在纠正连接加载向量索引")
            conn.execute("BEGIN IMMEDIATE")
            if expected_revision is not None and not self._management_matches(conn, old.id, expected_revision):
                return False
            updated = conn.execute(
                "UPDATE memories SET status = ? "
                "WHERE id = ? AND status = ? AND content = ? AND scope = ? AND created_at = ?",
                (
                    MemoryStatus.SUPERSEDED.value,
                    old.id,
                    old.status.value,
                    old.content,
                    old.scope,
                    old.created_at,
                ),
            )
            if updated.rowcount != 1:
                return False

            self._supersede_claims(conn, old.id, ClaimStatus.SUPERSEDED)
            self._insert_memory(conn, new, embedding)
            # 事实纠正沿用原适用期限；旧条的时效记录保留用于核对。
            conn.execute(
                "INSERT INTO memory_expiries(memory_id, expires_at) "
                "SELECT ?, expires_at FROM memory_expiries WHERE memory_id = ?",
                (new.id, old.id),
            )
            # 命令行输入是本次原话，宿主会话标识只属于旧条。
            self._attach_source(conn, new.id, {"quote": content})

        self._log_memory_insert(new)
        return True

    def get_memory(self, memory_id: str) -> Memory | None:
        """按主键取一条记忆；不存在返回 None（superseded/merged 也返回——历史永远可查）。"""
        row = self._conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return None if row is None else _row_to_memory(row)

    def update_status(self, memory_id: str, status: MemoryStatus) -> None:
        """更新单条记忆的 status（冲突裁决落库用；只改标记，不删行）。

        记忆不存在时是**无操作**（不抛错）——裁决可能作用在已被合并的旧记录上，
        这里保持幂等；存在性校验由调用方（conflict.py）负责。
        """
        self._conn.execute("UPDATE memories SET status = ? WHERE id = ?", (status.value, memory_id))
        self._conn.commit()

    def transition_memory(
        self, old: Memory, status: MemoryStatus, *, expected_revision: str | None = None,
    ) -> bool:
        """按读到的状态做用户操作；恢复会清除先前的显式截止时间。"""
        with closing(self._open_connection()) as conn, conn:
            if expected_revision is not None:
                conn.execute("BEGIN IMMEDIATE")
                if not self._management_matches(conn, old.id, expected_revision):
                    return False
            cur = conn.execute(
                "UPDATE memories SET status = ? WHERE id = ? AND status = ? AND scope = ?",
                (status.value, old.id, old.status.value, old.scope),
            )
            if cur.rowcount != 1:
                return False
            if status == MemoryStatus.ACTIVE:
                conn.execute("DELETE FROM memory_expiries WHERE memory_id = ?", (old.id,))
        return True

    def schedule_memory_expiry(self, memory: Memory, expires_at: int) -> bool:
        """登记用户明确的截止时间，同项目、当前有效才可登记。"""
        with closing(self._open_connection()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO memory_expiries(memory_id, expires_at) "
                "SELECT id, ? FROM memories WHERE id = ? AND scope = ? AND status = ? "
                "ON CONFLICT(memory_id) DO UPDATE SET expires_at = excluded.expires_at",
                (expires_at, memory.id, memory.scope, memory.status.value),
            )
        return cur.rowcount == 1

    def archive_due_memories(self) -> None:
        """物化已授权的过期状态，调用方继续用既有状态过滤。"""
        now = int(time.time())
        due = (
            "SELECT e.memory_id FROM memory_expiries e JOIN memories m ON m.id = e.memory_id "
            "WHERE e.expires_at <= ? AND m.status IN ('active', 'aging')"
        )
        if self._conn.execute(due + " LIMIT 1", (now,)).fetchone() is None:
            return
        with closing(self._open_connection()) as conn, conn:
            conn.execute(f"UPDATE memories SET status = 'archived' WHERE id IN ({due})", (now,))

    def touch(self, memory_id: str) -> None:
        """召回后更新 access_count += 1 与 last_accessed_at = now（为 M3 激活度预埋）。"""
        self._conn.execute(
            "UPDATE memories SET access_count = access_count + 1, last_accessed_at = ? "
            "WHERE id = ?",
            (int(time.time()), memory_id),
        )
        self._conn.commit()

    # ---- 写入治理（ADR-0024 / ADR-0026）----

    @property
    def data_dir(self) -> Path:
        """库文件所在目录（写入账本与钩子日志都落在它的 `logs/` 下）。"""
        return self._db_path.parent

    def count_since(self, since_ts: int) -> int:
        """统计 `created_at >= since_ts` 的记忆条数（每日写入熔断用）。

        走 `idx_memories_created_at` 索引；**含 superseded/merged**——
        熔断看的是"今天产生了多少条写入"，不是"今天有多少条还活着"。
        """
        row = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE created_at >= ?", (int(since_ts),)
        ).fetchone()
        return int(row[0])

    def find_similar_memory(
        self,
        embedding: list[float],
        *,
        scope: str = "global",
        min_cosine: float = 0.90,
        limit: int = 10,
        eligible: Callable[[Memory], bool] | None = None,
        dsh_evidence: str | None = None,
    ) -> tuple[Memory, float] | None:
        """在**同 scope 的存活记忆**里找最近邻，余弦够高才返回（ADR-0024 查重用）。

        ⚠️ **距离口径（2026-10-31 实测纠正）**：`memories_vec` 是 `vec0(embedding float[1024])`，
        没有声明 `distance_metric` ⇒ sqlite-vec 用的是 **L2 距离**，不是余弦距离。
        实测：正交向量返回 1.414（=√2），45° 返回 0.765，而非 1.0 / 0.293。
        所以这里把 L2 按单位球面换算回余弦：`cos = 1 − L2² / 2`。
        第一版直接把"余弦阈值"当 L2 用，等于要求余弦 ≥ 0.995 —— 查重几乎永远不触发，
        而且**测试全绿**（只有完全相同的内容才会命中），是最难发现的一类口径错误。

        为什么先 KNN 再过滤：vec0 的 `MATCH ... AND k = ?` 是特殊路径，
        不能与普通 `WHERE` 条件混用（见 `vector_search` 的注释）。

        Args:
            embedding: 待写入记忆的向量。**必须是单位向量**（`Embedder` 的
                `normalize_embeddings=True` 保证；L2↔余弦的换算只在单位球面上成立）。
            scope: 只在同一命名空间内查重（`project:a` 不该并进 `global`）。
            min_cosine: 余弦相似度下限（0–1）。0.90 ≈ L2 距离 0.447。
            limit: KNN 候选数（取大一点，避免同 scope 的条目被别的 scope 挤掉）。
            dsh_evidence: 已核验的证据摘要。先按证据及范围定候选，再比较向量。

        Returns:
            `(命中的既有记忆, 余弦)`；vec 不可用 / 没有足够近的 ⇒ `None`。
        """
        if not self._vec_ready or min_cosine <= 0:
            return None
        self.archive_due_memories()
        norm = math.sqrt(sum(x * x for x in embedding))
        if norm <= 0 or not math.isfinite(norm):
            return None
        if abs(norm - 1.0) > 0.01:
            logger.warning(
                "查重收到非单位向量（norm=%.3f）：L2↔余弦换算会失真，本次跳过查重。"
                "Embedder 应以 normalize_embeddings=True 输出。",
                norm,
            )
            return None
        # 单位球面上：L2² = 2 − 2cos ⇒ cos = 1 − L2²/2
        max_l2 = math.sqrt(max(0.0, 2.0 - 2.0 * min_cosine))
        evidence_filter = ""
        params: list[bytes | int | str] = [self._serialize_embedding(embedding), int(limit)]
        if dsh_evidence is not None:
            evidence_filter = (
                " AND memory_id IN (SELECT e.memory_id FROM dsh_memory_evidence e "
                "JOIN memories m ON m.id = e.memory_id "
                "WHERE e.digest = ? AND m.scope = ? AND m.status IN ('active', 'aging'))"
            )
            params.extend((dsh_evidence, scope))
        rows = self._conn.execute(
            "SELECT memory_id, distance FROM memories_vec "
            f"WHERE embedding MATCH ? AND k = ?{evidence_filter} ORDER BY distance",
            params,
        ).fetchall()
        for row in rows:
            l2 = float(row["distance"])
            if not math.isfinite(l2):
                # 零向量 / 归一化失败时距离可能是 NaN 或 inf。
                # 这种"距离不可比"的候选必须跳过——否则 NaN 与任何阈值比较都为 False，
                # 会被误判成"最近邻"（把两条无关记忆并掉）。
                logger.warning("跳过不可比的距离（memory_id=%s, distance=%r）", row["memory_id"], l2)
                continue
            if l2 > max_l2:
                break  # 已按距离升序，后面的只会更远
            memory = self.get_memory(str(row["memory_id"]))
            if memory is None or memory.scope != scope:
                continue
            if memory.status not in (MemoryStatus.ACTIVE, MemoryStatus.AGING):
                continue
            if eligible is None or eligible(memory):
                return memory, 1.0 - (l2 * l2) / 2.0
        return None

    def find_similar_memories(
        self,
        memory_id: str,
        *,
        scope: str | None = None,
        min_cosine: float = 0.60,
        limit: int = 3,
    ) -> list[tuple[Memory, float]]:
        """取该记忆的**多个**向量近邻（T16b 冲突候选筛；按余弦降序）。

        与 `find_similar_memory` 的分工：那个是**查重**用的「最近的一条够不够像」，
        阈值 0.90、只看第一；本方法是**候选筛**用的「有哪几条值得让 LLM 看一眼」，
        阈值低得多（默认 0.60）、要 top-k。两者共用同一套距离口径。

        ⚠️ 距离口径同 `find_similar_memory`：vec0 未声明 `distance_metric` ⇒ **L2**，
        必须按单位球面换算 `cos = 1 − L2²/2`（`CONVENTIONS` §9.9）。直接把 L2 当余弦
        比阈值，等于要求余弦 ≥ 0.995——候选筛会永远为空，而且测试全绿。

        为什么**非单位向量直接跳过并记 WARNING**：零向量的 L2 距任何向量都是常数，
        换算出来的"余弦"没有意义（实测零向量两两之间 cos≈1）⇒ 会把所有记忆对判成
        「几乎相同」，是最坏的一类假阳性。生产侧 `Embedder` 输出已归一，
        这里拒掉的是测试替身 / 历史脏数据。

        Args:
            memory_id: 参照记忆（它自己的向量从 `memories_vec` 读，取不到 ⇒ `[]`）。
            scope: 只在该命名空间内找；None ⇒ 用记忆自己的 scope（不跨域比）。
            min_cosine: 余弦下限（0–1）；`<= 0` ⇒ 关闭。
            limit: 最多返回几条。

        Returns:
            `[(记忆, 余弦)]`，按余弦降序；vec 不可用 / 向量不可用 / 无命中 ⇒ `[]`。
        """
        if not self._vec_ready or min_cosine <= 0 or limit <= 0:
            return []
        row = self._conn.execute(
            "SELECT embedding FROM memories_vec WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            logger.debug("记忆 %s 没有向量行，跳过向量候选筛", memory_id)
            return []
        raw = bytes(row["embedding"])
        if not raw or len(raw) % 4:
            logger.warning("记忆 %s 的向量字节长度异常（%d），跳过向量候选筛", memory_id, len(raw))
            return []
        embedding = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        norm = math.sqrt(sum(x * x for x in embedding))
        if not math.isfinite(norm) or abs(norm - 1.0) > 0.01:
            logger.warning(
                "记忆 %s 的向量不是单位向量（norm=%.3f）：L2↔余弦换算会失真，本次跳过向量候选筛",
                memory_id,
                norm,
            )
            return []
        target_scope = scope
        if target_scope is None:
            anchor = self.get_memory(memory_id)
            target_scope = anchor.scope if anchor is not None else "global"

        # 单位球面上：L2² = 2 − 2cos ⇒ cos = 1 − L2²/2
        max_l2 = math.sqrt(max(0.0, 2.0 - 2.0 * min_cosine))
        found: list[tuple[Memory, float]] = []
        seen: set[str] = set()
        # vec0 的 KNN 不能与 WHERE 混用，scope/status 只能在取回后过滤 ⇒ k 要放大。
        # 放大还不够时要**继续扩**：前 k 个近邻可能全是别的 scope / 已失效，
        # 固定 k 会把同 scope 的候选"饿死"（独立验收 2026-11-05 指出的边界）。
        k = max(limit * 4, 12)
        while True:
            rows = self._vector_knn(self._serialize_embedding(embedding), k)
            beyond_threshold = False
            for candidate in rows:
                candidate_id = str(candidate["memory_id"])
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                if candidate_id == memory_id:
                    continue
                l2 = float(candidate["distance"])
                if not math.isfinite(l2):
                    logger.warning(
                        "跳过不可比的距离（memory_id=%s, distance=%r）", candidate_id, l2
                    )
                    continue
                if l2 > max_l2:
                    beyond_threshold = True  # 已按距离升序，后面的只会更远
                    break
                memory = self.get_memory(candidate_id)
                if memory is None or memory.scope != target_scope:
                    continue
                if memory.status not in (MemoryStatus.ACTIVE, MemoryStatus.AGING):
                    continue
                # 候选自己的向量也要是单位向量：换算只在单位球面上成立。
                # 只校验锚点是不够的——历史脏数据/替身可能是零向量，
                # 它的距离换算出的"余弦"会把无关记忆抬进候选。
                if not self._is_unit_vector(candidate_id):
                    continue
                found.append((memory, 1.0 - (l2 * l2) / 2.0))
                if len(found) >= limit:
                    return found
            if beyond_threshold or len(rows) < k:
                return found
            k *= 2

    def _is_unit_vector(self, memory_id: str) -> bool:
        """该记忆的向量是否是单位向量（缺失/坏长度/非有限 ⇒ False，并记 WARNING）。"""
        row = self._conn.execute(
            "SELECT embedding FROM memories_vec WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return False
        raw = bytes(row["embedding"])
        if not raw or len(raw) % 4:
            logger.warning("记忆 %s 的向量字节长度异常（%d），跳过", memory_id, len(raw))
            return False
        values = struct.unpack(f"<{len(raw) // 4}f", raw)
        norm = math.sqrt(sum(x * x for x in values))
        if not math.isfinite(norm) or abs(norm - 1.0) > 0.01:
            logger.warning(
                "记忆 %s 的向量不是单位向量（norm=%.3f），跳过该候选", memory_id, norm
            )
            return False
        return True

    def unshared_profile_phrases(
        self, scope: str, phrases: tuple[str | None, ...],
    ) -> tuple[str, ...]:
        """项目条目仅能关联无其他作用域记录支持的完整画像原值。"""
        values = tuple(dict.fromkeys(phrase.strip() for phrase in phrases if phrase and phrase.strip()))
        if scope == "global":
            return values
        return tuple(
            value for value in values
            if self._conn.execute(
                "SELECT 1 FROM memories WHERE scope <> ? "
                "AND (TRIM(content) = ? OR TRIM(source_quote) = ?) LIMIT 1",
                (scope, value, value),
            ).fetchone() is None
        )

    def visible_memories(self, project_id: str | None) -> list[Memory]:
        """逐条管理与核验用；按作用域读所有状态。"""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE scope = 'global' OR scope = ? ORDER BY rowid",
            (f"project:{project_id}" if project_id else "global",),
        )
        return [
            memory for memory in map(_row_to_memory, rows)
            if not privacy.unbound_historical_project(memory.scope, memory.content, memory.source_quote)
        ]

    def management_projects(self) -> list[str]:
        """也列出仍有记忆、但未登记目录的项目。"""
        rows = self._conn.execute(
            "SELECT project_id FROM projects WHERE project_id <> '' UNION "
            "SELECT substr(scope, 9) FROM memories WHERE scope GLOB 'project:?*' ORDER BY 1"
        )
        return [str(row[0]) for row in rows]

    def management_page(
        self, scope: str, *, history: bool, search: str, status: str, limit: int, offset: int,
    ) -> tuple[int, list[Memory]]:
        """用同一读事务给出指定作用域、筛选条件下的条数及一页记录。"""
        conditions = ["scope = ?"]
        conditions.append("status NOT IN ('active', 'aging')" if history else "status IN ('active', 'aging')")
        parameters: list[object] = [scope]
        if status:
            conditions.append("status = ?")
            parameters.append(status)
        if search:
            conditions.append(
                "(instr(lower(content), lower(?)) > 0 OR instr(lower(source_quote), lower(?)) > 0 "
                "OR instr(lower(id), lower(?)) > 0 OR instr(lower(slug), lower(?)) > 0 "
                "OR EXISTS (SELECT 1 FROM memory_sources s WHERE s.memory_id = memories.id "
                "AND instr(lower(json_extract(CASE WHEN json_valid(s.source_json) "
                "THEN s.source_json ELSE '{}' END, '$.quote')), lower(?)) > 0))"
            )
            parameters.extend([search] * 5)
        where = " AND ".join(conditions)
        with closing(self._open_connection()) as conn, conn:
            conn.execute("BEGIN")
            total = int(conn.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", parameters).fetchone()[0])
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return total, [_row_to_memory(row) for row in rows]

    def management_detail(self, memory_id: str) -> ManagementDetail | None:
        """一次读取生成展示数据和写权限校验用的版本摘要。"""
        row = self._conn.execute(_MANAGEMENT_ROW, (memory_id,)).fetchone()
        if row is None:
            return None
        return ManagementDetail(
            _row_to_memory(row), self._decode_source(row["source_json"]), row["expires_at"],
            self._management_revision(row),
        )

    @staticmethod
    def _management_revision(row: sqlite3.Row) -> str:
        """召回次数等读侧计数不改变版本，原话、来源、作用域和期限会改变。"""
        values = [row[key] for key in row.keys() if key not in {"last_accessed_at", "access_count"}]
        expires_at = row["expires_at"]
        values.append(bool(expires_at is not None and expires_at <= int(time.time())))
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()

    @classmethod
    def _management_matches(cls, conn: sqlite3.Connection, memory_id: str, revision: str) -> bool:
        row = conn.execute(_MANAGEMENT_ROW, (memory_id,)).fetchone()
        return row is not None and cls._management_revision(row) == revision

    def maintenance_set_auto(self, scope: str, enabled: bool) -> None:
        """仅保存此库、此范围的自动检查开关。"""
        with closing(self._open_connection()) as conn, conn:
            conn.execute(
                "INSERT INTO maintenance_scopes(scope, automatic) VALUES (?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET automatic = excluded.automatic",
                (scope, int(enabled)),
            )

    def refresh_maintenance(self) -> dict[str, maintenance.Report]:
        """加载或写入后，只刷新显式启用的范围。"""
        scopes = self._conn.execute("SELECT scope FROM maintenance_scopes WHERE automatic = 1 ORDER BY scope").fetchall()
        return {row["scope"]: self.maintenance_check(row["scope"], automatic_only=True) for row in scopes}

    def refresh_maintenance_safely(self) -> None:
        """可选体检失败时保留主链路，下次加载可以重试。"""
        try:
            self.refresh_maintenance()
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            logger.exception("自动记忆体检未完成")

    def maintenance_check(self, scope: str, *, automatic_only: bool = False) -> maintenance.Report:
        """固定一次库快照，复用结果或只投影已变记录。"""
        with closing(self._open_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._maintenance_check(conn, scope, int(time.time()), automatic_only=automatic_only)

    def _maintenance_check(
        self, conn: sqlite3.Connection, scope: str, moment: int, *, automatic_only: bool = False,
    ) -> maintenance.Report:
        saved = conn.execute("SELECT * FROM maintenance_scopes WHERE scope = ?", (scope,)).fetchone()
        automatic = bool(saved and saved["automatic"])
        if automatic_only and not automatic:
            return maintenance.Report(scope, "", 0, False, ())
        if saved is None:
            conn.execute("INSERT INTO maintenance_scopes(scope) VALUES (?)", (scope,))
        if saved is None or saved["projection"] is None:
            entries: dict[str, maintenance.Projection] = {}
            changed = [
                row[0] for row in conn.execute("SELECT id FROM memories WHERE scope = ? ORDER BY id", (scope,))
            ]
        else:
            changed = [
                row[0] for row in conn.execute(
                    "SELECT memory_id FROM maintenance_changes WHERE scope = ? AND dirty = 1 "
                    "UNION SELECT e.memory_id FROM memory_expiries e JOIN memories m ON m.id = e.memory_id "
                    "WHERE m.scope = ? AND e.expires_at BETWEEN ? AND ? ORDER BY 1",
                    (scope, scope, saved["checked_at"] + 1, moment),
                )
            ]
            if not changed and saved["findings"] is not None:
                findings = tuple(
                    maintenance.Finding(item["kind"], tuple(item["ids"]), item["reason"])
                    for item in json.loads(saved["findings"])
                )
                return maintenance.Report(scope, saved["version"], 0, automatic, findings)
            entries = {
                mid: maintenance.Projection(**item) for mid, item in json.loads(saved["projection"]).items()
            }

        examined = 0
        for mid in changed:
            row = conn.execute(_MANAGEMENT_ROW, (mid,)).fetchone()
            if row is None or row["scope"] != scope:
                entries.pop(mid, None)
                continue
            claims = conn.execute(
                "SELECT subject, predicate, value, scope, valid_from, valid_to FROM claims "
                "WHERE memory_id = ? AND status = 'active' ORDER BY 1, 2, 3, 4, 5, 6",
                (mid,),
            )
            entries[mid] = maintenance.project_memory(
                _row_to_memory(row), self._decode_source(row["source_json"]),
                row["expires_at"], maintenance.fingerprint(sorted({tuple(claim) for claim in claims}, key=repr)),
                moment,
            )
            examined += 1
        conflicts = [
            tuple(row) for row in conn.execute(
                "SELECT c.source_id, c.target_id, c.status, c.resolution_type, c.should_ask_user "
                "FROM conflicts c JOIN memories a ON a.id = c.source_id JOIN memories b ON b.id = c.target_id "
                "WHERE a.scope = ? AND b.scope = ?",
                (scope, scope),
            )
        ]
        external = {
            row[0] for row in conn.execute(
                "SELECT CASE WHEN a.scope = ? THEN a.id ELSE b.id END FROM conflicts c "
                "JOIN memories a ON a.id = c.source_id JOIN memories b ON b.id = c.target_id "
                "WHERE a.scope <> b.scope AND (a.scope = ? OR b.scope = ?) "
                "AND c.status NOT IN ('resolved', 'dismissed')", (scope, scope, scope),
            )
        }
        findings = maintenance.group_findings(dict(sorted(entries.items())), conflicts, external)
        version = uuid4().hex
        conn.execute(
            "UPDATE maintenance_scopes SET version = ?, checked_at = ?, projection = ?, findings = ? WHERE scope = ?",
            (version, moment, json.dumps({mid: asdict(item) for mid, item in entries.items()}),
             json.dumps([asdict(item) for item in findings], ensure_ascii=False), scope),
        )
        conn.execute("UPDATE maintenance_changes SET dirty = 0 WHERE scope = ? AND dirty = 1", (scope,))
        return maintenance.Report(scope, version, examined, automatic, tuple(findings))

    def _maintenance_binding(self) -> str:
        stat = self._db_path.stat()
        return maintenance.fingerprint((str(self._db_path.resolve()), stat.st_dev, stat.st_ino))

    @staticmethod
    def _maintenance_ready(conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'maintenance_conflicts_delete'",
        ).fetchone() is not None

    @classmethod
    def _maintenance_revision(cls, conn: sqlite3.Connection, scope: str, mid: str) -> tuple[str, str]:
        row = conn.execute(_MANAGEMENT_ROW, (mid,)).fetchone()
        if row is None or row["scope"] != scope:
            raise ValueError("本批有记录已移走或擦除，请重新检查。")
        generation = conn.execute(
            "SELECT generation FROM maintenance_changes WHERE scope = ? AND memory_id = ?", (scope, mid),
        ).fetchone()
        return row["status"], maintenance.fingerprint((cls._management_revision(row), generation[0] if generation else 0))

    @staticmethod
    def _maintenance_batch(row: sqlite3.Row) -> maintenance.Batch:
        return maintenance.Batch(
            row["id"], row["scope"], row["kind"], row["state"],
            tuple(maintenance.Target(**target) for target in json.loads(row["targets"])),
            row["parent_id"], row["applied_at"],
        )

    def maintenance_history(self, scope: str) -> tuple[maintenance.Batch, ...]:
        """读取当前范围内最近的已执行回执。"""
        with closing(self._open_connection()) as conn:
            rows = conn.execute(
                "SELECT * FROM maintenance_batches WHERE scope = ? AND applied_at IS NOT NULL "
                "ORDER BY applied_at DESC, rowid DESC LIMIT 20", (scope,),
            )
            return tuple(map(self._maintenance_batch, rows))

    def maintenance_model_drafts(self, scope: str, batch_id: str | None = None) -> tuple[maintenance.Batch, ...]:
        """面板只取本库、本范围内尚可审阅的模型草稿。"""
        statement = (
            "SELECT * FROM maintenance_batches WHERE scope = ? AND owner = ? AND binding = ? "
            "AND state = 'pending' AND deadline > ? AND version = ("
            "SELECT version FROM maintenance_scopes WHERE scope = maintenance_batches.scope)"
        )
        parameters: tuple[object, ...] = (
            scope, maintenance.MODEL_DRAFT_OWNER, self._maintenance_binding(), int(time.time()),
        )
        if batch_id is not None:
            statement += " AND id = ?"
            parameters += (batch_id,)
        with closing(self._open_connection()) as conn:
            rows = conn.execute(statement + " ORDER BY rowid DESC LIMIT 8", parameters)
            return tuple(map(self._maintenance_batch, rows))

    def _dialogue_context(self, conn: sqlite3.Connection, nonce: str, scope: str) -> sqlite3.Row:
        turn = conn.execute(
            "SELECT * FROM dialogue_turns WHERE nonce = ? AND binding = ? AND expires_at > ?",
            (nonce, self._maintenance_binding(), int(time.time())),
        ).fetchone()
        if turn is None or (scope != "global" and scope != f"project:{turn['project_id'] or ''}"):
            raise ValueError("本轮宿主上下文已失效，或库与项目范围不一致。")
        return turn

    def dialogue_project(self, nonce: str) -> str | None:
        """从本轮原生事件读取项目身份。"""
        with closing(self._open_connection()) as conn:
            return self._dialogue_context(conn, nonce, "global")["project_id"]

    def dialogue_handled(self, prompt_key: str) -> bool:
        """只判断近期已交给授权流程的原生消息。"""
        return self._conn.execute(
            "SELECT 1 FROM conversation_operations WHERE event_key = ? AND created_at > ?",
            ("dialogue-plan:" + prompt_key, int(time.time()) - maintenance.PREVIEW_TTL),
        ).fetchone() is not None

    @staticmethod
    def _mark_dialogue_handled(conn: sqlite3.Connection, prompt_key: str, moment: int) -> None:
        conn.execute(
            "INSERT INTO conversation_operations(event_key, created_at) VALUES (?, ?) "
            "ON CONFLICT(event_key) DO UPDATE SET created_at = excluded.created_at",
            ("dialogue-plan:" + prompt_key, moment),
        )

    def dialogue_receive(
        self, session_key: str, project_id: str | None, event_key: str, decision: str | None,
        expected: tuple[str | None, str] | None = None, *, prompt_key: str,
    ) -> tuple[str | None, maintenance.Batch | None]:
        """替换当前轮次。在同一事务消费明确许可、执行上一计划并记录结果。"""
        moment = int(time.time())
        binding = self._maintenance_binding()
        with closing(self._open_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if decision is not None:
                replay = conn.execute(
                    "SELECT batch FROM dialogue_results WHERE event_key = ? AND session_key = ? "
                    "AND project_id IS ? AND binding = ? AND prompt_key = ?",
                    (event_key, session_key, project_id, binding, prompt_key),
                ).fetchone()
                if replay is not None:
                    data = json.loads(replay["batch"])
                    data["targets"] = tuple(maintenance.Target(**target) for target in data["targets"])
                    return None, maintenance.Batch(**data)
            old = conn.execute("SELECT * FROM dialogue_turns WHERE session_key = ?", (session_key,)).fetchone()
            compatible = old is not None and old["project_id"] == project_id and old["binding"] == binding
            if compatible and old["event_key"] == event_key:
                if old["prompt_key"] != prompt_key:
                    return None, None
                return (old["nonce"] if old["expires_at"] > moment and decision is None else None), None
            claimed = conn.execute(
                "INSERT INTO conversation_operations(event_key, created_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (event_key, moment),
            )
            if claimed.rowcount != 1:
                return None, None
            result = None
            if old is not None and old["binding"] == binding and old["operation"] == "maintenance":
                row = conn.execute(
                    "SELECT scope, id FROM maintenance_batches WHERE id = ? AND binding = ? AND owner = ?",
                    (old["reference"], binding, old["nonce"]),
                ).fetchone()
                if row is not None:
                    if compatible and decision is not None and expected is not None:
                        target_scope, target_id = expected
                        if (target_scope, target_id) not in {(None, row["id"][:12]), (row["scope"], row["id"])}:
                            return None, None
                        if target_scope is None:
                            collisions = conn.execute(
                                "SELECT COUNT(*) FROM maintenance_batches WHERE id GLOB ? AND binding = ?",
                                (target_id + "*", binding),
                            ).fetchone()[0]
                            if collisions != 1:
                                return None, None
                        result = self._maintenance_finish(
                            conn, row["scope"], old["nonce"], row["id"], binding, cancel=decision == "cancel",
                        )
                    else:
                        conn.execute(
                            "UPDATE maintenance_batches SET state = 'cancelled' WHERE id = ? AND state = 'pending'", (row["id"],),
                        )
            nonce = uuid4().hex
            if result is not None:
                self._mark_dialogue_handled(conn, prompt_key, moment)
                conn.execute(
                    "INSERT INTO dialogue_results(event_key, session_key, project_id, binding, prompt_key, batch) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (event_key, session_key, project_id, binding, prompt_key, json.dumps(asdict(result))),
                )
            conn.execute(
                "INSERT INTO dialogue_turns(session_key, project_id, event_key, nonce, binding, expires_at, prompt_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_key) DO UPDATE SET "
                "project_id = excluded.project_id, event_key = excluded.event_key, nonce = excluded.nonce, "
                "binding = excluded.binding, expires_at = excluded.expires_at, "
                "prompt_key = excluded.prompt_key, operation = NULL, reference = NULL",
                (session_key, project_id, event_key, nonce, binding,
                 moment + maintenance.PREVIEW_TTL if decision is None else moment, prompt_key),
            )
            return (nonce if decision is None else None), result

    def maintenance_preview(
        self, scope: str, owner: str, key: str, *, undo: bool = False, context: str | None = None,
        keep: tuple[str, ...] = (), archive: tuple[str, ...] = (),
    ) -> maintenance.Batch:
        """选定一组或指定回执，冻结一张逐批预览。"""
        binding = self._maintenance_binding()
        moment = int(time.time())
        with closing(self._open_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if binding != self._maintenance_binding():
                raise ValueError("记忆库已更换，请重新打开。")
            turn = self._dialogue_context(conn, context, scope) if context else None
            if turn is not None:
                owner = turn["nonce"]
            if archive and turn is None:
                raise ValueError("指定归档需要本轮原生宿主上下文。")
            report = self._maintenance_check(conn, scope, moment)
            parent_id = None
            if undo:
                row = conn.execute(
                    "SELECT * FROM maintenance_batches WHERE id = ? AND scope = ? AND kind IN ('merge', 'plan') AND state = 'applied'",
                    (key, scope),
                ).fetchone()
                if row is None:
                    raise ValueError("当前范围没有可撤销的这张整理回执。")
                parent_id = key
                targets = []
                for previous in self._maintenance_batch(row).targets:
                    status, revision = self._maintenance_revision(conn, scope, previous.id)
                    if (status, revision) != (previous.after, previous.revision):
                        raise ValueError("整理后的记录已经变化，本批无法撤销。")
                    targets.append(maintenance.Target(previous.id, status, previous.before, revision))
            elif archive:
                if set(keep) & set(archive) or len(keep) + len(archive) > maintenance.BATCH_SIZE:
                    raise ValueError("保留与归档目标必须不同，且不得超出单批上限。")
                targets = []
                for mid in (*keep, *archive):
                    status, revision = self._maintenance_revision(conn, scope, mid)
                    if status not in {"active", "aging"}:
                        raise ValueError("计划只能包含此范围内有效的记忆。")
                    if mid in archive:
                        self._check_batch_archive(conn, mid)
                    targets.append(maintenance.Target(mid, status, "archived" if mid in archive else status, revision))
            else:
                group = next((item for item in report.findings if item.kind == "duplicate" and item.key == key), None)
                if group is None:
                    raise ValueError("当前范围没有可整理的这一组，请重新检查。")
                targets = []
                for index, mid in enumerate(group.ids):
                    status, revision = self._maintenance_revision(conn, scope, mid)
                    targets.append(maintenance.Target(mid, status, "merged" if index else status, revision))
            batch_id = uuid4().hex
            kind = "undo" if undo else "plan" if archive else "merge"
            encoded = json.dumps([asdict(target) for target in targets])
            if turn is not None and turn["reference"] is not None:
                previous = conn.execute(
                    "SELECT * FROM maintenance_batches WHERE id = ? AND scope = ? AND version = ? "
                    "AND kind = ? AND parent_id IS ? AND targets = ? AND state = 'pending' AND deadline > ?",
                    (turn["reference"], scope, report.version, kind, parent_id, encoded, moment),
                ).fetchone()
                if previous is not None:
                    return self._maintenance_batch(previous)
                raise ValueError("这条用户消息已有一份待核对计划。请等下一条用户消息后再改方案。")
            if owner == maintenance.MODEL_DRAFT_OWNER:
                previous = conn.execute(
                    "SELECT * FROM maintenance_batches WHERE scope = ? AND owner = ? AND binding = ? "
                    "AND version = ? AND kind = ? AND parent_id IS ? AND targets = ? "
                    "AND state = 'pending' AND deadline > ? ORDER BY rowid DESC LIMIT 1",
                    (scope, owner, binding, report.version, kind, parent_id, encoded, moment),
                ).fetchone()
                if previous is not None:
                    return self._maintenance_batch(previous)
            conn.execute(
                "DELETE FROM maintenance_batches WHERE applied_at IS NULL AND deadline <= ?",
                (moment - maintenance.PREVIEW_TTL,),
            )
            conn.execute(
                "INSERT INTO maintenance_batches(id, scope, owner, binding, version, kind, parent_id, deadline, state, targets) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (batch_id, scope, owner, binding, report.version, kind, parent_id,
                 min(moment + maintenance.PREVIEW_TTL, turn["expires_at"]) if turn else moment + maintenance.PREVIEW_TTL,
                 encoded),
            )
            if turn is not None:
                conn.execute(
                    "UPDATE dialogue_turns SET operation = 'maintenance', reference = ? WHERE nonce = ?",
                    (batch_id, owner),
                )
                self._mark_dialogue_handled(conn, turn["prompt_key"], moment)
            return maintenance.Batch(batch_id, scope, kind, "pending", tuple(targets), parent_id, None)

    def maintenance_finish(self, scope: str, owner: str, batch_id: str, *, cancel: bool = False) -> maintenance.Batch:
        """原子领取、复核并提交整批和回执。重复提交返回已存结果。"""
        binding = self._maintenance_binding()
        with closing(self._open_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._maintenance_finish(conn, scope, owner, batch_id, binding, cancel=cancel)

    def _maintenance_finish(
        self, conn: sqlite3.Connection, scope: str, owner: str, batch_id: str, binding: str, *, cancel: bool,
    ) -> maintenance.Batch:
        row = conn.execute(
            "SELECT * FROM maintenance_batches WHERE id = ? AND scope = ? AND owner = ? AND binding = ?",
            (batch_id, scope, owner, binding),
        ).fetchone()
        if row is None or binding != self._maintenance_binding():
            raise ValueError("当前库、范围或会话没有这张整理预览。")
        batch = self._maintenance_batch(row)
        if batch.state != "pending":
            return batch
        moment = int(time.time())
        if cancel:
            state = "cancelled"
        elif moment >= row["deadline"] or row["version"] != self._maintenance_check(conn, scope, moment).version:
            state = "stale"
        else:
            state = "applied"
            self._apply_maintenance_batch(conn, batch, moment)
        if binding != self._maintenance_binding():
            raise ValueError("记忆库已更换，请重新打开。")
        conn.execute("UPDATE maintenance_batches SET state = ? WHERE id = ?", (state, batch_id))
        return self._maintenance_batch(conn.execute("SELECT * FROM maintenance_batches WHERE id = ?", (batch_id,)).fetchone())

    def _check_batch_archive(self, conn: sqlite3.Connection, mid: str) -> None:
        from March7.l0_profile import ProfileStore

        conflict = conn.execute(
            "SELECT 1 FROM conflicts WHERE (source_id = ? OR target_id = ?) AND status IS NOT 'dismissed' "
            "AND (status IS NOT 'resolved' OR should_ask_user = 1 OR resolution_type = 'uncertain') LIMIT 1",
            (mid, mid),
        ).fetchone()
        if conflict is not None:
            raise ValueError("这条仍有关联冲突待裁决，请先核对冲突。")
        row = conn.execute(_MANAGEMENT_ROW, (mid,)).fetchone()
        profile = ProfileStore(self.data_dir / config.PROFILE_FILENAME)
        if row["is_pinned"] or profile.matching_digests((row["content"], row["source_quote"])):
            raise ValueError("这条关联固定保留或画像，需要单独核对和处置。")

    def _apply_maintenance_batch(self, conn: sqlite3.Connection, batch: maintenance.Batch, moment: int) -> None:
        for target in batch.targets:
            status, revision = self._maintenance_revision(conn, batch.scope, target.id)
            if (status, revision) != (target.before, target.revision):
                raise ValueError("本批记录的状态或证据已经变化，请重新预览。")
            if target.after == "archived" and status != target.after:
                self._check_batch_archive(conn, target.id)
        if batch.kind == "undo":
            parent = conn.execute(
                "UPDATE maintenance_batches SET state = 'undone' WHERE id = ? AND state = 'applied'",
                (batch.parent_id,),
            )
            if parent.rowcount != 1:
                raise ValueError("原批次已经变化，请重新核对回执。")
        for target in batch.targets:
            if target.before != target.after:
                conn.execute("UPDATE memories SET status = ? WHERE id = ?", (target.after, target.id))
        final = [
            asdict(maintenance.Target(
                target.id, target.before, target.after, self._maintenance_revision(conn, batch.scope, target.id)[1],
            ))
            for target in batch.targets
        ]
        conn.execute(
            "UPDATE maintenance_batches SET targets = ?, applied_at = ? WHERE id = ?",
            (json.dumps(final), moment, batch.id),
        )

    def memories_containing(
        self, needles: tuple[str, ...], *, project_id: str | None
    ) -> list[Memory]:
        """只用明确提供的文字条件，精确匹配可见的正文、原话或别名。"""
        return [
            memory for memory in self.visible_memories(project_id)
            if any(
                needle in value
                for needle in needles
                for value in (memory.content, memory.source_quote or "", memory.trigger_text or "", memory.slug or "")
            )
        ]

    def list_recent(
        self, scope: str | None = None, since: int | None = None, limit: int = 50
    ) -> list[Memory]:
        """列出近期记忆（conflict.py 规则预检用：同 scope 近期写入 + 关键词重叠）。

        Args:
            scope: 命名空间过滤；None = 全部。
            since: Unix 秒，只取 created_at >= since 的；None = 不限。
            limit: 最多返回条数。

        Returns:
            按 created_at 倒序（同秒内按 rowid 倒序，保证结果确定性）的近期记忆。
        """
        sql = "SELECT * FROM memories"
        where: list[str] = []
        params: list[object] = []
        if scope is not None:
            where.append("scope = ?")
            params.append(scope)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_memory(r) for r in rows]

    def fts_search_any(
        self, text: str, limit: int = 20, *, eligible: Callable[[Memory], bool] | None = None
    ) -> list[Memory]:
        """多词 **OR** 检索：把 `text` 切成 token，**任一命中**即返回。

        为什么需要它：`fts_search` 走 `_fts_query`，它把查询切成 token 后用空格串起来
        ⇒ FTS5 的 MATCH 是**隐式 AND**（所有 token 都要命中）。
        后果（实测过的坑）：用户问「我做什么交易」会被切成
        「我做 / 做什 / 什么 / 么交 / 交易」五个 token 全部要求命中，
        而记忆正文是「做黄金 ETF 交易」⇒ **整句查不中**。

        ⇒ 宿主按用户提问做召回时必须用 OR 语义（本方法）；
        引擎的 `March7_recall` 仍走混合检索（RRF 融合 BM25 与向量），不受影响。

        Args:
            text: 原始查询文本（内部切 token）。
            limit: 返回条数上限。
            eligible: 可选的召回条件，满足条件的结果才占用 limit。

        Returns:
            命中的记忆列表（按 BM25 排序）；查询为空或投影为空时返回 `[]`。
        """
        tokens = list(dict.fromkeys(_tokenize(text)))
        if not tokens:
            return []
        # 长 token 优先（信息量更大）；只取前 16 个，避免 OR 表达式过长拖慢匹配
        picked = sorted(tokens, key=len, reverse=True)[:16]
        expression = " OR ".join(f'"{token}"' for token in picked)
        self.archive_due_memories()
        return self._fts_search(expression, limit, eligible)

    # ---- 两路检索原语（retriever.py 负责融合） ----

    def fts_search(
        self, query: str, limit: int = 20, *, eligible: Callable[[Memory], bool] | None = None
    ) -> list[Memory]:
        """FTS5 BM25 全文检索 top-limit（§3.3 管线的第一路）。

        查询走与入库同一套 CJK bigram 投影（见模块 docstring）。默认可查历史，
        传入 eligible 时按命中顺序取足满足条件的条目。

        查询投影为空（纯标点/空白）时直接返回 `[]`（FTS5 不接受空 MATCH 表达式）。
        """
        fts_query = _fts_query(query)
        if not fts_query:
            logger.debug("FTS 查询 %r 投影为空，跳过检索", query)
            return []
        self.archive_due_memories()
        return self._fts_search(fts_query, limit, eligible)

    def _fts_search(
        self, expression: str, limit: int, eligible: Callable[[Memory], bool] | None
    ) -> list[Memory]:
        """按 BM25 排序；过滤召回按顺序读游标，凑足合格条目即停止。"""
        sql = (
            "SELECT m.* FROM memories_fts f JOIN memories m ON m.rowid = f.rowid "
            "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts)"
        )
        if eligible is None:
            with closing(self._conn.execute(sql + " LIMIT ?", (expression, limit))) as cursor:
                return [_row_to_memory(row) for row in cursor]
        with closing(self._conn.execute(sql, (expression,))) as cursor:
            matches = filter(eligible, map(_row_to_memory, cursor))
            return list(islice(matches, max(limit, 0)))

    def vector_search(
        self, embedding: list[float], limit: int = 20, *,
        eligible: Callable[[Memory], bool] | None = None,
        max_distance: float | None = None,
    ) -> list[Memory]:
        """sqlite-vec 向量近邻检索 top-limit（§3.3 管线的第二路）。

        vec0 的 KNN 查询（`embedding MATCH ? AND k = ?`）是一条特殊路径，不能与其他
        主表过滤条件混用，故先取 id 再按序回主表。默认可查历史；传入 eligible
        时逐步扩大候选窗口，直到取足满足条件的条目或遍历完候选。

        Raises:
            RuntimeError: sqlite-vec 未就绪。
        """
        if not self._vec_ready:
            raise RuntimeError("sqlite-vec 未就绪：向量检索不可用（请用完整环境运行）。")
        self.archive_due_memories()
        encoded = self._serialize_embedding(embedding)
        if eligible is None and max_distance is None:
            rows = self._vector_knn(encoded, limit)
            return self._get_memories_in_order([row["memory_id"] for row in rows])
        if limit <= 0:
            return []

        with closing(self._iter_vector_neighbors(encoded, limit)) as candidates:
            neighbors = candidates if max_distance is None else takewhile(
                lambda row: row["distance"] <= max_distance, candidates,
            )
            matches = (self.get_memory(row["memory_id"]) for row in neighbors)
            visible = (memory for memory in matches if memory is not None and (eligible is None or eligible(memory)))
            return list(islice(visible, limit))

    def _vector_knn(self, encoded: bytes, limit: int) -> list[sqlite3.Row]:
        """按 L2 距离取 vec0 近邻，供召回与冲突候选筛复用。"""
        return self._conn.execute(
            "SELECT memory_id, distance FROM memories_vec "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (encoded, limit),
        ).fetchall()

    def _iter_vector_neighbors(self, encoded: bytes, limit: int) -> Generator[sqlite3.Row, None, None]:
        """扩大近邻窗口，达到 vec0 的 k 上限时用同一 L2 距离继续查。"""
        k = min(limit, _VEC_KNN_LIMIT)
        seen: set[str] = set()
        while True:
            rows = self._vector_knn(encoded, k)
            for row in rows:
                memory_id = row["memory_id"]
                if memory_id not in seen:
                    seen.add(memory_id)
                    yield row
            if len(rows) < k:
                return
            if k == _VEC_KNN_LIMIT:
                break
            k = min(k * 2, _VEC_KNN_LIMIT)

        # k 已不能再增大。用 vec0 的原始向量按相同距离排序，跳过已处理条目。
        with closing(self._conn.execute(
            "SELECT memory_id, vec_distance_L2(embedding, ?) AS distance "
            "FROM memories_vec ORDER BY distance", (encoded,)
        )) as cursor:
            for row in cursor:
                if row["memory_id"] not in seen:
                    yield row

    # ---- memory_sources 表（来源四元组，ADR-0016 决策 3）----

    def claim_conversation_operation(self, event_key: str) -> bool:
        """原子领取宿主事件；同一个事件只执行一次。"""
        with closing(self._open_connection()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO conversation_operations(event_key, created_at) VALUES (?, ?) "
                "ON CONFLICT(event_key) DO NOTHING", (event_key, int(time.time())),
            )
        return cur.rowcount == 1

    def allows_session_project(self, host: str, session_id: str, project_id: str | None) -> bool:
        """原子登记首个可信项目。已有会话换项目时需明确覆盖或新开会话。"""
        if not session_id or not project_id:
            return True
        args = (host, session_id)
        sql = "SELECT project_id FROM session_project_bindings WHERE host = ? AND session_id = ?"
        override = bool((os.environ.get(privacy.ENV_PROJECT) or "").strip())
        binding = self._conn.execute(sql, args).fetchone()
        if binding is not None and binding["project_id"] == project_id:
            if self._conn.execute(
                "SELECT 1 FROM session_project_drifts WHERE host = ? AND session_id = ? LIMIT 1", args,
            ).fetchone() is not None:
                with closing(self._open_connection()) as conn, conn:
                    conn.execute("DELETE FROM session_project_drifts WHERE host = ? AND session_id = ?", args)
            return True

        with closing(self._open_connection()) as conn, conn:
            conn.execute(
                "INSERT INTO session_project_bindings(host, session_id, project_id) VALUES (?, ?, ?) "
                "ON CONFLICT(host, session_id) DO NOTHING", (*args, project_id),
            )
            binding = conn.execute(sql, args).fetchone()
            matches = binding is not None and binding["project_id"] == project_id
            if not (matches or override):
                conn.execute(
                    "INSERT INTO session_project_drifts(host, session_id, project_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(host, session_id, project_id) DO NOTHING", (*args, project_id),
                )
            return matches or override

    def project_needs_confirmation(self, project_id: str) -> bool:
        """没有宿主会话身份的工具不能把有漂移冲突的目录当成已授权项目。"""
        if (os.environ.get(privacy.ENV_PROJECT) or "").strip():
            return False
        return self._conn.execute(
            "SELECT 1 FROM session_project_drifts WHERE project_id = ? LIMIT 1", (project_id,),
        ).fetchone() is not None

    def record_dsh_proof(
        self, session_id: str, message_id: str, project_id: str | None, digest: str,
    ) -> None:
        """登记原话摘要。相同消息 ID 的首次见证保持不变。"""
        now = int(time.time())
        with closing(self._open_connection()) as conn, conn:
            conn.execute("DELETE FROM dsh_source_proofs WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO dsh_source_proofs(session_id, message_id, project_id, digest, expires_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, message_id) DO NOTHING",
                (session_id, message_id, project_id, digest, now + 86400),
            )

    def matches_dsh_proof(
        self, session_id: str, message_id: str, project_id: str | None, digest: str,
    ) -> bool:
        """在同会话、同项目校对原话摘要和有效时间。"""
        return self._conn.execute(
            "SELECT 1 FROM dsh_source_proofs "
            "WHERE session_id = ? AND message_id = ? AND project_id IS ? AND digest = ? AND expires_at > ?",
            (session_id, message_id, project_id, digest, int(time.time())),
        ).fetchone() is not None

    def has_dsh_memory_evidence(self, memory_id: str) -> bool:
        """擦除核验通过记忆 ID 检查其判重摘要是否还有残留。"""
        return self._conn.execute(
            "SELECT 1 FROM dsh_memory_evidence WHERE memory_id = ?", (memory_id,),
        ).fetchone() is not None

    def attach_source(
        self, memory_id: str, source: dict[str, str | None], *, replace: bool = True,
    ) -> None:
        """把一条记忆的「来源四元组」写进 `memory_sources`（幂等：同 memory_id 覆盖写）。

        四元组 = `{session_id, message_id, revision, quote}`（ADR-0016 决策 3）。
        落库形态 = **一行一条来源，JSON 存**（`source_json`）；`session_id` / `message_id`
        两列是派生的查询键（见 DDL 注释），不构成第二份真相源。

        为什么要独立表而不是 `memories` 加列：ADR-0016 明确「JSON 存，不改表结构」，
        而 `memories` 的字段模型对应宪法级 `schemas.py`（增删字段要 ADR）。

        Args:
            memory_id: 已写入的 L2 记忆 id。
            source: 四元组字典；**只保留已知键**（未知键忽略），缺键记为 None。
            replace: False 时已有来源保留，仅在缺失时补齐。

        Raises:
            ValueError: `memory_id` 为空。
        """
        self._attach_source(self._conn, memory_id, source, replace=replace)
        self._conn.commit()

    @staticmethod
    def _attach_source(
        conn: sqlite3.Connection, memory_id: str, source: dict[str, str | None], *,
        replace: bool = True,
    ) -> None:
        """写入来源四元组，由调用方管理事务。"""
        if not memory_id:
            raise ValueError("attach_source 需要非空的 memory_id")
        payload = {
            key: (None if source.get(key) is None else str(source.get(key)))
            for key in ("session_id", "message_id", "revision", "quote")
        }
        conflict = (
            "DO UPDATE SET session_id = excluded.session_id, message_id = excluded.message_id, "
            "source_json = excluded.source_json"
            if replace else "DO NOTHING"
        )
        conn.execute(
            "INSERT INTO memory_sources (memory_id, session_id, message_id, source_json, created_at) "
            f"VALUES (?, ?, ?, ?, ?) ON CONFLICT(memory_id) {conflict}",
            (
                memory_id,
                payload["session_id"],
                payload["message_id"],
                json.dumps(payload, ensure_ascii=False),
                int(time.time()),
            ),
        )

    def get_source(self, memory_id: str) -> dict[str, str | None] | None:
        """取一条记忆的来源四元组；没有记录则返回 None。"""
        row = self._conn.execute(
            "SELECT source_json FROM memory_sources WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return self._decode_source(row["source_json"] if row else None)

    @staticmethod
    def _decode_source(raw: str | None) -> dict[str, str | None] | None:
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("memory_sources.source_json 解析失败，按无来源处理")
            return None
        return data if isinstance(data, dict) else None

    def has_source_message(self, session_id: str, message_id: str) -> bool:
        """该会话下是否已有「来自这条宿主消息」的记忆。

        这是 **Stop 钩子每轮幂等**的判据：宿主每轮结束都会触发 Stop，若不查一下，
        同一批用户消息会被反复跑 Judge（重复写入 + 重复烧 LLM 调用）。
        """
        if not session_id or not message_id:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM memory_sources WHERE session_id = ? AND message_id = ? LIMIT 1",
            (session_id, message_id),
        ).fetchone()
        return row is not None

    # ---- ingest_marks 表（宿主写入水位线）----

    def is_ingested(self, session_id: str, message_id: str) -> bool:
        """这条宿主消息是否已经被处理过（Stop 钩子的每轮幂等判据）。"""
        if not session_id or not message_id:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM ingest_marks WHERE session_id = ? AND message_id = ? LIMIT 1",
            (session_id, message_id),
        ).fetchone()
        return row is not None

    def mark_ingested(self, session_id: str, message_id: str) -> None:
        """标记「这条宿主消息已处理」（重复标记是幂等的）。"""
        if not session_id or not message_id:
            raise ValueError("mark_ingested 需要非空的 session_id 与 message_id")
        self._conn.execute(
            "INSERT INTO ingest_marks (session_id, message_id, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id, message_id) DO NOTHING",
            (session_id, message_id, int(time.time())),
        )
        self._conn.commit()

    def count_ingest_marks(self, since: int | None = None, until: int | None = None) -> int:
        """统计水位线条数 = **宿主真正处理过的用户消息数**（S1 的对账分母）。

        为什么这个分母不能用召回账本自己算：账本是"我们记了什么"，水位线是
        "宿主给了什么"。钩子根本没触发时账本一行都没有，自己对自己永远是 0 = 0；
        只有拿宿主侧的消息数当分母，才看得出"这一轮漏记了"。

        Args:
            since: 起始 Unix 秒（含）；None = 不限。
            until: 结束 Unix 秒（不含）；None = 不限。

        Returns:
            条数（`ingest_marks` 主键是 (session_id, message_id)，天然去重）。
        """
        sql = "SELECT COUNT(*) FROM ingest_marks WHERE 1 = 1"
        params: list[int] = []
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(int(since))
        if until is not None:
            sql += " AND created_at < ?"
            params.append(int(until))
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    # ---- projects 表（项目身份的库里映射，S1 / ADR-0037）----

    def get_project_id(self, root_hash: str) -> str | None:
        """按 `root_hash` 查已登记的项目假名（**root_hash 的唯一合法用法**）。

        为什么查库而不是每次重算：`project_id` 的派生规则将来可能变（长度、算法），
        库里已有的映射是**既成事实**——重算会让同一个项目在历史记忆与新会话之间
        变成两个身份，接力断在中间。
        """
        if not root_hash:
            return None
        row = self._conn.execute(
            "SELECT project_id FROM projects WHERE root_hash = ? LIMIT 1", (root_hash,)
        ).fetchone()
        return str(row[0]) if row else None

    def put_project(self, root_hash: str, project_id: str) -> None:
        """登记 `root_hash → project_id`（幂等：已存在则只刷新 `last_seen_at`）。

        ⚠️ **`norm_version` 只在首次插入时写**，后续 `last_seen_at` 刷新不动它。
        理由：这一列记的是"这行是按哪一版归一化规则算出来的 `root_hash`"。
        如果每次见到就覆盖成当前版本，那么"规则变过"这件事会被**下一次启动抹掉**，
        `norm_mismatch` 永远不报——而这正是它存在的唯一目的。

        Raises:
            ValueError: 两个参数为空——空值会让所有"未解析"的项目塌缩成同一个身份。
        """
        if not root_hash or not project_id:
            raise ValueError("put_project 需要非空的 root_hash 与 project_id")

        now = int(time.time())
        self._conn.execute(
            "INSERT INTO projects (project_id, root_hash, created_at, last_seen_at, norm_version) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(root_hash) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (project_id, root_hash, now, now, int(privacy.NORM_VERSION)),
        )
        self._conn.commit()

    def get_project_norm_version(self, root_hash: str) -> int | None:
        """按 `root_hash` 读已登记的归一化规则版本（`norm_mismatch` 的另一半）。

        Returns:
            版本号；该 `root_hash` 未登记 ⇒ `None`（新项目，谈不上"版本不一致"）。
        """
        if not root_hash:
            return None
        row = self._conn.execute(
            "SELECT norm_version FROM projects WHERE root_hash = ? LIMIT 1", (root_hash,)
        ).fetchone()
        return int(row[0]) if row is not None else None

    # ---- claims 表（claim 层，ADR-0018）----
    def insert_claim(self, claim: Claim) -> None:
        """写入一条 claim（`memory_id` 必须已存在；主键 / 时间戳缺失时自动补）。"""
        self.insert_claims(claim.memory_id, [claim])

    def insert_claims(self, memory_id: str, claims: list[Claim]) -> int:
        """批量写入某条记忆的 claim，返回写入条数（一次事务，要么全成）。

        claim 的 `memory_id` / `scope` 由调用方在构造时决定；这里只补 `id` 与
        `created_at`（缺省时），并**强制把 memory_id 归一到入参**——防止调用方把
        上一批的 claim 对象错挂到这条记忆上（这类错会静默产生虚假冲突记录）。

        Args:
            memory_id: 记忆 id。
            claims: 该记忆的 claim 列表；空列表是无操作。

        Raises:
            ValueError: `memory_id` 为空（claim 挂空 = 永远筛不出来的孤儿行）。
        """
        if not memory_id:
            raise ValueError("insert_claims 需要非空的 memory_id")
        if not claims:
            return 0
        now = int(time.time())
        rows: list[tuple[object, ...]] = []
        for claim in claims:
            rows.append(
                (
                    claim.id or str(uuid.uuid4()),
                    memory_id,
                    claim.subject or "用户",
                    claim.predicate,
                    claim.value,
                    claim.scope or "global",
                    claim.valid_from,
                    claim.valid_to,
                    claim.status.value if isinstance(claim.status, ClaimStatus) else str(claim.status),
                    claim.created_at or now,
                )
            )
        try:
            self._conn.executemany(
                "INSERT INTO claims "
                "(id, memory_id, subject, predicate, value, scope, valid_from, valid_to, "
                " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return len(rows)

    def list_claims(self, memory_id: str) -> list[Claim]:
        """取一条记忆的全部 claim（含已失效的：失效不删除，历史可查）。"""
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE memory_id = ? ORDER BY created_at ASC, id ASC",
            (memory_id,),
        ).fetchall()
        return [_row_to_claim(r) for r in rows]

    def find_claim_conflicts(
        self,
        subject: str,
        predicate: str,
        scope: str,
        *,
        exclude_memory_id: str | None = None,
        at: int | None = None,
        limit: int = 20,
    ) -> list[Claim]:
        """规则筛（`ADR-0018` 后果 A）：同主体 + 同属性 + 同范围 + 时间重叠 + 仍有效。

        这是 T16 **替代「关键词重叠」**的主判据（旧方案在实现上从未生效：`keywords`
        一直是空列表，规则预检永远筛不出东西）。**只做等值与区间比较，不做语义判断**
        ——「值到底冲不冲突」交给 LLM 四分类，规则筛的职责只是把候选压到个位数。

        时间重叠口径：`valid_from IS NULL` 视为不设下界、`valid_to IS NULL` 视为仍有效
        ⇒ v1 全 NULL 时恒重叠（`ADR-0031` 记录了这个「v1 不抽时间」的口径）。

        ⚠️ 额外 JOIN `memories` 过滤 `active/aging`：claim 的 status 是与记忆同步维护的，
        但**记忆可能被别的路径置为失效**（导出、将来的 tombstone）。只信 claim.status
        会让「已失效事实」继续参与比对——那是最坏的一类错：拿旧事实判新事实冲突。

        Args:
            subject: 主体（v1 恒「用户」）。
            predicate: 归一后的维度。
            scope: 作用域（`project:a` 不与 `global` 混比）。
            exclude_memory_id: 排除自身（同一条记忆的 claim 之间不算冲突）。
            at: 判定时刻（Unix 秒）；None = 现在。
            limit: 最多返回条数（规则筛是保守召回，不是精确判定）。

        Returns:
            命中的既有 claim（按 created_at 倒序）。
        """
        moment = int(time.time()) if at is None else int(at)
        sql = (
            "SELECT c.* FROM claims c JOIN memories m ON m.id = c.memory_id "
            "WHERE c.subject = ? AND c.predicate = ? AND c.scope = ? "
            "AND c.status = ? AND m.status IN ('active', 'aging') "
            "AND (c.valid_to IS NULL OR c.valid_to >= ?) "
            "AND (c.valid_from IS NULL OR c.valid_from <= ?)"
        )
        params: list[object] = [subject, predicate, scope, ClaimStatus.ACTIVE.value, moment, moment]
        if exclude_memory_id is not None:
            sql += " AND c.memory_id != ?"
            params.append(exclude_memory_id)
        # 同秒写入时按 rowid 倒序：`c.id` 是 uuid4，用它排序会让「先命中哪条」随运行变化，
        # 而 `conflict.pre_check` 的 `seen_targets` 是先到先得 ⇒ 落库 reason 文案不稳
        # （独立验收 2026-11-05 指出，与 `list_conflicts` 同一类问题）
        sql += " ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_claim(r) for r in rows]

    def supersede_claims(self, memory_id: str, status: ClaimStatus = ClaimStatus.SUPERSEDED) -> int:
        """把一条记忆的全部 claim 标为失效（`ADR-0018` 决策 6：裁决后同步，**不删除**）。

        Returns:
            受影响的行数（幂等：已失效的不会再改，返回 0 说明本来就没有有效 claim）。
        """
        changed = self._supersede_claims(self._conn, memory_id, status)
        self._conn.commit()
        return changed

    @staticmethod
    def _supersede_claims(conn: sqlite3.Connection, memory_id: str, status: ClaimStatus) -> int:
        """更新活跃 claim 的状态，由调用方管理事务。"""
        cur = conn.execute(
            "UPDATE claims SET status = ? WHERE memory_id = ? AND status = ?",
            (status.value, memory_id, ClaimStatus.ACTIVE.value),
        )
        return int(cur.rowcount or 0)

    def update_conflict_with(self, memory_id: str, target_ids: list[str]) -> list[str]:
        """把 `target_ids` 并进记忆的 `conflict_with` 列（幂等，不去重已有项）。

        为什么是「并集」而不是「覆盖」：同一轮里一条新记忆可能被多条既有记忆冲突，
        而 `pre_check` 是逐 claim 生成冲突的；覆盖写会让先发现的那条冲突在
        `Memory.conflict_with` 里消失（`conflicts` 表里还在，但记忆自己"忘了"）。

        Returns:
            更新后的 `conflict_with` 列表。
        """
        memory = self.get_memory(memory_id)
        if memory is None:
            logger.warning("update_conflict_with 目标记忆不存在：%s（忽略）", memory_id)
            return []
        merged = list(dict.fromkeys([*memory.conflict_with, *target_ids]))
        self._conn.execute(
            "UPDATE memories SET conflict_with = ? WHERE id = ?",
            (_json_dump(merged), memory_id),
        )
        self._conn.commit()
        return merged

    # ---- claim 回填标记（T16b，库 v3）----

    def mark_claim_backfill(self, memory_id: str, outcome: str, *, at: int | None = None) -> None:
        """记录「这条记忆已被回填脚本处理过」（幂等：同 id 覆盖写）。

        为什么需要它：抽取器按提示词拒掉「任务流水账」是**正常结果**，不是失败。
        只按「有没有 claim」判断待办，会让这些记忆永远排在队列里、每次重跑都重新付费。

        Args:
            memory_id: 记忆 id。
            outcome: `filled`（写了 claim）/ `empty`（模型明确说没有稳定事实）/ `failed`（调用失败）。
            at: Unix 秒；None ⇒ 现在。
        """
        if outcome not in _BACKFILL_OUTCOMES:
            raise ValueError(f"未知的回填结果 {outcome!r}（只允许 {sorted(_BACKFILL_OUTCOMES)}）")
        self._conn.execute(
            "INSERT INTO claim_backfill_marks (memory_id, attempted_at, outcome) VALUES (?, ?, ?) "
            "ON CONFLICT(memory_id) DO UPDATE SET "
            "attempted_at = excluded.attempted_at, outcome = excluded.outcome",
            (memory_id, int(time.time()) if at is None else int(at), outcome),
        )
        self._conn.commit()

    def claim_backfill_outcomes(self) -> dict[str, str]:
        """读出所有回填尝试结果（memory_id → outcome）；没试过的记忆不在字典里。"""
        rows = self._conn.execute(
            "SELECT memory_id, outcome FROM claim_backfill_marks"
        ).fetchall()
        return {str(row["memory_id"]): str(row["outcome"]) for row in rows}

    # ---- conflicts 表 ----

    def insert_conflict(self, conflict: Conflict) -> None:
        """写入一条冲突记录。"""
        self._conn.execute(
            "INSERT INTO conflicts "
            "(id, created_at, status, source_id, target_id, reason, confidence, detector, "
            " resolution_type, should_ask_user) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conflict.id,
                conflict.created_at,
                conflict.status.value,
                conflict.source_id,
                conflict.target_id,
                conflict.reason,
                conflict.confidence,
                conflict.detector.value,
                conflict.resolution_type.value if conflict.resolution_type is not None else None,
                int(conflict.should_ask_user),
            ),
        )
        self._conn.commit()

    def get_conflict(self, conflict_id: str) -> Conflict | None:
        """按主键取一条冲突记录。"""
        row = self._conn.execute("SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
        return None if row is None else _row_to_conflict(row)

    def list_conflicts(self, status: ConflictStatus | None = None) -> list[Conflict]:
        """列出冲突记录；status=None 列全部，否则按状态过滤（March7_conflicts 列 pending 用）。"""
        if status is None:
            rows = self._conn.execute(
                # rowid 而不是 id：`id` 是 uuid4，同一秒内多条冲突的排序会随机
                # ⇒ `March7_conflicts` 与机制面断言取到的"第一条"每次运行都不同
                # （独立验收 2026-11-05 实测：同一命令 6 次得到 2/3/4/3/2/3 条命中）。
                "SELECT * FROM conflicts ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM conflicts WHERE status = ? "
                "ORDER BY created_at DESC, rowid DESC",
                (status.value,),
            ).fetchall()
        return [_row_to_conflict(r) for r in rows]

    def update_conflict(self, conflict: Conflict) -> None:
        """回写冲突记录（裁决结果落库）。

        更新除 `id` / `created_at` 外的全部列（id 是主键、created_at 是发现时刻，均不可变）。
        """
        self._conn.execute(
            "UPDATE conflicts SET status = ?, source_id = ?, target_id = ?, reason = ?, "
            "confidence = ?, detector = ?, resolution_type = ?, should_ask_user = ? "
            "WHERE id = ?",
            (
                conflict.status.value,
                conflict.source_id,
                conflict.target_id,
                conflict.reason,
                conflict.confidence,
                conflict.detector.value,
                conflict.resolution_type.value if conflict.resolution_type is not None else None,
                int(conflict.should_ask_user),
                conflict.id,
            ),
        )
        self._conn.commit()

    # ---- episodes 表 ----

    def insert_episode(self, episode: Episode) -> None:
        """写入一条会话时间线记录。"""
        self._conn.execute(
            "INSERT INTO episodes (id, started_at, ended_at, host, conversation_id, summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                episode.id,
                episode.started_at,
                episode.ended_at,
                episode.host,
                episode.conversation_id,
                episode.summary,
            ),
        )
        self._conn.commit()

    # ---- 统计（March7_status） ----

    def stats(self) -> StatusResult:
        """返回记忆条数 / 库文件大小 / 最近写入 + 健康与容量指标（§3.5 March7_status 出参）。

        口径声明：
        - `memory_count` = memories 表**全部行数**（含 superseded/merged —— 失效不删除，
          历史也是记忆；要「可召回条数」请按 status 过滤后自行统计）；
        - `recallable_count` = status ∈ {active, aging}（真正参与召回的条数，`ADR-0026`）；
        - `db_size_bytes` = 主库文件 + `-wal` + `-shm`（WAL 下未 checkpoint 的新数据在
          `-wal` 里，只算主文件会**低估**占用）；
        - `last_write_at` = `MAX(created_at)`；空库为 None；
        - `today_writes` = `created_at ≥ 本地零点` 的条数（熔断判据，`ADR-0026`）；
        - `daily_write_cap` = 当前 `MARCH7_DAILY_WRITE_CAP`（0 = 不限）；
        - `march7_version` / `db_schema_version` / `backup_available` = 版本三元组
          （S11b / `ADR-0058` 决策 2）。`db_schema_version` 与 `schema_version` 恒等；
          `backup_available` 看数据目录里有没有 `.bak-v*`（能据此告诉用户"可恢复"）。
        """
        memory_count = int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        recallable_count = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE status IN ('active', 'aging')"
            ).fetchone()[0]
        )
        last_write_at = self._conn.execute("SELECT MAX(created_at) FROM memories").fetchone()[0]
        db_size = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self._db_path}{suffix}")
            if candidate.exists():
                db_size += os.path.getsize(candidate)
        version = self.schema_version
        return StatusResult(
            memory_count=memory_count,
            db_size_bytes=db_size,
            last_write_at=int(last_write_at) if last_write_at is not None else None,
            schema_version=version,
            recallable_count=recallable_count,
            today_writes=self.count_since(config.start_of_local_day()),
            daily_write_cap=config.daily_write_cap(),
            march7_version=_package_version(),
            db_schema_version=version,
            backup_available=_has_backup(self.data_dir),
        )

    # ---- handoffs 表（显式接力窗口，S2 / ADR-0041）----

    def put_handoff(
        self,
        project_id: str | None,
        handoff_id: str,
        *,
        started_by_host: str,
        started_at: int | None = None,
    ) -> None:
        """开一个 handoff（`state='open'`）。

        Raises:
            ValueError: `handoff_id` / `started_by_host` 为空。
            sqlite3.IntegrityError: 同一项目已有 `open` 的 handoff（`idx_one_open_handoff`）。

        ⚠️ **这个 IntegrityError 是设计的一部分**（`ADR-0042` R6）：应用层先查"有没有 open"
        在并发下必然漏（两宿主同时开 ⇒ 两次 SELECT 都见 0 行），所以让数据库来判。
        调用方要把它翻成可读的提示（「请先结束当前 handoff」），而不是吞掉。
        """
        if not handoff_id or not started_by_host:
            raise ValueError("put_handoff 需要非空的 handoff_id 与 started_by_host")
        self._conn.execute(
            "INSERT INTO handoffs (project_id, handoff_id, started_by_host, state, started_at) "
            "VALUES (?, ?, ?, 'open', ?)",
            (project_id, handoff_id, started_by_host, int(started_at or time.time())),
        )
        self._conn.commit()

    def get_handoff_state(self, handoff_id: str) -> str | None:
        """handoff 的当前状态（不存在 ⇒ `None`）。"""
        row = self._conn.execute(
            "SELECT state FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        return str(row[0]) if row else None

    def get_open_handoff(self, project_id: str | None) -> str | None:
        """该项目当前 `open` 的 handoff id（没有 ⇒ `None`）。

        `project_id` 为 `None` 时按 `IS NULL` 查：global-only 的 handoff 与
        「有项目」的 handoff 是两回事，不能混成一个桶。
        """
        if project_id is None:
            row = self._conn.execute(
                "SELECT handoff_id FROM handoffs WHERE project_id IS NULL AND state = 'open' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT handoff_id FROM handoffs WHERE project_id = ? AND state = 'open' "
                "ORDER BY started_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def set_handoff_state(
        self,
        handoff_id: str,
        state: str,
        *,
        reviewed_by: str | None = None,
    ) -> None:
        """改 handoff 状态（`open` / `pending_close` / `closed`）。

        Raises:
            ValueError: 状态不在三态内；或 `closed` 缺 `reviewed_by`
                （「谁确认的」不能是空——`ADR-0038` 决策 4 的删除/归档同一条纪律：
                用户可见的状态变化必须有责任人）。
        """
        if state not in _HANDOFF_STATES:
            raise ValueError(f"handoff 状态必须是 {sorted(_HANDOFF_STATES)} 之一，收到 {state!r}")
        if state == "closed" and not (reviewed_by or "").strip():
            raise ValueError("把 handoff 置为 closed 必须带 reviewed_by（谁确认结束的）")
        if self.get_handoff_state(handoff_id) is None:
            raise ValueError(f"handoff 不存在：{handoff_id}")
        self._conn.execute(
            "UPDATE handoffs SET state = ?, closed_at = ?, reviewed_by = ? WHERE handoff_id = ?",
            (
                state,
                int(time.time()) if state == "closed" else None,
                reviewed_by if state == "closed" else None,
                handoff_id,
            ),
        )
        self._conn.commit()

    def count_stale_handoffs(self, now: int | None = None, days: int = 14) -> int:
        """开了超过 `days` 天仍未 `closed` 的 handoff 数（`doctor` 的 `stale_open_handoffs`）。"""
        cutoff = int(now or time.time()) - days * 86400
        row = self._conn.execute(
            "SELECT COUNT(*) FROM handoffs "
            "WHERE state IN ('open','pending_close') AND started_at < ?",
            (cutoff,),
        ).fetchone()
        return int(row[0]) if row else 0

    # ---- working_items 表（工作层，S2 / ADR-0041）----

    def working_watermark(self) -> int:
        """当前最大 `seq`（空表 ⇒ 0）。

        ⚠️ **必须在任何写之前取一次**（`ADR-0042` R3）：写之后再取，本轮刚插入的条目
        会落进 `seq <= watermark` ⇒ 用户当轮就看到自己刚说的话被"回忆"。
        这条纪律靠调用顺序保证，不是靠函数自觉 ⇒ `user_prompt_submit.handle()` 里
        取水位线是**第一件事**（在检索与写入之前）。
        """
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM working_items").fetchone()
        return int(row[0]) if row else 0

    def add_working_item(
        self,
        *,
        handoff_id: str,
        content: str,
        kind: str,
        host: str,
        session_id: str,
        message_id: str | None = None,
        project_id: str | None = None,
        source_quote: str | None = None,
        event_ts: int | None = None,
    ) -> int:
        """写一条 working item，返回它的 `seq`；**幂等**（重复文本返回 0）。

        Args:
            handoff_id: 所属 handoff（必须是 `open` 的那一个——调用方负责判）。
            content: 抽取出来的那条进展/约束（已脱敏）。
            kind: `constraint` / `state` / `scratch`（`ADR-0039` 闭集）。
            host: 写入宿主。
            session_id: 宿主会话 id。
            message_id: 宿主消息 id（可空；幂等主键是 `content_hash` 不是它）。
            project_id: 项目假名（`None` = global-only）。
            source_quote: 用户原话（**已脱敏**；`ADR-0040` 的删除面第 9 处要清它）。
            event_ts: 宿主时间（缺省 = 现在）。

        Returns:
            `seq`（新写入）；`0` = 同 `(handoff_id, content_hash)` 已存在，本次没写。

        Raises:
            ValueError: `kind` 不在闭集内、或 `content` 归一化后为空。
        """
        if kind not in _WORKING_KINDS:
            raise ValueError(f"working kind 必须是 {sorted(_WORKING_KINDS)} 之一，收到 {kind!r}")
        text = (content or "").strip()
        if not text:
            raise ValueError("working item 的 content 不能为空")
        digest = working_content_hash(text)
        now = int(time.time())
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO working_items "
            "(id, host, session_id, message_id, handoff_id, project_id, kind, content, "
            " source_quote, content_hash, event_ts, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                f"w-{uuid.uuid4().hex[:12]}",
                host,
                session_id,
                message_id,
                handoff_id,
                project_id,
                kind,
                text,
                source_quote,
                digest,
                int(event_ts or now),
                now,
            ),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return 0
        row = self._conn.execute(
            "SELECT seq FROM working_items WHERE handoff_id = ? AND content_hash = ?",
            (handoff_id, digest),
        ).fetchone()
        return int(row[0]) if row else 0

    def list_injectable_working(
        self,
        project_id: str | None,
        *,
        watermark: int | None = None,
        limit: int = 2,
    ) -> list[dict[str, object]]:
        """读出**可以注入**的 working 条目（A3 的库层一半）。

        四个条件同时成立才注入（缺一条就会出事故，逐条都有 ADR）：

        1. handoff 是 `open`（`pending_close` / `closed` 都 0 注入——`ADR-0042` R5）；
        2. 条目自身 `status='active'`；
        3. `seq <= watermark`（本轮刚写的最早下一轮可见——`ADR-0042` R3）；
        4. 项目匹配：`project_id` 相同，或条目是 `global-only`（`project_id IS NULL`）。

        ⚠️ **`project_id` 为 `None` 时只返回 `project_id IS NULL` 的条目**，
        不返回「所有项目」的——那等于把 A 项目的约束灌进 B 项目（`ADR-0039` 决策 3）。

        Returns:
            `[{seq, content, kind, source_quote, handoff_id, project_id}]`，按 `seq` 升序
            （时间序，先说的先注入——后说的往往是对先说的修正）。
        """
        conditions = [
            "w.status = 'active'",
            "h.state = 'open'",
        ]
        params: list[object] = []
        if watermark is not None:
            conditions.append("w.seq <= ?")
            params.append(int(watermark))
        if project_id is None:
            conditions.append("w.project_id IS NULL")
        else:
            conditions.append("(w.project_id = ? OR w.project_id IS NULL)")
            params.append(project_id)
        sql = (
            "SELECT w.seq, w.content, w.kind, w.source_quote, w.handoff_id, w.project_id "
            "FROM working_items w JOIN handoffs h ON h.handoff_id = w.handoff_id "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY w.seq ASC LIMIT ?"
        )
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "seq": int(row["seq"]),
                "content": str(row["content"]),
                "kind": str(row["kind"]),
                "source_quote": row["source_quote"],
                "handoff_id": str(row["handoff_id"]),
                "project_id": row["project_id"],
            }
            for row in rows
        ]

    def close_working_item(self, seq: int) -> None:
        """把一条 working 条目标 `closed`（不再注入，但行保留、可审计）。"""
        self._conn.execute(
            "UPDATE working_items SET status = 'closed' WHERE seq = ?", (int(seq),)
        )
        self._conn.commit()

    def count_working_items(self, handoff_id: str | None = None) -> int:
        """统计 working 条数（按 handoff 过滤可选；`doctor` / 单测用）。"""
        if handoff_id is None:
            row = self._conn.execute("SELECT COUNT(*) FROM working_items").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM working_items WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    # ---- memory_meta 表（长期记忆旁挂元数据，S2 / ADR-0039）----

    def put_memory_meta(
        self,
        memory_id: str,
        *,
        category: str | None = None,
        subtype: str | None = None,
        verification: str = "unverified",
        sensitivity: str = "normal",
        salience_reason: str | None = None,
        last_confirmed_at: int | None = None,
    ) -> None:
        """写/更新一条记忆的 metadata（**幂等 upsert**：以 `memory_id` 为准）。

        Raises:
            ValueError: `memory_id` 为空（挂空 = 永远读不到的孤儿行）。
        """
        if not memory_id:
            raise ValueError("put_memory_meta 需要非空的 memory_id")
        self._conn.execute(
            "INSERT INTO memory_meta "
            "(memory_id, category, subtype, verification, sensitivity, salience_reason, "
            " last_confirmed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(memory_id) DO UPDATE SET "
            "  category=excluded.category, subtype=excluded.subtype, "
            "  verification=excluded.verification, sensitivity=excluded.sensitivity, "
            "  salience_reason=excluded.salience_reason, "
            "  last_confirmed_at=excluded.last_confirmed_at",
            (
                memory_id,
                category,
                subtype,
                verification,
                sensitivity,
                salience_reason,
                last_confirmed_at,
                int(time.time()),
            ),
        )
        self._conn.commit()

    def get_memory_meta(self, memory_id: str) -> dict[str, object] | None:
        """读一条记忆的 metadata（没有 ⇒ `None`，**不编默认值**）。"""
        row = self._conn.execute(
            "SELECT * FROM memory_meta WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return dict(row) if row else None

    def count_memory_meta(self, with_category: bool = False) -> int:
        """统计 metadata 行数（`with_category=True` 时只算 `category` 非空的）。"""
        if with_category:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM memory_meta WHERE category IS NOT NULL AND category != ''"
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM memory_meta").fetchone()
        return int(row[0]) if row else 0

    # ---- host_registry 表（宿主登记，S2 / ADR-0038）----

    def put_host_registry(self, host: str, data_dir_key: str, *, project_source: str | None = None) -> None:
        """登记一个宿主（幂等 upsert；`cwd_hashes_json` 保留原值不覆盖）。

        Raises:
            ValueError: `host` / `data_dir_key` 为空。
        """
        if not host or not data_dir_key:
            raise ValueError("put_host_registry 需要非空的 host 与 data_dir_key")
        now = int(time.time())
        self._conn.execute(
            "INSERT INTO host_registry "
            "(host, data_dir_key, schema_version, cwd_hashes_json, project_source, last_seen_at) "
            "VALUES (?, ?, ?, '{}', ?, ?) "
            "ON CONFLICT(host) DO UPDATE SET "
            "  data_dir_key=excluded.data_dir_key, schema_version=excluded.schema_version, "
            "  project_source=COALESCE(excluded.project_source, host_registry.project_source), "
            "  last_seen_at=excluded.last_seen_at",
            (host, data_dir_key, self.schema_version, project_source, now),
        )
        self._conn.commit()

    def get_host_registry(self, host: str) -> dict[str, object] | None:
        """读一个宿主的登记行（`cwd_hashes_json` 已解析成 `dict`）。"""
        row = self._conn.execute("SELECT * FROM host_registry WHERE host = ?", (host,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["cwd_hashes_json"] = _json_dict(data.get("cwd_hashes_json"))
        return data

    def record_host_cwd(self, host: str, cwd_hash: str, project_id: str) -> None:
        """把 `cwd_hash → project_id` 记进该宿主的映射（**CAS 读改写**）。

        Raises:
            ValueError: 同一 `cwd_hash` 在本宿主已有**不同** `project_id`（分叉），
                或库里另一宿主已把它映射到别的项目（`duplicate_cwd_hash`）。

        ⚠️ 为什么不是「只覆盖」：两宿主若把同一个 cwd 算成两个项目，症状是
        「换宿主就找不到项目」，而根因是归一化规则漂了。这里**响亮失败**，
        让问题在写入时就暴露，而不是等用户发现记忆对不上。
        """
        if not host or not cwd_hash or not project_id:
            raise ValueError("record_host_cwd 需要非空的 host / cwd_hash / project_id")
        row = self._conn.execute("SELECT * FROM host_registry WHERE host = ?", (host,)).fetchone()
        if row is None:
            raise ValueError(f"宿主未登记：{host}（先调用 put_host_registry）")
        mapping = _json_dict(row["cwd_hashes_json"])
        existing = mapping.get(cwd_hash)
        if existing is not None and existing != project_id:
            raise ValueError(
                f"cwd_hash {cwd_hash} 在本宿主已映射到 {existing}，不能改指 {project_id}"
            )
        # 跨宿主：同一 cwd_hash 必须映射到同一个 project_id（否则是分叉）
        others = self._conn.execute(
            "SELECT host, cwd_hashes_json FROM host_registry WHERE host != ?", (host,)
        ).fetchall()
        for other in others:
            other_mapping = _json_dict(other["cwd_hashes_json"])
            found = other_mapping.get(cwd_hash)
            if found is not None and found != project_id:
                raise ValueError(
                    f"duplicate_cwd_hash：{other['host']} 把 {cwd_hash} 映射到 {found}，"
                    f"本宿主是 {project_id} ⇒ 两宿主的数据目录或归一化规则不一致"
                )
        mapping[cwd_hash] = project_id
        self._conn.execute(
            "UPDATE host_registry SET cwd_hashes_json = ?, last_seen_at = ? WHERE host = ?",
            (json.dumps(mapping, ensure_ascii=False, sort_keys=True), int(time.time()), host),
        )
        self._conn.commit()

    # ---- project_basenames 表（拆分诊断，S3 / A5）----

    def record_basename(self, basename: str, project_id: str, *, at: int | None = None) -> None:
        """登记一个 `(basename, project_id)`（幂等：已存在则保留首次时间）。

        ⚠️ 只存目录末段，**不存路径、不存 root_hash**（见 `_PROJECT_BASENAMES_DDL`）。

        Raises:
            ValueError: 任一参数为空——空的 basename 会把所有项目塌缩成同一个兄弟。
        """
        if not basename or not project_id:
            raise ValueError("record_basename 需要非空的 basename 与 project_id")
        self._conn.execute(
            "INSERT INTO project_basenames (basename, project_id, first_seen_at) "
            "VALUES (?, ?, ?) ON CONFLICT(basename, project_id) DO NOTHING",
            (basename.casefold(), project_id, int(at if at is not None else time.time())),
        )
        self._conn.commit()

    def sibling_basenames(
        self, basename: str, project_id: str, *, since: int
    ) -> list[str]:
        """同一 basename 下、`first_seen_at >= since` 的**其他** project_id。

        排序保证确定性（诊断输出要能逐字比对）。
        """
        if not basename:
            return []
        rows = self._conn.execute(
            "SELECT project_id FROM project_basenames "
            "WHERE basename = ? AND project_id != ? AND first_seen_at >= ? "
            "ORDER BY project_id",
            (basename.casefold(), project_id, int(since)),
        ).fetchall()
        return [str(row[0]) for row in rows]

    # ---- erase：用户显式物理删除（S5 / ADR-0040）----
    #
    # ⚠️ **本段是全仓唯一的 DELETE 聚集地**。上面所有方法的「失效不删除」纪律没有变：
    # 自动管线（Judge / UPS / 整理）**不得**调用本段任何方法——只有用户显式 erase 才走。
    # 这条由 `test_erase_store.py` 与 `test_control_cli.py` 双重把守。

    def fts_hits_memory_id(self, memory_id: str) -> bool:
        """这条记忆**还能不能**被 FTS5 索引命中（删除后判残留的**唯一**可信判据）。

        ## 为什么不能用 `COUNT(*) FROM memories_fts`

        它是 `content='memories'` 的**外部内容表**：`COUNT(*)` 走的是主表，
        主表删了它自然返回 0 ⇒ 看起来"删干净了"，而索引里的 CJK 投影**还在**。
        实测（2026-11-11）：主表删掉后 `MATCH '黄金'` 仍命中 `rowid=1`，
        而 `SELECT COUNT(*)` 返回 0、`SELECT ... WHERE rowid NOT IN (SELECT rowid FROM memories)`
        也返回 0（外部内容表连 `rowid` 都从主表解析，孤儿行对它**不可见**）。

        ## 唯一能看见孤儿的办法：`fts5vocab`

        `CREATE VIRTUAL TABLE ... USING fts5vocab(<fts>, 'row')` 的 `doc` 列
        直接从**索引结构**读 docid，不经过主表 ⇒ 孤儿行在这里现形。

        Returns:
            `True` = 索引里仍有一条 docid 对应这条记忆（= 残留）；
            `False` = 索引里没有它（**或**它的原文已从主表消失且索引也没了）。
        """
        docs = self._fts_index_docids()
        if docs is None:
            return False  # 建不出 fts5vocab（极旧 SQLite）⇒ 不误报，由 A2 的 MATCH 用例兜底
        rowid_row = self._conn.execute(
            "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if rowid_row is not None:
            return int(rowid_row[0]) in docs
        # 主表已无此 id：索引里若有"主表不存在的 docid"，就可能是它的碎片 ⇒ 保守判残留
        live = self._live_rowids()
        return bool(docs - live)

    def _fts_index_docids(self) -> set[int] | None:
        """索引里真实存在的 docid 集合（走 `fts5vocab` 的 **`instance`** 视图）。

        ⚠️ **必须是 `instance` 而不是 `row`**：三个视图的 `doc` 列语义不同——
        | 视图 | `doc` 列的含义 |
        |---|---|
        | `row` | **包含该 term 的文档数**（不是 docid！） |
        | `col` | 同上（按列拆分） |
        | `instance` | **docid** ✅ |

        实测踩过（2026-11-11）：用 `row` 视图拿到 `{1, 2}`，误以为有两条 docid，
        实际那是"某个 term 出现在 1 篇文档里"与"2 篇文档里"。删掉一条后 `row` 视图
        报 `{1}`，被误读成"还有残留"，而 `instance` 正确报 `{2}`（只剩活着的那条）。

        Returns:
            docid 集合；建不出 vocab 表 ⇒ `None`（调用方按"看不见孤儿"处理）。
        """
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS temp.m7_fts_v "
                "USING fts5vocab(main, memories_fts, 'instance')"
            )
            rows = self._conn.execute("SELECT DISTINCT doc FROM temp.m7_fts_v").fetchall()
        except sqlite3.Error as exc:
            logger.debug("fts5vocab 不可用（%s），孤儿检测降级为不可见", exc)
            return None
        finally:
            try:
                self._conn.execute("DROP TABLE IF EXISTS temp.m7_fts_v")
            except sqlite3.Error:
                pass
        return {int(r[0]) for r in rows}

    def _live_rowids(self) -> set[int]:
        """主表里还活着的 rowid 集合。"""
        return {int(r[0]) for r in self._conn.execute("SELECT rowid FROM memories").fetchall()}

    def residual_vec_ids(self, memory_ids: tuple[str, ...] | list[str]) -> list[str]:
        """这些 id 里还有哪些留在 `memories_vec`（删除后判残留）。"""
        if not self._vec_ready or not memory_ids:
            return []
        marks = ",".join(["?"] * len(memory_ids))
        rows = self._conn.execute(
            f"SELECT memory_id FROM memories_vec WHERE memory_id IN ({marks})",
            tuple(memory_ids),
        ).fetchall()
        return [str(r[0]) for r in rows]

    def count_conflicts_touching(self, memory_id: str) -> int:
        """有多少条冲突记录引用这条记忆（`source_id` 或 `target_id`）。"""
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM conflicts WHERE source_id = ? OR target_id = ?",
                (memory_id, memory_id),
            ).fetchone()[0]
        )

    def count_episodes_touching(self, session_ids: tuple[str, ...] | list[str]) -> int:
        """有多少条 episode 落在这些会话下（会话 id 为空 ⇒ 0）。"""
        ids = [s for s in session_ids if s]
        if not ids:
            return 0
        marks = ",".join(["?"] * len(ids))
        return int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM episodes WHERE conversation_id IN ({marks})", tuple(ids)
            ).fetchone()[0]
        )

    def count_episodes_containing(self, needles: tuple[str, ...] | list[str]) -> int:
        """有多少条 episode 的 `summary` 里含任一 needle（按内容命中，不按会话）。

        ## 为什么不能只按 `conversation_id` 清

        `episodes.summary` 是**会话摘要正文**——它可能整段复述了用户的记忆内容
        （「这次聊了紫晶计划的进度」）。而 `conversation_id` 与记忆的
        `memory_sources.session_id` 不保证一致（episode 是宿主会话，记忆来源
        可能带 revision / 分支）。只按会话清会留下**按内容明明该清**的行，
        而审计若也只看会话，就会把这种残留报成 0（独立验收 2026-11-11 指出的
        第 7 处缺陷）。

        ⚠️ 用 `instr()` 不用 `LIKE`：needle 里可能有 `%` / `_`，
        `LIKE` 会把它们当通配符 ⇒ 命中范围静默变大。
        """
        cleaned = [n for n in needles if n]
        if not cleaned:
            return 0
        total = 0
        for needle in cleaned:
            total += int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE instr(COALESCE(summary, ''), ?) > 0",
                    (needle,),
                ).fetchone()[0]
            )
        return total

    def erase_episodes(
        self,
        session_ids: tuple[str, ...] | list[str] = (),
        needles: tuple[str, ...] | list[str] = (),
    ) -> int:
        """删掉命中的 episode 行（按会话 **或** 按 summary 含 needle），返回删了几行。

        会话命中与内容命中取并集：两者都可能指向"这段对话属于要删的那条记忆"。
        删的粒度是**整行**——`summary` 是一段连续正文，删半段等于留半段。
        """
        ids = [s for s in session_ids if s]
        cleaned = [n for n in needles if n]
        if not ids and not cleaned:
            return 0
        deleted = 0
        try:
            if ids:
                marks = ",".join(["?"] * len(ids))
                cur = self._conn.execute(
                    f"DELETE FROM episodes WHERE conversation_id IN ({marks})", tuple(ids)
                )
                deleted += int(cur.rowcount or 0)
            for needle in cleaned:
                cur = self._conn.execute(
                    "DELETE FROM episodes WHERE instr(COALESCE(summary, ''), ?) > 0", (needle,)
                )
                deleted += int(cur.rowcount or 0)
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return deleted

    def count_working_containing(self, needles: tuple[str, ...] | list[str]) -> int:
        """有多少条 working 的 `content` / `source_quote` 里含任一 needle。

        `working_items` 存的是**用户原话**（`ADR-0040` 第 9 处明写"用户原话 +
        当前 handoff"）。它的归属键是 `(handoff_id, project_id, session_id)`，
        而**不是** `memory_id` ⇒ 按项目/会话查会漏掉"同一项目、另一个会话"里
        把这句话重说了一遍的行（独立验收 2026-11-11 的第 9 处缺陷）。

        按内容命中才是"这条记忆的正文有没有留在工作层"的直接判据。
        """
        cleaned = [n for n in needles if n]
        if not cleaned:
            return 0
        total = 0
        for needle in cleaned:
            total += int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM working_items "
                    "WHERE instr(content, ?) > 0 OR instr(COALESCE(source_quote, ''), ?) > 0",
                    (needle, needle),
                ).fetchone()[0]
            )
        return total

    def erase_working_containing(
        self, needles: tuple[str, ...] | list[str], *, project_id: str | None = None
    ) -> int:
        """删掉 `content` / `source_quote` 含任一 needle 的 working 行，返回删了几行。

        ⚠️ 与 `erase_scope(project_id=...)` 的分工：
        - 按项目整删（`--project`）用 `erase_scope`：整个工作层一起清；
        - 删**单条**记忆时用本方法：只清"正文里真的出现了这条记忆"的行。
          后者不能退化成前者——删一条记忆顺手清空整个项目的工作层，
          是把用户没要求删的东西删了。
        """
        cleaned = [n for n in needles if n]
        if not cleaned:
            return 0
        deleted = 0
        try:
            for needle in cleaned:
                sql = (
                    "DELETE FROM working_items "
                    "WHERE (instr(content, ?) > 0 OR instr(COALESCE(source_quote, ''), ?) > 0)"
                )
                params = (needle, needle)
                if project_id is not None:
                    sql += " AND project_id = ?"
                    params += (project_id,)
                cur = self._conn.execute(sql, params)
                deleted += int(cur.rowcount or 0)
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return deleted

    def count_ingest_marks_for(self, session_ids: tuple[str, ...] | list[str]) -> int:
        """这些会话下有多少条写入水位线。"""
        ids = [s for s in session_ids if s]
        if not ids:
            return 0
        marks = ",".join(["?"] * len(ids))
        return int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM ingest_marks WHERE session_id IN ({marks})", tuple(ids)
            ).fetchone()[0]
        )

    def count_working_for(
        self,
        project_ids: tuple[str, ...] | list[str],
        session_ids: tuple[str, ...] | list[str] = (),
    ) -> int:
        """这些项目 / 会话下有多少条 working item。"""
        projects = [p for p in project_ids if p]
        sessions = [s for s in session_ids if s]
        if not projects and not sessions:
            return 0
        clauses: list[str] = []
        params: list[object] = []
        if projects:
            clauses.append(f"project_id IN ({','.join(['?'] * len(projects))})")
            params.extend(projects)
        if sessions:
            clauses.append(f"session_id IN ({','.join(['?'] * len(sessions))})")
            params.extend(sessions)
        return int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM working_items WHERE {' OR '.join(clauses)}", tuple(params)
            ).fetchone()[0]
        )

    def count_handoffs_for(self, project_ids: tuple[str, ...] | list[str]) -> int:
        """这些项目下有多少条 handoff。"""
        projects = [p for p in project_ids if p]
        if not projects:
            return 0
        marks = ",".join(["?"] * len(projects))
        return int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM handoffs WHERE project_id IN ({marks})", tuple(projects)
            ).fetchone()[0]
        )

    def erase_memory(self, memory_id: str) -> bool:
        """**物理删除**一条记忆及其全部派生面（`ADR-0040` 决策 1 的 `erase`）。

        这是全仓唯一的行删除入口，且只由用户显式操作触发（控制面 / CLI）。

        ## 为什么顺序不能换

        实测（2026-11-11）：

        - **先删 FTS 索引行**：`MATCH` 立刻不再命中 ✅
        - **先删主表再删索引**：索引行变孤儿，`MATCH` **仍然命中**（原文碎片留在索引里）❌

        所以顺序固定为：① FTS `'delete'` 命令 → ② vec → ③ sources / claims / conflicts
        → ④ 主表。

        ## 为什么必须用 FTS5 的 `'delete'` 命令而不是普通 `DELETE`

        `memories_fts` 是 `content='memories'` 的**外部内容表**，而索引里存的是
        `_fts_projection` 的 **CJK bigram 投影**。普通 `DELETE FROM memories_fts`
        会让 FTS5 去 content 表读**原文**、拿原文去抵销**投影**索引 ⇒ **静默失败**
        （实测：删完 `MATCH '黄金'` 仍返回 1 行，而 `COUNT(*)` 显示 0）。
        `'delete'` 命令要求调用方显式给出被索引的那两列值 ⇒ 传投影值才正确。

        ## 幂等

        删一个不存在的 id 返回 `False`（**不抛错**）：用户重复点删除是正常操作，
        抛错会让人以为"上次没删成功"。⚠️ 但**不能**对同一条跑两次 `'delete'` 命令
        ——第二次会因为索引里已无该 docid 而报 `database disk image is malformed`
        （实测）。所以第一步先判存在性。

        Args:
            memory_id: 要删的记忆 id。

        Returns:
            `True` = 真的删了；`False` = 本来就没有（幂等）。

        Raises:
            ValueError: `memory_id` 为空（空串语义不明，宁可响亮失败）。
        """
        if not memory_id:
            raise ValueError("erase_memory 需要非空的 memory_id")
        return bool(self.erase_memory_batch((memory_id,)))

    def erase_memory_batch(
        self, memory_ids: tuple[str, ...], *, expected: dict[str, Memory] | None = None,
        expected_revisions: dict[str, str] | None = None,
    ) -> int:
        """独立事务加锁后核对范围，整批删除；与宿主或另一个控制进程并发也不会误删。"""
        deleted = 0
        with closing(self._open_connection()) as conn, conn:
            if self._vec_ready and not self._load_vec_extension(conn):
                raise RuntimeError("无法为删除连接载入向量索引")
            conn.execute("BEGIN IMMEDIATE")
            rows = []
            for memory_id in dict.fromkeys(memory_ids):
                if expected_revisions is not None and not self._management_matches(
                    conn, memory_id, expected_revisions[memory_id],
                ):
                    raise ValueError("记忆已变化，请重新核对目标范围。")
                row = conn.execute("SELECT rowid, * FROM memories WHERE id = ?", (memory_id,)).fetchone()
                if expected is not None and (row is None or not self._same_erase_target(row, expected[memory_id])):
                    raise ValueError("记忆已变化，请重新核对目标范围。")
                if row is not None:
                    rows.append(row)
            for row in rows:
                self._erase_memory_row(conn, row)
                deleted += 1
        return deleted

    @staticmethod
    def _same_erase_target(row: sqlite3.Row, expected: Memory) -> bool:
        return (
            row["id"], row["scope"], row["content"], row["source_quote"], row["status"], row["created_at"],
        ) == (
            expected.id, expected.scope, expected.content, expected.source_quote,
            expected.status.value, expected.created_at,
        )

    def _erase_memory_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        memory_id = str(row["id"])
        rowid = int(row["rowid"])
        # 先擦检索索引，后擦主表和派生面；FTS 需要索引时的原投影。
        conn.execute(
            "INSERT INTO memories_fts(memories_fts, rowid, content, source_quote) "
            "VALUES('delete', ?, ?, ?)",
            (rowid, _fts_projection(row["content"]), _fts_projection(row["source_quote"])),
        )
        if self._vec_ready:
            conn.execute("DELETE FROM memories_vec WHERE memory_id = ?", (memory_id,))
        conn.execute(
            "DELETE FROM dsh_source_proofs WHERE (session_id, message_id) IN "
            "(SELECT session_id, message_id FROM memory_sources WHERE memory_id = ?) ",
            (memory_id,),
        )
        conn.execute("DELETE FROM memory_sources WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM dsh_memory_evidence WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM claims WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM conflicts WHERE source_id = ? OR target_id = ?", (memory_id, memory_id))
        conn.execute("DELETE FROM memory_meta WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM memory_expiries WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM claim_backfill_marks WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        if self._maintenance_ready(conn):
            conn.execute(
                "UPDATE maintenance_scopes SET projection = ("
                "SELECT json_group_object(item.key, json(item.value)) "
                "FROM json_each(maintenance_scopes.projection) item WHERE item.key <> ?), findings = NULL "
                "WHERE scope = ? AND projection IS NOT NULL", (memory_id, row["scope"]),
            )
            conn.execute("DELETE FROM maintenance_changes WHERE memory_id = ?", (memory_id,))
            conn.execute(
                "DELETE FROM dialogue_results WHERE EXISTS ("
                "SELECT 1 FROM json_each(dialogue_results.batch, '$.targets') target "
                "WHERE json_extract(target.value, '$.id') = ?)", (memory_id,),
            )
            conn.execute(
                "DELETE FROM maintenance_batches WHERE EXISTS ("
                "SELECT 1 FROM json_each(maintenance_batches.targets) target "
                "WHERE json_extract(target.value, '$.id') = ?)", (memory_id,),
            )

    def erase_scope(
        self,
        *,
        project_id: str | None = None,
        session_ids: tuple[str, ...] | list[str] = (),
        include_working: bool = True,
    ) -> "EraseScopeResult":
        """按**项目 / 会话**整批删除（`ADR-0040` 决策 1 的「单条 / 按类别 / 全部」）。

        单条删除（`erase_memory`）覆盖不到两类数据，它们没有 `memory_id`：

        - `working_items`（按 `project_id` / `session_id` 归属）；
        - `handoffs`（按 `project_id` 归属）。

        这两张表是 S2 新增的派生面（第 9/10 处），**必须**能被删除覆盖——
        `ADR-0040` 决策 5 特意要求它们「不得出现删不掉的冗余副本」。

        Args:
            project_id: 项目假名；`None` = 不动项目维度的数据。
            session_ids: 会话 id 列表（清 `ingest_marks` / `working_items`）。
            include_working: False = 只清会话水位线，不碰 working/handoffs。

        Returns:
            `EraseScopeResult`：各表删了多少行（**只有计数，没有内容**）。
        """
        projects = tuple(p for p in ([project_id] if project_id else []) if p)
        sessions = tuple(s for s in session_ids if s)
        deleted_working = deleted_handoffs = deleted_marks = 0
        try:
            if include_working and (projects or sessions):
                clauses: list[str] = []
                params: list[object] = []
                if projects:
                    clauses.append(f"project_id IN ({','.join(['?'] * len(projects))})")
                    params.extend(projects)
                if sessions:
                    clauses.append(f"session_id IN ({','.join(['?'] * len(sessions))})")
                    params.extend(sessions)
                cur = self._conn.execute(
                    f"DELETE FROM working_items WHERE {' OR '.join(clauses)}", tuple(params)
                )
                deleted_working = int(cur.rowcount or 0)
            if include_working and projects:
                cur = self._conn.execute(
                    f"DELETE FROM handoffs WHERE project_id IN ({','.join(['?'] * len(projects))})",
                    projects,
                )
                deleted_handoffs = int(cur.rowcount or 0)
            if sessions:
                cur = self._conn.execute(
                    f"DELETE FROM ingest_marks WHERE session_id IN ({','.join(['?'] * len(sessions))})",
                    sessions,
                )
                deleted_marks = int(cur.rowcount or 0)
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return EraseScopeResult(
            deleted_working=deleted_working,
            deleted_handoffs=deleted_handoffs,
            deleted_ingest_marks=deleted_marks,
        )

    def all_memory_ids(self) -> list[str]:
        """全部记忆 id（按写入顺序；「删除全部」与审计全表扫用）。"""
        rows = self._conn.execute("SELECT id FROM memories ORDER BY rowid").fetchall()
        return [str(r[0]) for r in rows]

    def all_session_ids(self) -> list[str]:
        """库中出现过的全部会话 id（来源表 + 水位线的并集）。"""
        rows = self._conn.execute(
            "SELECT session_id FROM memory_sources WHERE session_id IS NOT NULL "
            "UNION SELECT session_id FROM ingest_marks WHERE session_id IS NOT NULL"
        ).fetchall()
        return [str(r[0]) for r in rows if r[0]]

    def all_project_ids(self) -> list[str]:
        """库中登记过的全部项目假名。"""
        rows = self._conn.execute("SELECT project_id FROM projects ORDER BY created_at").fetchall()
        return [str(r[0]) for r in rows if r[0]]

    # ---- 内部工具 ----
    def _serialize_embedding(self, embedding: list[float]) -> bytes:
        """把 1024 维 float 列表序列化成 vec0 接受的 float32 字节串。"""
        import sqlite_vec

        return sqlite_vec.serialize_float32(embedding)

    def _get_memories_in_order(self, memory_ids: list[str]) -> list[Memory]:
        """按给定 id 顺序回主表取记忆（缺失的 id 直接跳过，不报错）。"""
        out: list[Memory] = []
        for memory_id in memory_ids:
            got = self.get_memory(memory_id)
            if got is not None:
                out.append(got)
        return out
