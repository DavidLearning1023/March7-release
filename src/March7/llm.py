"""llm.py — 双 client LLM 层（全 OpenAI 兼容接口，换 base_url 即可）。

角色分工（§3.4，异源原则是评测可信度的硬约束；**主力档 2026-10-12 经 ADR-0009 修订**）：
- main：**MiniMax-M3.1-Flash** —— Judge 候选提取 / 摘要 / 检索 query 改写（~85-90% 调用量；
  2026-11-01 由 M3 切换，见 `ADR-0027`）
- judge：**智谱 GLM**（`glm-5.3-flash` 等按量档，**2026-10-13 经 `ADR-0012` 定案**，
  原计划的阿里百炼 Qwen3.8-Max 因 key 始终未获批而作废）—— 冲突四分类裁决 /
  LLM-as-judge 评测（~10-15%）。异源约束只要求**不同厂商**，不要求通义。
- fallback：智谱 `glm-5.3` —— 第三档备胎，主力挂了手动切换（配置切换，不改代码）。
  ⚠️ 与 judge 同模 ⇒ 切换期间异源暂时失效，**切换必须记 warning 日志**，
  详见 `docs/decisions/0013-fallback语义与配置.md` 后果 D。

⚠️ **换厂商无需改本模块**：三个角色都从 `Config`（环境变量）取 base_url / key / model。
   但换之前注意：`main` 与**评测 harness 的答题侧共用** `MARCH7_LLM_MAIN_*`（见 `docs/TASKS.md` T6 段）。

结构化输出策略：response_format={"type": "json_object"} + 解析失败重试 1 次 +
再失败降级（Judge 失败 = 本轮不写入——宁可不记，不能记错，见 CONVENTIONS §3）。

⚠️ **本模块不 import MCP**（CONVENTIONS §2 红线）：评测 harness 直接调引擎，
   单测跑得快，将来换协议不动引擎。

**思考开销：能力表静态匹配，绝不运行时试错**（`ADR-0012` 后果 E + `ADR-0016` 决策 5）。
下面 `CAPABILITIES` 是**静态表**，不是协商状态机——与 `evals/harness/judge.py` 的
「按顺序试探、记住结果」是两套东西，**引擎侧刻意不用后者**：judge.py 那边一旦 400，
上层会降级成空回答；而本模块的 400 会一路抛到调用方，静默才是这里最坏的失败模式。
未命中表时，评测仍用 `validate_model` 拒绝；宿主运行时按通用档发请求并记录 WARNING
（ADR-0055 决策 5）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel

from March7 import config
from March7.config import Config, ConfigError

logger = logging.getLogger(__name__)


class LLMRole(str, Enum):
    """调用角色：决定走哪个 client（main=管线主力，judge=裁决层）。"""

    MAIN = "main"
    JUDGE = "judge"


class LLMError(RuntimeError):
    """LLM 调用或解析失败。

    **本模块唯一的失败出口**：重试耗尽后一律抛它，绝不返回空串 / 空列表冒充成功。
    调用方（`judge.py`）据此降级——Judge 管线的降级语义 = **本轮不写入**
    （CONVENTIONS §3「LLM 失败 ≠ 崩溃」：宁可不记，不能记错）。
    """


class FallbackNotConfiguredError(LLMError):
    """`switch_to_fallback()` 在 fallback 未配置时抛出（`ADR-0013` 决策 3）。

    **刻意不静默 no-op**：返回后调用方无法区分「已切到备胎」与「压根没切」，
    故障期间引擎会继续用一个挂掉的主力反复失败，而日志里什么异常都没有。
    """


@dataclass(frozen=True)
class _Endpoint:
    """一个角色的连接三元组。key 不出现在 repr/log 里（避免误记进日志文件）。"""

    base_url: str
    api_key: str
    model: str

    def __repr__(self) -> str:  # noqa: D105
        return f"_Endpoint(base_url={self.base_url!r}, model={self.model!r}, api_key=<已隐藏>)"


# 结构化输出与判分同档的 `max_tokens`。**这是上限不是用量**，取大值不额外花钱。
# 为什么要留足余量：强制思考模型的 reasoning 与正文**共享**输出预算。
# - 2026-10-12：智谱 `glm-5.x` 不压低时 `reasoning = 512/512`、content 为空；
# - 2026-11-01：MiniMax-M3 也开始无视 `thinking.disabled`，预算 512 时
#   `reasoning=512/512`、`finish_reason=length`、正文只有 `<think>…`，Judge 连续失败。
# 2048 是「思考 + 2–3 条候选 JSON」的保守上限；它是 cap 不是预留，实际按用量计费。
DEFAULT_MAX_TOKENS = 2048

# 单次调用超时（秒）。引擎运行时是每轮 1-2 次的低频调用，不该被长超时拖住。
DEFAULT_TIMEOUT_SECONDS = 60.0

# `ADR-0036`：钩子场景的**逐进程覆盖**。Stop 钩子受宿主 timeout=120s 约束，
# 而本模块最坏路径是 60s + 退避 2s + 重试 60s = 122s，再加导入就必然被宿主掐断。
# 钩子在碰引擎前设这个环境变量，把单次超时收紧；**默认值 60.0 不变** ⇒
# 评测 harness / MCP server / 单测一律不受影响。用户显式设了值就以用户为准。
ENV_TIMEOUT_SECONDS = "MARCH7_LLM_TIMEOUT_SECONDS"

# 结构化输出：**首次 + 重试 1 次**（= 任务卡口径「解析失败重试 1 次」）。
MAX_ATTEMPTS = 2

# 重试前的退避秒数；只对**可重试**错误退避（429 / 5xx / 网络与超时），
# 400 / 401 这类立刻重试没有意义，不白等。测试可 monkeypatch 为 0。
RETRY_BACKOFF_SECONDS = 2.0

# 能力表（ADR-0016 决策 5，T14 落地）：**按「端点 + 型号」静态匹配，命中即发，未命中即拒**。
#
# 为什么必须是静态表而不是运行时试错（`ADR-0012` 后果 E 铁律 1）：
# 传了后端不认的字段 ⇒ 400 ⇒ 调用失败；调用失败在本模块一路向上抛，
# 于是「试错」会变成「每个新端点先烧一次 400」。更糟的是布尔写法
# `thinking=False` 一律 400，必须是对象 `{"type": "disabled"}`。
#
# **与旧 `_THINKING_KNOBS` 的关键差异**：旧表未命中 ⇒ 静默返回 `{}`（不发私有字段），
# 那是**静默降级**——换了新端点就悄悄不关思考，思考以 `<think>…</think>` 内联进
# content，把结构化输出污染成解析失败（`ADR-0016` 决策 5 的原始动机）。
# ⇒ 评测未命中时 `validate_model()` 抛 ConfigError；宿主按 ADR-0055 使用通用档并留 WARNING。
#
# ⚠️ **匹配规则（ADR-0023，2026-10-31 收紧）**：此前是「型号子串包含」，
# 于是 `glm-5.3-flash` 会被 `glm-5` 那条**静默吸收**——没实测就继承了它的
# `reasoning_effort: low`，与「未知即拒绝」的意图正好相反（T14 验收 P3-3）。
# 现在分两档：
#   1. **精确档**：完整型号名（可带显式 `aliases`）逐字匹配，命中即用；
#   2. **家族兜底档**：`match_mode="family"` 的前缀条目，命中时**记 WARNING**
#      （"未单独实测"），并使用该家族已实测的写法——宁可 400 响亮失败，
#      也不要静默降级成"不发私有字段"（那会让思考内联进 content，见下）。
@dataclass(frozen=True)
class ModelCapability:
    """一个 (端点, 型号) 组合的能力声明。

    `off_extra_body` / `on_extra_body` 各是**实测不会 400 的写法**（`ADR-0012` 后果 E
    的探针结论），不是推测值；两者相同即表示该型号**关不掉思考**（只能降档）。
    """

    model_token: str
    """完整型号名（小写，精确匹配）；`match_mode="family"` 时退化为前缀。"""

    off_extra_body: dict
    """「尽量关思考」时随请求发送的 ``extra_body``。"""

    on_extra_body: dict
    """「保留思考」时随请求发送的 ``extra_body``；与 ``off_extra_body`` 相同 = 关不掉。"""

    can_disable_thinking: bool
    """思考能否真正关闭。``False`` ⇒ 请求「关思考」只能降档（记 warning，不报错）。"""

    note: str
    """实测依据（结论 + 日期）。"""

    match_mode: str = "exact"
    """`"exact"`（逐字匹配，可加别名）或 `"family"`（前缀兜底，命中记 WARNING）。"""

    aliases: tuple[str, ...] = ()
    """显式别名（完整名，小写）。只对 `match_mode="exact"` 有意义。"""

    base_url_hint: str = ""
    """端点约束（小写子串）；空串 = 不限端点。同一型号在不同端点可能有不同私有字段。"""



CAPABILITIES: tuple[ModelCapability, ...] = (
    ModelCapability(
        model_token="minimax-m3.1",
        aliases=("minimax-m3.1-flash", "minimax-m3.1-flash-preview"),
        off_extra_body={"reasoning_effort": "low", "reasoning_split": True},
        on_extra_body={"reasoning_split": True},
        can_disable_thinking=False,
        note="2026-11-01 实测（`MiniMax-M3.1-Flash`）：不接受 thinking.disabled；"
        "`reasoning_effort=low` 可降档，`reasoning_split=True` 可把思考移出 content。"
        "低档 + 分离实测 completion=136（reasoning=0）、正文为可直接解析的 JSON。",
    ),
    ModelCapability(
        model_token="minimax-m3",
        off_extra_body={"reasoning_effort": "low", "reasoning_split": True},
        on_extra_body={"reasoning_split": True},
        can_disable_thinking=False,
        note="2026-11-01 复测：服务端已**不再兑现** thinking.disabled（预算 512 时 "
        "reasoning=512、finish_reason=length、content 只有 <think>）⇒ 旧结论失效，"
        "改用 low + reasoning_split。M3.1-Flash 为当前主力，M3 仅保留兼容。",
    ),
    ModelCapability(
        model_token="glm-5",
        off_extra_body={"reasoning_effort": "low"},
        on_extra_body={"reasoning_effort": "low"},
        can_disable_thinking=False,
        note="2026-10-12 实测：始终思考，传 thinking 直接 400（code 1210），"
        "只能 reasoning_effort 降档（512 → 65~103）⇒ 关不掉思考，"
        "故上面 max_tokens 仍须留余量。",
    ),
    ModelCapability(
        model_token="glm-5.3-flash",
        off_extra_body={"reasoning_effort": "low"},
        on_extra_body={"reasoning_effort": "low"},
        can_disable_thinking=False,
        note="2026-11-03 实测（judge 端点）：`reasoning_effort=low` 可用，"
        "`finish_reason=stop`、completion=137、Judge 候选可解析；"
        "`thinking.disabled` 与 `reasoning_effort=minimal` 均 400（code 1210："
        "「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」）⇒ 关不掉思考。",
    ),
    ModelCapability(
        model_token="glm-5.3",
        off_extra_body={"reasoning_effort": "low"},
        on_extra_body={"reasoning_effort": "low"},
        can_disable_thinking=False,
        note="2026-11-03 实测（judge 端点）：与 `glm-5.3-flash` 同口径——"
        "`low` 可用（completion=155、候选可解析），`thinking.disabled` 与 "
        "`reasoning_effort=minimal` 均 400（code 1210）⇒ 关不掉思考。"
        "它同时是 fallback 档的型号，切换后仍走本条目。",
    ),
    ModelCapability(
        model_token="glm-4",
        off_extra_body={"thinking": {"type": "disabled"}},
        on_extra_body={},
        can_disable_thinking=True,
        note="2026-10-12 实测：thinking.disabled 可关（reasoning_tokens=0）；"
        "智谱无 reasoning_split 字段，「开思考」即保持端点默认。",
    ),
    ModelCapability(
        model_token="deepseek-flash",
        aliases=("deepseek-v4.1-flash",),
        off_extra_body={"thinking": {"type": "disabled"}},
        on_extra_body={},
        can_disable_thinking=True,
        base_url_hint="api.deepseek.com",
        note="2026-11-12 实测（S9 补跑，`ADR-0050` / `ADR-0051`）：**能真正关掉思考**——"
        "`thinking={'type':'disabled'}` 实测 reasoning_tokens=None、completion=19"
        "（同一题默认档 completion=139 / reasoning=118），正文完整可解析。"
        "`reasoning_effort` 三档均不报错，low 实测 completion 41 vs 默认 34，"
        "**降幅很小**（不像 glm/minimax 那样能从 512 压到几十）⇒ 想省钱应关思考而不是降档。"
        "⚠️ `on_extra_body={}` 是刻意的：'开思考' = **不干预**（保持端点默认）。"
        "曾写成 `reasoning_effort=low`，那会让 A/B 的'on'档其实是'降档'档，"
        "两边都不是'默认'，对照就失去意义。"
        "⚠️ 端点只认 `deepseek-flash` / `deepseek-v4-pro`：传 `deepseek-v4.1-flash-1017` "
        "会 400（那是腾讯云侧的型号名，不是 DeepSeek 官方 API 名）。",
    ),
    ModelCapability(
        model_token="deepseek-v4-pro",
        off_extra_body={"thinking": {"type": "disabled"}},
        on_extra_body={},
        can_disable_thinking=True,
        base_url_hint="api.deepseek.com",
        note="2026-11-12 实测（S9 补跑，`ADR-0050`）：与 `deepseek-flash` 同端点同写法，"
        "`thinking.disabled` 可用。`deepseek-flash` 的思考降幅很小（41 vs 34），"
        "所以关思考才是这个家族真正省钱的开关。",
    ),
    # ---- 家族兜底档（ADR-0023）：**必须排在精确档之后**，命中时记 WARNING ----
    # 为什么还要留兜底：厂商常在型号后加日期/档位后缀（`-1020`、`-highspeed`），
    # 全用精确匹配会让"加个后缀就启动失败"。兜底的语义是「按同家族已实测口径处理，
    # 但你得知道它没被单独验证过」——既不静默，也不把用户挡在门外。
    ModelCapability(
        model_token="minimax-m3",
        off_extra_body={"reasoning_effort": "low", "reasoning_split": True},
        on_extra_body={"reasoning_split": True},
        can_disable_thinking=False,
        match_mode="family",
        note="家族兜底：minimax-m3.* 的未登记变体，沿用 2026-11-01 复测的"
        "「低档 + 思考分离」写法。命中即记 WARNING；实测后请拆成精确条目。",
    ),
    ModelCapability(
        model_token="glm-5",
        off_extra_body={"reasoning_effort": "low"},
        on_extra_body={"reasoning_effort": "low"},
        can_disable_thinking=False,
        match_mode="family",
        note="家族兜底：glm-5.* 的未登记变体，沿用 glm-5.x「强制思考、只能降档」的实测口径。"
        "命中即记 WARNING；实测后请拆成精确条目（`glm-5.3` / `glm-5.3-flash` 已各自拆出）。",
    ),
    ModelCapability(
        model_token="glm-4",
        off_extra_body={"thinking": {"type": "disabled"}},
        on_extra_body={},
        can_disable_thinking=True,
        match_mode="family",
        note="家族兜底：glm-4.* 的未登记变体，沿用 glm-4「thinking.disabled 可关」的实测口径。"
        "命中即记 WARNING；实测后请拆成精确条目。",
    ),
)

# 思考策略开关（ADR-0009 后果 C）：`MARCH7_LLM_THINKING` = off（默认）| on。
# 口径与 `evals/harness/baselines.py::answer_extra_body` 的 `MARCH7_LLM_MAIN_THINKING` 一致，
# 便于评测侧与引擎侧用同一套 A/B 开关。
ENV_THINKING = "MARCH7_LLM_THINKING"
THINKING_MODES = ("off", "on")


def _env(name: str) -> str | None:
    """读环境变量——走 `config` 的包内出口，避免本模块散落 `os.environ`（CONVENTIONS §6）。"""
    from March7 import config as _config

    return _config._env(name)


_FAMILY_WARNED: set[str] = set()
"""已经为哪些型号记过「家族兜底」WARNING（同一型号只吵一次，避免每轮刷屏）。"""


def find_capability(model: str, base_url: str = "") -> ModelCapability | None:
    """按 (型号, 端点) 查能力表；查不到返回 None（**不抛**——供内部降级路径用）。

    匹配顺序（`ADR-0023`）：
    1. **精确档**：`model.strip().lower()` 与 `model_token` 或 `aliases` 逐字相等；
    2. **家族兜底档**：`startswith(model_token)`，命中即记一次 WARNING。

    为什么不再用子串包含：`glm-5.3-flash` 会被 `glm-5` **静默吸收**，
    于是「没实测过的型号」看起来像「已实测」——这正是 T14 验收 P3-3 指出的问题。
    """
    key = (model or "").strip().lower()
    if not key:
        return None
    url = (base_url or "").lower()

    for cap in CAPABILITIES:
        if cap.match_mode != "exact":
            continue
        if cap.base_url_hint and cap.base_url_hint not in url:
            continue
        if key == cap.model_token or key in cap.aliases:
            return cap

    for cap in CAPABILITIES:
        if cap.match_mode != "family":
            continue
        if cap.base_url_hint and cap.base_url_hint not in url:
            continue
        if key.startswith(cap.model_token):
            if key not in _FAMILY_WARNED:
                _FAMILY_WARNED.add(key)
                logger.warning(
                    "型号 %r 未单独实测，按家族 %r 的能力口径处理（ADR-0023）：%s。"
                    "实测后请拆成精确条目，否则「未验证」会一直跟着它。",
                    model,
                    cap.model_token,
                    cap.note,
                )
            return cap
    return None


def validate_model(model: str, base_url: str = "") -> ModelCapability:
    """校验型号在能力表里有登记；**未知型号直接抛 ConfigError**（ADR-0016 决策 5）。

    评测在构造客户端时调用，以免请求失败后被上层算成空回答。

    Args:
        model: 型号名。
        base_url: 端点（参与匹配，同一型号在不同端点的私有字段可能不同）。

    Returns:
        命中的能力声明。

    Raises:
        ConfigError: 型号未登记（**家族兜底不算未登记**，但会记 WARNING）。
    """
    cap = find_capability(model, base_url)
    if cap is None:
        registered = sorted({f"{c.model_token}（{c.match_mode}）" for c in CAPABILITIES})
        raise ConfigError(
            f"未知模型 {model!r}（端点 {base_url!r}）：能力表 CAPABILITIES 里没有它的条目。"
            "请先在 llm.py 的 CAPABILITIES 里补一条实测结论（ADR-0016 决策 5：未知即拒绝，"
            f"不静默降级成「不发私有字段」）。已登记：{registered}"
        )
    return cap


def current_thinking_mode() -> str:
    """读当前思考策略（`off` / `on`）；未设置或非法值一律回落到 `off`。

    **非法值不报错**：这是可选的实验开关，不是配置必需项——写错了回落到默认档即可，
    但会记 WARNING（静默忽略会让「以为在跑 A/B 其实没跑」变成可能）。
    """
    raw = (_env(ENV_THINKING) or "").strip().lower()
    if not raw:
        return "off"
    if raw not in THINKING_MODES:
        logger.warning(
            "%s=%r 不是合法值 %s，回落到 'off'（记这条是为了避免「以为在跑 A/B 其实没跑」）。",
            ENV_THINKING,
            raw,
            THINKING_MODES,
        )
        return "off"
    return raw


def extra_body_for(model: str, base_url: str, thinking: str = "off") -> dict:
    """按 (型号, 端点, 思考策略) 返回要附加的 ``extra_body``。

    通用档与能力表声明无私有字段的型号都返回空字典。

    Args:
        model: 型号名。
        base_url: 端点。
        thinking: `off`（尽量关）/ `on`（保留思考）。

    Returns:
        要并入请求的 ``extra_body``；查不到型号时返回空 dict。
    """
    cap = find_capability(model, base_url)
    if cap is None:
        return {}
    if thinking == "on":
        return dict(cap.on_extra_body)
    return dict(cap.off_extra_body)


def _extra_body(base_url: str, model: str) -> dict:
    """按端点 + 型号返回要附加的 ``extra_body``（**思考策略 = off**）。

    保留这个旧入口是为了不破坏既有调用与单测；新代码请用 `extra_body_for()`。
    """
    return extra_body_for(model, base_url, thinking="off")


def _is_retryable(exc: Exception) -> bool:
    """是否值得退避重试：限流(429) / 服务端(5xx) / 网络与超时（无 status_code）。

    与 `evals/harness/baselines.py` 同一判据口径。
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        return True  # APIConnectionError / Timeout 等：重试
    return status == 429 or status >= 500


def _sleep_before_retry() -> None:
    """退避后再重试（测试可 monkeypatch `RETRY_BACKOFF_SECONDS` 为 0）。"""
    if RETRY_BACKOFF_SECONDS > 0:
        time.sleep(RETRY_BACKOFF_SECONDS)


def _timeout_seconds() -> float:
    """本次进程要用的单次 LLM 超时（秒）。

    仅读环境变量、**不缓存**：同一个进程里钩子先设值、后建 client，顺序是确定的；
    不缓存也让测试改 env 后能立刻复算。非法值（非数字 / ≤0）不静默吞——
    记 WARNING 后回落默认 60.0（`CONVENTIONS.md` §3「禁静默吞异常」）。
    """
    raw = config._env(ENV_TIMEOUT_SECONDS)
    if raw is None or not str(raw).strip():
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r 不是合法秒数，回落默认 %.1fs", ENV_TIMEOUT_SECONDS, raw, DEFAULT_TIMEOUT_SECONDS
        )
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning(
            "%s=%r 必须为正数，回落默认 %.1fs", ENV_TIMEOUT_SECONDS, raw, DEFAULT_TIMEOUT_SECONDS
        )
        return DEFAULT_TIMEOUT_SECONDS
    return value


def _default_transport(endpoint: _Endpoint) -> Any:
    """默认 transport：真实 OpenAI 兼容客户端。

    延迟导入保持模块导入轻量（同 `evals/harness` 的做法）。
    ``max_retries=0``：SDK 自带的重试会与本模块的「重试 1 次」叠加，
    让实际尝试次数变成不可预期；关掉它，这里的次数才是可数、可测的。
    """
    from openai import OpenAI  # 依赖表既有；延迟导入保持模块导入轻量

    return OpenAI(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        timeout=_timeout_seconds(),
        max_retries=0,
    )


TransportFactory = Callable[[_Endpoint], Any]
"""把连接三元组变成「能调 ``.chat.completions.create(**kwargs)`` 的对象」。

默认走 `_default_transport`；**测试注入假 transport，不打真 API**（CONVENTIONS §2 数据红线）。
假对象只要实现 ``.chat.completions.create`` 并返回带
``.choices[0].message.content`` 的响应即可。
"""


class LLMClient:
    """双 client 封装：统一的结构化输出 + 重试 + 备胎切换。

    key/base_url/model 全部来自 Config（环境变量），不许硬编码（CONVENTIONS §3）。
    引擎核心不得 import MCP（CONVENTIONS §2 红线）。
    """

    def __init__(
        self, config: Config, transport: TransportFactory | None = None, *, strict_models: bool = True
    ) -> None:
        """按 Config 初始化 OpenAI 兼容 client。

        Args:
            config: 配置快照，由 ``load_config()`` 构建。
            transport: 可选的 client 工厂（测试注入假 transport）；缺省走真实 OpenAI 客户端。
            strict_models: 评测使用默认严格口径。宿主显式选择通用档，并在用到裁决时才连接 JUDGE。

        Raises:
            ConfigError: fallback 半配置，或严格模式下 JUDGE 缺项、型号未登记。
        """
        self._transport = transport or _default_transport
        self._main = _Endpoint(
            base_url=config.llm_main_base_url,
            api_key=config.llm_main_api_key,
            model=config.llm_main_model,
        )
        self._judge = _Endpoint(
            base_url=config.llm_judge_base_url,
            api_key=config.llm_judge_api_key,
            model=config.llm_judge_model,
        )
        self._fallback = _fallback_endpoint(config)
        self._judge_client: Any = None
        if strict_models:
            if not self.judge_configured:
                raise ConfigError("评测需要完整的 MARCH7_LLM_JUDGE_* 三项配置。")
            validate_model(self._main.model, self._main.base_url)
            validate_model(self._judge.model, self._judge.base_url)
            if self._fallback is not None:
                validate_model(self._fallback.model, self._fallback.base_url)
        else:
            self._warn_generic_model(LLMRole.MAIN, self._main)

        self._main_client = self._transport(self._main)
        if strict_models:
            self._judge_client = self._transport(self._judge)

        # 异源检查（`llm.py` 模块文档那句「与 judge 同模 ⇒ 切换必须记 warning」的落地）。
        # 2026-11-12 才补：MiniMax 欠费后把 MAIN 指向智谱、复用了 JUDGE 的 key，
        # main/judge 变成同一个 glm-5.3-flash，**异源原则失效却无声无息**——
        # 而 LLM-as-judge 用同源模型判自己会系统性偏高（`ADR-0012` 选异源的初衷）。
        # 只对**同端点同型号**报：不同厂商即使型号名相同也不影响异源。
        if (
            self.judge_configured
            and self._main.model == self._judge.model
            and self._main.base_url.rstrip("/") == self._judge.base_url.rstrip("/")
        ):
            logger.warning(
                "main 与 judge 同端点同模（%s @ %s）⇒ **异源原则失效**，"
                "此配置下的 LLM-as-judge 评测会带同源偏差；历史异源口径的数字不可与本口径横比。"
                "见 `ADR-0012` 与 `docs/BACKLOG.md` 的 2026-11-12 条。",
                self._main.model,
                self._main.base_url,
            )

        # 最近一次调用的 token 用量（只读观测位，供写入账本记账；不进任何工具出参）。
        # 口径：**最后一次尝试**的 usage（重试场景下第一次的消耗不计入——账本记的是
        # 「这一轮花了多少」的下界，宁可低估，也不要把重试的重复消耗算成两轮）。
        self.last_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        # 最近一次调用的结束原因（只读观测位）。`length` = 输出预算被思考吃光，
        # 这是 2026-11-01 静默空回答的真实现场；写进失败消息，免得只看到“JSON 非法”。
        self.last_finish_reason: str | None = None

        # 思考策略在**构造时**一次判定（与 evals/harness 同口径）：它必须在首次调用前
        # 就是对的（`ADR-0009` 后果 C 的开/关 A/B 就靠这个开关）。
        self._thinking = current_thinking_mode()
        logger.info(
            "LLM 配置已载入：main=%s judge=%s thinking=%s",
            self._main.model,
            self._judge.model if self.judge_configured else "未配置",
            self._thinking,
        )

    @property
    def judge_configured(self) -> bool:
        """三项齐全才启用裁决，避免把半套凭据送到错误端点。"""
        return all(value.strip() for value in (self._judge.base_url, self._judge.api_key, self._judge.model))

    @property
    def model_unverified(self) -> bool:
        """配置中是否有未登记的完整端点，供宿主状态工具展示。"""
        endpoints = (self._main, self._judge if self.judge_configured else None, self._fallback)
        return any(
            endpoint is not None and find_capability(endpoint.model, endpoint.base_url) is None
            for endpoint in endpoints
        )

    def complete(self, role: LLMRole, system: str, user: str) -> str:
        """单轮补全（非 JSON 场景，如摘要 / query 改写）。

        **单次调用，不重试**：非结构化场景的失败没有「重试就变好」的性质，
        摘要 / query 改写失败应当由调用方显式降级，而不是在这里悄悄多烧一次调用。

        Args:
            role: 调用角色（候选提取 / 摘要 / 改写用 MAIN，冲突裁决用 JUDGE）。
            system: system prompt。
            user: user 消息。

        Returns:
            模型返回的文本（已去首尾空白）。

        Raises:
            LLMError: 调用失败；原始异常挂在 ``__cause__`` 上。
        """
        endpoint, client = self._resolve(role)
        try:
            return self._post(client, endpoint, system, user, response_format=None)
        except Exception as exc:
            logger.warning(
                "complete 失败（role=%s model=%s）：%s", role.value, endpoint.model, exc
            )
            raise LLMError(f"LLM 补全失败（role={role.value}, model={endpoint.model}）") from exc

    def complete_json(
        self, role: LLMRole, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel:
        """结构化补全：response_format json + Pydantic 校验 + 失败重试 1 次。

        Args:
            role: 调用角色（Judge 管线用 MAIN，冲突裁决用 JUDGE——异源约束）。
            system: system prompt（如 Judge prompt 的 Cyrene 改写版）。
            user: user 消息（如本轮 transcript）。
            schema: 期望的 Pydantic 模型（如 JudgeOutput）。

        Returns:
            解析并校验通过的模型实例。

        Raises:
            LLMError: 重试 1 次仍失败（API 报错 / JSON 非法 / schema 校验不过）则向上抛，
                由调用方决定降级——Judge 管线的降级 = **本轮不写入**。
                本模块**不**返回空结果冒充成功：那会把「记错」和「没记」混成同一件事。
        """
        endpoint, client = self._resolve(role)
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = self._post(
                    client, endpoint, system, user, response_format={"type": "json_object"}
                )
                return self._parse(raw, schema)
            except Exception as exc:  # noqa: BLE001 — 一律记录后再判重试，不静默吞
                last = exc
                logger.warning(
                    "complete_json 第 %d/%d 次失败（role=%s model=%s）：%s",
                    attempt,
                    MAX_ATTEMPTS,
                    role.value,
                    endpoint.model,
                    exc,
                )
                if attempt < MAX_ATTEMPTS and _is_retryable(exc):
                    _sleep_before_retry()
        raise LLMError(
            f"结构化补全失败：重试 {MAX_ATTEMPTS} 次仍失败"
            f"（role={role.value}, model={endpoint.model}, "
            f"finish_reason={self.last_finish_reason!r}）"
        ) from last

    def switch_to_fallback(self) -> None:
        """主力故障时手动切换到 fallback（`ADR-0013`）——只改运行时配置，不改代码。

        ⚠️ **只替换 ``LLMRole.MAIN``**：``LLMRole.JUDGE`` 恒定走 `MARCH7_LLM_JUDGE_*`，
        不随切换漂移（`ADR-0013` 后果 D 缓解条款 2）。

        Raises:
            FallbackNotConfiguredError: fallback 三项全空（未配置）。**不静默 no-op。**
        """
        if self._fallback is None:
            raise FallbackNotConfiguredError(
                "MARCH7_LLM_FALLBACK_* 全空 = 未配置，无法切换到 fallback"
                "（ADR-0013 决策 3）。刻意不静默 no-op：否则调用方分不清「已切」与「没切」。"
            )
        previous = self._main
        self._warn_generic_model(LLMRole.MAIN, self._fallback)
        fallback_client = self._transport(self._fallback)
        self._main = self._fallback
        self._main_client = fallback_client
        logger.warning(
            "主力 LLM 已切到 fallback：%s(%s) → %s(%s)，Unix 时间戳 %d。"
            "⚠️ 此窗口内裁决层与主力同厂商，异源硬约束暂时失效（ADR-0013 后果 D）；"
            "建议窗口内暂停自动写入——异源失效期间的写入质量不该由代码假装有保障。",
            previous.base_url,
            previous.model,
            self._fallback.base_url,
            self._fallback.model,
            int(time.time()),
        )

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _warn_generic_model(role: LLMRole, endpoint: _Endpoint) -> None:
        """使用未登记的型号时提示通用档，日志只记角色和型号。"""
        if find_capability(endpoint.model, endpoint.base_url) is None:
            logger.warning("LLM %s 型号 %r 未登记，使用通用档（ADR-0055）。", role.value, endpoint.model)

    def _resolve(self, role: LLMRole) -> tuple[_Endpoint, Any]:
        """按角色取当前的连接三元组与 client（切换后 main 会变，judge 不变）。"""
        if role is LLMRole.JUDGE:
            if not self.judge_configured:
                raise LLMError("MARCH7_LLM_JUDGE_* 未配置完整，无法自动裁决。")
            if self._judge_client is None:
                self._warn_generic_model(role, self._judge)
                try:
                    self._judge_client = self._transport(self._judge)
                except Exception as exc:
                    raise LLMError("无法初始化 JUDGE 客户端，请检查裁决端点配置。") from exc
            return self._judge, self._judge_client
        return self._main, self._main_client

    def _post(
        self,
        client: Any,
        endpoint: _Endpoint,
        system: str,
        user: str,
        response_format: dict | None,
    ) -> str:
        """发一次请求并取回文本。**不做任何重试逻辑**（重试在 complete_json 一层）。"""
        kwargs: dict[str, Any] = {
            "model": endpoint.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        extra = extra_body_for(endpoint.model, endpoint.base_url, thinking=self._thinking)
        if extra:  # 能力表命中但本档无私有字段（如 glm-4 的「开思考」档）⇒ 保持端点默认
            kwargs["extra_body"] = extra
        # 每次请求先清空：API 连接失败时不能把上一次的 finish_reason 误当本次结果。
        self.last_finish_reason = None
        response = client.chat.completions.create(**kwargs)
        usage = getattr(response, "usage", None)
        self.last_usage = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        }
        choice = response.choices[0]
        self.last_finish_reason = getattr(choice, "finish_reason", None)
        return (choice.message.content or "").strip()

    @staticmethod
    def _parse(raw: str, schema: type[BaseModel]) -> BaseModel:
        """解析 JSON 并做 Pydantic v2 校验。

        刻意**不做**「剥 markdown 代码围栏」之类的容错：围栏本身就是没拿到
        `response_format` 的信号，重试一次通常就正常了；静默修补只会把
        「模型没按要求输出」这件事藏起来。空串（静默空回答）在这里直接判失败。
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型返回的不是合法 JSON：{raw[:200]!r}") from exc
        return schema.model_validate(payload)


def _fallback_endpoint(config: Config) -> _Endpoint | None:
    """把 fallback 三字段折成一个三元组；全空返回 ``None``（未配置）。

    Raises:
        ConfigError: 只填了一部分。``load_config()`` 已是第一道防线，
            这里再挡一次，保证绕过 ``load_config()`` 直接构造 Config 的调用方
            （例如测试）也拿不到半配置（`ADR-0013` 决策 2）。
    """
    filled = {
        "MARCH7_LLM_FALLBACK_BASE_URL": config.llm_fallback_base_url,
        "MARCH7_LLM_FALLBACK_API_KEY": config.llm_fallback_api_key,
        "MARCH7_LLM_FALLBACK_MODEL": config.llm_fallback_model,
    }
    present = {k: v for k, v in filled.items() if v and v.strip()}
    if not present:
        return None
    if len(present) != len(filled):
        absent = [k for k, v in filled.items() if k not in present]
        raise ConfigError(
            f"MARCH7_LLM_FALLBACK_* 处于半配置状态（ADR-0013 决策 2）："
            f"缺 {'、'.join(absent)}；必须全空或全填。"
        )
    return _Endpoint(
        base_url=config.llm_fallback_base_url,
        api_key=config.llm_fallback_api_key,
        model=config.llm_fallback_model,
    )
