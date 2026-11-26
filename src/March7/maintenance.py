"""本机记忆体检的证据分组，不调用模型。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from March7 import privacy
from March7.schemas import Memory

BATCH_SIZE = 20
MODEL_DRAFT_OWNER = "maintenance-model-draft"
PREVIEW_TTL = 600
_RESIDENCE = re.compile(r"(?:我|用户)?(?:现在|目前)?(?:住在|居住在)([\u4e00-\u9fff]{2,12})")
_PREFERENCE = re.compile(r"(?:我|用户)?(?:喜欢|喜爱)([\u4e00-\u9fff]{2,12})")
# 跨消息只用封闭取值，任意宾语可能依赖时点、语境或所指对象。
_CITIES = frozenset(
    "北京 上海 天津 重庆 香港 澳门 台北 杭州 宁波 温州 南京 苏州 无锡 合肥 福州 厦门 济南 青岛 郑州 武汉 "
    "长沙 南昌 广州 深圳 珠海 佛山 东莞 南宁 海口 三亚 成都 贵阳 昆明 拉萨 西安 兰州 西宁 银川 乌鲁木齐 "
    "石家庄 太原 呼和浩特 沈阳 大连 长春 哈尔滨".split()
)
_PREFERENCES = frozenset(
    "红色 橙色 黄色 绿色 青色 蓝色 紫色 粉色 灰色 黑色 白色 棕色 "
    "书法 围棋 象棋 游泳 跑步 骑行 摄影 园艺 烘焙 读书 露营 徒步 登山 绘画 水彩".split()
)
_CONTEXT = re.compile(
    r"今天|昨天|前天|明天|后天|本周|这周|下周|上周|本月|下月|上个月|下个月|这个月|今年|明年|去年|"
    r"本次|这次|当地|这里|那里|那家|这家|那间|这间|这座|那座|那所|这所|这个|那个|上一个|下一个"
)
FINDING_NAMES = {"duplicate": "同义重复", "related": "相关但独立", "update": "已有证据支持的更新", "review": "待复核"}


def fingerprint(value: Any) -> str:
    """为本机证据索引生成摘要。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _fact(text: str) -> tuple[str, str]:
    text = text.strip().removesuffix("。").strip()
    if match := _RESIDENCE.fullmatch(text):
        city = match[1].removesuffix("市")
        if city in _CITIES:
            return "residence", city
    if match := _PREFERENCE.fullmatch(text):
        if match[1] in _PREFERENCES:
            return "preference", match[1]
    return "literal", text


@dataclass(frozen=True)
class Projection:
    status: str
    duplicate: str
    terms: tuple[str, ...]
    unbound: bool
    pinned: bool
    created_at: int


def project_memory(
    memory: Memory, source: dict[str, str | None] | None,
    expires_at: int | None, claims: str, now: int,
) -> Projection:
    """只有完整证据对得上，才保留可用于合并的分组键。"""
    quote = memory.source_quote or ""
    stored_quote = (source or {}).get("quote") or quote
    unbound = privacy.unbound_historical_project(memory.scope, memory.content, quote)
    timed_out = expires_at is not None and expires_at <= now
    status = "archived" if timed_out else memory.status.value
    claim, evidence = _fact(memory.content), _fact(quote)
    trigger = _fact(memory.trigger_text) if memory.trigger_text else None
    # 句式以外的同文可能指不同上下文。只有同一条原生消息才提供等价证据。
    source = source or {}
    origin = (memory.host, source.get("session_id"), source.get("message_id"), source.get("revision"))
    unresolved_reference = any(_CONTEXT.search(text) for text in (memory.content, quote, memory.trigger_text or ""))
    self_contained = (
        claim == evidence and claim[0] != "literal" and quote.strip().startswith("我")
        and not unresolved_reference and trigger in (None, evidence)
    )
    same_origin_key = origin if all(origin[:3]) and not unresolved_reference else None
    duplicate = ""
    if (
        claim[1] and evidence[1] and evidence == _fact(stored_quote)
        and not unbound and not memory.conflict_with and not memory.evidence_ids
        and (self_contained or same_origin_key is not None)
        and status in {"active", "aging"}
    ):
        duplicate = fingerprint((claim, evidence, trigger, expires_at, claims, None if self_contained else same_origin_key))
    compact = re.sub(r"[^\w]", "", memory.content[:96]) if status in {"active", "aging"} and not unbound else ""
    terms = tuple(sorted({fingerprint(compact[i:i + 3])[:12] for i in range(len(compact) - 2)}))
    return Projection(status, duplicate, terms, unbound, memory.is_pinned, memory.created_at)


@dataclass(frozen=True)
class Finding:
    kind: str
    ids: tuple[str, ...]
    reason: str

    @property
    def key(self) -> str:
        return fingerprint((self.kind, self.ids, self.reason))[:12]


@dataclass(frozen=True)
class Report:
    scope: str
    version: str
    examined: int
    auto: bool
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class Target:
    id: str
    before: str
    after: str
    revision: str


@dataclass(frozen=True)
class Batch:
    id: str
    scope: str
    kind: str
    state: str
    targets: tuple[Target, ...]
    parent_id: str | None
    applied_at: int | None


def group_findings(
    items: dict[str, Projection], conflicts: list[tuple[str, str, str, str | None, int]], external: set[str],
) -> list[Finding]:
    """形成互不跨范围的分组，模糊相似度只用于提示。"""
    findings: list[Finding] = []
    blocked = set(external)
    update_sources: set[str] = set()
    findings.extend(Finding("review", (mid,), "存在跨范围的未决冲突") for mid in external if not items[mid].unbound)
    for source, target, status, resolution, asks in conflicts:
        left, right = items[source], items[target]
        if left.unbound or right.unbound:
            continue
        if (
            status == "resolved" and not asks and resolution in {"preference_evolution", "direct_conflict"}
            and left.status in {"active", "aging"} and right.status == "superseded"
        ):
            update_sources.add(source)
            findings.append(Finding("update", (source, target), "已裁决，新条有效，旧条保留历史"))
        elif status != "dismissed" and (
            status != "resolved" or asks or resolution == "uncertain"
            or resolution in {"preference_evolution", "direct_conflict"}
        ):
            blocked.update((source, target))
            findings.append(Finding("review", (source, target), "冲突仍需核对"))
        elif status == "resolved" and resolution in {"context_difference", "unrelated"}:
            blocked.update((source, target))
            findings.append(Finding("related", (source, target), "既有裁决保留两条"))

    exact: dict[str, list[str]] = defaultdict(list)
    terms: dict[str, list[str]] = defaultdict(list)
    for mid, item in items.items():
        if item.status not in {"active", "aging"}:
            continue
        if item.unbound:
            findings.append(Finding("review", (mid,), "旧条项目归属待核对"))
            continue
        if item.duplicate and mid not in blocked:
            exact[item.duplicate].append(mid)
        for term in item.terms:
            terms[term].append(mid)

    for ids in exact.values():
        ids.sort(key=lambda mid: (
            mid not in update_sources, not items[mid].pinned, items[mid].status != "active", items[mid].created_at, mid,
        ))
        keep, *others = ids
        victims = tuple(mid for mid in others if not items[mid].pinned and mid not in update_sources)
        for offset in range(0, len(victims), BATCH_SIZE - 1):
            batch = victims[offset:offset + BATCH_SIZE - 1]
            findings.append(Finding("duplicate", (keep, *batch), "完整原话与结论符合相同事实"))

    shown: set[str] = set()
    for _, ids in sorted(terms.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        available = [mid for mid in ids if mid not in shown]
        for offset in range(0, len(available), BATCH_SIZE):
            group = tuple(available[offset:offset + BATCH_SIZE])
            if len(group) < 2:
                continue
            signatures = {items[mid].duplicate or mid for mid in group}
            if len(signatures) > 1:
                shown.update(group)
                findings.append(Finding("related", group, "文字相关，分别保留并核对条件"))
    order = {kind: index for index, kind in enumerate(("duplicate", "update", "review", "related"))}
    return sorted(set(findings), key=lambda finding: (order[finding.kind], finding.ids, finding.reason))
