"""核对 DSH 补记来源。持久层只接收宿主消息摘要。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from March7.store import MemoryStore

ENV_REQUIRE_DSH_SOURCE = "MARCH7_DSH_REQUIRE_SOURCE"
_MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_FACT_SENTENCE = re.compile(r"[^。！？!?；;\r\n]+")
# 逗号之后带冒号的独立标签，可在同一句里另起一条规则。
_RULE_LABEL = re.compile(r"[^，,：:]{1,40}[：:](?![/\d])")
_COORDINATION = r"(?:另外(?:还有)?|而且|以及|另有|至于|并且|同样|但是|而|但)"
_BOUNDARY = re.compile(rf"(?P<comma>[，,])\s*(?:{_COORDINATION}\s*)?|{_COORDINATION}\s*")
_REVIEW_REFERENCE = (
    r"(?:(?:下面|以下|上面|上述)(?:这|那)?|这|那|此)(?:一)?(?:条|段|项|件|个)?"
    r"(?:内容|事实|信息|偏好|习惯|规则|事项|结论|事)?"
)
_MEMORY_DESTINATION = r"(?:记|存|写|放|加)(?:入|进|到|在)(?:长期)?记忆(?:里|中|内)?"
_REVIEW_REQUEST = (
    r"(?:(?:(?:请(?:你)?|烦请|麻烦(?:你)?|帮我|帮忙|再|顺便)\s*)*"
    rf"(?:(?:补记|补录|记录|保存|记住|记下(?:来)?|记一下|回顾|{_MEMORY_DESTINATION})"
    rf"(?:{_REVIEW_REFERENCE}|一下)?"
    rf"|(?:把|将){_REVIEW_REFERENCE}"
    rf"(?:记(?:住|下(?:来)?)|{_MEMORY_DESTINATION}))(?:就(?:行|好|可以(?:了)?)|即可)?"
    r"|(?:please\s+)?(?:remember|record|review|save)"
    r"(?:\s+(?:this|that|the\s+(?:above|following)))?(?:\s+to\s+(?:long-term\s+)?memory)?)"
)
_REVIEW_PREFIX = re.compile(rf"^\s*{_REVIEW_REQUEST}\s*[，,：:]\s*", re.IGNORECASE)
_THANKS = r"(?:谢谢(?:你|啦)?|多谢|辛苦了|感谢(?:你)?|thank\s+you|thanks)"
_REVIEW_SUFFIX = re.compile(rf"(?:[，,]\s*(?:{_REVIEW_REQUEST}|{_THANKS})\s*)+$", re.IGNORECASE)
_RECORD_INSTRUMENT = re.compile(
    r"^(?:我|用户)?(?:用|使用)(?P<medium>.+?)(?:来|以)?(?:记录|记下)(?P<object>.+)$"
)
_RECORD_LOCATION = re.compile(r"^(?P<object>.+?)(?:记录|记)(?:在|到|入)(?P<medium>.+)$")
_TOPICALIZED_OBJECT = re.compile(
    r"^(?P<object>[^我，,：:]+)我(?P<predicate>[^我，,：:]*(?:检查|查看|查验|核对|记录|整理|提交|保存|维护|喜欢|使用))$"
)
_WEEKLY = re.compile(r"每(?:个)?(?:星期|礼拜|周)(?=[一二三四五六日天])")
_WEEKDAY = re.compile(r"(?:星期|礼拜|周)([一二三四五六日天])")
_SCHEDULE = r"每周[一二三四五六日天](?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜间)?"
_FRONTED_SCHEDULE = re.compile(rf"^(?P<schedule>{_SCHEDULE})我(?P<predicate>.+)$")
_UNIVERSAL_SCHEDULE = re.compile(rf"^(我{_SCHEDULE})都(?=会|要|须|必须)")
_REQUIREMENT = re.compile(
    r"(?P<waived>不(?:必|须|需(?:要)?|用)|无(?:需|须)|免(?:予|于|检|验|查|登记|出示))"
    r"|(?P<forbidden>不得|不许|不能|严禁|禁止)"
    r"|(?P<required>必须|应当|需要|须)"
    r"|(?P<permitted>可以|获准|允许)"
)
_MODAL_WORD = re.compile(
    r"(?P<waived>不必|不须|不需要|无需|无须)"
    r"|(?P<forbidden>不得|不许|严禁|禁止)"
    r"|(?P<required>必须|应当|须)"
)
_MODAL_FORM = {"waived": "无须", "forbidden": "禁止", "required": "必须"}
_PAST_RULE = re.compile(
    r"^\s*(?:旧(?:规则|规定|要求|流程|办法|做法)|原(?:规则|规定)|此前|原先|以前|过去|先前|曾经)"
)
_PHASE = re.compile(
    r"(?P<arrival>入场|进场|进门|入站|进站|入园|入馆)"
    r"|(?P<departure>离场|出场|退场|出门|出站|离站|出园|离馆)"
)
_EVENT_ORDER = re.compile(
    r"(?P<event>[\u3400-\u9fff]{2})(?:完成|结束)?(?:之|以)?(?P<order>前|后)(?!台|端|期|面)"
)


@dataclass(frozen=True)
class FactEvidence:
    """从已验证的原话中找到的来源片段及判重信息。"""

    source_quote: str
    digest: str | None = None
    allow_rephrasing: bool = False


def is_native_message_id(message_id: str) -> bool:
    """工具调用标识属于调用日志，不可用作用户消息标识。"""
    return _MESSAGE_ID.fullmatch(message_id) is not None and not message_id.startswith(
        ("call_", "tool_call", "tooluse_")
    )


def message_digest(text: str) -> str:
    """以同一换行和边界空白口径计算完整用户消息摘要。"""
    normalized = text.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def fact_spans(text: str, quotes: list[str]) -> list[tuple[int, int]]:
    """保留陈述原位，分开规则标签，并跳过显式的补记请求。"""
    quote_starts: set[int] = set()
    quote_ends: set[int] = set()
    for quote in set(quotes):
        for match in re.finditer(re.escape(quote), text):
            quote_starts.add(match.start())
            quote_ends.add(match.end())

    spans: list[tuple[int, int]] = []
    heading_start: int | None = None
    for match in _FACT_SENTENCE.finditer(text):
        sentence = match[0]
        if not sentence.strip():
            continue
        prefix = _REVIEW_PREFIX.match(sentence)
        if prefix is not None and prefix.end() == len(sentence):
            continue
        if sentence.rstrip().endswith(("：", ":")) and text[match.end():].startswith(("\r", "\n")):
            heading_start = match.start()
            continue
        start = match.start() + (prefix.end() if prefix is not None else 0)
        suffix = _REVIEW_SUFFIX.search(sentence, start - match.start())
        end = match.start() + suffix.start() if suffix is not None else match.end()
        for boundary in _BOUNDARY.finditer(text, start, end):
            left_end = boundary.start()
            while left_end > start and text[left_end - 1].isspace():
                left_end -= 1
            right_start = boundary.end()
            comma = boundary.group("comma")
            if comma is not None:
                after_comma = boundary.start() + len(comma)
                while after_comma < right_start and text[after_comma].isspace():
                    after_comma += 1
                if after_comma in quote_starts:
                    right_start = after_comma
            separates_quotes = left_end in quote_ends and right_start in quote_starts
            labelled = comma is not None and _RULE_LABEL.match(text, right_start, end) is not None
            if not separates_quotes and not labelled:
                continue
            spans.append((heading_start if heading_start is not None else start, boundary.start()))
            heading_start = None
            start = right_start
        if start < end:
            spans.append((heading_start if heading_start is not None else start, end))
        heading_start = None
    return spans


def _quote_locations(text: str, quote: str, spans: list[tuple[int, int]]) -> set[tuple[int, ...]]:
    """返回每次引文出现对应的原话范围。"""
    locations = set()
    for match in re.finditer(re.escape(quote), text):
        overlaps = tuple(
            index for index, (start, end) in enumerate(spans)
            if start < match.end() and end > match.start()
        )
        if overlaps:
            locations.add(overlaps)
    return locations


def _event_orders(text: str) -> dict[str, set[str]]:
    """提取同一动作的前后关系。证据不清晰时仍按原话分支核对。"""
    orders: dict[str, set[str]] = {}
    for match in _EVENT_ORDER.finditer(text):
        orders.setdefault(match["event"], set()).add(match["order"])
    return orders


def _weekdays(text: str) -> set[str]:
    return {day.replace("天", "日") for day in _WEEKDAY.findall(text)}


def _supports_summary(summary: str, statements: list[str], locations: set[tuple[int, ...]]) -> bool:
    """引文要有可核对的原词依据，也要排除更明确指向其他陈述的摘要。"""
    required = {match.lastgroup for match in _REQUIREMENT.finditer(summary)}
    phase = {match.lastgroup for match in _PHASE.finditer(summary)}
    historical = _PAST_RULE.match(summary.strip().removeprefix("用户")) is not None
    event_orders = _event_orders(summary)
    weekdays = _weekdays(summary)
    compatible = set()
    for index, statement in enumerate(statements):
        if not historical and _PAST_RULE.match(statement):
            continue
        statement_phase = {match.lastgroup for match in _PHASE.finditer(statement)}
        if len(phase) == 1 and statement_phase and phase.isdisjoint(statement_phase):
            continue
        statement_weekdays = _weekdays(statement)
        if weekdays and statement_weekdays and weekdays.isdisjoint(statement_weekdays):
            continue
        if any(
            event in event_orders and orders.isdisjoint(event_orders[event])
            for event, orders in _event_orders(statement).items()
        ):
            continue
        modes = {match.lastgroup for match in _REQUIREMENT.finditer(statement)}
        if len(required) != 1 or not modes or not required.isdisjoint(modes):
            compatible.add(index)
    locations = {tuple(sorted(compatible.intersection(location))) for location in locations}
    locations.discard(())
    if not locations:
        return False
    direct_support = _literal_support(summary, statements) & compatible
    if direct_support:
        return any(direct_support.intersection(location) for location in locations)

    # 摘要点名的主体能在原话定位时，其他句子里的通用动作词不能覆盖这个位置。
    topic = summary.strip().removeprefix("用户").removeprefix("我").strip()
    topic_support: set[int] = set()
    for end in range(3, len(topic) + 1):
        matches = {index for index in compatible if topic[:end] in statements[index]}
        if not matches:
            break
        topic_support = matches

    # 语气词用于保留规则原文，不能充当「入场」或「离场」这样的辨别依据。
    support_text = _MODAL_WORD.sub(" ", topic)
    anchors = {
        support_text[index:index + 2] for index in range(len(support_text) - 1)
        if support_text[index:index + 2].isalnum()
    }
    support_statements = [_MODAL_WORD.sub(" ", statement) for statement in statements]
    hits = [{anchor for anchor in anchors if anchor in statement} for statement in support_statements]
    for location in locations:
        if topic_support and not topic_support.intersection(location):
            continue
        quoted_hits = set().union(*(hits[index] for index in location))
        if quoted_hits and not any(quoted_hits < hits[index] for index in compatible):
            return True
    return False


def _literal_support(summary: str, statements: list[str]) -> set[int]:
    text = summary.strip(" 。！？!?；;").removeprefix("用户").strip()
    return {index for index, statement in enumerate(statements) if text and text in statement}


def normalize_rule_wording(text: str) -> str:
    """统一含义相同的义务词，保留肯否和被约束的动作。"""
    return _MODAL_WORD.sub(lambda match: _MODAL_FORM[match.lastgroup or ""], text)


def _normalized_statement(statement: str) -> str | list[str]:
    """识别明确的对象前置和记录关系，保留参与者及原有条件。"""
    text = re.sub(r"\s*[：:]\s*", "：", " ".join(statement.split()))
    text = _WEEKLY.sub("每周", text)
    schedule = _FRONTED_SCHEDULE.fullmatch(text)
    if schedule is not None:
        text = "我" + schedule["schedule"] + schedule["predicate"]
    text = _UNIVERSAL_SCHEDULE.sub(r"\1", text)
    topicalized = _TOPICALIZED_OBJECT.fullmatch(text)
    if topicalized is not None:
        text = "我" + topicalized["predicate"] + topicalized["object"]
    text = normalize_rule_wording(text)
    for form in (_RECORD_INSTRUMENT, _RECORD_LOCATION):
        match = form.fullmatch(text)
        if match and not any(mark in text for mark in ("：", "，", ",")):
            return ["记录", match["medium"].strip(), match["object"].strip()]
    return text


def fact_evidence(
    text: str, summary: str, source_quote: str, quotes: list[str], spans: list[tuple[int, int]],
) -> FactEvidence | None:
    """对齐候选和宿主原话，明确标到了另一条陈述时拒绝。"""
    statements = [text[start:end].strip() for start, end in spans]
    source_locations = _quote_locations(text, source_quote, spans)
    if not _supports_summary(summary, statements, source_locations):
        return None

    # 引文指向同一条陈述时补齐该条原话，可保留只在上下文里的适用条件。
    selected: set[int] = set()
    for quote in quotes:
        if any(quote != other and quote in other for other in quotes):
            continue
        matches = _quote_locations(text, quote, spans)
        # 单独的片段出现于多条规则中，无法确定是哪条的证据。
        if len(matches) != 1:
            return FactEvidence(source_quote)
        selected.update(matches.pop())

    if not selected:
        return FactEvidence(source_quote)
    if not _supports_summary(summary, statements, {tuple(selected)}):
        return None
    literal = _literal_support(summary, statements)
    if len(literal) == 1 and literal <= selected:
        selected = literal
    evidence = [statements[index] for index in sorted(selected)]
    if len(evidence) == 1:
        if source_quote in evidence[0] or evidence[0] in source_quote:
            source_quote = evidence[0]
    normalized = [_normalized_statement(statement) for statement in evidence]
    can_rephrase = len(evidence) == 1 and not any(mark in evidence[0] for mark in ("，", ","))
    return FactEvidence(source_quote, message_digest(json.dumps(normalized, ensure_ascii=False)), can_rephrase)


def attest_message(
    store: MemoryStore, session_id: str, message_id: str, project_id: str | None, text: str,
) -> bool:
    """宿主已经验证角色，只登记本会话、本项目的消息标识和散列。"""
    if not is_native_message_id(session_id) or not is_native_message_id(message_id) or not text.strip():
        return False
    store.record_dsh_proof(session_id, message_id, project_id, message_digest(text))
    return True


def verify_transcript(
    store: MemoryStore, session_id: str, message_id: str, project_id: str | None, transcript: str,
) -> str:
    """校对一条完整用户消息。原话片段由 Judge 从该消息中截取。"""
    if not is_native_message_id(session_id) or not is_native_message_id(message_id):
        raise ValueError("来源必须使用宿主提供的用户消息 ID，不能填工具调用 ID。")
    text = transcript.strip()
    for candidate in (text, text.removeprefix("用户：").strip(), text.removeprefix("user:").strip()):
        if candidate and store.matches_dsh_proof(session_id, message_id, project_id, message_digest(candidate)):
            return candidate
    raise ValueError("来源与当前项目、会话或用户原话不符。请按宿主给出的原话及消息 ID 分条补记。")
