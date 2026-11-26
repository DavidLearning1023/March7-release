"""_common.py — WorkBuddy 钩子脚本的共享底座（宿主协议外壳 + 可单测的纯逻辑）。

为什么抽成独立文件：三个钩子脚本共用同一套「读 stdin → 算 → 往 stdout 吐协议 JSON
→ **恒 exit 0**」的外壳。抽成一份 ⇒ 协议层只有一个实现，改一处三个脚本同时受益；
而各脚本里的**纯逻辑**（转写树解析、画像格式化、召回格式化）都是无副作用的纯函数，
可以在 MCode 里直接 import + 单测，**不必起宿主**（CONVENTIONS §4「先写测试再实现」）。

⚠️ **六个坑**（`docs/CONVENTIONS.md` §5；实测报告 `evals/reports/spike-hooks-memo.md`）：
1. hooks 仅接受 `type:"command"`；
2. **失败路径必须恒 exit 0**——`exit 2` 不是「警告」是**整轮熔断**：模型不运行、无回答、
   **无 Stop** ⇒ 若记忆写入挂在 Stop 上，UPS 一旦 exit 2 就是**静默丢记忆且无告警**；
3. Windows 下钩子经 Git Bash 执行 ⇒ 命令里的路径要加引号、用 `/`；
4. Python 钩子必须强制 UTF-8 stdout（否则中文注入触发 GBK 编码报错）；
5. 配置在启动时快照 ⇒ 改 `settings.json` 必须**完全退出** WorkBuddy 再重开（只关窗口不算）；
6. 转写是**树状**结构：字段全在**顶层**（`id` / `parentId` / `role` / `content` / `timestamp`），
   `content` 是**块数组**（按 `record["message"]["content"]` 解析会**静默取空**）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

# ⚠️ **本模块刻意不 import March7**（任何一包）。
# 理由：钩子是**宿主拉起的独立进程**——用哪个解释器跑它，宿主说了算（可能是
# WorkBuddy 自带的 Python，那儿没有 March7、也没有 pydantic/sqlite-vec）。
# 若在模块顶层 `from March7 import config`，解释器一换就 **import 期就崩**，
# 退出码非 0（坑 2：失败路径必须恒 exit 0）⇒ 钩子静默失效，界面毫无提示。
# 所以这里只依赖标准库；需要 March7 的地方一律**在 handler 内部懒导入**，
# 导入失败会被 `run_hook` 兜住 → 记日志 → 仍然 exit 0。

logger = logging.getLogger("March7.hooks")

ENV_DATA_DIR = "MARCH7_HOME"
"""数据目录环境变量名（与 `March7.config.ENV_DATA_DIR` 同名，口径一致）。"""

DEFAULT_DATA_DIR_NAME = ".March7"
"""默认数据目录名（与 `March7.config.DEFAULT_DATA_DIR` 一致）。"""

HOST_CONFIG_DIR_NAME = ".workbuddy"
HOST_MCP_CONFIG_NAME = "mcp.json"
SERVER_KEY = "March7"
"""宿主 MCP 配置里 March7 server 的 key（与 `server.SERVER_NAME` 同名）。"""

_HOOK_HOST_DIR = Path(__file__).resolve().parents[1]
_IS_INSTALLED = _HOOK_HOST_DIR.name.lower() == HOST_CONFIG_DIR_NAME

_MODEL_ENV_GROUPS = tuple(
    tuple(f"MARCH7_LLM_{role}_{field}" for field in ("BASE_URL", "API_KEY", "MODEL"))
    for role in ("MAIN", "JUDGE", "FALLBACK")
)

EVENT_SESSION_START = "SessionStart"
EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
EVENT_STOP = "Stop"

PROCESS_START = time.perf_counter()
"""本钩子进程的起点（`perf_counter`，单调时钟）。

为什么需要：账本的 `latency_ms` 只从"开始跑写入管线"算起，**不含**导入 torch/BGE-M3、
解析转写、加载宿主配置 —— 而这三段恰恰是每轮 Stop 的固定开销（T14 实测一轮 27s）。
T15 要回答"每轮成本"，就必须有进程级起点（`ADR-0029`）。
"""

HOOK_START_ENV = "MARCH7_HOOK_START_MS"
"""与 `src/March7/judge.py::ENV_HOOK_START_MS` **同值**。

为什么在钩子层复制一份字符串、而不 import 引擎常量：钩子进程一旦 `import March7.*`
就会拉起 pydantic/torch 的导入链，把"进程启动"本身变成被测量对象，
反而破坏"先记起点、后干活"的语义。
"""


def elapsed_ms() -> int:
    """从钩子进程启动到现在的毫秒数（单调时钟，不受系统时间调整影响）。"""
    return int((time.perf_counter() - PROCESS_START) * 1000)


# ---------------------------------------------------------------------------
# Stop 预算（`ADR-0036`）——宿主 timeout=120s，钩子必须在那之前自己收口
# ---------------------------------------------------------------------------

ENV_STOP_DEADLINE_MS = "MARCH7_STOP_DEADLINE_MS"
"""本轮 Stop 的**内部截止**（毫秒，自进程起点算）；默认 45s。"""

DEFAULT_STOP_DEADLINE_MS = 45_000
"""默认截止。取 45s 的算理：单次 LLM 20s + 退避 2s + 重试 20s = 42s，
再加导入/解析约 2–3s ⇒ 正常最坏路径刚好落在截止内，且远早于宿主 120s。"""

ENV_LLM_TIMEOUT_SECONDS = "MARCH7_LLM_TIMEOUT_SECONDS"
"""与 `src/March7/llm.py::ENV_TIMEOUT_SECONDS` **同值**（此处刻意复制字符串，
不 import 引擎：钩子进程一旦 import March7 就会拉起 torch，把「进程启动」
本身变成被测量的对象）。"""

DEFAULT_STOP_LLM_TIMEOUT_SECONDS = 20.0
"""钩子场景的单次 LLM 超时。引擎默认 60s ⇒ 最坏 60+2+60=122s 必被宿主掐断。"""


def _positive_env(name: str, default: Any, cast: Callable[[str], Any]) -> Any:
    """读一个必须为正数的环境变量；非法值**留痕**后回落默认（不静默吞）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        log_event("stop_budget_invalid", name=name, value=raw, fallback=str(default))
        return default
    if value <= 0:
        log_event("stop_budget_invalid", name=name, value=raw, fallback=str(default))
        return default
    return value


def install_stop_budget() -> int:
    """在**碰引擎之前**装好本轮预算，返回绝对 deadline（毫秒，自进程起点算）。

    - 用户显式设了 `MARCH7_LLM_TIMEOUT_SECONDS`：**合法的**值原样尊重、不覆盖；
      **非法的**值（非数字 / ≤0）留痕后回落到 20s，绝不让它一路回落到引擎默认 60s。
      没设 ⇒ 收紧到 20s（引擎默认 60s 在 Stop 场景会顶穿宿主 120s）。
    - `MARCH7_STOP_DEADLINE_MS` 同理，默认 45s。

    ⚠️ **截止不是抢占式**：检查点在「处理下一条待写消息之前」。单条 LLM 调用
    若跑满超时，会在它返回后才退出；所以真正防止顶穿宿主的是「收紧单次超时」
    这一半，deadline 负责拦住「一轮里攒了很多条待写消息」的累积情形。两者缺一不可。
    """
    raw = (os.environ.get(ENV_LLM_TIMEOUT_SECONDS) or "").strip()
    if not raw:
        os.environ[ENV_LLM_TIMEOUT_SECONDS] = str(DEFAULT_STOP_LLM_TIMEOUT_SECONDS)
    else:
        # 用户显式设了值：**合法值原样尊重**（不覆盖），非法值则留痕后回落到钩子默认。
        # ⚠️ 不能把非法值原样留给引擎——`llm._timeout_seconds()` 会回落到 60s，
        # 于是 60+2+60=122s 又顶穿宿主 120s，且钩子里没有可查的日志
        # （独立验收 P2-④ 的现场）。宁可覆盖一个拼错的值，也不能让治理静默失效。
        try:
            valid = float(raw) > 0
        except (TypeError, ValueError):
            valid = False
        if not valid:
            log_event(
                "stop_budget_invalid",
                name=ENV_LLM_TIMEOUT_SECONDS,
                value=raw,
                fallback=str(DEFAULT_STOP_LLM_TIMEOUT_SECONDS),
            )
            os.environ[ENV_LLM_TIMEOUT_SECONDS] = str(DEFAULT_STOP_LLM_TIMEOUT_SECONDS)
    return _positive_env(ENV_STOP_DEADLINE_MS, DEFAULT_STOP_DEADLINE_MS, int)


def _mark_process_start() -> None:
    """把进程启动时刻写成 Unix 毫秒，供引擎算端到端耗时。"""
    try:
        start_ms = int((time.time() - (time.perf_counter() - PROCESS_START)) * 1000)
        os.environ[HOOK_START_ENV] = str(start_ms)
    except Exception:  # noqa: BLE001 - 记不上起点不影响主流程
        log_event("ERROR", hook_event="mark_process_start", traceback=traceback.format_exc())

# 宿主把「被钩子拦下的轮次」当成一条**普通 assistant 消息**落盘（spike memo §7.3）——
# 解析转写时必须过滤，否则会把「钩子拦截告警」当成模型输出采集成记忆。
BLOCKED_NOTICE_MARKER = "operation blocked by hook"

# 注入面的**全部实现**（围栏 / 祈使句标记 / 长度上限 / 页脚声明）已下沉到引擎层
# `March7.injection`（`ADR-0029`）：生产钩子与评测 harness 必须共用同一份渲染，
# 否则两边漂一次，A/B 测的就不是线上路径。
# 这里只做 re-export，保持 `from _common import format_recall_context` 的旧调用方式可用；
# **不要在本文件重新实现一份**（包括常量与正则）。
# ---------------------------------------------------------------------------
from March7.injection import (  # noqa: E402 - 见上方说明：渲染只有这一份
    INSTRUCTION_LIKE_MARK,
    MAX_INJECTED_CHARS,
    MAX_MEMORY_CONTENT_CHARS,
    MAX_MEMORY_QUOTE_CHARS,
    MAX_PROFILE_FIELD_CHARS,
    NOT_ICE_HEADER,
    clip_text,
    format_profile_context,
    format_recall_context,
    format_recall_context_detailed,
    looks_like_instruction,
)

__all__ = [
    "INSTRUCTION_LIKE_MARK",
    "MAX_INJECTED_CHARS",
    "MAX_MEMORY_CONTENT_CHARS",
    "MAX_MEMORY_QUOTE_CHARS",
    "MAX_PROFILE_FIELD_CHARS",
    "NOT_ICE_HEADER",
    "clip_text",
    "format_profile_context",
    "format_recall_context",
    "format_recall_context_detailed",
    "looks_like_instruction",
]



# ---------------------------------------------------------------------------
# 协议外壳
# ---------------------------------------------------------------------------


def force_utf8_stdout() -> None:
    """坑 4：stdout 强制 UTF-8（Windows 下默认 GBK，中文注入会直接编码报错）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - 极端环境兜底
        pass


def read_payload() -> dict[str, Any]:
    """从 stdin 读钩子事件载荷；读不到 / 不是 JSON 对象 ⇒ 返回空字典。

    ⚠️ **必须剥掉 BOM**（实测踩到：Windows 侧写 stdin 时可能带 UTF-8 BOM，
    甚至**连着两个**）。带 BOM 的 `json.loads` 会抛 `JSONDecodeError` ⇒ 载荷被
    **整体丢弃** ⇒ 钩子「跑了但什么都没做」——界面毫无提示，日志也只有一行
    `ups_skipped_no_prompt`。这种失败比崩掉更难查，所以这里显式剥 BOM + 去空白。

    解码用 `utf-8-sig`（它会吃掉一个 BOM），再用 `lstrip` 兜住**多个** BOM 与前后空白。
    """
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    except Exception:
        return {}
    text = raw.lstrip("\ufeff").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def emit_context(event_name: str, context: str) -> None:
    """把注入内容写到 stdout（宿主协议通道，**只输出这一种 JSON**）。"""
    output = {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def host_mcp_config_path() -> Path:
    """安装副本从同一宿主目录找配置，源码直接调试时沿用用户目录。"""
    host_dir = _HOOK_HOST_DIR if _IS_INSTALLED else Path.home() / HOST_CONFIG_DIR_NAME
    return host_dir / HOST_MCP_CONFIG_NAME


def _mcp_env_from_host_config() -> dict[str, Any]:
    """读取 March7 的 MCP 子进程环境表。旧安装缺失配置时沿用钩子环境。"""
    path = host_mcp_config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.debug("WorkBuddy MCP 配置读取失败（%s），沿用钩子环境。", type(exc).__name__)
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    entry = servers.get(SERVER_KEY)
    if not isinstance(entry, dict):
        return {}
    env = entry.get("env")
    return env if isinstance(env, dict) else {}


def apply_host_model_env() -> None:
    """Stop 读取 MCP 的模型凭据。配置中出现的角色整组覆盖，避免混用厂商和密钥。"""
    env = _mcp_env_from_host_config()
    for keys in _MODEL_ENV_GROUPS:
        if any(key in env for key in keys):
            for key in keys:
                value = env.get(key)
                if isinstance(value, str) and value.strip():
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)


def resolve_bound_data_dir() -> Path | None:
    """安装副本优先认 MCP 配置，源码调试优先认环境变量。"""
    raw = (os.environ.get(ENV_DATA_DIR) or "").strip()
    if raw and not _IS_INSTALLED:
        return Path(raw)
    from_host = str(_mcp_env_from_host_config().get(ENV_DATA_DIR) or "").strip()
    if from_host or raw:
        return Path(from_host or raw)
    return None


def resolve_data_dir() -> Path:
    """记忆钩子原有的目录解析保留引擎默认值，无需读取模型配置。"""
    return resolve_bound_data_dir() or Path.home() / DEFAULT_DATA_DIR_NAME


def apply_data_dir_env() -> Path:
    """把解析出来的数据目录**回写进 `MARCH7_HOME` 环境变量**，返回该目录。

    **为什么必须回写**：引擎侧 `server._build_engine()` → `config.load_config()`
    取数据目录**只看环境变量**（`MARCH7_HOME` 或默认 `~/.March7`）——
    它不知道钩子解析出了什么。钩子若解析到 `.March7-dev` 而环境变量没设，
    引擎就会把记忆写进 **生产库 `~/.March7`** ⇒ 破数据红线（CONVENTIONS §2）。
    回写之后，**钩子的 store 与引擎的 store 指向同一个库**，不存在分叉。

    ⚠️ 只改**当前进程**的环境（子进程继承），不动用户级/系统级环境变量——
    用户级环境变量是全局状态，不该被一个钩子脚本悄悄改掉。
    """
    data_dir = resolve_data_dir()
    os.environ[ENV_DATA_DIR] = str(data_dir)
    return data_dir


def log_event(event_name: str, **fields: Any) -> None:
    """把钩子事件写进 `<data_dir>/logs/hooks-YYYYMMDD.jsonl`。

    **stdout/stderr 是宿主协议通道，不能当日志用**（CONVENTIONS §3）⇒ 日志落文件。
    日志本身失败也必须静默——钩子绝不能因为「写不了日志」而影响宿主。

    ⚠️ 第一个参数叫 `event_name` 而不是 `event`：**它必须与 `**fields` 里的键不撞名**。
    曾经写成 `log_event("ERROR", event=event, ...)` ⇒ `TypeError: got multiple values
    for argument 'event'` ⇒ **错误处理路径自己抛异常** ⇒ 异常逃出 `run_hook`
    ⇒ 脚本以非 0 退出码结束 ⇒ 整轮熔断（坑 2）。这行是那次 bug 的墓碑，别改回去。
    """
    try:
        log_dir = resolve_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {"ts": int(time.time()), "event": event_name, **fields}
        path = log_dir / f"hooks-{time.strftime('%Y%m%d')}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def run_hook(handler: Callable[[dict[str, Any]], None]) -> int:
    """钩子统一入口：**失败路径恒 exit 0**（坑 2）。

    任何异常都记进日志后吞掉——但**必须留痕**：静默失败是最坏的失败模式
    （「钩子没生效」与「钩子生效了但没内容」在界面上长得一模一样）。

    面板入口交接结果后正常返回。处理器误抛退出异常时也保留正常会话。

    此外把**本进程启动时刻**写进环境变量，供引擎算端到端耗时（`ADR-0029`）：
    账本的 `latency_ms` 只覆盖写入管线，不含导入 torch、解析转写这类固定开销；
    要回答"一轮 Stop 到底花了多久"，必须有进程级起点。
    """
    force_utf8_stdout()
    _mark_process_start()
    payload = read_payload()
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    # **只记键名、不记值**（S1 / `ADR-0037`）：载荷里可能有 cwd / transcript_path 这类
    # 绝对路径。记键名是为了回答"宿主到底给了哪些字段"——项目路径的字段名在各宿主、
    # 各版本之间并不统一，靠猜会写出永远不生效的解析（`resolve_project` 的入参就是这么定的）。
    log_event("hook_start", hook_event=event, payload_keys=sorted(payload.keys()))
    try:
        handler(payload)
    except BaseException:  # noqa: BLE001 - 钩子层必须兜住一切
        # ⚠️ 键名用 `hook_event`（不是 `event`）：`log_event` 的第一个位置参数
        # 已经占用了 `event` 这个名字（见 `log_event` 的墓碑注释）。
        log_event("ERROR", hook_event=event, traceback=traceback.format_exc())
        return 0
    return 0


# ---------------------------------------------------------------------------
# 纯逻辑（可单测，不碰 IO）
# ---------------------------------------------------------------------------


def parse_transcript(path: str | None) -> list[dict[str, Any]]:
    """读转写 jsonl → 记录列表。**字段全在顶层**（坑 6）；坏行跳过不中断。"""
    if not path or not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    except Exception:
        return records
    return records


def message_text(record: dict[str, Any]) -> str:
    """取一条 message 记录的正文（`content` 是**块数组**，文本在 `block.text`）。"""
    content = record.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


# 宿主把自己的上下文塞进用户消息里（实测 2026-10-20：一条用户消息的 content
# 共 6882 字符，其中只有末尾 19 个字符是用户真正说的话）：
#   <system-reminder data-role="user-context">…宿主注入的 SOUL/IDENTITY/USER…</system-reminder>
#   <system-reminder …>…memory_and_skills_reminder…</system-reminder>
#   <user_query>用户真正说的那句话</user_query>
# ⇒ **不剥掉就等于把宿主的档案当成用户原话**喂给 Judge（它会从 USER.md 里
# 抽「用户在香港城市大学读硕士」这类事实，来源却标成「用户原话」）——
# 那是**把别人的档案记成用户的事实**，比漏记更坏。
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder\b.*?</system-reminder>", re.DOTALL)
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)


def strip_host_context(text: str) -> str:
    """剥掉宿主注入的 `<system-reminder>` 块（只留用户/助手自己说的话）。"""
    return _SYSTEM_REMINDER_RE.sub("", text or "").strip()


def user_own_text(record: dict[str, Any]) -> str:
    """**用户自己说的那句话**（剥掉宿主注入，只取 `<user_query>` 里的正文）。

    口径顺序：
    1. 有 `<user_query>…</user_query>` ⇒ 取其中内容（宿主把用户真话放在这里）；
    2. 否则剥掉 `<system-reminder>` 块后剩下的正文；
    3. 再否则返回原始文本（不猜、不编）。
    """
    raw = message_text(record)
    matches = _USER_QUERY_RE.findall(raw)
    if matches:
        return "\n".join(m.strip() for m in matches if m.strip()).strip()
    return strip_host_context(raw)


def assistant_own_text(record: dict[str, Any]) -> str:
    """助手自己说的话（同样剥掉宿主注入块）。"""
    return strip_host_context(message_text(record))


def is_blocked_notice(text: str) -> bool:
    """这条消息是不是「被钩子拦下的轮次」的通知？（spike memo §7.3）"""
    return BLOCKED_NOTICE_MARKER in (text or "")


def current_branch(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从**最新叶子**回溯出当前有效分支（返回顺序：根 → 叶）。

    为什么取「最新叶子」：用户编辑/重新生成一条消息 ⇒ 宿主写出**新 id** 的记录，
    旧分支叶子的时间戳更早 ⇒ 最新叶子就是新分支 ⇒ **被编辑掉的旧分支不作数**
    （ADR-0016 决策 3「只认当前有效分支」）。

    ⚠️ **叶子只认 `type == "message"` 的记录**（实测踩到，2026-10-20）：
    转写里除了对话消息，还有 `session-meta` / `ai-title` / `file-history-snapshot`
    这类**兄弟节点**——它们**没有 `parentId`**（不是对话树的一部分），但**时间戳更晚**
    （`ai-title` 是模型给会话自动起的标题，落在用户消息之后）。
    若把「所有带 id 的记录」都当叶子候选 ⇒ 最新叶子会选中 `ai-title`
    ⇒ 沿 `parentId` 回溯只拿到 `ai-title` 自己 ⇒ **分支里一条用户消息都没有**
    ⇒ Stop 钩子静默地什么都不写（日志里只有 `stop_no_user_message`）。
    对话树里的 `reasoning` / `function_call` 不影响这个结论——它们是**祖先**，
    回溯时会自然被带上，而 `branch_user_messages` 只收 `type == "message"`。

    ⚠️ 纯函数：只看传入的 records，不读文件。
    """
    if not records:
        return []
    by_id: dict[str, dict[str, Any]] = {}
    parented: set[str] = set()
    for rec in records:
        rid = rec.get("id")
        if isinstance(rid, str):
            by_id[rid] = rec
            parent = rec.get("parentId")
            if isinstance(parent, str):
                parented.add(parent)
    if not by_id:
        return []
    leaves = [
        rec
        for rid, rec in by_id.items()
        if rid not in parented and rec.get("type") == "message"
    ]
    if not leaves:
        # 转写里一条 message 都没有（异常形态）⇒ 退回「所有叶子」的老口径，
        # 宁可多回溯一点，也不要整轮什么都不处理。
        leaves = [rec for rid, rec in by_id.items() if rid not in parented]
    if not leaves:
        leaves = list(by_id.values())
    leaf = max(leaves, key=lambda rec: rec.get("timestamp") or 0)
    branch: list[dict[str, Any]] = []
    node: dict[str, Any] | None = leaf
    seen: set[str] = set()
    while isinstance(node, dict):
        rid = node.get("id")
        if isinstance(rid, str):
            if rid in seen:  # 防环（转写里理论上不会，但别把宿主卡死）
                break
            seen.add(rid)
        branch.append(node)
        parent = node.get("parentId")
        node = by_id.get(parent) if isinstance(parent, str) else None
    branch.reverse()
    return branch


def branch_user_messages(branch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """当前分支上的**用户消息**（过滤钩子拦截通知与空正文）。

    正文取 `user_own_text`（**只取用户自己说的那句**，不含宿主注入的上下文块）。
    """
    out: list[dict[str, Any]] = []
    for rec in branch:
        if rec.get("type") != "message":
            continue
        if rec.get("role") != "user":
            continue
        text = user_own_text(rec)
        if not text.strip() or is_blocked_notice(text):
            continue
        out.append(rec)
    return out


def branch_assistant_messages(branch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """当前分支上的**助手消息**（同样过滤钩子拦截通知与空正文）。"""
    out: list[dict[str, Any]] = []
    for rec in branch:
        if rec.get("type") != "message":
            continue
        if rec.get("role") != "assistant":
            continue
        text = assistant_own_text(rec)
        if not text.strip() or is_blocked_notice(text):
            continue
        out.append(rec)
    return out


def build_turn_transcript(
    user_record: dict[str, Any],
    branch: list[dict[str, Any]],
) -> str:
    """把**一轮**对话组装成「对话转写」：用户那句话 + 紧随其后的助手回答。

    为什么传「转写」而不是只传用户那一句：Judge 的提示词要的是
    「请从下面这段**对话转写**中提取记忆候选」，单行残句是它的**退化输入**——
    候选的字段归属（画像哪个字段 / 是不是 L2 记忆）会因此摇摆。
    T3 spike 的原始设计也是「读转写 → 调 process_transcript」（见 hooks/README.md）。

    为什么只带**这一轮**（而不是整条分支）：写入水位线 `ingest_marks` 是**按消息**记的，
    整条分支重喂会让 Judge 反复抽取同一批事实 ⇒ 重复入库。
    一轮一块 ⇒ 水位线与内容一一对应。

    助手的回答只作**上下文**；Judge 自己的提示词要求候选必须是「用户明确表达」
    且带用户原话背书（规则②的 attribution 双条件 + 规则④的原话校验），
    所以助手话里的内容不会被当成用户事实记下来。
    """
    lines = ["用户：%s" % user_own_text(user_record).strip()]
    user_id = user_record.get("id")
    seen_user = False
    for rec in branch:
        if rec.get("id") == user_id:
            seen_user = True
            continue
        if not seen_user:
            continue
        if rec.get("type") != "message" or rec.get("role") != "assistant":
            continue
        text = assistant_own_text(rec).strip()
        if not text or is_blocked_notice(text):
            continue
        lines.append("助手：%s" % text)
        break  # 只取紧随其后的那一条回答
    return "\n".join(lines)


CONTEXT_TURNS = 2
"""写入时带给 Judge 的**上文轮数**（`ADR-0049` 决策 1 的上限）。

为什么是 2 而不是"整条分支"：
- 1 轮常常不够——指代链经常跨两跳（「也做出名气了」→「招牌是粿条」→「我开小吃店」）；
- 3 轮以上收益迅速变平，但 token 是**每轮**都付，且「重复抽出上文事实」的风险随长度上升。
2 是能覆盖实测那批首句的最小值（S9 探针：`s-0066#0` 靠前一会话的末 2 轮才抽出候选）。
要改这个数，先改 `ADR-0049`。
"""


def build_context(
    user_record: dict[str, Any],
    branch: list[dict[str, Any]],
    *,
    turns: int = CONTEXT_TURNS,
) -> str | None:
    """本轮**之前**的 `turns` 个用户轮（含各自紧随的助手回答），拼成上文。

    Args:
        user_record: 本轮的用户消息记录。
        branch: 当前有效分支（根 → 叶）。
        turns: 取几轮；0 或负数 ⇒ 返回 `None`。

    Returns:
        上文文本；本轮是分支第一个用户轮（没有上文）时返回 `None`
        ——**不返回空串**：空串会让下游拼出一个空围栏，白吃 token 且让 prompt 变脏。

    ⚠️ **只往回看，不往前看**：分支是根→叶有序的，本函数只取 `user_record` **之前**的
    用户轮。往前看会把「后来的话」当上文，那是信息泄漏（等于把答案提前告诉 Judge）。

    为什么助手回答也要带上：Judge 的提示词是按「对话转写」写的（`用户：…` / `助手：…`），
    只喂用户单句会让它的输入形状与训练/校准时的形状不一致。
    """
    if turns <= 0:
        return None
    user_id = user_record.get("id")
    # 先定位本轮在分支里的位置；找不到（分支与记录不同源）⇒ 不给上文，宁缺勿错
    position = None
    for index, rec in enumerate(branch):
        if rec.get("id") == user_id:
            position = index
            break
    if position is None:
        return None
    prior_users = [
        rec
        for rec in branch[:position]
        if rec.get("type") == "message"
        and rec.get("role") == "user"
        and user_own_text(rec).strip()
        and not is_blocked_notice(user_own_text(rec))
    ]
    if not prior_users:
        return None
    # 每轮用 build_turn_transcript 拼（它自己会在整条 branch 里找紧随的助手回答）
    blocks = [build_turn_transcript(rec, branch) for rec in prior_users[-turns:]]
    return "\n".join(blocks)


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

⚠️ **S9 起本函数在 `_common` 里**（原在 `user_prompt_submit.py`）：
Stop 通道也要解析项目身份（写入侧赋 scope，`ADR-0049` 决策 3），而两个钩子是独立进程、
各自动态 import——放两份就会有「UPS 解析对了、Stop 解析错了」这种只在跨项目时才发作的漂移。
`user_prompt_submit.py` 保留同名 re-export（历史测试与排查脚本按它取）。
"""


def project_value(payload: dict[str, Any]) -> object:
    """从载荷里取项目路径的第一个非空候选（取不到 ⇒ `None`）。"""
    for key in PROJECT_PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def project_identity(
    payload: dict[str, Any], *, known_project: Callable[[str], str | None] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """解析项目身份 → `(project_id, project_root, unresolved_event)`。

    走**全仓唯一**的判据 `privacy.resolve_active_project`（S14 / `ADR-0063` §4.1）：
    `MARCH7_PROJECT` → 载荷 `cwd` → `None`。写侧（本函数）与读侧（`server._call_project_id`）
    共用它，所以"钩子按 A 项目写、工具面按 B 项目读"这种漂移在结构上不可能出现。

    ⚠️ **刻意不传 `process_cwd`**：钩子进程的 cwd 由宿主经 Git Bash 起进程时的目录决定，
    没有实测过它等于用户的工作区（`PWD` 实测就是脏的，见 `ADR-0063` §2.2）。
    载荷缺 `cwd` 时保留缺身份，项目事实由写入管线拒写。

    `root_hash` **只经 `store` 做库内比较**，绝不返回、绝不进日志。
    """
    from March7 import privacy

    resolution = privacy.resolve_active_project(payload_value=project_value(payload), known_project=known_project)
    return resolution.project_id, resolution.root, resolution.event


# 注入渲染（format_profile_context / format_recall_context）已整体迁到
# `March7.injection`（ADR-0029），本文件顶部 re-export。此处不再保留实现。
