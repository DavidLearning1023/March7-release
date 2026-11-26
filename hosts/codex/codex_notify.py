"""codex_notify.py — Codex 宿主的 Stop 等价通道（S14 / `ADR-0061`）

## 为什么是 `notify` 而不是 hooks

`ADR-0061` §2 记了实测：Codex 0.169.0 **有** hooks 引擎（二进制里有
`SessionStart` / `UserPromptSubmit` / `Stop` / `PreToolUse` / `PostToolUse` /
`PreCompact` / `SubagentStop` 七个事件的完整 JSON schema，字段与 WorkBuddy 同源），
但**配置入口没查到**：试过 4 个位置（`$CODEX_HOME/hooks.json`、`<cwd>/.codex/hooks.json`、
`<cwd>/.codex/hooks`、`config.toml` 内嵌）都不生效，且不给错误——静默跳过。

而 `notify` 通道**实测可用**（`ADR-0061` §3）：每轮结束时宿主调用
`notify = ["<exe>", "<args...>"]`，最后追加一个 JSON 参数：

```json
{"type": "agent-turn-complete", "thread-id": "...", "turn-id": "...",
 "cwd": "...", "client": "codex_exec",
 "input-messages": ["用户这轮说的话"],
 "last-assistant-message": "模型这轮的回话"}
```

**这正是 Judge 需要的原材料**：`input-messages` 就是用户原话（`source_quote` 的来源），
`thread-id` 就是会话 id（`ingest_marks` 的键），`turn-id` 是天然的幂等键。

## 与 WorkBuddy 通道的三处差异（都必须处理，不能假装一样）

| 维度 | WorkBuddy Stop 钩子 | Codex notify |
|---|---|---|
| 原文来源 | `transcript_path` 指向的**树状转写**，要按当前分支过滤 | `input-messages` **已过滤好**（就是这轮用户说的） |
| 幂等键 | `session_id` + 消息 `id` | `thread-id` + `turn-id`（每轮一个） |
| 失败语义 | 恒 exit 0（`exit 2` 会熔断整轮） | **必须 exit 0**：notify 是旁路调用，非零会让宿主打警告并污染界面 |
| 注入通道 | `SessionStart` / `UserPromptSubmit` 可直接注入 | **没有注入**（notify 是单向的）⇒ 画像只能靠 MCP 工具面主动查 |

最后一条是本宿主最实质的短板：**Codex 侧只有"写"，没有"开局注入"**。
所以对外表述必须写成「Codex: 能自动记，但要靠它主动查」（`ADR-0061` §4）。

## 落库走哪条路径

**不复用 `hosts/workbuddy/hooks/stop.py`**（那支要吃树状转写），而是直接调
`March7.server.write_with_context` —— 与 MCP 工具面**同一条代码路径**
（`stop.py` 也是这么做的）。这样硬过滤 / 画像护栏 / 来源落库的行为不可能分叉。

## 用法

写进 `$CODEX_HOME/config.toml`（安装器自动做，见 `hosts/codex/install.py`）：

```toml
notify = ["C:\\path\\to\\python.exe", "C:\\path\\to\\codex_notify.py",
          "--data-dir", "C:/Users/<你>/.March7"]
```

Codex 会把额外参数**追加**在最后。本脚本不依赖位置——它在 `argv` 里
**从后往前找第一个能解析成 JSON 的参数**。

## 为什么数据目录要写在**命令行**里（`ADR-0066`）

`[mcp_servers.March7.env]` 的 `MARCH7_HOME` **只管 MCP server 那一个子进程**。
Codex 起 `notify` 时，继承的是 **Codex 自己的**进程环境——里面没有这个变量
⇒ 本脚本回落到 `~/.March7` ⇒ **自动写入与工具面读写两个不同的库**（实测 P0：
10 条事实写进了默认目录，而工具面一条都查不到）。

⇒ 已登记的回调优先读取同一份配置的 MCP 库目录（ADR-0080）。独立调用沿用
`--data-dir <path>` → `MARCH7_HOME` → `~/.March7`。解析完回写进程环境。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 引擎在 src/ 下；从 hosts/codex/ 出发要上两级再进 src
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

HOST_NAME = "codex"
"""写进 `memories.host` 的宿主标识（`docs/NAMING.md` §2）。"""

DATA_DIR_FLAG = "--data-dir"
"""数据目录参数（`ADR-0066` §5.3）。与 `install.py` 的同名常量必须同值。"""

EVENT_CWD_DRIFT = "codex_cwd_drift"
"""写进写入账本的事件名（`ADR-0067`）。`control why` 按它出一档结论。"""


def data_dir_arg(argv: list[str]) -> str | None:
    """从 argv 里取 `--data-dir <path>` 的值（没有 ⇒ `None`）。

    为什么手写而不引 argparse：本脚本要**容忍任意未知参数**（Codex 会往末尾追加
    JSON，将来也可能加别的），argparse 的默认行为是遇到不认识的参数直接 exit 2
    —— 而 notify 非零会污染宿主界面。宁可自己扫一遍。
    """
    for index, raw in enumerate(argv):
        if raw != DATA_DIR_FLAG:
            continue
        if index + 1 >= len(argv):
            return None
        value = (argv[index + 1] or "").strip()
        return value if value and not value.startswith("-") else None
    return None


def _find_payload(argv: list[str]) -> dict | None:
    """从 argv 尾部找出 Codex 传进来的 JSON（从后往前，取第一个能解析的）。

    为什么不写死 `argv[-1]`：`notify` 数组里我们自己的参数个数由安装器决定，
    写死下标会在"以后多加一个参数"时静默失效——而静默失效正是本项目最怕的形态。
    """
    for raw in reversed(argv):
        text = (raw or "").strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _resolve_data_dir(argv: list[str] | None = None) -> Path:
    """注册回调跟随 MCP 绑定，独立调用沿用 ADR-0066 的数据目录顺序。"""
    from March7.control import codex_config

    flag = data_dir_arg(argv or [])
    configured = None
    if flag:
        settings = codex_config.read_settings(codex_config.default_config_path())
        if codex_config.installed_notify(settings.get("notify"), Path(__file__).resolve(), Path(flag).resolve()):
            configured = codex_config.mcp_data_dir(codex_config.mcp_environment(settings))
    raw = flag or (os.environ.get("MARCH7_HOME") or "").strip()
    data_dir = Path(raw) if raw else Path.home() / ".March7"
    data_dir = configured or data_dir
    os.environ["MARCH7_HOME"] = str(data_dir)
    return data_dir


def build_transcript(user_text: str, assistant_text: str) -> str:
    """把这一轮拼成 Judge 要的「对话转写」。

    为什么要带助手那一句：Judge 的提示词要判断「用户这句话是不是在陈述自己的事实」，
    单看一句用户话会丢掉"他是在回答助手提问"这个语境（实测：同样的句子在
    问答对里更容易被判为事实陈述）。与 WorkBuddy 侧 `build_turn_transcript` 同口径。
    """
    lines = [f"用户：{user_text.strip()}"]
    if assistant_text and assistant_text.strip():
        lines.append(f"助手：{assistant_text.strip()}")
    return "\n".join(lines)


def pick_new_message(messages: object, *, already_seen: list[str]) -> str:
    """从 `input-messages` 里挑出**这一轮新增的那一句**。

    ## 为什么需要这个函数（实测真 bug，2026-11-16）

    `input-messages` **不是"这一轮"**，而是**累积的**。实测两次 notify：

    ```
    第 1 次  ['第一句：我叫阿树']
    第 2 次  ['我还养了两只猫。用两个字回答：好', '第二句：我住在杭州']   ← 含上一轮的
    ```

    直接用 `"\\n".join(...)` 会让上一轮的话被**重新判分**。幂等键是
    `(thread, turn)` 而 `resume` 会换新 thread ⇒ 幂等挡不住 ⇒ **同一条事实重复入库**
    （实测复现：`用户自称「阿树」` 出现两遍）。

    ## 怎么挑

    取**最后一条**：Codex 把本轮的 prompt 追加在末尾（上面两次实测都符合）。
    若最后一条为空（只有空白），返回空串——**不向前回退**去猜，那是编造。

    Args:
        messages: `input-messages` 原值（类型未验，可能是 list 或其它）。
        already_seen: 保留形参以便将来做「按内容去重」；当前实现不依赖它。

    Returns:
        这一轮的用户原话（已 strip）；拿不到就返回 `""`。
    """
    if not isinstance(messages, list) or not messages:
        return ""
    for raw in reversed(messages):
        text = str(raw or "").strip()
        if text:
            return text
    return ""


def main(argv: list[str] | None = None) -> int:
    """入口。**无论发生什么都必须返回 0**（notify 是旁路，非零只会污染宿主界面）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    payload = _find_payload(args)
    if payload is None:
        print("[March7] notify: 未在参数中找到 JSON，跳过", file=sys.stderr)
        return 0

    if str(payload.get("type") or "") != "agent-turn-complete":
        # 未来可能新增别的 type；不认识就安静跳过，不猜
        return 0

    thread_id = str(payload.get("thread-id") or "")
    turn_id = str(payload.get("turn-id") or "")
    # ⚠️ 只取最后一条（见 `pick_new_message` 的实测记录）。
    user_text = pick_new_message(payload.get("input-messages"), already_seen=[])

    if not thread_id or not user_text.strip():
        print("[March7] notify: 缺 thread-id 或用户原话，跳过", file=sys.stderr)
        return 0

    # 数据目录**先解析再碰引擎**（`ADR-0066` §5.3）：`--data-dir` 在命令行里，
    # `MARCH7_HOME` 通常不在这个子进程的环境里。解析完顺手回写环境变量。
    try:
        from March7.control.conversation import match_memory_request, parse_user_command
        from March7.control.dialogue import source_key
        from March7.control.panel_launcher import match_open_request

        if match_open_request(user_text):
            return 0
        request = match_memory_request(user_text)
        if parse_user_command(user_text) is not None or (request is not None and request.action != "remember"):
            return 0

        data_dir = _resolve_data_dir(args)

        from March7.server import write_with_context
        from March7.store import MemoryStore

        transcript = build_transcript(user_text, str(payload.get("last-assistant-message") or ""))

        # 幂等：`turn-id` 是"这一轮"的唯一键。重放同一轮（宿主重试）不会二次入库。
        # ⚠️ 但 `resume` 会换新 `thread-id`（实测）⇒ 幂等**挡不住累积重判**，
        # 那件事由 `pick_new_message` 从源头解决（只喂新的一句）。
        store = MemoryStore(data_dir / "March7.db")
        store.init_schema()
        try:
            session_key = thread_id
            message_key = turn_id or thread_id
            if store.is_ingested(session_key, message_key):
                return 0
            if store.dialogue_handled(source_key(HOST_NAME, thread_id, user_text)):
                return 0
            # 漂移落账本（`ADR-0066` §6.2 / `ADR-0067`）：只写 stderr 等于没写——
            # notify 的 stderr 宿主不展示，用户永远看不到。落进写入账本后
            # `control why` 才能回答"这条为什么记到别的项目里了"。
            # 放在幂等检查**之后**：重放同一轮不该多出一条事件。
            if cwd_drift(str(payload.get("cwd") or ""), os.getcwd()):
                _record_drift(data_dir, thread_id=thread_id, turn_id=message_key)
            project_id = _project_id(str(payload.get("cwd") or ""), host_cwd=os.getcwd(), store=store)
            if not store.allows_session_project(HOST_NAME, session_key, project_id):
                _record_drift(data_dir, thread_id=thread_id, turn_id=message_key, blocked=True)
                return 0
            write_with_context(
                transcript,
                conversation_id=thread_id,
                host=HOST_NAME,
                source_session_id=thread_id,
                source_message_id=message_key,
                source_revision=None,
                # `host_cwd` 有两个用途：① 载荷没给 cwd 时兜底（`ADR-0063` §4.1 第 ③ 档）；
                # ② 与载荷 cwd 比对，提示可能的 resume 漂移（§3.1）。
                project_id=project_id,
                context=None,  # notify 只给这一轮，没有上文；不编造
            )
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 —— 旁路通道：任何异常都不许冒泡到宿主
        # 只写 stderr：notify 的 stdout 会被宿主当输出展示
        print(
            f"[March7] notify 处理失败（不影响本轮对话）：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    return 0


def _project_id(cwd: str, *, host_cwd: str | None = None, store: MemoryStore | None = None) -> str | None:
    """按 cwd 解析项目身份（`ADR-0049`）。解析不出来 ⇒ `None` ⇒ 写 `global`（不猜）。

    ## `resume` 的 cwd 漂移（实测真 bug，`ADR-0063` §3.1）

    同一会话连续两次 notify：

    ```
    第 1 次  codex exec -C C:\\tmp\\projA -- "…"    → cwd = C:\\tmp\\projA   ✅
    第 2 次  codex exec resume --last -- "…"        → cwd = <敲命令的那个目录>  ❌
    ```

    `resume` 没有 `-C`（实测 `unexpected argument '-C' found`）⇒ 它复用**进程 cwd**，
    而进程 cwd 取决于用户在哪个目录敲的命令，不是上次的项目目录。

    **后果**：在 A 项目 resume，记忆被记到 B 项目 ⇒ 下次在 A 项目里查不到，且完全静默。

    ## 怎么处理

    **不猜、不修正**：`cwd` 就是宿主给的，照它记。但把"疑似漂移"写进 stderr
    与账本（`codex_cwd_drift`），让 `control why` 能回答"这条为什么记到别的项目了"。

    为什么不试图"纠正"它（例如沿用上一次的 cwd）：那需要跨轮状态，
    而 notify 是**一次一个进程**（无状态）。靠读库猜"上次是哪个项目"会在
    用户真的换了项目时给出错误答案——比漂移更糟。
    """
    if not cwd:
        # 载荷**没给** cwd ⇒ 走第 ③ 档（进程 cwd）。工具面同理（`ADR-0063` §4.1）。
        return _resolve_via_engine(payload_value=None, process_cwd=host_cwd, store=store)

    resolved = _resolve_cwd(cwd)
    if resolved is None:
        # 给了但不可用（目录已删/改名）⇒ **不回落**到进程 cwd：
        # 那会把"宿主指向 A、进程恰好在 B"变成一次静默误记（本模块最怕的形态）。
        return _resolve_via_engine(payload_value=None, process_cwd=None, store=store)

    # 漂移检测：宿主给的 cwd 与本进程 cwd 不同 ⇒ 可能是一次 resume。
    # 只提示、不干预（理由见 docstring）。
    if host_cwd and cwd_drift(cwd, host_cwd):
        print(
            "[March7] 提示：宿主给的 cwd 与本进程 cwd 不同。"
            "若这是一次 `codex exec resume`，记忆可能记到了别的项目（ADR-0063 §3.1）。",
            file=sys.stderr,
        )

    return _resolve_via_engine(payload_value=resolved, process_cwd=None, store=store)


def cwd_drift(payload_cwd: str, host_cwd: str | None) -> bool:
    """宿主给的 cwd 与本进程 cwd 是否**不一致**（疑似 `codex exec resume`）。

    ⚠️ 只看"两个都有且不同"，**不判谁对**：`resume` 没有 `-C`（实测
    `unexpected argument '-C' found`），它复用进程 cwd，而进程 cwd 取决于用户在
    哪个目录敲的命令 —— 所以两个值都没有"天然正确"的一方。
    本函数只负责"这次该不该留个痕"，不负责修正（修正需要跨轮状态，
    而 notify 是一次一个进程，见 `_project_id` 的 docstring）。

    任何异常 ⇒ `False`（notify 恒 exit 0 的纪律；宁可漏报也别崩）。
    """
    if not payload_cwd or not host_cwd:
        return False
    try:
        return _norm(host_cwd) != _norm(payload_cwd)
    except Exception:  # noqa: BLE001
        return False


def _resolve_via_engine(
    *, payload_value: str | None, process_cwd: str | None, store: MemoryStore | None = None,
) -> str | None:
    """把两个候选交给**全仓唯一**的判据（`privacy.resolve_active_project`）。

    为什么不在本文件里写优先级：写侧（这里）与读侧（`server._call_project_id`）、
    以及 WorkBuddy 钩子必须用**同一份**判据，否则会出现"按 A 写、按 B 读"
    （`ADR-0063` §4.2）。任何异常 ⇒ `None`（notify 恒 exit 0 的纪律）。
    """
    try:
        from March7 import privacy

        return privacy.resolve_active_project(
            payload_value=payload_value, process_cwd=process_cwd,
            known_project=store.get_project_id if store is not None else None,
        ).project_id
    except Exception:  # noqa: BLE001
        return None


def _resolve_cwd(cwd: str) -> str | None:
    """校验 cwd 是个**真实存在的目录**再拿去做项目解析。

    为什么要查存在性：`resume` 可能给出一个已被删掉/改名的目录，那时
    `resolve_project` 会解析出一个"幽灵项目"——用户永远查不到它，也删不掉它。
    ⇒ 不存在就当没有项目身份（写 `global`，宁漏不串）。
    """
    try:
        path = Path(cwd)
        return str(path) if path.is_dir() else None
    except OSError:
        return None


def _norm(path: str) -> str:
    """路径归一化（Windows 大小写不敏感）。"""
    return os.path.normcase(os.path.normpath(path))


def _record_drift(data_dir: Path, *, thread_id: str, turn_id: str, blocked: bool = False) -> None:
    """把一次疑似漂移写进**写入账本**（事件行，不是管线行）。

    ## 为什么事件行进同一个文件

    `control why` 只读 `logs/writes-YYYYMMDD.jsonl` 这一个文件（`ADR-0058` 决策 1：
    用户只需要一条命令回答"为什么没记住"）。另开一个文件 = 用户永远不知道要去看它。
    "一次管线一行"的口径由 `ledger.aggregate` 保证：它**跳过**带 `event` 的行
    （`ADR-0067`），所以 `doctor` 的 `runs` 不会被漂移事件污染。

    ## 为什么不写路径

    这一行会被用户贴进 issue。路径里有用户名、项目名（往往就是公司名/课题名），
    而"漂不漂"这件事不写路径也说得清。要定位就自己 `pwd` 一下——账本不替他记。
    """
    try:
        from March7 import ledger

        ledger.append(
            {
                "event": EVENT_CWD_DRIFT,
                "project_blocked": blocked,
                "host": HOST_NAME,
                "conversation_id": thread_id,
                "turn_id": turn_id,
                "written": 0,
                "rejected": 0,
                "failed": False,
            },
            data_dir,
        )
    except Exception as exc:  # noqa: BLE001 —— 记账失败绝不影响写入主流程
        print(f"[March7] 漂移事件记账失败（不影响本轮）：{type(exc).__name__}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
