"""config.py — 运行配置（数据目录、LLM 型号、环境变量）。

职责（§4 仓库结构）：路径（~/.March7/）、LLM 型号、环境变量读取。
环境变量命名：MARCH7_HOME / MARCH7_LLM_*（全大写，见 docs/NAMING.md §2.1 的例外条款）。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 环境变量名（唯一出口，其他模块不许自己 os.environ）
ENV_DATA_DIR = "MARCH7_HOME"
ENV_LLM_MAIN_BASE_URL = "MARCH7_LLM_MAIN_BASE_URL"
ENV_LLM_MAIN_API_KEY = "MARCH7_LLM_MAIN_API_KEY"
ENV_LLM_MAIN_MODEL = "MARCH7_LLM_MAIN_MODEL"
ENV_LLM_JUDGE_BASE_URL = "MARCH7_LLM_JUDGE_BASE_URL"
ENV_LLM_JUDGE_API_KEY = "MARCH7_LLM_JUDGE_API_KEY"
ENV_LLM_JUDGE_MODEL = "MARCH7_LLM_JUDGE_MODEL"
ENV_LLM_FALLBACK_BASE_URL = "MARCH7_LLM_FALLBACK_BASE_URL"
ENV_LLM_FALLBACK_API_KEY = "MARCH7_LLM_FALLBACK_API_KEY"
ENV_LLM_FALLBACK_MODEL = "MARCH7_LLM_FALLBACK_MODEL"

# 写入治理（ADR-0024 / ADR-0026）——**运行时策略开关，不属于 Config 快照**：
# 它们不影响"能不能跑"，只影响"写多少、怎么合并"，因此不参与 load_config 的必需项校验。
ENV_DEDUP_COSINE = "MARCH7_DEDUP_COSINE"
ENV_DAILY_WRITE_CAP = "MARCH7_DAILY_WRITE_CAP"
ENV_CONFLICT_VEC_MIN_COSINE = "MARCH7_CONFLICT_VEC_MIN_COSINE"
ENV_CONFLICT_VEC_TOP_K = "MARCH7_CONFLICT_VEC_TOP_K"

DEFAULT_DEDUP_COSINE = 0.90
"""写入查重的余弦阈值（≥ 该值视为"同一件事"）。`0` = 关闭查重。

为什么是 0.90（2026-10-31 标定，`scripts/calibrate_dedup.py`）：在"摘要 vs 摘要"口径下，
同义组 7/8 落在 0.930–0.990（1 条 0.886 是跨句式改写），异义组最高 0.884（"喜欢咖啡/喜欢茶"
这类最小对立对）。0.90 是唯一同时做到"零误并 + 只漏 1 条"的区间；再低会把最小对立对并掉。
另有**字面相似度**闸门（≥0.80）配合，两个信号同时满足才合并。
"""

DEFAULT_DAILY_WRITE_CAP = 50
"""每日 L2 写入条数上限（熔断阈值）。`0` = 不限。

为什么是 50：保守 Judge 正常一天写 3–10 条；50 是 **6–15 倍余量**，
它的作用不是"省额度"，而是**在异常（重复刷写、死循环、转写重复投喂）时踩刹车**。
触顶后 L0 画像仍可更新（画像是覆盖写，不会膨胀）。
"""

DEFAULT_CONFLICT_VEC_MIN_COSINE = 0.57
"""冲突**候选筛**的向量余弦下限（`0` = 关闭向量通道）。

为什么不是查重的 0.90：两者要的东西相反。查重是"并掉"，误并要求零；候选筛只是
"送进四分类看一眼"，漏掉才是损失。

为什么是 0.57（2026-11-05 标定，`scripts/calibrate_vector_channel.py`，108 条 `cr`、
881 次写入、30 条可还原真冲突对）：

| θ | top-k | 候选/写 | 真对召回 |
|---|---|---|---|
| 0.55 | 3 | 0.90 | 90.0% |
| 0.57 | **1** | **0.45** | **66.7%** |
| 0.60 | 3 | 0.39 | 63.3% |

预算取「每写 ≤0.5 次裁决调用」⇒ 0.57 + top-1 是预算内召回最高的一档；
端到端 12 条 `cr` 探针从 **0/12 抬到 3/12**（25.0%）。阈值是**本语料**标定的，
换数据集必须重跑脚本。
"""

DEFAULT_CONFLICT_VEC_TOP_K = 1
"""单次写入最多取几个向量近邻当冲突候选。

取 1 而不是 3：0.57 下 top-1 与 top-3（阈值 0.59）召回相同（20/30），但候选数只有
0.45 vs 0.48、且每条候选都要一次裁决调用。top-3 只有在把阈值降到 0.55 时才换来
90% 召回，代价是 0.90 次/写（预算的两倍）——留给需要更高召回的场景用 env 打开。
"""


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    """读整数型策略开关：非法值回落到默认并记 WARNING（不静默忽略）。"""
    raw = (_env(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _warn_bad_env(name, raw, default)
        return default
    if value < minimum:
        _warn_bad_env(name, raw, default)
        return default
    return value


def _float_env(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    """读浮点型策略开关（同上：非法/越界回落默认 + WARNING）。"""
    raw = (_env(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _warn_bad_env(name, raw, default)
        return default
    if not (minimum <= value <= maximum):
        _warn_bad_env(name, raw, default)
        return default
    return value


def _warn_bad_env(name: str, raw: str, default: object) -> None:
    """策略开关写错时的统一告警（用 logging 而非 print，见 CONVENTIONS §3）。"""
    import logging

    logging.getLogger(__name__).warning(
        "%s=%r 不是合法取值，回落到默认 %r（记这条是为了避免「以为开了/关了其实没变」）。",
        name,
        raw,
        default,
    )


def dedup_cosine() -> float:
    """查重余弦阈值；`0` = 关闭查重（ADR-0024）。

    与其它策略开关**不同**：越界/非数字**抛 `ConfigError`**，不再"回落默认 + WARNING"。

    为什么这个开关必须响亮失败：0.90 与 0.60 的差别不是"松紧"，而是"两条记忆要不要
    合成一条"。用户把 `MARCH7_DEDUP_COSINE` 拼成 `85`（漏了小数点）时，静默回落 0.90
    会让他以为改过、实际没生效，下一轮冲突裁决仍按旧口径**合并**记忆——
    合并是不可逆的（原话还在，但"当时为什么合并"再也查不到）。
    而 `0` 是**合法且有意义**的（关闭查重）⇒ 只记 WARNING，让"查重被我关了"这件事
    在日志里留一行，不阻断写入。

    Raises:
        ConfigError: 取值不是数字，或不在 `[0, 1]` 内。
    """
    raw = (_env(ENV_DEDUP_COSINE) or "").strip()
    if not raw:
        return DEFAULT_DEDUP_COSINE
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{ENV_DEDUP_COSINE}={raw!r} 不是数字。查重阈值不静默回落——"
            f"请设成 0（关闭）或 0–1 之间的小数（默认 {DEFAULT_DEDUP_COSINE}）。"
        ) from exc
    if not (0.0 <= value <= 1.0):
        raise ConfigError(
            f"{ENV_DEDUP_COSINE}={raw!r} 超出 [0, 1]。0 = 关闭查重，其余应为余弦阈值"
            f"（默认 {DEFAULT_DEDUP_COSINE}）。看起来像漏了小数点？"
        )
    if value == 0.0:
        logging.getLogger(__name__).warning(
            "%s=0 ⇒ 查重**已关闭**：重复记忆会被照常写入（ADR-0024 的合法档位）。",
            ENV_DEDUP_COSINE,
        )
    return value


def daily_write_cap() -> int:
    """每日 L2 写入熔断阈值；`0` = 不限（ADR-0026）。"""
    return _int_env(ENV_DAILY_WRITE_CAP, DEFAULT_DAILY_WRITE_CAP)


def conflict_vector_min_cosine() -> float:
    """冲突候选筛的向量余弦下限；`0` = 关闭向量通道（T16b / `ADR-0032`）。"""
    return _float_env(ENV_CONFLICT_VEC_MIN_COSINE, DEFAULT_CONFLICT_VEC_MIN_COSINE)


def conflict_vector_top_k() -> int:
    """单次写入最多取几个向量近邻当冲突候选（T16b / `ADR-0032`）。"""
    return _int_env(ENV_CONFLICT_VEC_TOP_K, DEFAULT_CONFLICT_VEC_TOP_K)


def start_of_local_day(now: float | None = None) -> int:
    """本地零点的 Unix 秒（"今天写了几条"的分界，ADR-0026）。

    为什么用**本地**时间：用户说的"一天"是本地概念；用 UTC 会让东八区的统计
    在早上 8 点前后跳变，熔断窗口与人的直觉错位。
    """
    stamp = time.localtime(now if now is not None else time.time())
    midnight = time.struct_time(
        (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 0, 0, 0, stamp.tm_wday, stamp.tm_yday, stamp.tm_isdst)
    )
    return int(time.mktime(midnight))


# 数据目录：生产 ~/.March7/；开发/测试一律用 ~/.March7-dev/（见 docs/CONVENTIONS.md §2 数据红线）
DEFAULT_DATA_DIR = Path.home() / ".March7"
DEV_DATA_DIR = Path.home() / ".March7-dev"

# 数据文件名（见 docs/NAMING.md §2）
DB_FILENAME = "March7.db"
PROFILE_FILENAME = "profile.json"


class Config(BaseModel):
    """运行时配置快照——路径、主力模型、可选裁决模型与备胎。

    模型分工（§3.4，异源是硬约束；**主力档 2026-10-12 经 ADR-0009 修订**）：
    - main：管线主力（**MiniMax-M3.1-Flash**，2026-11-01 起；`ADR-0027`）——
      候选提取 / 摘要 / query 改写，~85-90% 调用量
    - judge：可选裁决层（**智谱 GLM**，2026-10-13 经 ADR-0012 定案）——冲突四分类 /
      claim 抽取；未配置时由用户确认裁决（ADR-0055）。
    - fallback：智谱 `glm-5.3`，主力挂了手动切换（**与 judge 同模 ⇒ 切换期间异源
      暂时失效，见 ADR-0013 后果 D**；三个变量「全空 = 未配置」，半配置报错）

    ⚠️ **换厂商只需改环境变量**：三者都走 OpenAI 兼容端点，`LLMClient` 只认
    base_url / key / model，代码零改动。但有两点必须一并想：
    ① `main` 与**评测 harness 的答题侧共用** `MARCH7_LLM_MAIN_*`（见 `docs/TASKS.md` T6 段）；
    ② 评测侧另有厂商私有参数需适配（思考开关，见 `evals/harness/baselines.answer_extra_body`）。
    """

    data_dir: Path
    db_path: Path
    profile_path: Path
    llm_main_base_url: str
    llm_main_api_key: str
    llm_main_model: str
    llm_judge_base_url: str
    llm_judge_api_key: str
    llm_judge_model: str
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""


class ConfigError(RuntimeError):
    """环境变量不满足要求：必需项缺失，或 fallback 处于半配置状态。

    一律**显式抛出**，绝不返回「半个配置」——半个配置去打真实 API 的后果，
    要么是 401/404 反复重试，要么更糟：猜错了 key 而端点看起来是好的
    （`ADR-0013` 理由 3）。半配置比全空危险得多，所以在这层就拒绝。
    """


# 首装只要求 MAIN 三项。评测在构建 LLM 客户端时继续检查裁决配置与能力表。
_REQUIRED_ENV_NAMES: tuple[str, ...] = (
    ENV_LLM_MAIN_BASE_URL,
    ENV_LLM_MAIN_API_KEY,
    ENV_LLM_MAIN_MODEL,
)

_JUDGE_ENV_NAMES: tuple[str, ...] = (
    ENV_LLM_JUDGE_BASE_URL,
    ENV_LLM_JUDGE_API_KEY,
    ENV_LLM_JUDGE_MODEL,
)

# fallback 是**可选项**：三个变量要么全空（= 未配置，合法），要么全填。
# 为什么「全空」必须是一个明确的合法状态：这三个变量自建仓起就一直没人填，
# 若把空当成错误，则每次启动都要先造一个假配置才能跑起来（见 `ADR-0013` 决策 1）。
_FALLBACK_ENV_NAMES: tuple[str, ...] = (
    ENV_LLM_FALLBACK_BASE_URL,
    ENV_LLM_FALLBACK_API_KEY,
    ENV_LLM_FALLBACK_MODEL,
)


def _read_env(name: str) -> str:
    """读环境变量并去掉首尾空白；未设置时返回空串。

    **空串与未设置不区分**：调用方按「哪些是必需项」决定空串算不算缺失，
    这样 fallback 的「全空 = 未配置」和 main 的「空 = 缺失」共用同一套读取逻辑。
    """
    return (_env(name) or "").strip()


def load_config(dev: bool = False) -> Config:
    """从环境变量构建 Config（T11）。

    MAIN 缺失项一次报全。JUDGE 未配置则关闭自动裁决，半配置时列出缺项。
    fallback 另按 `ADR-0013` 决策 2 校验「全空 = 未配置 / 半配置报错」。

    Args:
        dev: True 时数据目录强制指向 ~/.March7-dev/（开发/测试模式，见 CONVENTIONS §2）。
            此时 `MARCH7_HOME` 被忽略——测试绝不直连真实 ~/.March7/（数据红线）。

    Returns:
        配置快照。judge 与 fallback 未配置时，三字段均为空串。

    Raises:
        ConfigError: MAIN 任一必需项缺失，或 fallback 三项只填了一部分。
    """
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in _REQUIRED_ENV_NAMES:
        raw = _read_env(name)
        if raw:
            values[name] = raw
        else:
            missing.append(name)
    if missing:
        raise ConfigError(
            f"缺少必需的环境变量：{'、'.join(missing)}。"
            "WorkBuddy 请运行 hosts/workbuddy/install.py --set-llm；"
            "其他宿主请在对应的进程环境中补齐。"
        )

    judge = {name: _read_env(name) for name in _JUDGE_ENV_NAMES}
    missing_judge = [name for name, raw in judge.items() if not raw]
    if missing_judge and len(missing_judge) != len(judge):
        logger.warning("JUDGE 配置不完整，缺 %s。冲突裁决需要用户确认。", "、".join(missing_judge))

    fallback: dict[str, str] = {name: _read_env(name) for name in _FALLBACK_ENV_NAMES}
    filled = [name for name, raw in fallback.items() if raw]
    if filled and len(filled) != len(fallback):
        absent = [name for name, raw in fallback.items() if not raw]
        raise ConfigError(
            f"MARCH7_LLM_FALLBACK_* 处于半配置状态（ADR-0013 决策 2）：已填 "
            f"{'、'.join(filled)}，缺 {'、'.join(absent)}。必须全空（未配置）或全填，"
            "不猜测其余项——猜错的后果是用 A 厂商的 key 打 B 厂商的端点。"
        )

    data_dir = resolve_data_dir(dev=dev)

    return Config(
        data_dir=data_dir,
        db_path=data_dir / DB_FILENAME,
        profile_path=data_dir / PROFILE_FILENAME,
        llm_main_base_url=values[ENV_LLM_MAIN_BASE_URL],
        llm_main_api_key=values[ENV_LLM_MAIN_API_KEY],
        llm_main_model=values[ENV_LLM_MAIN_MODEL],
        llm_judge_base_url=judge[ENV_LLM_JUDGE_BASE_URL],
        llm_judge_api_key=judge[ENV_LLM_JUDGE_API_KEY],
        llm_judge_model=judge[ENV_LLM_JUDGE_MODEL],
        llm_fallback_base_url=fallback[ENV_LLM_FALLBACK_BASE_URL],
        llm_fallback_api_key=fallback[ENV_LLM_FALLBACK_API_KEY],
        llm_fallback_model=fallback[ENV_LLM_FALLBACK_MODEL],
    )


def resolve_data_dir(dev: bool = False) -> Path:
    """数据目录：`MARCH7_HOME` → 默认 `~/.March7/`；`dev=True` 强制 `~/.March7-dev/`。

    为什么单独抽出来：导出 CLI / 运维脚本只需要**路径**，不需要 LLM key。
    以前它们为了拿一个目录而调 `load_config()`，于是「没配判分 key」会让一个纯读操作
    直接失败——耦合得毫无道理。这里只做路径解析，不校验任何 LLM 配置。
    """
    if dev:
        return DEV_DATA_DIR
    return Path(_read_env(ENV_DATA_DIR) or DEFAULT_DATA_DIR)


def _env(name: str) -> str | None:
    """读单个环境变量（包内工具，避免各模块散落 os.environ）。"""
    return os.environ.get(name)
