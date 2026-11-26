"""统一识别原生用户对上一份计划的明确许可。"""

from __future__ import annotations

import json
import re
from hashlib import sha256

_CONFIRM = re.compile(
    r"(?:现在|我|请你|请|先)*(?:确认|同意|批准|执行)(?:执行)?(?:原|刚才的|当前|这个|这份|该|上述)?"
    r"(?:预览(?:草稿)?|草稿|方案|计划|记忆整理)?(?:并执行|吧)?"
    r"|(?:好(?:的)?|可以|行)[，, ]*(?:就)?(?:按|照)(?:刚才的|这个|这份|原|上面的)(?:方案|计划)(?:来|做|执行)(?:吧)?"
    r"|(?:现在|请你|请|先|帮我)*(?:就)?(?:按|照)(?:刚才的|这个|这份|原|上面的)方案(?:执行|做)(?:吧)?"
    r"|(?:可以执行|就这么办)|(?:confirm|approve)(?: (?:this|the) (?:plan|preview))?",
    re.I,
)
_CANCEL = re.compile(
    r"(?:现在|我|请你|请|先)*(?:取消|撤回|不要执行)(?:原|刚才的|当前|这个|这份|该|上述)?"
    r"(?:预览(?:草稿)?|草稿|方案|计划|记忆整理)?(?:吧)?|cancel(?: (?:this|the) (?:plan|preview))?",
    re.I,
)
_REFERENCE = re.compile(r"(?<![\w-])M7-([a-f0-9]{12})(?![a-f0-9])", re.I | re.ASCII)


def confirmation_code(batch_id: str) -> str:
    """生成供用户核对、在同一会话返回的草稿编号。"""
    return "M7-" + batch_id[:12]


def decision(prompt: str) -> tuple[str, str] | None:
    """只接受带具体草稿编号的明确许可，避免把其他对话当成授权。"""
    matches = list(_REFERENCE.finditer(prompt))
    if len(matches) != 1:
        return None
    match = matches[0]
    before = prompt[:match.start()].rstrip(" ：:,，")
    before = before.removesuffix("编号").rstrip(" ：:,，")
    text = (before + " " + prompt[match.end():]).strip().rstrip("。！!.").strip()
    if _CONFIRM.fullmatch(text):
        return "confirm", match[1].lower()
    if _CANCEL.fullmatch(text):
        return "cancel", match[1].lower()
    return None


def hint(nonce: str) -> str:
    """提供模型登记计划的短期上下文。"""
    return (
        "【March7 本轮上下文】当用户请求记忆管理时，给提议工具传入 context="
        f"{nonce}。仅供工具使用。不要展示或复用其他轮次的上下文。"
    )


def source_key(host: str, session_id: str, prompt: str) -> str:
    """用同一轮原话识别管理动作，避免 Stop 把它再当作待补记事实。"""
    return sha256(json.dumps((host, session_id, prompt.strip()), ensure_ascii=False).encode("utf-8")).hexdigest()
