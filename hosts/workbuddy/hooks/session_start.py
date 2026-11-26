"""session_start.py — SessionStart 钩子：新会话注入 L0 用户画像。

职责：把 `~/.March7/profile.json` 的 L0 画像（八字段）注入宿主上下文，
让宿主模型**一开始就知道用户是谁**（称呼 / 职业 / 长期兴趣…）。

**为什么这条通道最值钱**：它是**确定性**的——不依赖模型「想起来」去调工具。
跨会话记忆的第一个提问（「我做什么交易」）在新会话里能被答对，靠的就是它。

⚠️ **幂等**（CONVENTIONS §5 坑 5 的补充）：`source` 有 `startup`（新建会话）与
`resume`（重开旧会话）两种，**重开旧会话会再注入一次** ⇒ 注入内容必须幂等。
本脚本**只读不写** ⇒ 天然幂等（重复注入不产生任何副作用）。

⚠️ **恒 exit 0**（坑 2）：`exit 2` = 整轮熔断（模型不运行、无回答、无 Stop）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402 - 必须在 sys.path 调整之后导入
    EVENT_SESSION_START,
    emit_context,
    format_profile_context,
    log_event,
    resolve_data_dir,
    run_hook,
)


def _load_profile() -> dict:
    """读 L0 画像文件；不存在 / 读不了 ⇒ 空字典（**不抛**）。"""
    path = resolve_data_dir() / "profile.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log_event("profile_read_error", path=str(path), error=str(exc))
        return {}
    if not isinstance(data, dict):
        return {}
    from March7.l0_profile import profile_view

    return profile_view(data, path.parent)


def handle(payload: dict) -> None:
    """注入 L0 画像。**没有画像就什么都不注入**（不注入「暂无」这类噪音）。"""
    session_id = str(payload.get("session_id") or "")
    source = str(payload.get("source") or "")
    profile = _load_profile()
    context = format_profile_context(profile)
    if not context:
        log_event("session_start_no_profile", session_id=session_id, source=source)
        return
    emit_context(EVENT_SESSION_START, context)
    log_event(
        "session_start_injected",
        session_id=session_id,
        source=source,
        chars=len(context),
        fields=sorted(k for k, v in profile.items() if str(v or "").strip()),
    )


if __name__ == "__main__":
    sys.exit(run_hook(handle))
