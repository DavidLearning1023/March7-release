"""stop.py — Stop 钩子：读本轮转写 → 跑 Judge 准入管线 → 写库。

这是 March7 的**确定性写入通道**：不依赖模型「想起来」去调 `March7_write`，
每一轮结束都会自动走一遍「抽取候选 → 四条硬过滤 → 入库 / 写画像」。

⚠️ **六个坑**（`docs/CONVENTIONS.md` §5）：
1. 仅 `type:"command"`；2. **恒 exit 0**（`exit 2` = 整轮熔断 ⇒ 本轮没有 Stop ⇒
   写入闭环直接断，且界面毫无提示）；3. Windows 经 Git Bash 执行 ⇒ 路径加引号、用 `/`；
4. stdout 强制 UTF-8；5. 配置启动时快照；6. 转写是**树状**结构，字段全在顶层。

**「只认当前有效分支」**（ADR-0016 决策 3）：本脚本只处理**当前分支**上的用户消息。
用户编辑/重新生成一条消息 ⇒ 宿主写出**新 id** 的记录 ⇒ 新分支的消息 id 未被处理过
⇒ 会被重新处理；而被编辑掉的旧分支不在当前分支上 ⇒ 它的原话**不会**成为新事实的来源。

**每轮幂等**：`ingest_marks` 记录「已处理过的宿主消息 id」⇒ 同一批消息不会被反复跑 Judge
（否则每轮都重复烧一次 LLM 调用 + 重复写画像）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402 - 必须在 sys.path 调整之后导入
    EVENT_STOP,
    apply_data_dir_env,
    apply_host_model_env,
    branch_user_messages,
    build_context,
    build_turn_transcript,
    current_branch,
    elapsed_ms,
    install_stop_budget,
    log_event,
    parse_transcript,
    project_identity,
    run_hook,
    user_own_text,
)

HOST_NAME = "workbuddy"
"""写进 `memories.host` 的宿主标识（docs/NAMING.md §2）。"""


def handle(payload: dict) -> None:
    """Stop 主流程：定位待处理的用户消息 → 写库 → 记水位线。"""
    session_id = str(payload.get("session_id") or "")
    transcript_path = str(payload.get("transcript_path") or "")
    if not session_id:
        log_event("stop_skipped_no_session")
        return

    # ⚠️ **必须在碰引擎之前**装预算：`llm.py` 在 client 构造时读超时环境变量，
    # 晚一步就来不及收紧（`ADR-0036`）。顺序：预算 → 数据目录 → 引擎。
    deadline_ms = install_stop_budget()

    # ⚠️ **必须在碰引擎之前**把数据目录回写进环境变量：
    # 引擎侧 `load_config()` 只看 `MARCH7_HOME`，不回写就会把记忆写进
    # **生产库 `~/.March7`**（数据红线，CONVENTIONS §2）。
    data_dir = apply_data_dir_env()

    records = parse_transcript(transcript_path)
    if not records:
        log_event("stop_no_transcript", session_id=session_id, path=transcript_path)
        return

    branch = current_branch(records)
    user_messages = branch_user_messages(branch)
    if not user_messages:
        log_event("stop_no_user_message", session_id=session_id, records=len(records))
        return

    from March7.control.panel_launcher import match_open_request

    if match_open_request(user_own_text(user_messages[-1])):
        log_event("stop_skipped_panel_request", session_id=session_id)
        return
    user_messages = [record for record in user_messages if not match_open_request(user_own_text(record))]

    # 项目身份（`ADR-0049` 决策 3）：写入侧按它赋 `scope=project:<id>`。
    from March7 import ledger, privacy
    from March7.store import MemoryStore

    store = MemoryStore(data_dir / "March7.db")
    store.init_schema()
    try:
        project_id, _project_root, unresolved = project_identity(payload, known_project=store.get_project_id)
        if project_id is None and unresolved:
            log_event("stop_no_project", session_id=session_id, reason=unresolved)
        if not store.allows_session_project(HOST_NAME, session_id, project_id):
            ledger.append({"event": privacy.EVENT_PROJECT_SESSION_CHANGED, "host": HOST_NAME, "conversation_id": session_id}, data_dir)
            return
        pending = [rec for rec in user_messages if not store.is_ingested(session_id, str(rec.get("id") or ""))]
        if not pending:
            log_event("stop_already_ingested", session_id=session_id, candidates=len(user_messages))
            return

        # 走**与 MCP 工具面同一条代码路径**（`server.March7_write`）——
        # 两条通道共用一份实现 ⇒ 硬过滤 / 画像护栏 / 来源落库的行为**不可能分叉**。
        from March7 import server
        from March7.server import last_extraction_failed, write_with_context
        from March7.control.conversation import match_memory_request, parse_user_command
        from March7.control.dialogue import source_key

        for idx, record in enumerate(pending):
            # `ADR-0036` 硬截止：到点就**整轮收口、不打水位线**，让下一轮自动重试。
            # 不打水位线是刻意的——与 `stop_extraction_failed` 同一条纪律：
            # 打了就等于宣布「这条消息判过了」，剩下的待写消息会永久丢失。
            if elapsed_ms() >= deadline_ms:
                log_event(
                    "stop_deadline_exceeded",
                    session_id=session_id,
                    elapsed_ms=elapsed_ms(),
                    deadline_ms=deadline_ms,
                    remaining=len(pending) - idx,
                    note="本轮预算用尽 ⇒ 剩余消息不打水位线，下一轮自动重试",
                )
                return
            message_id = str(record.get("id") or "")
            text = user_own_text(record).strip()
            if not message_id or not text:
                continue
            if (parse_user_command(text) is not None or match_memory_request(text) is not None
                    or store.dialogue_handled(source_key(HOST_NAME, session_id, text))):
                store.mark_ingested(session_id, message_id)
                continue
            # 传**这一轮的对话转写**（用户那句话 + 紧随其后的助手回答），
            # 而不是单行残句——Judge 的提示词要的就是「对话转写」（T3 原设计）。
            transcript = build_turn_transcript(record, branch)
            # 上文 = 本轮之前的 2 个用户轮（`ADR-0049` 决策 1）；会话第一句时为 `None`。
            # 为什么在钩子里拼而不是引擎里拼：只有钩子手里有整条分支（引擎只拿到转写串）。
            context = build_context(record, branch)
            result = write_with_context(
                transcript,
                conversation_id=session_id,
                host=HOST_NAME,
                source_session_id=session_id,
                source_message_id=message_id,
                # WorkBuddy 的转写**没有独立的 revision 字段**；消息 id 变化即修订
                # （编辑/重新生成会写出新 id）⇒ 这里不编造 revision 号。
                source_revision=None,
                project_id=project_id,
                context=context,
            )
            # ⚠️ **「没写进去」有两种完全不同的原因**（实测真机踩到）：
            #   ① Judge 判定这轮没有值得记的（正常）；
            #   ② LLM 调用/解析重试耗尽 ⇒ 引擎**什么都没判**（失败）。
            # 两者在 `WriteResult` 里长得一模一样（四个字段全空）⇒ 必须问引擎
            # 要那个只读观测位。② 若还照常打水位线，这条事实就**永久丢了**
            # （下一轮不会重试）——所以② 不打水位线，让下一轮自动重试。
            extraction_failed = last_extraction_failed()
            if extraction_failed:
                log_event(
                    "stop_extraction_failed",
                    session_id=session_id,
                    message_id=message_id,
                    chars=len(transcript),
                    elapsed_ms=elapsed_ms(),
                    deadline_ms=deadline_ms,
                    note="LLM 调用失败 ⇒ 本轮不写、不打水位线，下一轮自动重试",
                )
                continue
            store.mark_ingested(session_id, message_id)
            log_event(
                "stop_ingested",
                session_id=session_id,
                message_id=message_id,
                written=result.written_count,
                updated_l0=result.updated_l0_count,
                rejected=result.rejected_count,
                conflicts=len(result.conflict_candidates),
                # ⚠️ **拒收理由与写入内容都要落进审计日志**。
                # 理由：钩子注入与拒收在界面上**都不可见**（spike memo §7.1）——
                # 「拒绝了」与「没跑」在用户眼里一模一样。拒绝是合法结果（宁可漏记），
                # 但**看不见的拒绝就等于静默失败**。
                written_contents=[m.content for m in result.written],
                rejected_reasons=[
                    {"rule": r.rule, "reason": r.reason} for r in result.rejected
                ],
                updated_l0_fields=list(result.updated_l0_fields),
                elapsed_ms=elapsed_ms(),
                # `deadline_ms` 兼作**部署版本标记**：新副本一定有这个字段，
                # 11-03 的旧副本没有。doctor 据此判断「预算/截止那一半是否已生效」，
                # 而不是靠猜延迟高低（独立验收 P1-A：延迟低只说明引擎侧惰性化生效，
                # 因为 `src/March7` 走 editable 包、改完即时生效，压根不需要部署钩子）。
                deadline_ms=deadline_ms,
            )
    finally:
        store.close()


def _handle_with_host_model(payload: dict) -> None:
    """安装入口把 MCP 配置传给独立的 Stop 进程。"""
    apply_host_model_env()
    handle(payload)


if __name__ == "__main__":
    sys.exit(run_hook(_handle_with_host_model))
