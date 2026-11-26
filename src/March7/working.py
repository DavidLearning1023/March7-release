"""working.py — 工作层规则抽取（内部代号：驿路）

`docs/NAMING.md` 的命名脉络：**流光忆庭**（store 的库）、**投映**（injection 的渲染）。
驿路 = 从上一站到下一站之间那条正在走的路——它只在你**正在走**的时候有意义，
到了驿站（handoff 关闭）就不再是路。

## 这个模块解决什么

用户换工具继续同一个项目时，不想重新解释「今天只跑 CPU」「这周的进度卡在 X」。
这些事实的生命周期不是「永久」也不是「一轮」——它们属于**当前这段工作**
（`ADR-0038` 的 handoff）。长期库收它们会把短期状态永久化；只靠对话上下文，
换宿主就丢。

## 三条硬约束（全部来自 ADR，不是风格偏好）

1. **不走 LLM、不走 BGE**（`ADR-0038` 非目标 + `ADR-0026`）：
   UPS 每轮都跑，钩子是独立进程。走 LLM = 每轮加一次 API 往返；
   走嵌入 = 每轮加载 BGE-M3（实测 7.5s）。**这条路径必须毫秒级**。
   所以这里是**纯规则**：触发词 + 排除式 + 长度/形态护栏。

2. **宁可漏，不可错**（`ADR-0042` R7/R8）：
   working 层的内容**每轮都会注入给模型**。误抽一句"用户要求改 store.py"，
   下一轮模型就会把它当既成事实。所以排除式比触发词更严格：
   - 祈使句开头（`帮我` / `请` / `麻烦`）⇒ **不抽**（那是任务指令，`ADR-0028`）；
   - 疑问句（`吗` / `?` / `怎么`）⇒ **不抽**（那是提问，不是事实）；
   - 第一人称之外的主语（`他` / `她` / `它`）⇒ 抽取但标 scratch（他人状态不算约束）。

3. **不生成 `profile` / `project` durable 记忆**（`ADR-0038` 非目标）：
   本模块**只能**返回 working 条目。它的出参里没有「长期记忆」这个概念——
   想提长期记忆必须显式走 Judge（`Stop` 那条通道），不能在这里"顺手提升"。

## 为什么 kind 是三个而不是一个

`ADR-0039` 决策 1：闭集里每个值必须有**可验收的注入差异**。三个值的差异是
「注入条数上限」与「重要度」，不是措辞分类：

| kind | 典型 | 为什么单独一类 |
|---|---|---|
| `constraint` | 「今天只跑 CPU」「先不装新依赖」 | 违反它会让工作白做 ⇒ 优先级最高 |
| `state` | 「正在改 store 的迁移」「卡在 A2」 | 决定"下一步做什么" ⇒ 次高 |
| `scratch` | 「构建挂了」「等 X 回消息」 | 一次性，命中才注入 |
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 闭集（与 ADR-0039 同源；store.WORKING_KINDS 是运行时校验的那一份）
# ---------------------------------------------------------------------------

KIND_CONSTRAINT = "constraint"
KIND_STATE = "state"
KIND_SCRATCH = "scratch"

WORKING_KINDS: tuple[str, ...] = (KIND_CONSTRAINT, KIND_STATE, KIND_SCRATCH)
"""`kind` 的三值闭集。**store 层有同名常量做硬校验**，这里是抽取侧的语义定义。"""

MAX_ITEMS_PER_TURN = 2
"""单轮最多抽几条（`ADR-0039` 注入矩阵：working ≤2 条）。

为什么是 2 而不是更多：working 块和长期召回块共用同一个上下文预算，
而单轮用户真正说的"新进展"通常只有一句。抽 5 条只会把 2 条真信息埋掉。
"""

MAX_ITEM_CHARS = 120
"""单条 working 的字符上限。超过多半是把任务描述整段抄进来了 ⇒ 截断会失真，直接丢弃。"""

MIN_ITEM_CHARS = 4
"""单条下限。低于这个长度的抽取结果基本是碎片（「好」「嗯」「可以」）。"""

# ---------------------------------------------------------------------------
# 触发词：命中才进入抽取（ADR-0042 §2 取舍 3：宁可 narrow）
# ---------------------------------------------------------------------------

_CONSTRAINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 「今天只跑 CPU」「这次先不装」「暂时不要动 X」
    re.compile(r"(?:今天|今晚|这周|本周|现在|目前|暂时|先|只)\s*(?:只|先|别|不要|不|仅)"),
    re.compile(r"(?:不(?:要|能|许)|别|禁止|避免)\s*(?:动|改|碰|装|跑|用|提交|推送|删)"),
    re.compile(r"(?:只(?:能|可以|用|跑)|仅(?:限|用|跑))"),
    re.compile(r"必须\s*(?:先|在|用|跑|等)"),
    re.compile(r"(?:保持|继续|沿用)\s*(?:现状|原样|旧|当前)"),
)

_STATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:正在|已在|刚刚|已经)\s*(?:改|做|写|跑|看|查|处理|推进)"),
    re.compile(r"(?:卡在|堵在|停在|困在)"),
    re.compile(r"(?:下一步|接下来|然后|等下)\s*(?:要|会|去|做)"),
    re.compile(r"(?:进度|目前|现阶段)\s*(?:是|在|到)"),
    re.compile(r"(?:完成|做完|跑通|过了|绿了)\s*(?:了|一半|部分)?"),
)

_SCRATCH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:挂了|崩了|报错|失败|红[了掉]|超时)"),
    re.compile(r"等\s*(?:他|她|对面|对方|回复|消息|邮件|审批)"),
    re.compile(r"(?:临时|凑合|先这样)"),
)

_PREFILTER_RE = re.compile(
    "|".join(
        p.pattern for p in (_CONSTRAINT_PATTERNS + _STATE_PATTERNS + _SCRATCH_PATTERNS)
    )
)

# ---------------------------------------------------------------------------
# 排除式（比触发词更严格：宁可漏，不可错）
# ---------------------------------------------------------------------------

_IMPERATIVE_HEAD_RE = re.compile(
    r"^\s*(?:请|帮我|帮忙|麻烦|给我|替我|能不能|可以|要不|要么|试试|试着)"
)
"""祈使句开头 ⇒ 不抽。

为什么这一条必须存在：`ADR-0028` 已经定了「任务指令不入库」。用户说
「帮我把 store.py 的迁移改一下」——这句话里同时命中 `改` 这个 state 触发词，
但它**不是进展，是指令**。抽进去之后，下一轮模型会以为"用户说迁移已经改了"。
"""

_INTERROGATIVE_RE = re.compile(r"[?？]\s*$|(?:吗|么|呢)\s*[?？]?\s*$|怎么|为什么|为什么|是否")
"""疑问句 ⇒ 不抽（提问不是事实）。"""

_AI_SPEECH_HEAD_RE = re.compile(r"^\s*(?:助手|AI|模型|它|你(?:说|认为|觉得))[:：]")
"""明显的第三方/助手发言 ⇒ 不抽（`ADR-0038`：不把第三方内容抽成用户事实）。"""

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
"""代码块整段剔掉再抽取（代码里的 `不` / `只` 会误命中触发词）。"""

_URL_RE = re.compile(r"https?://\S+")

_QUOTE_HEAD_RE = re.compile(r"^\s*>\s?.*$", re.MULTILINE)
"""引用块（用户粘贴别人的话）⇒ 整行剔除。粘贴的内容不是用户自己的状态。"""


@dataclass(frozen=True)
class WorkingCandidate:
    """一条抽出来的 working 候选（**还没写库**，落库由调用方决定）。

    - `content`：进注入的那句话（已归一化空白，≤`MAX_ITEM_CHARS`）。
    - `kind`：三值闭集之一。
    - `source_quote`：用户在原文里**真正说的那句**（截到 `MAX_ITEM_CHARS`）；
      注入时与原话双注入（与长期层同一纪律：结论 + 证据）。
    - `fingerprint`：命中的触发词类别（**只作日志/归因**，不参与判据）。
    """

    content: str
    kind: str
    source_quote: str
    fingerprint: str


def _normalize(text: str) -> str:
    """NFKC + 折叠空白（全角空格、连续换行都压成单空格）。

    与 `store.working_content_hash` 的归一化**同向**（那里是 NFKC + strip）：
    这里多折叠内部空白，是为了让注入文本不会因为用户按了回车而多出空行。
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def _strip_noise(text: str) -> str:
    """剔掉不该抽取的段落：代码块 / 引用块 / URL。"""
    cleaned = _CODE_FENCE_RE.sub(" ", text or "")
    cleaned = _QUOTE_HEAD_RE.sub("", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    return cleaned


def _split_sentences(text: str) -> list[str]:
    """按句末标点切句（中英文都切）。**不按逗号切**——逗号切开会把条件句切成半句。"""
    parts = re.split(r"(?<=[。！!；;])|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _classify(sentence: str) -> tuple[str, str] | None:
    """判这一句属于哪个 kind；不属于任何一类 ⇒ `None`。

    Returns:
        `(kind, fingerprint)` 或 `None`。`fingerprint` 是命中的**类别名**
        （`constraint` / `state` / `scratch`），用于在账本里回答"哪类规则在出活"。
    """
    # constraint 优先：同一句里既有「只跑 CPU」又有「正在跑」，约束更重要
    if any(p.search(sentence) for p in _CONSTRAINT_PATTERNS):
        return KIND_CONSTRAINT, "constraint_pattern"
    if any(p.search(sentence) for p in _STATE_PATTERNS):
        return KIND_STATE, "state_pattern"
    if any(p.search(sentence) for p in _SCRATCH_PATTERNS):
        return KIND_SCRATCH, "scratch_pattern"
    return None


def should_attempt(text: str) -> bool:
    """这句话是否值得进入抽取（**节流阀**，`ADR-0042` R7）。

    为什么要先判一次：UPS 每轮都跑，而绝大多数轮次（问问题、聊天、跑命令）
    根本不含工作状态。先做一次正则 prefilter，不命中就直接不打开 `working_items`，
    省掉后面的切句与分类。

    ⚠️ `等` 这类单字**必须带搭配**才命中（`等 + 人/回复`），否则「等待」「等于」
    全都会被当成工作状态（蓝图 §4.5 第 7 条）。
    """
    cleaned = _strip_noise(text)
    if not cleaned.strip():
        return False
    if _IMPERATIVE_HEAD_RE.match(cleaned):
        # 整句以祈使开头 ⇒ 任务指令，不抽（但可能后面还有别的句子，故只作快速否）
        return False
    return bool(_PREFILTER_RE.search(cleaned))


def extract(text: str, *, limit: int = MAX_ITEMS_PER_TURN) -> list[WorkingCandidate]:
    """从用户自己说的话里抽 0–`limit` 条 working 候选。

    Args:
        text: **用户自己的话**（调用方负责先剥掉宿主注入的 `<system-reminder>`，
            见 `hosts/workbuddy/hooks/_common.user_own_text`）。
        limit: 最多几条（默认 2，`ADR-0039` 注入矩阵）。

    Returns:
        候选列表（可能为空——**空是正常结果**，不是失败）。按句子顺序返回。

    ⚠️ 三条「不抽」的判定在这个函数里，逐条都有理由：

    1. 祈使句开头（`_IMPERATIVE_HEAD_RE`）⇒ 是任务指令（`ADR-0028`）；
    2. 疑问句（`_INTERROGATIVE_RE`）⇒ 是提问，不是事实；
    3. 助手/AI 发言（`_AI_SPEECH_HEAD_RE`）⇒ 第三方内容（`ADR-0038`）；
    4. 太短（`< MIN_ITEM_CHARS`）或太长（`> MAX_ITEM_CHARS`）⇒ 碎片或整段任务描述。
    """
    cleaned = _strip_noise(text)
    if not cleaned.strip() or limit <= 0:
        return []

    results: list[WorkingCandidate] = []
    seen: set[str] = set()
    for sentence in _split_sentences(cleaned):
        if len(results) >= limit:
            break
        if _IMPERATIVE_HEAD_RE.match(sentence):
            continue
        if _AI_SPEECH_HEAD_RE.match(sentence):
            continue
        if _INTERROGATIVE_RE.search(sentence):
            continue
        classified = _classify(sentence)
        if classified is None:
            continue
        kind, fingerprint = classified
        content = _normalize(sentence)
        if not (MIN_ITEM_CHARS <= len(content) <= MAX_ITEM_CHARS):
            continue
        # 同一轮内文本完全相同的句子只留一条（同一句话重述不是新进展）
        key = content.casefold()
        if key in seen:
            continue
        seen.add(key)
        results.append(
            WorkingCandidate(
                content=content,
                kind=kind,
                source_quote=content,
                fingerprint=fingerprint,
            )
        )
    return results


def detect_secret(text: str) -> str | None:
    """明显的 secret 检测：命中 ⇒ 返回类别名（`sk_key` / `bearer` / `password` / `long_token`）。

    为什么必须挡（`ADR-0042` R9）：working 条目会**下一轮注入给模型**，
    写进库再注入等于把密钥贴进上下文。这里只做**启发式**，不保证全抓——
    所以报告里必须写「未测漏检率」，不得声称"敏感信息已被过滤"。
    """
    raw = text or ""
    if re.search(r"\bsk-[A-Za-z0-9_\-]{16,}", raw):
        return "sk_key"
    if re.search(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", raw, re.IGNORECASE):
        return "bearer"
    if re.search(r"(?:password|passwd|pwd|密码|口令)\s*[:=：]\s*\S{4,}", raw, re.IGNORECASE):
        return "password"
    # 长的高熵串（≥32 位无空格字母数字混合）——覆盖 GitHub token / base64 私钥片段
    if re.search(r"\b(?=[A-Za-z0-9]{32,})(?=[A-Za-z]*[0-9])(?=[0-9]*[A-Za-z])[A-Za-z0-9]{32,}\b", raw):
        return "long_token"
    return None


__all__ = [
    "KIND_CONSTRAINT",
    "KIND_SCRATCH",
    "KIND_STATE",
    "MAX_ITEMS_PER_TURN",
    "MAX_ITEM_CHARS",
    "MIN_ITEM_CHARS",
    "WORKING_KINDS",
    "WorkingCandidate",
    "detect_secret",
    "extract",
    "should_attempt",
]
