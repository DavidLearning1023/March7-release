"""user_prompt_submit.py — UserPromptSubmit 钩子：**每轮**按当前提问召回记忆并注入。

职责：拿用户这一轮的 prompt 去检索记忆库，把命中的记忆（`content` + 用户原话）
以 `additionalContext` 注入本轮上下文 ⇒ 宿主模型**不需要**主动调 `March7_recall`
就能用上长期记忆。

⚠️ **为什么走纯 BM25（不走向量）**：宿主每轮都会触发这个钩子，而钩子是**独立进程**——
走混合检索就等于**每轮重新加载 BGE-M3**（实测 7.5s + 数 GB 显存），宿主会卡到不可用。
纯 BM25 走 FTS5 索引（入库时已写好），毫秒级。

⚠️ **为什么用 OR 语义**（`store.fts_search_any`）：FTS5 的 MATCH 是**隐式 AND**，
整句提问切出来的 token 必须全部命中 ⇒「我做什么交易」查不中「做黄金 ETF 交易」。

⚠️ **恒 exit 0**（坑 2）。

**三态 + 四事件**（S1 / `ADR-0037`）：
本轮的结果必须落进 `decisions-YYYYMMDD.jsonl`，且四个事件互斥完备：
`no_hit` / `all_filtered` / `all_omitted` / `injected`。
为什么要记这么细：`ups_no_hit`（检索没命中）与「命中但全被状态过滤」在旧日志里
长得一模一样，而这两种情况的修法完全相反；不记明细就只能靠猜。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402 - 必须在 sys.path 调整之后导入
    EVENT_USER_PROMPT_SUBMIT,
    apply_data_dir_env,
    apply_host_model_env,
    elapsed_ms,
    emit_context,
    log_event,
    project_identity,
    project_value,
    resolve_bound_data_dir,
    run_hook,
)

RECALL_LIMIT = 5
"""召回条数上限（与 `March7_recall` 的默认 top_k 一致）。"""

HOST_NAME = "workbuddy"
"""写进 `host_registry` / `memories.host` 的宿主标识（与 `stop.py` 同值）。"""

"""参与召回的 status（`archived` / `superseded` / `merged` 不召回，CONVENTIONS §9）。"""

PROJECT_PATH_KEYS = ("cwd",)
"""载荷里承载「当前项目根」的键。

**已由真机实测收窄**（2026-11-10 13:0x，读 `hook_start` 的 `payload_keys`）：

| 事件 | 实际键 | 有 cwd？ |
|---|---|---|
| `UserPromptSubmit` | client / **cwd** / generation_id / hook_event_name / is_internal_continuation / model / permission_mode / prompt / session_id / transcript_path / version | ✅ |
| `Stop` | agent_type / background_tasks / client / **cwd** / generation_id / hook_event_name / is_truncation_recovery / last_assistant_message / model / permission_mode / session_crons / session_id / stop_hook_active / transcript_path / version | ✅ |
| `SessionStart` | client / generation_id / hook_event_name / model / permission_mode / session_id / source / transcript_path / version | ❌（只注入画像，不需要项目身份） |

所以**只认 `cwd`**。原先那份候选列表（`project_dir` / `workspace` / `workspace_folder`）
实测一个都没出现——留着就是死代码：读代码的人会以为宿主支持那些键，
而写测试的人会以为那些分支值得覆盖。要加新键，先拿真机 `payload_keys` 来。

⚠️ **S9 起实现搬去 `_common`**（`ADR-0049` 决策 3 需要 Stop 通道也解析项目身份）。
本文件保留同名 re-export：`test_host_hooks.py` 与历史排查脚本按这两个名字取。
**不要在钩子里再写第二份**——两份漂一次，就会变成「UPS 解析对了、Stop 解析错了」，
而那只在跨项目时才发作。
"""

_project_value = project_value
_project_identity = project_identity


WORKING_BLOCK_HEADER = "【March7 工作记忆 · 当前 handoff】"
"""工作块标题。**与长期召回块分开**：两块的时效性完全不同（长期是事实，工作是当前状态），
混在一个围栏里模型无法区分「用户长期如此」与「用户这周如此」。

⚠️ 渲染实现已下沉到 `March7.injection.format_working_context`（S3）：
本文件只保留这个常量作为**向后兼容的 re-export**（`test_ups_working.py` 与
历史排查脚本按它断言）。真正的渲染只有一份，不要在钩子里重写。
"""

WORKING_KIND_LABELS = {
    "constraint": "约束",
    "state": "进展",
    "scratch": "临时",
}
"""kind → 人话标签（`ADR-0039` 闭集）。**渲染侧的实现在 `injection` 模块**，
这里同上是兼容 re-export。"""


def _capture_working(
    store,
    *,
    prompt: str,
    project_id: str | None,
    project_root: str | None,
    session_id: str,
    message_id: str | None,
) -> None:
    """把本轮 prompt 里的工作状态写进 `working_items`（**失败隔离**）。

    ⚠️ **任何异常都在这里吞掉并记日志**（`ADR-0042` R2）：working 是"锦上添花"，
    而长期召回是本轮的主功能。让一个规则抽取的正则把整轮注入拖垮，
    用户看到的是"记忆功能全挂了"，而根因只是 working 分支。

    五个前置条件缺一即跳过（**不猜**）：
    1. 有 `project_id`（没有项目就没有"当前项目的工作"）；
    2. 该项目有 `open` 的 handoff（不自动开——开 handoff 是用户的显式动作）；
    3. `working.should_attempt()` 命中（节流阀，`ADR-0042` R7）；
    4. 没有明显 secret（`ADR-0042` R9）；
    5. 抽取出 ≥1 条。
    """
    if not project_id:
        log_event("ups_working_skipped", reason="no_project", session_id=session_id)
        return

    from March7 import privacy, working

    try:
        handoff_id = store.get_open_handoff(project_id)
        if not handoff_id:
            log_event("ups_working_skipped", reason="no_open_handoff", session_id=session_id)
            return
        if not working.should_attempt(prompt):
            log_event("ups_working_skipped", reason="prefilter", session_id=session_id)
            return
        secret = working.detect_secret(prompt)
        if secret:
            # 只记类别名，**绝不记命中的那段文本**（那正是 secret 本身）
            log_event(
                "ups_working_secret_rejected",
                session_id=session_id,
                secret_kind=secret,
            )
            return
        candidates = working.extract(prompt)
        if not candidates:
            log_event("ups_working_skipped", reason="no_candidate", session_id=session_id)
            return
        written = 0
        for candidate in candidates:
            # 脱敏在与"入库/注入"同一处做（`ADR-0037` 决策 3）：原文里的绝对路径
            # 不该进库——working 会**下一轮注入**，路径进注入比进日志更严重。
            quoted = privacy.redact_view(candidate.source_quote, project_root=project_root).text
            body = privacy.redact_view(candidate.content, project_root=project_root).text
            seq = store.add_working_item(
                handoff_id=handoff_id,
                content=body,
                kind=candidate.kind,
                host="workbuddy",
                session_id=session_id,
                message_id=message_id,
                project_id=project_id,
                source_quote=quoted,
            )
            if seq:
                written += 1
        log_event(
            "ups_working_captured",
            session_id=session_id,
            handoff_id=handoff_id,
            candidates=len(candidates),
            written=written,
            kinds=[c.kind for c in candidates],
        )
    except Exception as exc:  # noqa: BLE001 —— 失败隔离：working 绝不能拖垮整轮注入
        log_event(
            "ERROR",
            hook_event=EVENT_USER_PROMPT_SUBMIT,
            stage="working_capture",
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )


def _compose_context(
    working_items: list[dict],
    recall_text: str,
    *,
    project_root: str | None,
) -> str:
    """把工作块与长期召回块拼成最终注入文本（**空块不出现**）。

    ⚠️ **S3 起本函数只是兼容薄壳**：渲染的唯一实现在
    `March7.injection.format_injection`（`ADR-0029` 的同一纪律——生产与评测必须共用
    一份渲染，两边漂一次 A/B 测的就不是线上路径）。保留它是因为
    `tests/test_ups_working.py` 与历史排查脚本按它断言"两块怎么拼"。

    顺序刻意是「工作块在前」：它是**本轮上下文**，长期记忆是背景事实；
    模型读到的第一段应该是"用户现在在做什么"，否则它容易拿三个月前的架构决定
    来回答今天的进度问题。

    预算：工作块 ≤300 字（`ADR-0039` 注入矩阵），**不计入**长期召回的 1200 字上界
    ⇒ T15 的 424 字基线口径不受影响（它测的是长期召回块）。
    """
    from March7.injection import format_injection

    render = format_injection(
        working_items=working_items,
        memories=[],  # 长期块已由调用方渲染好；这里只拼 working
        project_root=project_root,
    )
    return "\n\n".join(part for part in (render.working_text, recall_text) if part)


WORKING_MAX_ITEMS = 2
"""单轮最多注入的工作条目数（`ADR-0039` 注入矩阵：working ≤2 条）。

⚠️ 兼容 re-export：渲染侧的唯一常量在 `March7.injection.MAX_WORKING_ITEMS`。
"""

WORKING_MAX_ITEM_CHARS = 120
"""单条工作条目的注入上限（与抽取上限同值：超了在抽取阶段就被丢弃）。

⚠️ 兼容 re-export：渲染侧的唯一常量在 `March7.injection.MAX_WORKING_ITEM_CHARS`。
"""


def _project_diagnostics(
    store, *, project_id: str, project_root: str, root_hash: str
) -> list[dict]:
    """跑三个项目身份诊断（S3 / A5），返回要落日志的事件（**失败隔离**）。

    三个事件各自覆盖一种根因，**全部只报警、不自动合并**（`ADR-0039` 决策 2）：
    - `duplicate_cwd_hash`：两宿主把同一个 cwd 算成两个项目；
    - `norm_mismatch`：同一 root_hash 但归一化规则版本不同；
    - `project_split_suspected`：同一目录名在 24h 内出现第二个 project_id。

    ⚠️ **任何异常都在这里吞掉**（同 `_capture_working` 的纪律）：
    诊断是观测手段，绝不能因为"算诊断时出了个错"而让整轮注入消失。

    ⚠️ 事件里**不带路径**（`diagnostics.ALLOWED_FIELDS` 白名单强制）：
    只有 `project_id` + `host` + 计数。`root_hash` 只作为**入参**用于库内等值查询，
    绝不进事件、不进日志（它进日志等于把路径指纹贴出去）。

    顺序敏感（三点，顺序即语义）：
    1. **split 先于登记**：反过来新项目永远看不见兄弟（它把自己算进去了）；
    2. **norm_mismatch 先于 `put_project`**：`put_project` 只在首次插入写 `norm_version`，
       所以读旧值必须在写之前——写在前面会把"版本变过"这件事抹掉；
    3. **duplicate 先于 `record_host_cwd`**：后者在不一致时会抛，
       而钩子必须恒 exit 0 ⇒ 事件必须在抛之前记下来。
    """
    import os

    from March7 import diagnostics, privacy

    events: list[dict] = []
    try:
        basename = project_root.rstrip("/").rsplit("/", 1)[-1]

        # ① split（先检测、后登记）
        split = diagnostics.detect_project_split(
            store, basename=basename, project_id=project_id
        )
        if split:
            events.append(split)
        diagnostics.record_basename(store, basename=basename, project_id=project_id)

        # ② norm_mismatch（先读库里的版本，再让 put_project 去写）
        mismatch = diagnostics.detect_norm_mismatch(
            store,
            root_hash=root_hash,
            project_id=project_id,
            norm_version=privacy.NORM_VERSION,
        )
        if mismatch:
            events.append(mismatch)

        # ③ duplicate_cwd_hash（host_registry 的键与 root_hash 同源，取前 16 位做短键）
        cwd_hash = root_hash[:16]
        duplicate = diagnostics.detect_duplicate_cwd_hash(
            store, host=HOST_NAME, cwd_hash=cwd_hash, project_id=project_id
        )
        if duplicate:
            events.append(duplicate)

        store.put_host_registry(HOST_NAME, data_dir_key_for(store))
        try:
            store.record_host_cwd(HOST_NAME, cwd_hash, project_id)
        except ValueError:
            # 分叉已由上面的 detect 记过事件；不再抛给宿主（恒 exit 0）
            pass
    except Exception as exc:  # noqa: BLE001 —— 诊断失败不得影响注入
        log_event("ups_diagnostics_failed", error=type(exc).__name__, detail=str(exc)[:200])
    return events


def data_dir_key_for(store) -> str:
    """数据目录的**哈希**（`host_registry.data_dir_key`；不存绝对路径）。

    为什么是哈希：这张表的用途是"两宿主是不是指向同一个库"，
    存绝对路径等于把路径写进库；存哈希足以做等值比较。
    """
    import hashlib

    return hashlib.blake2s(str(store.data_dir).encode("utf-8"), digest_size=8).hexdigest()


def _memory_meta_for(store, memory_ids: list[str]) -> tuple[dict[str, str], dict[str, int]]:
    """批量读 `memory_meta` → `(categories, confirmed_at)`，喂给注入层的配额/时效标签。

    为什么单独一次批量读：`format_injection` 要按 category 分配额，逐条查库会让
    注入路径变成 N 次 IO（UPS 每轮都跑，这条路径的成本要压到毫秒级）。
    """
    categories: dict[str, str] = {}
    confirmed_at: dict[str, int] = {}
    for memory_id in memory_ids:
        try:
            meta = store.get_memory_meta(memory_id)
        except Exception:  # noqa: BLE001 —— 老库没有 memory_meta 表时降级为无标签
            continue
        if not meta:
            continue
        category = meta.get("category")
        if isinstance(category, str) and category:
            categories[memory_id] = category
        stamp = meta.get("last_confirmed_at")
        if isinstance(stamp, int) and stamp > 0:
            confirmed_at[memory_id] = stamp
    return categories, confirmed_at


def _try_open_panel(prompt: str, session_id: str) -> bool:
    """先打开绑定库，给正常助手轮次提供结果，并跳过本轮记忆召回。"""
    try:
        from March7.control.panel_launcher import (
            match_open_request,
            open_chat_panel,
            panel_reply_context,
        )
    except Exception as exc:  # noqa: BLE001 —— 缺依赖不能让整轮熔断，交回正常路径
        log_event(
            "ERROR",
            hook_event=EVENT_USER_PROMPT_SUBMIT,
            stage="panel_import",
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return False
    if not match_open_request(prompt):
        return False

    try:
        # ⚠️ **必须把钩子已经解析出的库传进去**（S17 独立验收抓到的真缺陷）：
        # `panel_launcher.resolve_binding()` 认的是 `Path.home()/.workbuddy/mcp.json`，
        # 而钩子认的是**安装副本所在目录**的 `mcp.json`（`_common.host_mcp_config_path`）。
        # 两个规则一旦不同（沙盒、非默认宿主目录、`MARCH7_HOME` 与配置不一致），
        # 就会出现"面板打开 A 库、记忆写进 B 库"——那正是卡里「验证不会连接其他库」
        # 要防的事。传参之后，面板与钩子**在结构上**不可能分叉。
        result = open_chat_panel(resolve_bound_data_dir(), desktop=True)
    except Exception as exc:  # noqa: BLE001 —— 打开失败要出声，但不能熔断
        log_event(
            "ups_panel_failed",
            session_id=session_id,
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        failure = f"没有打开 March7 记忆面板：{type(exc).__name__}。"
        emit_context(EVENT_USER_PROMPT_SUBMIT, panel_reply_context(failure))
        return True

    log_event(
        "ups_panel_open",
        session_id=session_id,
        opened=result.opened,
        reused=result.reused,
        port=result.port,
        source=result.source,
        failure=result.failure,
        # 从钩子进程启动算起：这是"原生路径不等待模型"的证据坐标
        elapsed_ms=elapsed_ms(),
    )
    emit_context(EVENT_USER_PROMPT_SUBMIT, panel_reply_context(result.chat_message()))
    return True


def handle(payload: dict) -> None:
    """按本轮 prompt 召回 → 注入 → 记决策。**召回不到就什么都不注入**。"""
    prompt = str(payload.get("prompt") or "").strip()
    session_id = str(payload.get("session_id") or "")
    if not prompt:
        log_event("ups_skipped_no_prompt", session_id=session_id)
        return
    native = payload.get("hook_event_name") == EVENT_USER_PROMPT_SUBMIT and payload.get("is_internal_continuation") is False
    event_id = str(payload.get("message_id") or payload.get("generation_id") or "") if native else ""
    if not payload.get("is_internal_continuation") and _try_open_panel(prompt, session_id):
        if session_id and (bound := resolve_bound_data_dir()) is not None:
            from March7.control.conversation import clear_conversation_turn

            clear_conversation_turn(bound, HOST_NAME, session_id, event_id=event_id, prompt=prompt)
        return

    from March7 import diagnostics, ledger, privacy, receipt
    from March7.injection import format_injection
    from March7.retriever import AUTO_RECALL_MIN_SHARED, select_automatic_recall
    from March7.store import MemoryStore

    # 与 Stop 钩子同一口径：先把数据目录回写进环境变量，钩子自己的 store
    # 与引擎（若被触发）永远指向同一个库。
    data_dir = apply_data_dir_env()
    store = MemoryStore(data_dir / "March7.db")
    store.init_schema()
    project_id: str | None = None
    project_root: str | None = None
    unresolved: str | None = None
    # ⚠️ **水位线必须在任何写之前取一次**（卡步骤 4 / `ADR-0042` R3）：
    # 写之后再取，本轮刚插入的条目会落进 `seq <= watermark`
    # ⇒ 用户当轮就看到自己刚说的话被"回忆"，像复读机。
    # 它在这里、而不是在 working 相关的代码旁边，就是为了让顺序**在结构上**不可能颠倒。
    watermark = store.working_watermark()
    working_items: list[dict] = []
    diagnostic_events: list[dict] = []
    dialogue_context = ""
    try:
        project_id, project_root, unresolved = _project_identity(payload, known_project=store.get_project_id)
        if not store.allows_session_project(HOST_NAME, session_id, project_id):
            if session_id and native:
                from March7.control.conversation import clear_conversation_turn

                clear_conversation_turn(data_dir, HOST_NAME, session_id, event_id=event_id, prompt=prompt)
            project_id, project_root, unresolved = None, None, privacy.EVENT_PROJECT_SESSION_CHANGED
            emit_context(EVENT_USER_PROMPT_SUBMIT, "【March7】当前工作目录与此会话的项目身份不一致。本轮已暂停项目记忆，请核对目录或新开会话。")
            log_event(unresolved, host=HOST_NAME)
            return
        if project_id and project_root:
            # 先查库、再登记：库里已有的映射是既成事实（派生规则将来变了也不能改历史）。
            digest = privacy.root_hash_for(project_root)
            project_id = store.get_project_id(digest) or project_id
            # S3 / A5：三种项目身份错乱都要留痕（只报警、不自动合并）。
            # ⚠️ **必须在 `put_project` 之前跑**：`put_project` 只在首次插入写
            # `norm_version`，但 `norm_mismatch` 要读的是**写之前**那个值；
            # 顺序反了，"规则变过"这件事会被本次写入抹掉。
            diagnostic_events = _project_diagnostics(
                store, project_id=project_id, project_root=project_root, root_hash=digest
            )
            store.put_project(digest, project_id)
        if session_id and native:
            from March7.control.conversation import handle_user_command, match_memory_request, operation_reply_context

            request = match_memory_request(prompt)
            if request is not None and request.action == "remember":
                apply_host_model_env()

            result = handle_user_command(
                store, prompt, project_id=project_id, session_id=session_id, host="workbuddy",
                event_id=event_id,
                offer_dialogue=True,
            )
            if result is not None and result["status"] == "dialogue_context":
                dialogue_context = operation_reply_context(result)
            elif result is not None:
                emit_context(EVENT_USER_PROMPT_SUBMIT, operation_reply_context(result))
                log_event("ups_control", session_id=session_id, status=result["status"])
                return
        # 读工作层（**只用 watermark 之前的**）：必须在写之前读，否则本轮写入会漏进来。
        working_items = store.list_injectable_working(project_id, watermark=watermark)
        selection = select_automatic_recall(
            store, prompt, min_shared=AUTO_RECALL_MIN_SHARED, limit=RECALL_LIMIT, project_id=project_id,
        )
        hits, memories = selection.candidates, selection.memories
        filtered_ids, out_of_scope_ids = selection.outdated, selection.out_of_scope
        categories, confirmed_at = _memory_meta_for(store, [m.id for m in memories])
        # S3 步骤 2/3：两块合成 + 类别标签 + 配额，全部走引擎层唯一那份实现
        render = format_injection(
            working_items=working_items,
            memories=memories,
            project_root=project_root,
            categories=categories,
            confirmed_at=confirmed_at,
        )
        # 写工作层。**它排在「读」之后**：这不是风格，是 A3 的判据（本轮写的本轮不可见）。
        _capture_working(
            store,
            prompt=prompt,
            project_id=project_id,
            project_root=project_root,
            session_id=session_id,
            message_id=str(payload.get("message_id") or "") or None,
        )
    finally:
        store.close()

    # 诊断事件落日志（**不带路径**：只有 project_id + host + 计数）
    for event in diagnostic_events:
        log_event(event["event"], **{k: v for k, v in event.items() if k != "event"})

    considered = len({memory.id for memory in [*hits, *memories]})
    outcome = ledger.classify_recall(considered, len(memories), bool(render.recall_text))
    # A4：回执与注入内容同源——同一个 `render` 既产出文本也产出凭条，
    # 所以"回执说注入 3 条、文本里只有 2 条"在结构上不可能发生。
    built = receipt.build_receipt(
        render, project_id=project_id, prompt_chars=len(prompt), categories=categories
    )
    ledger.append_decision(
        {
            "event": "recall_decision",
            "outcome": outcome,
            "session_id": session_id,
            "prompt_chars": len(prompt),
            "candidates": considered,
            "injected": list(render.recall.injected_ids),
            "omitted": list(render.recall.omitted_ids),
            "omitted_reasons": dict(render.recall.omitted_reasons),
            "filtered": filtered_ids,
            # S3：因作用域被挡掉的候选（不是"没命中"，也不是"状态失效"）
            "out_of_scope": out_of_scope_ids,
            "low_relevance_count": len(selection.low_relevance),
            "chars": len(render.recall_text),
            "project_id": project_id,
            # 工作层对账（S2）：本轮看到几条、本轮写了几条
            "working_visible": len(working_items),
            "working_seq": [int(item["seq"]) for item in working_items],
            "working_kinds": [str(item["kind"]) for item in working_items],
            "watermark": watermark,
            # 三档未解析事件 + 视图事件（去重保序；不含任何路径原文）
            "privacy_events": list(
                dict.fromkeys(list(render.events) + ([unresolved] if unresolved else []))
            ),
            # A4 的凭条（`ledger.latest_receipt` / `March7_status` 读它）
            "receipt": built,
        },
        data_dir,
    )

    context = render.text
    diagnostics.trace_recall(
        data_dir, project_id, channel="automatic", host=HOST_NAME, session_id=session_id,
        match_count=len(render.recall.injected_ids), chars=len(render.recall_text),
        filtered=bool(hits) and not render.recall_text,
    )
    if not context:
        # 旧事件名保留（`doctor` / 历史排查脚本按它统计），明细看 decisions-*。
        log_event(
            "ups_no_hit" if outcome == ledger.RECALL_NO_HIT else f"ups_{outcome}",
            session_id=session_id,
            prompt_chars=len(prompt),
            candidates=len(hits),
            filtered=len(filtered_ids),
            out_of_scope=len(out_of_scope_ids),
            working_visible=len(working_items),
        )
        if dialogue_context:
            emit_context(EVENT_USER_PROMPT_SUBMIT, dialogue_context)
        return
    emit_context(EVENT_USER_PROMPT_SUBMIT, "\n".join(filter(None, (context, dialogue_context))))
    log_event(
        "ups_injected",
        session_id=session_id,
        prompt_chars=len(prompt),
        hits=len(memories),
        chars=len(context),
        injected_ids=list(render.recall.injected_ids),
        working_ids=list(render.working_ids),
        out_of_scope=len(out_of_scope_ids),
        omitted=len(render.recall.omitted_ids),
    )


if __name__ == "__main__":
    sys.exit(run_hook(handle))
