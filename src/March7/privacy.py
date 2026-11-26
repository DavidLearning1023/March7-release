"""privacy.py — 路径隐私层（内部代号：磨砂）

职责：把「绝对路径」变成**可对账、不可还原到机器**的视图，供注入、日志、导出共用。

为什么单独成模块（`ADR-0037`，S1）：
1. **模型看到的是脱敏后的原话**，用户在自己的导出里能看到完整原文（D6 口径）。
   这条口径要成立，就必须有**唯一一份**路径识别与替换实现——两份实现漂一次，
   就会出现「注入里脱了、导出里没脱」或者反过来的事故，而它们都长得像正常输出。
2. 路径里含着用户名、项目名、盘符、旧的文件夹命名——这些是**跨工具接力**时
   最先泄露的东西（把 A 工具的会话贴到 B 工具，路径跟着原文一起走）。

三条硬约束（写进代码，不靠记性）：
- **只做词法归一化**：`casefold → 反斜杠转 / → 折叠 → 去尾 → 去尾点/空格 → NFC`。
  绝不做 `realpath` / `os.path.abspath` / 8.3 短名展开——那些要**碰文件系统**：
  钩子进程每轮都跑，一次 `realpath` 就是一次磁盘 IO，而且会把「用户没写全的路径」
  悄悄补成真实路径（等于把猜测当事实记下来）。
- **`root_hash` 只在库里比较**：它进 `projects` 表当索引，不出现在日志 / 导出 / 注入。
  对外（含日志）只出现 `project_id = p-<6 hex>`，那是**假名**，不是路径的散列指纹。
- **本模块只用标准库**：钩子进程每轮 import 它，多拖一个 pydantic 就是每轮几百毫秒。

⚠️ 本模块的 `PATH_RE` 是**全仓唯一**的路径正则。别处再写一份 ⇒
`tests/test_privacy.py::test_no_second_path_regex` 会红。
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 三档未解析事件（观测口径，落进钩子日志）
# ---------------------------------------------------------------------------

EVENT_PROJECT_UNRESOLVED = "project_unresolved"
EVENT_PROJECT_SESSION_CHANGED = "project_session_changed"
"""拿不到项目身份：视图里有路径，但本轮没有可用的项目根 ⇒ 只能整段隐去。"""

EVENT_PATH_OMITTED = "path_omitted"
"""确有路径被隐去（这是**正常结果**，不是错误；记它是为了让「脱了多少」可对账）。"""

EVENT_PROJECT_PATH_UNPARSEABLE = "project_path_unparseable"
"""载荷里给了项目路径，但**词法上就不成立**（非字符串 / 空 / 相对路径）。"""

EVENT_PROJECT_EPHEMERAL = "project_ephemeral"
"""载荷给的路径**是宿主的会话临时目录**（末段是会话时间戳）⇒ 本轮没有项目身份。

为什么它不是 `project_unresolved`：两者的修法相反。
`project_unresolved` = 宿主没给/没给全，要改适配层；
`project_ephemeral` = 宿主给了一个**不该当项目**的东西，要改的是**我们的判据**。
混成一个事件，排查时会一直去查"为什么解析不出 cwd"（而 cwd 明明有）。

实测来源（S1 真机 2026-11-10）：无工作区会话的 `cwd` = `C:/Users/<user>/WorkBuddy/<ts>`，
`<ts>` 形如 `2026-11-10-12-59-49` ⇒ 每会话一个 `project_id` ⇒ 记忆碎片化。
"""

UNRESOLVED_EVENTS = (
    EVENT_PROJECT_UNRESOLVED,
    EVENT_PATH_OMITTED,
    EVENT_PROJECT_PATH_UNPARSEABLE,
    EVENT_PROJECT_EPHEMERAL,
)
"""四档事件的**全集**（互斥地描述同一次解析的不同失败点，不是四个计数器）。"""

_EPHEMERAL_TAIL_RE = re.compile(r"(?:^|/)\d{4}-?\d{2}-?\d{2}[-_ ]\d{2}[-_:]?\d{2}[-_:]?\d{2}$")
"""会话临时目录的**词法指纹**：末段是「日期 + 时分秒」。

⚠️ **必须带时分秒，只带日期不算**（`test_date_only_directory_is_not_ephemeral` 钉住）：
按天归档的目录（`D:/work/march7/2026-11-10`）是人会给项目起的名字，误判它等于
让用户在某一天的项目记忆整天"没有项目归属"。宿主给的是**到秒**的会话标识。

两种格式都认：`2026-11-10-12-59-49`（宿主实测）与 `20261110-125949`（紧凑写法）。"""


def is_ephemeral_root(value: str | None) -> bool:
    """这个路径是不是**宿主的会话临时目录**（不碰文件系统）。

    判据 = 归一化后末段匹配「日期 + 时分秒」。为什么不看父目录名：宿主换版本可能改
    临时区位置（实测已是 `WorkBuddy/<ts>`，将来可能是 `<tmp>/wb/<ts>`），
    只认末段时间戳更稳；而「用时间戳给真实项目命名」在实践中几乎不存在。

    **代价（已知且接受）**：真有用户把项目放在叫 `2026-11-10-12-59-49` 的目录里，
    会被判成临时 ⇒ 回落到 `global`。方向是**保守**的：少记项目归属，而不是记错归属。
    """
    normalized = normalize_path(value) if isinstance(value, str) else ""
    if not normalized:
        return False
    if normalized in ("/",) or re.fullmatch(r"[a-z]:/?", normalized):
        return False  # 盘符根 / POSIX 根是"没有项目"，不是"临时项目"
    return bool(_EPHEMERAL_TAIL_RE.search(normalized.rstrip("/")))

PLACEHOLDER_PROJECT = "<项目路径>"
"""项目内路径的占位符：保留「这是本项目里的东西」这层信息，不保留是哪个目录。"""

PLACEHOLDER_EXTERNAL = "<路径>"
"""项目外路径的占位符：连「属于哪个项目」都不给。"""

PROJECT_ID_PREFIX = "p-"

NORM_VERSION = 1
"""路径归一化规则的**版本位**（`ADR-0039` 决策 2 / 蓝图 T20d）。

它**不是迁移方案，是检测位**：两个宿主若用不同的归一化规则（大小写、尾分隔符、
软链接、8.3 短名），同一个目录会算出两个 `root_hash`，表现是"换宿主就找不到项目"。
写死规则 + 记下版本号之后，`projects.norm_version` 与当前代码不一致时就能记一次
`norm_mismatch`——**只报警、不自动合并**。

⚠️ **什么时候必须 +1**：改动 `normalize_path` 的**任一步**（顺序、大小写折叠、
尾点处理、NFC 归一化）。改了却不 +1，历史 `root_hash` 会与你新算出的不一致，
而 `norm_mismatch` 会**静默不报**——那比报错更坏。

为什么从 1 起而不是 0：0 留给"本机制落地前写下的行"（v4/v5 时代的 `projects` 记录
没有这个字段，迁移时回填 0），于是"老行 vs 新代码"天然构成一次 0 → 1 的不一致，
第一次升级就会被看见，而不是被当成"一切正常"。
"""

# ---------------------------------------------------------------------------
# 路径识别（全仓唯一一份）
# ---------------------------------------------------------------------------

# 三类绝对路径。**刻意不匹配相对路径**（`src/March7/x`、`a/b`、`1/2`）：
# 相对路径不泄露机器信息，误伤它会把正常正文改成 `<路径>`（负例 5b 钉的就是这件事）。
_WIN_ABS = r"(?<![\w:])[A-Za-z]:[\\/][^\s\"'<>|，。；：！？（）\[\]{}]*"
_UNC_ABS = r"(?<![\w:])\\\\[^\s\"'<>|，。；：！？（）\[\]{}]+"
_POSIX_ABS = (
    r"(?<![\w:])/(?:Users|home|root|mnt|Volumes)/"
    r"[^\s\"'<>|，。；：！？（）\[\]{}]*"
)

PATH_RE = re.compile("|".join((_WIN_ABS, _UNC_ABS, _POSIX_ABS)))
"""绝对路径识别（唯一实现）。`PATH_RE.pattern` 出现在第二个文件 ⇒ 测试红。"""

_SPLIT_RE = re.compile(f"({PATH_RE.pattern})")
"""带捕获组的同一份模式——只用于把文本切成「路径 / 非路径」交替片段。

为什么要单独一个对象：`re.split` 在**没有捕获组**时会把分隔符丢掉，于是调用方
拿到的是"只剩正文"，根本分不清哪段原来是路径（实测踩过：把非路径片段误判成
"路径未被脱敏"）。模式本身仍来自 `PATH_RE.pattern`，不存在第二份路径语法。
"""


def split_path_spans(text: str) -> list[tuple[str, bool]]:
    """把文本切成 `[(片段, 是否路径)]`（按出现顺序，空片段丢弃）。

    回归脚本靠它证明「新旧渲染的差异**只**落在路径片段上」。
    """
    parts = _SPLIT_RE.split(text or "")
    return [(part, index % 2 == 1) for index, part in enumerate(parts) if part]

_TRAILING_PUNCT = ".,;:!?"


def looks_absolute(normalized: str) -> bool:
    """归一化后的字符串是否**词法上**是绝对路径（不碰文件系统）。"""
    value = normalized or ""
    if not value:
        return False
    if re.match(r"^[a-z]:/", value):  # casefold 之后盘符是小写
        return True
    return value.startswith("//") or value.startswith("/")


def normalize_path(value: str) -> str:
    """词法归一化（**顺序即定义**，改动等于改口径）：

    1. `casefold`：Windows 路径大小写不敏感；不折叠就会出现 `D:/work/march7` 与
       `d:/march7` 两个项目身份。
    2. 反斜杠转 `/`：Windows 与 POSIX 用同一套分隔符，后面才好折叠与比较。
    3. 折叠连续的 `/`（`D://a///b` → `d:/a/b`；UNC 的前导 `//` 单独保留）。
    4. 去尾 `/`（`d:/a/` → `d:/a`；根 `/` 除外）。
    5. 去尾点/空格（Windows 语义：`a. ` 与 `a` 是同一个目录）——**循环直到稳定**，
       因为 `a. .` 去掉一轮后还会剩下 `a.`。
    6. NFC：同一串 Unicode 可能有多种码点表示（macOS 的 NFD 文件名），不统一
       就会把「同一个目录」算成两个项目。

    Returns:
        归一化后的字符串；输入不是字符串时返回空串（由调用方记为 unparseable）。
    """
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value).casefold()
    text = text.replace("\\", "/")
    if text.startswith("//"):  # UNC：保留前导双斜杠，只折叠其后的
        text = "//" + re.sub(r"/+", "/", text[2:])
    else:
        text = re.sub(r"/+", "/", text)
    # 「去尾」与「去尾点/空格」要**交替做到稳定**：`a/. ` 去掉空格后露出的 `/` 也要去掉。
    # 两个例外不剥：`/`（POSIX 根）与 `d:/`（盘符根）——剥成 `d:` 就不再是绝对路径。
    while len(text) > 1 and text[-1] in "/. ":
        if re.fullmatch(r"[a-z]:/", text) or text == "/":
            break
        text = text[:-1]
    return unicodedata.normalize("NFC", text)


def project_id_for(root: str) -> str:
    """项目根 → 稳定的假名 `p-<6 hex>`（跨会话、跨机器同一个根得到同一个 id）。

    为什么是**确定性派生**而不是随机发号：项目身份要在「本周的会话」与「上周的记忆」
    之间对得上；随机 id 每次启动都换一个，接力就断了。取 6 hex（24 位）而不是完整散列，
    是因为它要出现在日志与视图里——短到能读，又足够避免碰撞（本项目量级 ≪ 16M）。

    Raises:
        ValueError: 归一化后为空 / 不是绝对路径（调用方应记为 unparseable，而不是猜）。
    """
    normalized = normalize_path(root)
    if not looks_absolute(normalized):
        raise ValueError(f"不是绝对路径，无法生成 project_id：{root!r}")
    digest = hashlib.blake2s(normalized.encode("utf-8"), digest_size=3).hexdigest()
    return f"{PROJECT_ID_PREFIX}{digest}"


def root_hash_for(root: str) -> str:
    """项目根 → 库内比较用的散列（**只在 `projects` 表里当索引，绝不外泄**）。

    与 `project_id_for` 分开的理由：`project_id` 是**给人和模型看的假名**（短、可读），
    `root_hash` 是**给 SQL 比较用的键**（长、无碰撞顾虑）。两者混用就会有人
    把 root_hash 打进日志——那等于把路径的指纹贴到日志里。
    """
    normalized = normalize_path(root)
    if not looks_absolute(normalized):
        raise ValueError(f"不是绝对路径，无法生成 root_hash：{root!r}")
    return hashlib.blake2s(normalized.encode("utf-8"), digest_size=16).hexdigest()


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectResolution:
    """一次项目路径解析的结果（四档事件里的前三档由它产生）。"""

    project_id: str | None = None
    root: str | None = None
    event: str | None = None
    """`None` = 解析成功；否则是 `project_path_unparseable` / `project_unresolved` /
    `project_ephemeral`。"""


def resolve_project(value: Any) -> ProjectResolution:
    """载荷里的项目路径 → `ProjectResolution`（**纯词法**，不碰文件系统）。

    四档分工：
    - **没给**（None / 空串）⇒ `project_unresolved`：本轮就是没有项目上下文，
      不是错误，但视图里的路径只能整段隐去。
    - **给了但词法不成立**（非字符串 / 相对路径）⇒ `project_path_unparseable`。
    - **是宿主的会话临时目录** ⇒ `project_ephemeral`：**不给 `project_id`、不给 `root`**。
      理由见 `is_ephemeral_root`：给了 id 就会让 S3 的 scope 过滤把记忆碎片化。
    - **成立** ⇒ 返回 `project_id` + `root`（`root` 只给 `redact_view` 做前缀比较用，
      不落日志、不进库；进库的是 `root_hash`）。

    ⚠️ 检查顺序：**先判 ephemeral，再返回 id**。反过来就白做了——
    一旦 `project_id` 被算出来，调用方（UPS）就会把它写进 `projects` 表和 working。
    """
    if value is None:
        return ProjectResolution(event=EVENT_PROJECT_UNRESOLVED)
    if not isinstance(value, str) or not value.strip():
        return ProjectResolution(event=EVENT_PROJECT_PATH_UNPARSEABLE)
    normalized = normalize_path(value)
    if not looks_absolute(normalized):
        return ProjectResolution(event=EVENT_PROJECT_PATH_UNPARSEABLE)
    if is_ephemeral_root(normalized):
        return ProjectResolution(event=EVENT_PROJECT_EPHEMERAL)
    return ProjectResolution(project_id=project_id_for(normalized), root=normalized)


ENV_PROJECT = "MARCH7_PROJECT"
"""显式项目根的逃生舱（`ADR-0063` §4.1 的第 ① 档，**默认不设**）。

为什么要它：当「载荷给的 cwd」与「进程 cwd」互相矛盾时（Codex `resume` 那种情况），
用户需要一个"我就想记到这个项目里"的确定性出口。写侧与读侧都认它。
"""

_PROJECT_REFERENCE = re.compile(
    r"(?:本|该|此|这个|当前)项目|项目(?:的|里|内|当前|已经|已|规定)|"
    r"\b(?:this|current|the) project(?:'s|\b)", re.IGNORECASE,
)
_PROJECT_CONTEXT_LIMIT = re.compile(
    r"(?:只|仅)(?:讨论|适用|用于).{0,16}项目|"
    r"本项目.{0,14}(?:其他项目|另一个项目).{0,8}(?:不同|无关)|"
    r"仅限本项目|\b(?:only|specific to) this project\b", re.IGNORECASE,
)
_PROJECT_TOPICS = re.compile(
    r"封面|初稿|排期|截止|发布|交付|部署|构建|审批|采购|预算|里程碑|接口|仓库|分支|"
    r"\b(?:cover|draft|deadline|release|deployment|budget|approval|milestone|repository)\b",
    re.IGNORECASE,
)
_PROJECT_ARTIFACT = re.compile(r"封面|初稿|发布窗口|上线窗口|交付日期|审批流程|合并策略")
_ARTIFACT_DECISION = re.compile(r"统一(?:用|采用|使用)|定稿|定在|原定|改为|改用|确认用|以后一直这样")
_CROSS_PROJECT = re.compile(r"(?:所有|每个|各个|不同)项目(?:都|均)?|跨项目统一|\bacross (?:all |my )?projects\b", re.IGNORECASE)
_SHORT_ACK = re.compile(
    r"(?:好(?:的|吧)?|嗯+|对(?:的)?|行|可以|收到|明白(?:了)?|继续|就这样|确认|ok|yes|sure|thanks)+",
    re.IGNORECASE,
)


def short_acknowledgement(text: str) -> bool:
    """短确认没有独立主题。"""
    return _SHORT_ACK.fullmatch(re.sub(r"[\W_]+", "", text)) is not None


def explicitly_project_bound(text: str) -> bool:
    """文本有明确的项目限定，可用于旧全局项的保守可见性判断。"""
    return bool(_PROJECT_REFERENCE.search(text))


def unbound_historical_project(scope: str, content: str, quote: str | None) -> bool:
    """旧全局项目事实没有可信项目归属，只有本机全库维护面可检查。"""
    return scope == "global" and (
        explicitly_project_bound(quote or "") or explicitly_project_bound(content) or artifact_decision(quote or "")
    )


def artifact_decision(text: str) -> bool:
    """可识别的交付物决定，无明确跨项证据时按项目事实保护。"""
    return bool(_PROJECT_ARTIFACT.search(text) and _ARTIFACT_DECISION.search(text) and not _CROSS_PROJECT.search(text))


def evidence_is_project_bound(quote: str, context: str | None, *, summary: str = "") -> bool:
    """只用原话和明确限定的同事项上文，避免把聊天所在目录等同于事实范围。"""
    topic_text = summary if short_acknowledgement(quote) else quote
    return explicitly_project_bound(quote) or artifact_decision(quote) or bool(
        context and _PROJECT_CONTEXT_LIMIT.search(context)
        and set(_PROJECT_TOPICS.findall(context.casefold())) & set(_PROJECT_TOPICS.findall(topic_text.casefold()))
    )


def resolve_active_project(
    *,
    payload_value: Any = None,
    process_cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    known_project: Callable[[str], str | None] | None = None,
) -> ProjectResolution:
    """**一次调用此刻**该用哪个项目（`ADR-0063` §4.1，唯一判据）。

    优先级：`MARCH7_PROJECT` → 载荷给的 cwd → 进程 cwd（`os.getcwd()`）→ `None`。

    ## 三条必须记住的口径

    1. **绝不读 `PWD`**。实测（`ADR-0063` §2.2）它固定为"启动 shell 的目录"，
       与真实 cwd 无关。用它会让**所有**项目的记忆落到同一个错误项目里，
       而且每个项目都"有"记忆 ⇒ 看不出来。这条是实测结论，不是洁癖。
    2. **第一个非空来源生效，不向后回落**。`MARCH7_PROJECT` 设了却解析不出来
       ⇒ 直接返回那个"解析失败"的结果，**不偷偷改用它下面的 cwd**。
       理由：它是用户显式指定的逃生舱；静默回落等于把"我以为记到 A 了"
       变成"其实记到 B 了"——正是本函数要消灭的那类静默失败。
    3. **拿不到就是 `None`**。只查全局记忆，项目状态由写入管线按 `ADR-0076` 拒写。

    Args:
        payload_value: 宿主载荷里的项目路径（WorkBuddy / Codex 的 `cwd`、
            DSH 的 `--cwd`）。`None` / 空串 ⇒ 视为该来源不存在。
        process_cwd: 进程自己的工作目录。**由调用方传入**（本函数不自己调
            `os.getcwd()`）：钩子进程的 cwd 由宿主的 shell 决定，未必可信，
            那些调用点要能明确选择"不用它"。
        env: 环境变量映射（默认 `os.environ`；测试可注入）。

    Returns:
        `ProjectResolution`：成功时带 `project_id` + `root`；否则只带 `event`。
    """
    source = os.environ if env is None else env
    override = str(source.get(ENV_PROJECT) or "").strip()
    for candidate in (override, payload_value, process_cwd):
        if candidate is None:
            continue
        if isinstance(candidate, str) and not candidate.strip():
            continue
        result = resolve_project(candidate)
        if result.root is None:
            return result
        if known_project and (existing := known_project(root_hash_for(result.root))):
            return ProjectResolution(project_id=existing, root=result.root)
        # 先查旧映射，已经登记的项目不会因实际目录别名的变化而自动重绑。
        try:
            native = Path(candidate)
            if native.is_absolute() and native.is_dir():
                canonical = resolve_project(str(native.resolve(strict=True)))
                if canonical.root and known_project:
                    existing = known_project(root_hash_for(canonical.root))
                    if existing:
                        return ProjectResolution(project_id=existing, root=canonical.root)
                return canonical
        except OSError:
            return ProjectResolution(event=EVENT_PROJECT_PATH_UNPARSEABLE)
        return result
    return ProjectResolution(event=EVENT_PROJECT_UNRESOLVED)


@dataclass(frozen=True)
class RedactedView:
    """脱敏结果：文本 + 隐去了几段 + 产生了哪些事件（事件去重保序）。"""

    text: str
    replaced: int = 0
    events: tuple[str, ...] = field(default_factory=tuple)


def _under_project(normalized: str, project_root: str | None) -> bool:
    """归一化路径是否落在项目根之下（`==` 或 `root/` 前缀，纯字符串比较）。"""
    if not project_root:
        return False
    root = project_root.rstrip("/")
    if not root:
        return False
    return normalized == root or normalized.startswith(root + "/")


def redact_view(text: str, *, project_root: str | None = None) -> RedactedView:
    """把文本里的绝对路径替换成占位符（**唯一实现**，注入 / 日志 / 导出都走这里）。

    Args:
        text: 原文（可以是 `None`，按空串处理）。
        project_root: 本轮解析出的项目根（已归一化或未归一化都行，内部会再归一化）。
            `None` ⇒ 无法判断哪段是本项目的 ⇒ 全部走 `<路径>` 并记 `project_unresolved`。

    Returns:
        `RedactedView`：`text` 是渲染后的文本；`replaced` 是被替换的段数；
        `events` 是本次产生的三档事件（去重、保序）。
    """
    raw = text or ""
    if not isinstance(raw, str) or not raw:
        return RedactedView(text="" if raw is None else str(raw))

    root = normalize_path(project_root) if project_root else None
    events: list[str] = []
    if root is None and PATH_RE.search(raw):
        events.append(EVENT_PROJECT_UNRESOLVED)

    replaced = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal replaced
        found = match.group(0)
        core = found.rstrip(_TRAILING_PUNCT)
        suffix = found[len(core):]
        normalized = normalize_path(core)
        replaced += 1
        if EVENT_PATH_OMITTED not in events:
            events.append(EVENT_PATH_OMITTED)
        placeholder = PLACEHOLDER_PROJECT if _under_project(normalized, root) else PLACEHOLDER_EXTERNAL
        return placeholder + suffix

    redacted = PATH_RE.sub(_sub, raw)
    return RedactedView(text=redacted, replaced=replaced, events=tuple(events))


__all__ = [
    "ENV_PROJECT",
    "EVENT_PATH_OMITTED",
    "EVENT_PROJECT_EPHEMERAL",
    "EVENT_PROJECT_UNRESOLVED",
    "EVENT_PROJECT_PATH_UNPARSEABLE",
    "NORM_VERSION",
    "PATH_RE",
    "PLACEHOLDER_EXTERNAL",
    "PLACEHOLDER_PROJECT",
    "PROJECT_ID_PREFIX",
    "ProjectResolution",
    "RedactedView",
    "UNRESOLVED_EVENTS",
    "is_ephemeral_root",
    "looks_absolute",
    "normalize_path",
    "project_id_for",
    "redact_view",
    "resolve_active_project",
    "resolve_project",
    "root_hash_for",
    "split_path_spans",
]
