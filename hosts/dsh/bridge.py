"""bridge.py — DSH 侧的**召回桥**：一个进程、一次查询、一段可注入文本。

为什么需要它：DSH 的注入点是 **Cordis 插件（Node/JS）**，而记忆库在 **Python** 那边。
两者之间只有两条路——MCP 工具调用（要模型主动想起来，属"纯工具"档）或
**宿主插件直接问引擎**。本文件是后者的"引擎那一端"：插件用 `spawnSync` 拉起它、
把本轮的提问喂进 stdin、拿回一段已经渲染好的注入文本。

⚠️ **与本仓其它宿主脚本的三条纪律一致**（`hosts/workbuddy/hooks/_common.py`）：
1. **恒 exit 0**：DSH 侧一旦看到非 0 退出码，插件回调里就有异常风险；
   失败必须是"没内容"而不是"报错"；
2. 召回和画像只读。只有主会话原生用户入站可以授权对话处置或明确补记；
3. **数据目录必须显式**：解析不到就**什么都不输出**，绝不回落到 `~/.March7`
   ——那会静默新建一个空生产库（`ADR-0043` §4 的头号风险）。

数据目录解析顺序（**与 WorkBuddy 侧同源优先**）：
`MARCH7_HOME` → `~/.dsh/march7-adapter.json` 的 `data_dir` → 放弃。

⚠️ 刻意**不读** `~/.workbuddy/mcp.json`：那是 WorkBuddy 的配置，
DSH 侧每次启动都去读它会让两个宿主的配置耦合（改 WorkBuddy 会悄悄改 DSH）。
`install.py` 在安装时把两者的值对齐一次，之后各自独立。

用法（插件调用）：
    python hosts/dsh/bridge.py --mode profile          # 会话开始那一段（画像）
    python hosts/dsh/bridge.py --mode recall           # 每轮召回（stdin: {"prompt": "..."}）
    python hosts/dsh/bridge.py --prompt "..."          # 手测 recall

输出：**纯文本**（已渲染的注入块）；无内容 / 任何异常 ⇒ 空输出 + exit 0。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

ADAPTER_CONFIG_NAME = "march7-adapter.json"
"""`install.py` 写出的适配器配置（在 `~/.dsh/` 下，**不在本仓**）。"""

ENV_DATA_DIR = "MARCH7_HOME"
"""与 `March7.config.ENV_DATA_DIR` 同值（此处复制字符串，不 import 引擎：
导入引擎会拉起 pydantic/sqlite-vec，而"解析数据目录"这一步必须先于任何导入）。"""

DEFAULT_TOP_K = 5
MAX_PROMPT_CHARS = 2000
"""超过这个长度的"提问"不是提问（多半是整段转写被误喂进来）⇒ 截断再做检索。"""

MODE_PROFILE = "profile"
MODE_RECALL = "recall"
MODE_CONTROL = "user-control"
MODE_SOURCE = "attest-source"
"""两种注入内容：`profile` = 会话开始就带上的背景（B 档）；`recall` = 按本轮提问召回。"""


def adapter_config_path() -> Path:
    """`~/.dsh/march7-adapter.json` 的路径（可用 `MARCH7_DSH_CONFIG` 覆盖，便于测试）。"""
    override = (os.environ.get("MARCH7_DSH_CONFIG") or "").strip()
    if override:
        return Path(override)
    home = (os.environ.get("DSH_HOME") or "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".dsh") / ADAPTER_CONFIG_NAME


def resolve_data_dir() -> Path | None:
    """解析数据目录；**解析不到返回 None**（调用方据此"什么都不做"）。

    ⚠️ 这里的 `None` 是刻意的：回落到 `~/.March7` 会在用户毫无察觉的情况下
    新建一个空生产库，而界面上一切正常——这种失败比报错难查得多。
    """
    raw = (os.environ.get(ENV_DATA_DIR) or "").strip()
    if raw:
        return Path(raw)
    try:
        with open(adapter_config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = str(data.get("data_dir") or "").strip()
    return Path(value) if value else None


def read_request(argv: list[str]) -> dict:
    """读请求：`--prompt X` 优先，否则从 stdin 读 JSON；坏输入 ⇒ 空字典。"""
    for i, arg in enumerate(argv):
        if arg == "--prompt" and i + 1 < len(argv):
            return {"prompt": argv[i + 1]}
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace").strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def render_injection(
    prompt: str, top_k: int = DEFAULT_TOP_K, project_id: str | None = None, *,
    cwd: str | None = None, session_id: str = "",
) -> str:
    """检索 + 渲染。**只读**，任何异常由调用方兜住。

    渲染**必须**走 `March7.injection.format_injection`（`ADR-0029`：生产与评测共用
    一份渲染，否则两边漂一次，测的就不是线上路径）；这里只负责"取哪几条"。

    Args:
        project_id: 本轮项目假名。`None` ⇒ 只放行 `global`（`ADR-0039` 默认拒绝）。

    ⚠️ **2026-11-16 修正一条此前写错的注释**：这里原来写着"DSH 侧拿不到项目根"。
    实测（`ADR-0063` §2.2）`os.getcwd()` **是能拿到的**——插件用
    `spawnSync` 起本进程时不传 cwd ⇒ 本进程继承宿主的 cwd。缺的不是能力，
    是**没人把它传进来**。现在由插件显式传 `--cwd`（`ADR-0063` §4.3）。
    """
    from March7 import diagnostics
    from March7.injection import format_injection
    from March7.retriever import AUTO_RECALL_MIN_SHARED, select_automatic_recall
    from March7.store import MemoryStore

    data_dir = resolve_data_dir()
    if data_dir is None:
        return ""
    store = MemoryStore(data_dir / "March7.db")
    try:
        store.init_schema()
        if project_id is None:
            project_id = _project_id_from_cwd(cwd, known_project=store.get_project_id)
        if not store.allows_session_project("dsh", session_id, project_id):
            return "【March7】工作目录和该会话的项目不一致，本轮暂停项目记忆。请核对目录或新开会话。"
        selection = select_automatic_recall(
            store, prompt[:MAX_PROMPT_CHARS], min_shared=AUTO_RECALL_MIN_SHARED,
            limit=max(1, int(top_k)), project_id=project_id,
        )
        memories = selection.memories
        categories: dict[str, str] = {}
        for memory in memories:
            try:
                meta = store.get_memory_meta(memory.id)
            except Exception:
                meta = None
            category = (meta or {}).get("category")
            if isinstance(category, str) and category:
                categories[memory.id] = category
        render = format_injection(memories=memories, categories=categories or None)
        diagnostics.trace_recall(
            data_dir, project_id, channel="automatic", host="dsh", session_id=session_id or None,
            match_count=len(render.recall.injected_ids), chars=len(render.recall_text),
            filtered=bool(selection.candidates) and not render.recall_text,
        )
        return render.text
    finally:
        store.close()


def render_profile() -> str:
    """会话开始要带上的那段背景（L0 画像）。**只读**，没有画像就返回空串。

    与 WorkBuddy 的 `SessionStart` 钩子**同一份渲染**（`March7.injection.format_profile_context`），
    数据来源也同一处：`<data_dir>/profile.json`。两个宿主读同一个文件，
    所以"换了工具它就不认识我了"这件事不可能发生在这八个字段上。
    """
    from March7.injection import format_profile_context

    data_dir = resolve_data_dir()
    if data_dir is None:
        return ""
    try:
        with open(data_dir / "profile.json", "r", encoding="utf-8") as fh:
            profile = json.load(fh)
    except FileNotFoundError:
        return ""
    if not isinstance(profile, dict):
        return ""
    from March7.l0_profile import profile_view

    return format_profile_context(profile_view(profile, data_dir))



def _project_id_from_cwd(
    cwd: str | None, *, known_project: Callable[[str], str | None] | None = None,
) -> str | None:
    """把 `--cwd` 解析成项目假名；任何异常/无效值 ⇒ `None`（只召回 global）。

    ⚠️ **恒不抛**：本文件的三条纪律第一条是"恒 exit 0"。
    这里若抛出去，插件侧会看到非 0 退出码 ⇒ 注入失败（`ADR-0043` §1.4）。

    为什么校验目录存在性：宿主可能给一个已被删掉/改名的路径。那时解析出的
    是个"幽灵项目"——用户永远查不到、也删不掉它。⇒ 不存在就当没有项目身份。
    """
    try:
        from March7 import privacy

        path = Path(cwd) if cwd else None
        return privacy.resolve_active_project(
            payload_value=str(path) if path is not None and path.is_absolute() and path.is_dir() else None,
            known_project=known_project,
        ).project_id
    except BaseException:  # noqa: BLE001 - 恒不抛（见 docstring）
        return None


def main(argv: list[str]) -> int:
    """入口：**恒 exit 0**；成功时把注入文本写到 stdout（无内容就是空）。"""
    parser = argparse.ArgumentParser(description="March7 → DSH 宿主桥")
    parser.add_argument(
        "--mode",
        choices=(MODE_PROFILE, MODE_RECALL, MODE_CONTROL, MODE_SOURCE),
        default=MODE_RECALL,
        help="profile=会话背景（B 档）｜recall=按本轮提问召回（实验性）",
    )
    parser.add_argument("--prompt", default=None, help="手测用：直接给检索词")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--dialogue", action="store_true", help="传递原生用户轮次的草稿上下文")
    parser.add_argument(
        "--cwd",
        default=None,
        help="本轮的项目根（由插件传 process.cwd()；不传 ⇒ 只召回 global 记忆）",
    )
    args, _unknown = parser.parse_known_args(argv)

    try:
        request = read_request(argv) if args.mode in (MODE_CONTROL, MODE_SOURCE) else {}
        if args.mode == MODE_SOURCE:
            from March7.control.panel_launcher import (
                match_open_request,
                open_chat_panel,
                panel_reply_context,
            )

            if match_open_request(str(request.get("prompt") or "")):
                from March7.control.conversation import clear_conversation_turn

                data_dir = resolve_data_dir()
                session_id = str(request.get("session_id") or "")
                if data_dir is not None and session_id:
                    clear_conversation_turn(
                        data_dir, "dsh", session_id, event_id=str(request.get("message_id") or "") if args.dialogue else "",
                        prompt=str(request.get("prompt") or ""),
                    )
                panel = open_chat_panel(data_dir)
                result = {"opened": panel.opened, "message": panel_reply_context(panel.chat_message())}
                sys.stdout.write("panel:" + json.dumps(result, ensure_ascii=False))
                return 0

        if args.mode == MODE_PROFILE:
            text = render_profile()
        elif args.mode in (MODE_CONTROL, MODE_SOURCE):
            from contextlib import closing

            from March7.control.conversation import (
                clear_conversation_turn, handle_user_command, operation_reply_context,
            )
            from March7.source_proof import attest_message
            from March7.store import MemoryStore

            prompt = str(request.get("prompt") or "")
            data_dir = resolve_data_dir()
            session_id = str(request.get("session_id") or "")
            message_id = str(request.get("message_id") or "")
            if data_dir is None or not session_id:
                return 0
            if not prompt.strip():
                clear_conversation_turn(data_dir, "dsh", session_id)
                return 0
            os.environ[ENV_DATA_DIR] = str(data_dir)
            with closing(MemoryStore(data_dir / "March7.db")) as store:
                store.init_schema()
                project_id = _project_id_from_cwd(args.cwd, known_project=store.get_project_id)
                if not store.allows_session_project("dsh", session_id, project_id):
                    clear_conversation_turn(
                        data_dir, "dsh", session_id, event_id=message_id if args.dialogue else "", prompt=prompt,
                    )
                    text = operation_reply_context({"status": "rejected", "message": "工作目录和当前会话的项目不一致，请核对目录或新开会话。"})
                    sys.stdout.write(("operation:" if args.mode == MODE_SOURCE else "") + text)
                    return 0
                result = (
                    handle_user_command(
                        store, prompt, project_id=project_id, session_id=session_id,
                        event_id=message_id, host="dsh",
                        offer_dialogue=args.dialogue,
                    )
                    if message_id else None
                )
                if result is not None and result["status"] == "dialogue_context" and args.mode == MODE_SOURCE:
                    proof = attest_message(store, session_id, message_id, project_id, prompt)
                    text = ("ok:" if proof else "dialogue:") + operation_reply_context(result)
                elif result is not None:
                    text = operation_reply_context(result)
                    if args.mode == MODE_SOURCE:
                        text = "operation:" + text
                elif args.mode == MODE_SOURCE:
                    text = "ok" if attest_message(store, session_id, message_id, project_id, prompt) else ""
                else:
                    text = ""
        else:
            request = {"prompt": args.prompt} if args.prompt else read_request(argv)
            prompt = str(request.get("prompt") or "").strip()
            if not prompt:
                return 0
            try:
                top_k = int(request.get("top_k") or args.top_k or DEFAULT_TOP_K)
            except (TypeError, ValueError):
                top_k = DEFAULT_TOP_K
            text = render_injection(prompt, top_k=top_k, cwd=args.cwd, session_id=str(request.get("session_id") or ""))
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001 - 恒 exit 0（见模块 docstring）
        # 留痕但不进 stdout（stdout 是注入通道，混进 traceback 会被注入进 prompt）
        try:
            sys.stderr.write(traceback.format_exc())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
