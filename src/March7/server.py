"""server.py — MCP 工具面（8 个记忆工具及 S17 面板工具，见 ADR-0014、ADR-0075）。

本模块是引擎核心之外**唯一**允许 import MCP 的地方（CONVENTIONS §2 红线）。
8 个工具的入参/出参与《March7-实现规划-v1.md》§3.5 严格一致，签名 S0 冻结。
工具描述用中文+英文双语（宿主模型可能英文思考，用户是中文使用者）。
引擎依赖（store/judge/retriever/...）在启动时组装注入，S4 实现装配逻辑。

**SDK v2（ADR-0014）**：`from mcp.server.mcpserver import MCPServer`；
`mcp.server.fastmcp` 在 v2 **已被删除**（不是弃用），照旧路径写必错。
8 工具的**工具名显式写死**（`name="March7_write"`）——不依赖装饰器从大写函数名推导，
因为工具名会被写进宿主 SKILL.md，是本项目最贵的一项命名约定（docs/NAMING.md §2）。

**异常语义**（CONVENTIONS §3）：引擎核心向上抛具体异常，**本模块**负责把异常转成
宿主可读的错误返回（MCP 协议层会把工具异常转成 `isError` 结果，消息即给宿主模型看）。

⚠️ **本模块刻意不写 `from __future__ import annotations`**（其余模块都写）：
`mcp dev` / `mcp run` 用 `importlib` **直接执行本文件**（`spec.loader.exec_module`），
**不把模块登记进 `sys.modules`**；而 `@dataclass` 在 PEP 563 字符串注解下要去
`sys.modules.get(cls.__module__).__dict__` 查本模块的命名空间——查不到就
`AttributeError: 'NoneType' object has no attribute '__dict__'`（实测 2026-10-20）。
本模块的注解全部在定义前可求值（无前向引用），去掉 future import 即可绕开。
`tests/test_server.py::test_module_loads_like_the_mcp_cli_loads_it` 钉住这条。

⚠️ **本模块还要把「包目录」从 `sys.path` 里摘掉**（`_drop_package_dir_from_sys_path`）：
`mcp dev` / `mcp run <file>` 会把**本文件所在目录**插到 `sys.path[0]`
（`mcp/cli/cli.py` 的 `_import_server`）⇒ 本目录里的模块名**一旦与标准库同名**就构成**遮蔽**
⇒ 标准库 `cProfile` 拿不到 `profile.run` ⇒ `torch._dynamo` 的 `import cProfile` 断链
⇒ transformers / torch 整条导入链崩 ⇒ `Embedder()` 构造失败
⇒ **8 个工具全部返回 `Error executing tool <name>`**（实测 2026-10-20，验收 P0-1）。
**历史案例**：本包的画像模块原名 `profile.py`，与标准库 `profile` 撞名 ⇒ 已改名 `l0_profile.py`
（`ADR-0019`）。**但护栏保留**——它防的是**任何未来新增的**撞名模块，不只是那一例。
`import March7.*` 走的是**已安装的包**（`src` 在路径上，**不是** `src/March7`），
所以摘掉本目录**不影响任何正常导入**——它本来就不该做 `sys.path` 入口。
"""

import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from March7 import diagnostics, maintenance, privacy
from March7.config import ENV_DATA_DIR, Config, load_config
from March7.control.panel_launcher import PANEL_TOOL_DESCRIPTION, open_chat_panel
from March7.conflict import ConflictChecker
from March7.embedder import Embedder, LazyEmbedder
from March7.export import export_json, export_markdown
from March7.judge import Judge, process_transcript
from March7.llm import LLMClient
from March7.l0_profile import ProfileStore
from March7.retriever import ACTIVE_RECALL_MAX_L2, Retriever, scope_allows
from March7.schemas import (
    Conflict,
    ConflictStatus,
    ExportFormat,
    ExportResult,
    L0Profile,
    Memory,
    ResolutionType,
    StatusResult,
    WriteResult,
)
from March7.source_proof import ENV_REQUIRE_DSH_SOURCE, is_native_message_id, verify_transcript
from March7.store import MemoryStore

logger = logging.getLogger(__name__)


def _drop_package_dir_from_sys_path() -> None:
    """把「本包所在目录」从 `sys.path` 里摘掉（**必须在任何重量级 import 之前**调用）。

    **为什么**：`mcp dev` / `mcp run <file>` 的加载器（`mcp/cli/cli.py::_import_server`）
    会把 server 文件所在目录插到 `sys.path[0]`。本目录里的模块名**一旦与标准库同名**
    就构成**遮蔽** ⇒ 标准库 `cProfile` 拿不到 `profile.run` ⇒ `torch._dynamo` 的
    `import cProfile` 断链 ⇒ transformers / torch 整条导入链崩 ⇒ `Embedder()` 构造失败
    ⇒ **8 个工具全部不可调**（验收 P0-1，实测 2026-10-20）。

    **历史案例**：本包的画像模块原名 `profile.py`，与标准库 `profile` 撞名
    ⇒ 已改名 `l0_profile.py`（`ADR-0019`）。**但本护栏保留**：它是**通用防线**，
    防的是**任何未来新增的**撞名模块，并由
    `tests/test_server.py::test_package_dir_does_not_shadow_stdlib_modules` 持续盯着。

    **为什么摘掉是安全的**：`import March7.*` 走的是**已安装的包**（editable 安装把
    `src` 放进路径，**不是** `src/March7`）⇒ 摘掉本目录不影响任何正常导入。
    正常按包启动（`python -m March7`）时本目录**根本不在** `sys.path` 上 ⇒ 本函数是空操作。
    """
    package_dir = os.path.normcase(os.path.abspath(os.path.dirname(__file__)))
    kept = [
        entry
        for entry in sys.path
        if os.path.normcase(os.path.abspath(entry or os.getcwd())) != package_dir
    ]
    if len(kept) != len(sys.path):
        logger.debug("已从 sys.path 摘掉包目录（防止遮蔽标准库同名模块）：%s", package_dir)
        sys.path[:] = kept


_drop_package_dir_from_sys_path()


def _as_tool_error(exc: Exception) -> ToolError:
    """把引擎层的异常转成宿主可读的工具错误（**保留原消息**）。

    为什么必须转（MCP v2 的实测语义）：只有 `ToolError` 会把消息带进
    `is_error=True` 的 content 里**给宿主模型看**；其它异常被 SDK 当成「崩溃」，
    客户端只拿到 `Error executing tool <name>`，**原话一个字都看不到**。
    而本项目「让拒绝理由可见」正是画像护栏存在的意义（docs/NAMING.md §4.2）——
    理由被吞掉，护栏就等于没有。

    引擎层（profile.py 等）**不 import MCP**（CONVENTIONS §2 红线），所以那一层的
    异常是普通的 `ValueError`；转换只发生在这里这一层。
    """
    return ToolError(str(exc))

SERVER_NAME = "March7"
"""MCP server key（docs/NAMING.md §2：写入 mcp.json 的 key）。"""


# ---------------------------------------------------------------------------
# 引擎依赖图（进程内单例）
# ---------------------------------------------------------------------------


class _PendingConflictChecker:
    """**降级用**的占位冲突检测器（T16 起不再是默认路径）。

    历史：`conflict.py` 属 T16 范围，它的 `ConflictChecker.__init__` 曾在构造期抛
    `NotImplementedError` ⇒ 引擎组装时接不住。T16 落地后 `_build_engine()` 直接装配真检测器，
    本类只剩两个用途：① 单测里扮演「预检这一步不可用」；② 引擎被注入成缺检查器时的兜底。
    三个方法都抛 `NotImplementedError`，由调用方**降级 + 记日志**（宁可不判，不能判错）。
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def pre_check(self, new_memory: Memory) -> list[Conflict]:
        """未落地：抛 NotImplementedError，由 judge 侧的降级路径接管。"""
        raise NotImplementedError("conflict.py 尚未落地（T16）")

    def list_pending(self) -> list[Conflict]:
        """未落地：抛 NotImplementedError，由 server 侧回落到 store 直读。"""
        raise NotImplementedError("conflict.py 尚未落地（T16）")

    def resolve(self, conflict_id: str, resolution: ResolutionType) -> Conflict:
        """未落地：抛 NotImplementedError，由 server 侧转成宿主可读的错误。"""
        raise NotImplementedError("conflict.py 尚未落地（T16）")


@dataclass
class Engine:
    """引擎依赖图——8 个工具共用的那一组对象（进程内单例）。

    字段都是引擎层的具体类型，**不含任何 MCP 对象**：MCP 只在 server.py 这层出现。
    """

    config: Config
    llm: LLMClient
    embedder: Embedder | LazyEmbedder
    store: MemoryStore
    profile_store: ProfileStore
    judge: Judge
    retriever: Retriever
    conflict_checker: ConflictChecker | _PendingConflictChecker


_ENGINE: Engine | None = None


def _build_engine() -> Engine:
    """组装引擎依赖图（config → llm/embedder/store/judge/retriever/...）并缓存。

    进程内单例：MCP 工具与钩子脚本共用同一批对象（尤其同一个 SQLite 连接）。

    Raises:
        ConfigError: 环境变量不满足要求（缺必需项 / fallback 半配置）——
            **响亮失败**，不返回「半个配置」（ADR-0013）。
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    config = load_config()
    llm = LLMClient(config, strict_models=False)
    # `ADR-0036`：**惰性编码器**。Stop 钩子每轮独立进程，而多数轮次 Judge 判定
    # 「无可记」⇒ 根本不会调 embed_one。这里若 `Embedder()` 就白付一次 14.4s 权重加载。
    embedder = LazyEmbedder()
    store = MemoryStore(config.db_path)
    store.init_schema()
    profile_store = ProfileStore(config.profile_path)
    judge = Judge(llm=llm)
    retriever = Retriever(store, embedder)

    conflict_checker: ConflictChecker | _PendingConflictChecker
    try:
        conflict_checker = ConflictChecker(store=store, llm=llm if llm.judge_configured else None)
    except NotImplementedError:
        logger.warning(
            "冲突检测器尚未落地（T16）：写入管线将跳过冲突预检，"
            "March7_conflicts 走 store 直读兜底。"
        )
        conflict_checker = _PendingConflictChecker(store)

    _ENGINE = Engine(
        config=config,
        llm=llm,
        embedder=embedder,
        store=store,
        profile_store=profile_store,
        judge=judge,
        retriever=retriever,
        conflict_checker=conflict_checker,
    )
    logger.info("引擎组装完成（数据目录：%s）", config.data_dir)
    return _ENGINE


def _set_engine(engine: Engine | None) -> None:
    """注入/清空引擎实例（**仅供单测**：用假引擎顶掉真 API 与 torch 依赖）。"""
    global _ENGINE
    _ENGINE = engine


def _engine() -> Engine:
    """工具面专用：组装引擎，**失败时给出能读懂的理由**。

    ## 为什么必须有这一层（2026-11-16 Codex App 实测，V2 的根因）

    MCP SDK 对**非 `ToolError`** 的异常一律包成
    `UnexpectedToolError("Error executing tool <name>")`，并且**丢掉原始消息**
    （`mcp/server/mcpserver/tools/base.py`）。于是：

    - 真因：`[mcp_servers.March7.env]` 里没有 `MARCH7_LLM_*` ⇒ `load_config()` 抛
      `ConfigError("缺少必需的环境变量：…")`；
    - 用户/模型看到：`Error executing tool March7_status`
      —— 和"服务崩了"长得一模一样，连"缺变量"这三个字都没有。

    这一类"启动失败被压成一句无信息量的话"是本项目反复踩的坑（`ADR-0058`：
    「看不见的失败 = 静默失败」）。所以工具面统一走这里：**把真因原样交给宿主模型**。

    为什么只包工具面、不包 `write_with_context` / `last_extraction_failed`：
    后两个是**钩子**调的，钩子的纪律是"恒 exit 0 + 自己的日志"，不该收到 MCP 的异常类型。
    """
    try:
        return _build_engine()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            "记忆层无法启动：{kind}: {msg}\n"
            "（若在 Codex 里用：检查 ~/.codex/config.toml 的 [mcp_servers.March7.env] "
            "是否带 MARCH7_LLM_*；跑 `python hosts/codex/install.py --doctor` 可自查）".format(
                kind=type(exc).__name__, msg=exc
            )
        ) from exc


def _call_project_id() -> str | None:
    """**本次工具调用**该用哪个项目（`ADR-0063` §4.1；S14 落地）。

    为什么在**调用点**算，而不是塞进 `_build_engine()` 的单例：
    引擎是**进程级**单例，而项目身份是**调用级**事实。宿主的 cwd 在一次会话里
    可以变（用户 `cd` 了、`codex resume` 换了目录），单例一旦缓存它，
    后面的每一次调用都会拿到第一次那个 —— 那是"记到别的项目里"的另一种写法。

    优先级由 `privacy.resolve_active_project` 一家说了算（写侧与读侧共用同一判据，
    `ADR-0063` §4.2）：`MARCH7_PROJECT` → 进程 cwd → `None`。
    工具面没有"载荷 cwd"这一档 —— MCP 协议根本不传 cwd（`ADR-0063` §2.1），
    进程 cwd 是宿主起的子进程继承来的那一个（实测可靠，§2.2）。

    Returns:
        项目假名 `p-<6 hex>`；拿不到 ⇒ `None`（只读写 `global`，不降级、不猜）。
    """
    return _call_project_resolution().project_id


def _call_project_resolution() -> privacy.ProjectResolution:
    """MCP 无宿主会话 ID。检测到目录有恢复冲突时只允许全局查询。"""
    try:
        cwd: str | None = os.getcwd()
    except OSError:  # cwd 已被删掉（`getcwd` 会抛）⇒ 当成没有项目身份
        cwd = None
    store = _engine().store
    resolution = privacy.resolve_active_project(process_cwd=cwd, known_project=store.get_project_id)
    if resolution.project_id is not None and store.project_needs_confirmation(resolution.project_id):
        return privacy.ProjectResolution(event=privacy.EVENT_PROJECT_SESSION_CHANGED)
    return resolution


def _manual_mark(transcript: str) -> str:
    """给「不带来源标识的手动写入」生成一个确定性水位线键（ADR-0020）。

    为什么用内容摘要而不是「会话 + 时间」：手动写入没有 message_id，
    唯一能判「是不是同一件事」的信息就是 transcript 本身。
    取 SHA-1 前 16 位足够避免碰撞，又不会把用户原话以明文形式写进
    `ingest_marks`（那张表里只有 id，不存正文）。

    ⚠️ **不是安全哈希**：这里只用于去重，不用于任何鉴权场景。
    """
    digest = hashlib.sha1(transcript.strip().encode("utf-8")).hexdigest()[:16]
    return f"manual:{digest}"


# ---------------------------------------------------------------------------
# 8 个工具（§3.5）——函数体已实现，注册见 register_tools
# ---------------------------------------------------------------------------


def March7_write(
    transcript: str,
    conversation_id: str,
    host: str,
    source_session_id: str | None = None,
    source_message_id: str | None = None,
    source_revision: str | None = None,
) -> WriteResult:
    """核心闭环：跑 Judge 管线 → 四条硬过滤 → 入库 / 写入记忆（Write memories）。

    Args:
        transcript: 本轮对话转写。
        conversation_id: 来源会话 id。
        host: 来源宿主（workbuddy / dsh / claude）。
        source_session_id: 来源会话标识（ADR-0016 决策 7）。**T14 起随记忆落库**
            （来源四元组 `{session_id, message_id, revision, quote}` 的会话级部分，
            `quote` 由引擎按每条记忆自己的 `source_quote` 补齐）。
        source_message_id: 来源消息 id（同上，落库）。宿主侧据此做**每轮幂等**
            ——同一 message_id 不会被反复写入。
        source_revision: 来源修订号（同上，落库）。

    Returns:
        写了几条、拒了几条、拒绝理由（拒绝理由让宿主模型可见，形成透明感）。

    ⚠️ **本函数签名是 MCP 工具面的公开契约**（`tests/test_server.py::test_write_signature`
    钉死"恰为 6 个参数"）。`context` / `project_id` 这两个**只该由适配层填**的参数
    走 `write_with_context`，**不加到这里**——否则宿主模型会在自己的工具列表里
    看到两个它无法正确填写的槽（`ADR-0049`，工具面不暴露内部入参）。

    S14 起：工具面自己填 `project_id`（`_call_project_id()`，来源见 `ADR-0063` §4.1）。
    在此之前工具面的手动补记**恒写 `global`** ⇒ 同一条事实经钩子记是 `project:...`、
    经工具面记是 `global`，用户看到的是"两条一样的记忆，一条在哪都能查到"。
    """
    # 先摸一下引擎：只为了把"起不来"变成可读错误（真因见 `_engine` 的 docstring）。
    # `_build_engine()` 是进程内单例，这次调用不会多付一次装配成本。
    engine = _engine()
    project = _call_project_resolution()
    if project.event == privacy.EVENT_PROJECT_SESSION_CHANGED:
        raise ToolError("当前目录的项目身份有会话冲突，已暂停补记。请核对工作目录或明确指定项目。")
    project_id = project.project_id
    attested_text = None
    if os.environ.get(ENV_REQUIRE_DSH_SOURCE) == "1":
        session_id = source_session_id or conversation_id
        if host != "dsh" or conversation_id != session_id or not source_message_id or source_revision:
            raise ToolError("DSH 补记需要宿主原生会话 ID、用户消息 ID 和完整的用户原话。")
        try:
            attested_text = verify_transcript(engine.store, session_id, source_message_id, project_id, transcript)
        except ValueError as exc:
            raise _as_tool_error(exc) from exc
    elif host == "dsh" and source_message_id and not is_native_message_id(source_message_id):
        raise ToolError("来源必须填写用户消息 ID，不能填工具调用 ID。")
    return write_with_context(
        "用户：" + attested_text if attested_text is not None else transcript,
        conversation_id=conversation_id,
        host=host,
        source_session_id=source_session_id,
        source_message_id=source_message_id,
        source_revision=source_revision,
        project_id=project_id,
        **({"attested_text": attested_text} if attested_text is not None else {}),
    )


def write_with_context(
    transcript: str,
    *,
    conversation_id: str,
    host: str,
    source_session_id: str | None = None,
    source_message_id: str | None = None,
    source_revision: str | None = None,
    context: str | None = None,
    project_id: str | None = None,
    attested_text: str | None = None,
) -> WriteResult:
    """`March7_write` 的**完整形态**：额外接受会话上文与项目身份（`ADR-0049`）。

    **不是 MCP 工具**（未在 `register_tools` 注册）。调用者是**各宿主的自动写通道**：

    | 调用者 | 第 4 个参数从哪来 | 依据 |
    |---|---|---|
    | `hosts/workbuddy/hooks/stop.py` | 树状转写里的整条分支（能拼上文） | 本轮 |
    | `hosts/codex/codex_notify.py` | Codex `notify` 传的 `input-messages` | S14 / `ADR-0061` |

    这两样（整条分支 / 载荷里的 cwd）都不该让宿主模型填。

    ⚠️ **新增调用者必须走同一个函数**，不许各自调 `process_transcript`：
    `March7_write` 里有一套**水位线幂等 + 空转写短路 + 失败不打水位线**的逻辑
    （`ADR-0020` 单一写者）。分叉成两份实现，这三点就会各自漂。

    Args:
        transcript: 本轮对话转写。
        conversation_id: 来源会话 id。
        host: 来源宿主。
        source_session_id / source_message_id / source_revision: 来源四元组的会话级部分。
        context: 本轮之前的会话上文（前 2 轮）。`None` ⇒ 与引入它之前行为一致。
        project_id: 项目假名 `p-<6 hex>`。非空 ⇒ 记忆写 `scope=project:<id>`；
            空/None ⇒ `global`。

    Returns:
        同 `March7_write`。
    """
    engine = _build_engine()
    # 来源四元组的会话级部分（ADR-0016 决策 3，T14 落地）。三个槽任一非空即视为
    # 「本次写入带来源」⇒ 引擎会把它挂到每条落库记忆上。
    # 缺省回落：`session_id` 为空时用 `conversation_id` 顶替（宿主不一定提供 session id），
    # 但**只回落这一个字段**——message_id / revision 缺失就是缺失，不猜。
    source: dict[str, str | None] | None = None
    if source_session_id or source_message_id or source_revision:
        source = {
            "session_id": source_session_id or conversation_id,
            "message_id": source_message_id,
            "revision": source_revision,
        }
        logger.info(
            "来源标识已收到，将随记忆落库（ADR-0016 决策 3）：session=%r message=%r revision=%r",
            source["session_id"],
            source["message_id"],
            source["revision"],
        )
    # ---- 单一写者（ADR-0020）：本工具与 Stop 钩子共用一套水位线 ----
    # 为什么工具面也要打水位线：SKILL.md 曾经要求「每轮结束前调 March7_write」，
    # 而 Stop 钩子**也**每轮写一次 ⇒ 同一轮被 Judge 判两遍、同一事实入库两条。
    # 现在语义收敛为「Stop 是唯一自动写者」，但工具面仍是**手动补记**入口
    # （用户明确说「记住这条」时用）。补记若与钩子撞车，必须能自己认出来。
    mark_session = str((source or {}).get("session_id") or conversation_id or "")
    mark_message = str((source or {}).get("message_id") or "") or _manual_mark(transcript)
    if mark_session and engine.store.is_ingested(mark_session, mark_message):
        logger.info(
            "跳过重复写入：来源已处理过（session=%s mark=%s）——ADR-0020 单一写者",
            mark_session,
            mark_message[:24],
        )
        return WriteResult()

    if not transcript.strip():
        # 空转写不值得烧一次 LLM；也不能打水位线（否则「空」会永久占位）。
        logger.warning("March7_write 收到空 transcript（conversation_id=%r），直接返回空结果。", conversation_id)
        return WriteResult()

    conflict_checker = engine.conflict_checker
    if conflict_checker is None:  # 防御：引擎若被注入成缺检查器，退化成「跳过预检」而非崩在调用方
        conflict_checker = _PendingConflictChecker(engine.store)
    result = process_transcript(
        transcript,
        conversation_id=conversation_id,
        host=host,
        judge=engine.judge,
        store=engine.store,
        profile_store=engine.profile_store,
        conflict_checker=conflict_checker,
        embedder=engine.embedder,
        source=source,
        context=context,
        project_id=project_id,
        **({"attested_text": attested_text} if attested_text is not None else {}),
    )
    # LLM 失败**不打水位线** ⇒ 下一轮（或下一次手动调用）自动重试。
    # 与 stop.py 同一判据：失败 ≠ 无需记录，前者要能重来。
    if not engine.judge.extraction_failed:
        engine.store.mark_ingested(mark_session, mark_message)
    if result.written_count:
        engine.store.refresh_maintenance_safely()
    return result


def last_extraction_failed() -> bool:
    """上一次 Judge 候选抽取是否**因 LLM 调用失败**而降级为空列表（只读观测位）。

    为什么需要它（宿主侧实测，2026-10-20）：写入结果 `WriteResult` 的四个字段
    在两种情况下**长得一模一样**——
      ① 「Judge 判定这轮没有值得记的」 ⇒ `written=[] / rejected=[]`（正常）；
      ② 「LLM 调用/解析重试耗尽」⇒ 同样 `written=[] / rejected=[]`（**失败**）。
    宿主（模型）看到空结果无从判断，于是**以为记忆库坏了**，转而写自己的记忆文件
    （实测真机：模型收下 `written: []` 后就去建 `.workbuddy/memory/MEMORY.md`）。
    ⇒ 宿主钩子必须能区分这两者，否则失败就是**静默的**（写不进、还不报错）。

    只读、不改任何工具的入参/出参结构（ADR-0003 的冻结口径不动）。
    """
    engine = _build_engine()
    return bool(engine.judge.extraction_failed)


def March7_recall(query: str, top_k: int = 5) -> list[Memory]:
    """混合检索记忆（Recall memories）：BM25 + 向量 + RRF。

    Args:
        query: 检索词（问题关键词）。
        top_k: 返回条数，默认 5。

    Returns:
        记忆列表（content + source_quote + 时间戳，双注入防 AI 替你回忆）。

    ⚠️ S14 起按**本次调用**的项目身份过滤（`_call_project_id()`）：`global` 恒可见，
    `project:<本轮 id>` 只在本项目可见（`ADR-0039` 决策 3，`ADR-0063` §4.2）。
    在这之前工具面**从不传 `project_id`** ⇒ `retrieve` 默认 `None` ⇒ 只返回 `global`，
    于是"钩子写进项目的记忆，模型一条都查不到"。这正是 A1/A2 要修的那个缺口。
    """
    engine = _engine()
    project_id = _call_project_id()
    try:
        hits = engine.retriever.retrieve(query, top_k, project_id=project_id, max_distance=ACTIVE_RECALL_MAX_L2)
    except Exception:
        diagnostics.trace_recall(engine.store.data_dir, project_id, channel="active", failed=True)
        raise
    diagnostics.trace_recall(engine.store.data_dir, project_id, channel="active", match_count=len(hits))
    return hits


def March7_profile(
    action: str,
    updates: dict[str, str] | None = None,
    source_quote: str | None = None,
) -> L0Profile:
    """L0 画像读写（Get or update user profile）。

    写入门槛（护栏，ADR-0003）：画像是项目「AI 推断的一律进不了画像」这条差异化的
    所在，所以 update **必须携带 `source_quote`（用户原话）**，且原话要能通过
    「绝对词黑名单」校验；缺 source_quote 的 update 一律拒绝（理由带
    「宁可漏记，不可误记」前缀）。正常路径仍应走 `March7_write` 的 Judge 管线，
    本工具只服务「用户明确要求改画像」这一种显式场景。

    Args:
        action: "get" 读取 / "update" 更新。
        updates: action == "update" 时的字段字典（如 {"occupation": "..."}）；
            action == "get" 时忽略。
        source_quote: 用户提出该修改时的原话（action == "update" 时**必填**）。

    Returns:
        更新后的完整画像。

    Raises:
        ToolError: action 未知、缺 source_quote、或护栏不通过（理由带
            「宁可漏记，不可误记」前缀）——**消息会原样送到宿主模型**。
    """
    engine = _engine()
    if action == "get":
        return engine.profile_store.recall_view()
    if action == "update":
        # 护栏在 l0_profile.apply_user_update 里（唯一真相源）：缺 source_quote 一律拒。
        try:
            return engine.profile_store.apply_user_update(updates or {}, source_quote or "")
        except ValueError as exc:
            raise _as_tool_error(exc) from exc
    raise ToolError(f"未知的 profile action：{action!r}——只支持 'get' 与 'update'。")


def March7_search(keyword: str) -> list[Memory]:
    """BM25 纯关键词检索（Keyword search）：精确找回专有名词/代码。

    Args:
        keyword: 关键词。

    Returns:
        记忆列表。

    ⚠️ 与 `March7_recall` 同口径：`project_id` 按**本次调用**解析（`_call_project_id()`），
    不传就只看得见 `global`（`ADR-0039` 决策 3）。
    """
    engine = _engine()
    return engine.retriever.search_keyword(keyword, project_id=_call_project_id())


def March7_conflicts() -> list[Conflict]:
    """列出待裁决冲突（List pending conflicts）。

    「待裁决」= 还没结束的冲突：`candidate`（刚被规则筛出、四分类未跑）/
    `pending`（裁决说不确定，必须问用户）/ `confirmed`（类型已判定，等用户决定
    要不要让旧记忆退场）。`resolved` 与 `dismissed` 不再出现（`ADR-0031` 决策 3）。

    Returns:
        未结束的冲突列表（含各自的 `resolution_type` 与 `should_ask_user`）。
    """
    engine = _engine()
    checker = engine.conflict_checker
    try:
        rows = checker.list_pending() if checker is not None else engine.store.list_conflicts(status=ConflictStatus.PENDING)
    except NotImplementedError:
        logger.warning("冲突检查器暂不可用，按库中待定状态查询。")
        rows = engine.store.list_conflicts(status=ConflictStatus.PENDING)
    project_id = _call_project_id()
    return [row for row in rows if _visible_conflict(engine.store, row, project_id)]


def _visible_conflict(store: MemoryStore, conflict: Conflict, project_id: str | None) -> bool:
    """裁决和列表都要求冲突两端可见。任一端缺失即拒绝。"""
    for memory_id in (conflict.source_id, conflict.target_id):
        memory = store.get_memory(memory_id)
        if memory is None or not scope_allows(memory, project_id):
            return False
    return True


_MAINTENANCE_OPTIONS = {
    "maintenance_check": "page",
    "maintenance_review": "query",
    "maintenance_preview": "group",
    "maintenance_plan": "archive",
    "maintenance_undo_preview": "receipt",
}


def _maintenance_proposal(store: MemoryStore, raw: str, resolution: str) -> dict[str, Any]:
    """接收模型选择的范围与目标，只返回检查或本机预览。"""
    option = _MAINTENANCE_OPTIONS[resolution]
    try:
        if len(raw) > 1800:
            raise ValueError("计划参数超出单批上限。")
        request = json.loads(raw)
        allowed = {"scope", "library_id", "context", option} | ({"keep"} if option == "archive" else set())
        if not isinstance(request, dict) or set(request) - allowed:
            raise ValueError("请只提供此操作支持的范围和目标字段。")
        library_id = diagnostics.library_identifier(store.data_dir)
        if "library_id" in request and request["library_id"] != library_id:
            raise ValueError("工具绑定的库与当前会话给出的库编号不一致。请先核对宿主配置。")
        context = request.get("context")
        if "context" in request and (not isinstance(context, str) or not re.fullmatch(r"[a-f0-9]{32}", context)):
            raise ValueError("请使用本轮真实宿主提供的上下文。")
        native_project = store.dialogue_project(context) if context else None
        label = request.get("scope")
        if label == "project":
            project_id = native_project if context else _call_project_id()
            if not project_id:
                raise ValueError("当前宿主缺少可信项目身份。请在正确项目重试或到本机面板核对。")
            scope = f"project:{project_id}"
        elif label == "personal":
            scope = "global"
        else:
            raise ValueError("请选择个人或当前项目范围。")

        if option == "page":
            page = request.get("page", 1)
            if type(page) is not int or not 1 <= page <= 9_999:
                raise ValueError("页码无效。")
            report = store.maintenance_check(scope)
            offset = (page - 1) * 6
            return {
                "status": "maintenance_report", "scope": label, "library_id": library_id,
                "total_groups": len(report.findings),
                "page": page, "has_more": offset + 6 < len(report.findings),
                "groups": [
                    {"kind": item.kind, "key": item.key, "count": len(item.ids), "reason": item.reason}
                    for item in report.findings[offset:offset + 6]
                ],
                "receipts": [
                    {"id": batch.id, "kind": batch.kind, "state": batch.state}
                    for batch in store.maintenance_history(scope)[:6]
                ],
            }
        if option == "query":
            query = request.get("query")
            if not context or not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
                raise ValueError("请用本轮上下文和具体文字核对有限条目。")
            total, rows = store.management_page(scope, history=False, search=query.strip(), status="", limit=6, offset=0)
            return {
                "status": "maintenance_review", "matches": [_maintenance_public_id(memory.id) for memory in rows],
                "total": total,
                "records": [_maintenance_target_evidence(store, memory.id, scope) for memory in rows],
            }
        if option == "archive":
            archive, keep = request.get("archive"), request.get("keep", [])
            if not context or not isinstance(archive, list) or not archive or not isinstance(keep, list):
                raise ValueError("请提供本轮上下文及明确的归档条目。")
            ids = keep + archive
            if (
                len(ids) > maintenance.BATCH_SIZE
                or any(not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", mid) for mid in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValueError("请提供不重复的有效条目编号。")
            draft = store.maintenance_preview(
                scope, maintenance.MODEL_DRAFT_OWNER, "", context=context,
                keep=tuple(sorted(keep)), archive=tuple(sorted(archive)),
            )
            return _maintenance_draft_result(store, draft, label, library_id, context)
        key = request.get(option)
        if option == "group" and "group" not in request:
            groups = [item for item in store.maintenance_check(scope).findings if item.kind == "duplicate"]
            if len(groups) != 1:
                return {
                    "status": "not_ready", "duplicate_groups": len(groups),
                    "message": "每次请选一组同义重复。其他情形用具体文字核对条目，或按用户指定的目标提出计划。",
                }
            key = groups[0].key
        length = 12 if option == "group" else 32
        if not isinstance(key, str) or not re.fullmatch(rf"[a-f0-9]{{{length}}}", key):
            raise ValueError("组号或回执号无效，请重新检查。")
        draft = store.maintenance_preview(
            scope, maintenance.MODEL_DRAFT_OWNER, key, undo=option == "receipt", context=context,
        )
        return _maintenance_draft_result(store, draft, label, library_id, context)
    except (ValueError, RecursionError) as exc:
        message = "整理参数格式无效。" if isinstance(exc, (json.JSONDecodeError, RecursionError)) else str(exc)
        return {"status": "rejected", "message": message}


def _maintenance_public_id(memory_id: str) -> str | None:
    return memory_id if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", memory_id) else None


def _maintenance_target_evidence(store: MemoryStore, memory_id: str, scope: str) -> dict[str, Any]:
    from March7.injection import format_recall_context

    detail = store.management_detail(memory_id)
    evidence = ""
    if detail is not None and detail.memory.scope == scope:
        memory = detail.memory
        source_quote = (detail.source or {}).get("quote") or memory.source_quote
        evidence = format_recall_context([memory.model_copy(update={"source_quote": source_quote})])
    return {"id": _maintenance_public_id(memory_id), "evidence": evidence}


def _maintenance_draft_result(
    store: MemoryStore, draft: maintenance.Batch, label: str, library_id: str, context: str | None,
) -> dict[str, Any]:
    result = {
        "status": "maintenance_preview", "scope": label, "library_id": library_id,
        "id": draft.id, "kind": draft.kind,
        "targets": [
            {"id": _maintenance_public_id(target.id),
             "before": target.before, "after": target.after}
            for target in draft.targets
        ],
    }
    if context:
        from March7.control.dialogue import confirmation_code

        result["confirmation_code"] = code = confirmation_code(draft.id)
        result["records"] = [_maintenance_target_evidence(store, target.id, draft.scope) for target in draft.targets]
        result["message"] = (
            f"请逐条回显实际计划和状态，并展示草稿编号 {code}。目前尚未写入。"
            f"请让用户在同一聊天带编号明确确认或取消，例如「按这个方案执行 {code}」。"
        )
    else:
        result["message"] = (
            "此草稿尚未改动记忆。需要聊天确认时请使用原生宿主提供的本轮上下文重做预览。"
            "也可在本机面板的「记忆体检」核对这张草稿。"
        )
    return result


def March7_resolve(conflict_id: str, resolution: str) -> Conflict | dict[str, Any]:
    """四分类裁决落库（Resolve a conflict）。

    Args:
        conflict_id: 冲突 id。
        resolution: 四分类之一（unrelated / context_difference /
            preference_evolution / direct_conflict）。

    Returns:
        更新后的冲突记录（被裁决失效的记忆标 superseded/merged，不删除）。

    Raises:
        ToolError: 裁决类型不在四分类取值域内，或冲突消解尚未落地（T16）。
    """
    engine = _engine()
    if resolution in _MAINTENANCE_OPTIONS:
        return _maintenance_proposal(engine.store, conflict_id, resolution)
    if resolution == "preview_erase":
        from March7.control.conversation import propose_conversation_erase, propose_erase

        if conflict_id.lstrip().startswith("{"):
            try:
                proposal = json.loads(conflict_id)
                context, selector = proposal["context"], proposal["selector"]
                if not isinstance(context, str) or not isinstance(selector, str):
                    raise TypeError
            except (TypeError, ValueError, KeyError):
                return {"status": "rejected", "message": "删除预览需要有效的本轮上下文和单条记忆 ID。"}
            return propose_conversation_erase(engine.store, context, selector, project_id=_call_project_id())

        return propose_erase(engine.store, conflict_id, project_id=_call_project_id())
    try:
        resolution_type = ResolutionType(resolution)
    except ValueError as exc:
        allowed = "、".join(item.value for item in ResolutionType)
        raise ToolError(
            f"未知的冲突裁决类型：{resolution!r}——合法取值：{allowed}。"
        ) from exc
    checker = engine.conflict_checker
    if checker is None:
        raise ToolError("冲突消解不可用：引擎装配时没有拿到 ConflictChecker。")
    if isinstance(checker, _PendingConflictChecker):
        raise ToolError("冲突消解当前不可用（引擎处于降级形态）：本工具暂不可用。")
    target = engine.store.get_conflict(conflict_id)
    if target is None or not _visible_conflict(engine.store, target, _call_project_id()):
        raise ToolError("当前项目看不到这条冲突记录，请核对库和项目。")
    try:
        return checker.resolve(conflict_id, resolution_type)
    except KeyError as exc:
        raise ToolError(f"冲突记录不存在：{conflict_id}") from exc
    except NotImplementedError as exc:
        # 只在引擎被注入成 `_PendingConflictChecker`（降级形态）时才会走到这里
        raise ToolError("冲突消解当前不可用（引擎处于降级形态）：本工具暂不可用。") from exc


def March7_export(format: str = "md", inline: bool = False) -> ExportResult:
    """全部记忆导出成人类可读文件（Export all memories）——透明化叙事。

    Args:
        format: "md"（markdown，默认）/ "json"。
        inline: 是否把导出内容一并返回。默认 False（只给路径，省 token）；
            用户明确要求「给我看看我记住了什么」时传 True。

    Returns:
        ExportResult(path + memory_count + 可选 content)。

    Raises:
        ToolError: 导出格式不在 {md, json} 取值域内。

    ⚠️ **本工具不提供"看原文"参数**（`ADR-0037`）：它的返回值会进模型上下文，
    而 D6 口径是「模型看到的是脱敏后的原话」。用户要看完整原文，走本地 CLI
    （`scripts/export_memories.py` 的逃生舱开关）——那条路不经模型。
    这也是 `tests/test_server.py::test_export_signature` 能继续钉死两个参数的原因。
    """
    engine = _engine()
    try:
        export_format = ExportFormat(format)
    except ValueError as exc:
        allowed = "、".join(item.value for item in ExportFormat)
        raise ToolError(f"未知的导出格式：{format!r}——合法取值：{allowed}。") from exc

    profile = engine.profile_store.load()
    # 输出目录 = 数据目录（~/.March7/）：导出文件跟着记忆库走，
    # 不往宿主进程的工作目录里丢文件。
    output_dir = Path(engine.config.data_dir)
    if export_format is ExportFormat.JSON:
        path = export_json(engine.store, profile, output_dir=output_dir)
    else:
        path = export_markdown(engine.store, profile, output_dir=output_dir)

    content = path.read_text(encoding="utf-8") if inline else None
    memory_count = engine.store.stats().memory_count
    return ExportResult(
        path=str(path),
        memory_count=memory_count,
        format=export_format,
        content=content,
    )


class _ToolStatusResult(StatusResult):
    """在 MCP 状态响应上附带安装契约的型号提示。"""

    model_unverified: bool = False
    diagnostics: dict[str, Any] | None = None


def March7_status() -> _ToolStatusResult:
    """宿主健康检查（Health check）：记忆条数 / 库大小 / 最近写入 + 容量与版本。

    `ADR-0026` 起额外返回四个字段（**只增不改**，旧消费者不受影响）：
    `schema_version`（库结构版本）、`recallable_count`（真正能召回的条数）、
    `today_writes`（今日写入）、`daily_write_cap`（熔断阈值，0 = 不限）。
    更完整的体检（含账本与钩子事件）用 `scripts/doctor.py`。

    S3（A4）起再追加两个字段：`last_receipt` / `last_receipt_summary`——
    回答「刚才那轮为什么注入这几条」。回执取自当天账本最近一条**带凭条**的行
    （`ledger.latest_receipt`）；没有凭条 ⇒ `None` + 空摘要，**不编**。

    Returns:
        统计信息。
    """
    engine = _engine()
    project = _call_project_resolution()
    project_id = project.project_id
    stats = _ToolStatusResult(
        **engine.store.stats().model_dump(),
        model_unverified=engine.llm is not None and engine.llm.model_unverified,
    )
    try:
        stats.diagnostics = diagnostics.runtime_snapshot(engine.store, project_id)
        stats.diagnostics["project_state"] = project.event or "bound"
    except Exception as exc:
        logger.warning("生成运行诊断失败（状态检查仍可用）：%s", type(exc).__name__)
    # 回执失败绝不能拖垮健康检查（它是诊断功能，不是主功能）
    try:
        from March7 import receipt

        built = diagnostics.visible_receipt(receipt.status_receipt_field(engine.config.data_dir), project_id)
    except Exception as exc:  # noqa: BLE001 —— 体检要给人话，不能甩栈
        logger.warning("读注入回执失败（status 其余字段不受影响）：%s", exc)
        return stats
    return stats.model_copy(
        update={
            "last_receipt": built,
            "last_receipt_summary": receipt.summarize(built) if built else "",
        }
    )


def March7_open_panel() -> str:
    """打开当前 MCP 安装绑定的本机面板，无需装配记忆引擎。"""
    bound = os.environ.get(ENV_DATA_DIR, "").strip()
    return open_chat_panel(Path(bound) if bound else None).chat_message()


# ---------------------------------------------------------------------------
# 工具注册（MCPServer v2）
# ---------------------------------------------------------------------------

# 工具描述：中文 + 英文双语（宿主模型可能英文思考，用户是中文使用者）。
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "March7_write": (
        "兼容的手动补记工具。若本轮提供【March7 操作结果】，直接转述实际结果，勿重复写入。"
        "补记只接受用户明确要求和用户原话。禁止猜测来源、编造原话或把未写入说成已记。\n"
        "Write this turn's transcript into long-term memory "
        "(Judge admission pipeline: prefer missing over wrong)."
    ),
    "March7_recall": (
        "按问题关键词混合检索记忆（BM25 + 向量 + RRF），返回浓缩结论 + 用户原话。\n"
        "空结果表示当前范围没有足够相关的记忆。查不到时可用 March7_status 核对项目和诊断。\n"
        "Hybrid-retrieve memories by keyword (BM25 + vector + RRF); "
        "returns condensed facts plus the user's original wording."
    ),
    "March7_profile": (
        "读取或更新 L0 用户画像（action='get' / 'update'）。"
        "更新必须带 source_quote（用户原话）——AI 推断的信息永远进不了画像。\n"
        "Read or update the L0 user profile. Updates require source_quote "
        "(the user's own words) - AI inferences never enter the profile."
    ),
    "March7_search": (
        "纯 BM25 关键词检索（精确找回专有名词、代码、编号）。\n"
        "转述匹配结论与原话。歧义时列出候选，找不到时如实说明。"
        "Pure BM25 keyword search for exact terms, names, or code identifiers."
    ),
    "March7_conflicts": (
        "列出待裁决的冲突（两条记忆互相矛盾、需要人工/模型裁决的候选）。\n"
        "List pending memory conflicts that need adjudication."
    ),
    "March7_resolve": (
        "按用户表达的意思选择检查记忆、清理重复、预览或撤销整理，无需用户使用固定句式。"
        "resolution='maintenance_check' 检查并列组与回执，'maintenance_preview' 生成同义组预览，"
        "'maintenance_undo_preview' 生成撤销预览。'maintenance_review' 接收 query 核对具体条目，"
        "'maintenance_plan' 接收 keep（保留编号）和 archive（归档编号）两组明确目标。"
        "conflict_id 传 JSON 字符串，scope='personal' 或 'project'，"
        "可分别传 page、group（仅一组时可省）或 receipt。若已有原生体检或工具回复的库编号，"
        "传 library_id 原样核对。用户要在聊天里完成，传本轮原生 context；它仅能提出计划。"
        "逐条展示真实计划和 confirmation_code，请用户在同一聊天带草稿编号明确确认或取消。"
        "用户要求预览或改方案时先核对，跨条件的取舍按用户明确的保留与归档选择拟计划。"
        "勿代用户执行，勿用终端、SQL 或换库文件做整理和撤销。"
        "历史用途为对一条冲突做四分类裁决（unrelated / context_difference / "
        "preference_evolution / direct_conflict）；失效记忆标 superseded/merged。"
        "resolution='preview_erase' 只生成预览。收到本轮宿主预览入口后，"
        "conflict_id 传含 context 与 selector 的 JSON 对象，selector 是单条可见记忆 ID。"
        "展示真实目标，让用户在同一聊天另发「确认删除」或「取消删除」，勿展示内部标识。"
        "旧客户端直接传 ID 列表走原预览方式。禁止未经授权裁决。\n"
        "Adjudicate a conflict into one of four classes; outdated memories are "
        "marked superseded/merged, never deleted."
    ),
    "March7_export": (
        "把全部记忆导出成人类可读文件（透明化叙事）。默认只返回路径与条数；"
        "inline=True 时一并返回内容。\n"
        "Export all memories to a human-readable file; returns path and count by "
        "default, content only when inline=True."
    ),
    "March7_status": (
        "宿主健康检查：记忆条数 / 库文件大小 / 最近写入时间。\n"
        "Health check: memory count, database size, and last write time."
    ),
}


def register_tools(mcp: MCPServer) -> None:
    """在 MCPServer 实例上注册记忆工具和本机面板工具。

    工具名**显式写死**（`name="March7_write"` 等）——工具名会进宿主 SKILL.md，
    是本项目最贵的一项命名约定（docs/NAMING.md §2），不能靠装饰器从函数名推导。

    Args:
        mcp: MCP v2 的 `MCPServer` 实例。
    """
    mcp.add_tool(
        March7_write, name="March7_write", description=_TOOL_DESCRIPTIONS["March7_write"]
    )
    mcp.add_tool(
        March7_recall,
        name="March7_recall",
        description=_TOOL_DESCRIPTIONS["March7_recall"],
    )
    mcp.add_tool(
        March7_profile,
        name="March7_profile",
        description=_TOOL_DESCRIPTIONS["March7_profile"],
    )
    mcp.add_tool(
        March7_search, name="March7_search", description=_TOOL_DESCRIPTIONS["March7_search"]
    )
    mcp.add_tool(
        March7_conflicts,
        name="March7_conflicts",
        description=_TOOL_DESCRIPTIONS["March7_conflicts"],
    )
    mcp.add_tool(
        March7_resolve,
        name="March7_resolve",
        description=_TOOL_DESCRIPTIONS["March7_resolve"],
    )
    mcp.add_tool(
        March7_export, name="March7_export", description=_TOOL_DESCRIPTIONS["March7_export"]
    )
    mcp.add_tool(
        March7_status, name="March7_status", description=_TOOL_DESCRIPTIONS["March7_status"]
    )
    mcp.add_tool(March7_open_panel, name="March7_open_panel", description=PANEL_TOOL_DESCRIPTION)
    logger.info("已注册 9 个工具（%s_*）", SERVER_NAME)


mcp = MCPServer(
    name=SERVER_NAME,
    instructions=(
        "March7 是跨宿主的长期记忆 MCP server：把用户明确表达的个人信息记住，"
        "并在需要时召回。写入走 Judge 准入管线（宁可漏记，不可误记）。"
        "\nMarch7 is a host-agnostic long-term memory server: it remembers what the "
        "user explicitly states and recalls it on demand."
        "\n用户想检查记忆、整理重复或看撤销方案时，调用 March7_resolve 的 maintenance 系列操作。"
        "已有本轮实际结果时直接转述。传本轮原生 context 后可在聊天核对并取得用户明确确认。"
        f"\n{PANEL_TOOL_DESCRIPTION}"
    ),
)
register_tools(mcp)


__all__ = [
    "March7_write",
    "March7_recall",
    "March7_profile",
    "March7_search",
    "March7_conflicts",
    "March7_resolve",
    "March7_export",
    "March7_status",
    "March7_open_panel",
    "register_tools",
    "_build_engine",
    "_call_project_id",
    "_engine",
    "Engine",
    "mcp",
]
