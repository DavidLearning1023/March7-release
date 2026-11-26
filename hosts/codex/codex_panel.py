"""Codex 原生用户入站。打开固定面板或处理有明确来源的记忆操作。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from March7.control import codex_config
from March7.control.panel_launcher import (
    match_open_request,
    open_chat_panel,
    panel_reply_context,
)

_LOGGER = logging.getLogger(__name__)


def _apply_model_env(env: dict[str, str]) -> None:
    """使用本宿主 MCP 已配置的模型凭据，整组覆盖避免混用。"""
    for role in ("MAIN", "JUDGE", "FALLBACK"):
        keys = [f"MARCH7_LLM_{role}_{field}" for field in ("BASE_URL", "API_KEY", "MODEL")]
        if any(key in env for key in keys):
            for key in keys:
                value = env.get(key)
                if isinstance(value, str) and value.strip():
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)


def main(argv: list[str] | None = None) -> int:
    """执行原生入站的固定能力，并把结果交给正常模型轮。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (ValueError, OSError):
        return 0
    if (
        not isinstance(payload, dict)
        or payload.get("hook_event_name") != "UserPromptSubmit"
        or payload.get("is_internal_continuation")
    ):
        return 0
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return 0
    session_id = str(payload.get("session_id") or "")
    try:
        settings = codex_config.read_settings(args.config) if args.config else {}
        mcp_env = codex_config.mcp_environment(settings)
        data_dir = codex_config.mcp_data_dir(mcp_env) or args.data_dir
    except (OSError, AttributeError, ValueError, TypeError):
        _LOGGER.warning("无法核实 Codex 的 March7 库配置")
        context = (
            "【March7 配置】无法核实 MCP 的库目录，本轮未提供记忆操作上下文。"
            "如果本轮在操作 March7，请核对 Codex 配置中的绝对库目录并重启宿主。"
        )
    else:
        context = _prompt_context(data_dir, mcp_env, payload, prompt, session_id)
    if not context:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context,
    }}))
    return 0


def _prompt_context(
    data_dir: Path, mcp_env: dict[str, str] | None, payload: dict, prompt: str, session_id: str,
) -> str:
    """面板和对话管理共享本轮库绑定。"""
    if match_open_request(prompt):
        if session_id:
            from March7.control.conversation import clear_conversation_turn

            try:
                clear_conversation_turn(
                    data_dir, "codex", session_id, event_id=str(payload.get("turn_id") or ""), prompt=prompt,
                )
            except (OSError, RuntimeError, sqlite3.Error):
                _LOGGER.exception("未能清理上一轮记忆引用")
        return panel_reply_context(open_chat_panel(data_dir).chat_message())
    return _operation_context(data_dir, mcp_env, payload, prompt, session_id) if session_id else ""


def _operation_context(
    data_dir: Path, mcp_env: dict[str, str] | None, payload: dict, prompt: str, session_id: str,
) -> str:
    from March7 import privacy
    from March7.control.conversation import (
        clear_conversation_turn, handle_user_command, match_memory_request, operation_reply_context, parse_user_command,
    )
    from March7.store import MemoryStore

    request = match_memory_request(prompt)
    try:
        operation = request is not None or parse_user_command(prompt) is not None
        turn_id = str(payload.get("turn_id") or "")
        if operation and not turn_id:
            clear_conversation_turn(data_dir, "codex", session_id)
            return operation_reply_context({"status": "rejected", "message": "宿主未提供用户轮次，请打开记忆面板续办。"})
        os.environ["MARCH7_HOME"] = str(data_dir)
        if request is not None and request.action == "remember":
            if mcp_env is None:
                mcp_env = codex_config.mcp_environment(codex_config.read_settings(codex_config.default_config_path()))
            _apply_model_env(mcp_env or {})
        with closing(MemoryStore(data_dir / "March7.db")) as store:
            store.init_schema()
            project = privacy.resolve_active_project(
                payload_value=payload.get("cwd"), process_cwd=os.getcwd(), known_project=store.get_project_id,
            )
            if not store.allows_session_project("codex", session_id, project.project_id):
                clear_conversation_turn(data_dir, "codex", session_id, event_id=turn_id, prompt=prompt)
                return operation_reply_context({"status": "rejected", "message": "工作目录和当前会话的项目不一致，请核对目录或新开会话。"})
            result = handle_user_command(
                store, prompt, project_id=project.project_id, session_id=session_id, host="codex", event_id=turn_id,
                offer_dialogue=True,
            )
            if request is not None and request.action == "remember":
                store.mark_ingested(session_id, turn_id)
        return operation_reply_context(result) if result is not None else ""
    except (OSError, RuntimeError, AttributeError, TypeError, ValueError, sqlite3.Error):
        _LOGGER.exception("Codex 原生记忆操作未完成")
        return operation_reply_context({"status": "failed", "message": "未能核实本轮操作；请打开记忆面板核对。"})


if __name__ == "__main__":
    raise SystemExit(main())
