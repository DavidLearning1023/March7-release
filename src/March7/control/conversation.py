"""对话控制面。执行入口只接收宿主确认的用户入站文本。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from March7 import config, delete_receipt, diagnostics, erase, maintenance
from March7.control import dialogue
from March7.l0_profile import ProfileStore
from March7.retriever import scope_allows
from March7.schemas import ConflictStatus, Memory, MemoryStatus, ResolutionType
from March7.source_proof import is_native_message_id
from March7.store import ManagementDetail, MemoryStore

COMMAND = re.compile(r"/March7[ \t]+(erase|approve|cancel|archive|restore|expire|fix|history)[ \t]+([^\r\n]+)")
EXPLICIT_ERASE = re.compile(
    r"(?:请)?(?:删除|擦除)(?:记忆[ \t]*(?P<id>[\w-]+(?:,[\w-]+)*)"
    r"|含有「(?P<literal>[^」\r\n]+)」的记忆"
    r"|与记忆[ \t]*(?P<reference>[\w-]+)[ \t]*冲突的旧记忆"
    r"|与(?P<current>新记忆)冲突的旧记忆)[。！]?"
)
APPROVAL = re.compile(r"(确认擦除|取消擦除)[ \t]+([a-f0-9]{32})")
SELECTOR_ID = re.compile(r"[\w-]+")
TOKEN = re.compile(r"[a-f0-9]{32}")
PREVIEW_TTL = 600

_NOT_FOUND = "指定的记忆不存在、不可见，或目标条件没有匹配。"
_SELECTION_HELP = "请给出完整记忆 ID、明确文字条件，或用新版记忆 ID 指明冲突范围。"
_LOGGER = logging.getLogger(__name__)
_MAX_CANDIDATES = 5
_ACTIVE = {MemoryStatus.ACTIVE, MemoryStatus.AGING}
_REFERENCE = re.compile(r"(?:(?:刚才|上一轮|上轮)的?)?(?:这|那|一)条(?:记忆)?")
_NUMBERED = re.compile(r"第[ \t]*([一二三四五1-5])[ \t]*条(?:记忆)?")
_POLITE = re.compile(r"^(?:(?:请(?:你)?|麻烦(?:你)?|帮我|给我)[ \t]*|用[ \t]*March7[ \t]+)+")
_AMBIGUOUS = re.compile(
    r"全部|所有|任何|这些|那些|有关|相关|之类|其他|或者|以及|并且|同时|"
    r"不要|别删|不需要|不能|不用|不想|不必|别动|如果|除非|否则|假设|只是|举例|吗$|好不好$|行不行$"
)
_QUOTES = re.compile(r"[「」“”\"『』‘’]")
_CORRECTION = re.compile(r"(?:把|将)(.+?)(?:改成|改为|纠正为|更正为)(.+)")
_QUALIFIED_CORRECTION_TARGET = re.compile(
    r'March7[ \t]+(?:测试)?记忆库(?:里|中)的?[ \t]*(「[^」]+」|『[^』]+』|“[^”]+”|"[^"]+")'
    r'(?:[ \t]*这条记忆)?[ \t]*[，,]?'
)
_LOOKUP = re.compile(r"(查找历史|查一下历史|找一下|查一下|查找|搜一下|搜索|找找|查查)(.+)")
_QUESTION = re.compile(r"(?:你)?(?:还)?记得(?:关于)?(.+?)(?:吗|的记忆吗)")
_ACTIONS = {
    "永久删除": "erase", "永久擦除": "erase", "永久删掉": "erase", "删除掉": "erase",
    "删除": "erase", "删掉": "erase", "擦除": "erase", "忘掉": "preview", "归档": "archive", "收起来": "archive",
    "恢复": "restore", "还原": "restore",
}
_VERBS = "|".join(_ACTIONS)
_VERB = re.compile(rf"({_VERBS})(.+)?")
_POSTFIX = re.compile(rf"把(.+?)({_VERBS})")
_REMEMBER = re.compile(r"(?:替我)?(?:补记一下|补记|记下来|记一下|记住|记下)[：: \t，,]*(.+)")
_EXPORT = re.compile(r"(?:把)?(?:我的|全部|所有)?记忆(?:全部)?(?:导出|导出来)|导出(?:我的|全部|所有)?记忆(?:为 ?(JSON|Markdown))?", re.I)
_STATUS = re.compile(r"(?:(?:看看|查看|查一下)(?:我的)?记忆库(?:状态|现在怎么样)|记忆库(?:状态|里有多少条记忆|现在怎么样))")
_DIAGNOSTICS = re.compile(r"(?:(?:查看|查一下)?(?:March7 ?)?记忆诊断|当前(?:是哪个|绑定了哪个)记忆库(?:和项目)?|当前(?:是哪个|绑定了哪个)项目|当前库和项目)")
_WHY_WRITE = re.compile(r"(?:本轮|这轮|刚才)?为什么(?:本轮|这轮|刚才)?(?:没|没有)(?:记住|写入)(?:这条(?:记忆)?)?")
_WHY_RECALL = re.compile(r"(?:本轮|这轮|刚才)?为什么(?:用了|用|注入了|召回了)这条(?:记忆)?")
_RECEIPT = re.compile(r"(?:看看|查看|查一下)?(?:刚才的|最近的|最新的)?(?:删除|擦除)回执")
_CONFIRM = re.compile(r"确认(?:永久)?(?:删除|删掉|擦除)(?:这条(?:记忆)?|刚才那条(?:记忆)?)?")
_CANCEL = re.compile(r"取消(?:永久)?(?:删除|删掉|擦除)(?:这条(?:记忆)?)?")
_MAINTENANCE_CHECK = re.compile(r"(?:(?:检查|体检)(个人|当前项目)记忆|给(个人|当前项目)记忆做个体检)(?:[ \t]+第([1-9][0-9]{0,3})页)?")
_MAINTENANCE_QUICK_PREVIEW = re.compile(r"整理(个人|当前项目)记忆")
_MAINTENANCE_COMMAND = re.compile(
    r"(预览撤销|预览|确认|取消|查看|开启|关闭)(个人|当前项目)记忆(整理回执|整理|自动体检)"
    r"(?:[ \t]+([a-f0-9]{12}|[a-f0-9]{32}))?"
)


@dataclass(frozen=True)
class MemoryRequest:
    action: str
    target: str = ""
    content: str = ""


def _unquote(text: str) -> str:
    text = text.strip()
    for left, right in (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"')):
        if text.startswith(left) and text.endswith(right):
            return text[1:-1].strip()
    return text


def _natural_target(raw: str) -> str | None:
    text = raw.strip().removesuffix("这条记忆").strip()
    if not text:
        text = raw.strip()
    quoted = _unquote(text)
    if not quoted or _QUOTES.search(quoted):
        return None
    if text == quoted and (re.search(r"[，,；;。：:！!？?]", text) or _AMBIGUOUS.search(text)):
        return None
    return quoted


def match_memory_request(prompt: str) -> MemoryRequest | None:
    """解析一条原生中文请求。狭义匹配失败时保持普通聊天。"""
    text = _POLITE.sub("", prompt.strip()).rstrip("。！!").strip()
    if not text or "\n" in text or "\r" in text:
        return None
    question = text.endswith(("？", "?"))
    query = text.rstrip("？?").strip()
    if match := _MAINTENANCE_CHECK.fullmatch(query):
        return MemoryRequest("maintenance_check", match[1] or match[2], match[3] or "1")
    if match := _MAINTENANCE_QUICK_PREVIEW.fullmatch(text):
        return MemoryRequest("maintenance_quick_preview", match[1])
    if match := _MAINTENANCE_COMMAND.fullmatch(text):
        action, scope, feature, key = match.groups()
        if feature == "自动体检" and action in {"开启", "关闭"} and key is None:
            return MemoryRequest("maintenance_auto", scope, action)
        if feature == "整理回执" and action == "查看" and key is None:
            return MemoryRequest("maintenance_history", scope)
        commands = {"预览": (12, "preview"), "预览撤销": (32, "undo"), "确认": (32, "apply"), "取消": (32, "cancel")}
        if feature == "整理" and action in commands and key and len(key) == commands[action][0]:
            return MemoryRequest("maintenance_" + commands[action][1], scope, key)
    if _STATUS.fullmatch(query):
        return MemoryRequest("status")
    if _DIAGNOSTICS.fullmatch(query):
        return MemoryRequest("diagnostics")
    if _WHY_WRITE.fullmatch(query):
        return MemoryRequest("why_write")
    if _WHY_RECALL.fullmatch(query):
        return MemoryRequest("why_recall")
    if match := _QUESTION.fullmatch(query):
        if target := _natural_target(match[1]):
            return MemoryRequest("lookup", target)
    if match := _LOOKUP.fullmatch(query):
        raw = match[2].strip()
        if raw.startswith("关于") and raw.endswith("的记忆"):
            raw = raw.removeprefix("关于").removesuffix("的记忆")
        if target := _natural_target(raw):
            return MemoryRequest("history_lookup" if match[1].endswith("历史") else "lookup", target)
    if question:
        return None
    if _CONFIRM.fullmatch(text):
        return MemoryRequest("confirm")
    if _CANCEL.fullmatch(text):
        return MemoryRequest("cancel")
    if _RECEIPT.fullmatch(text):
        return MemoryRequest("receipt")
    if match := _EXPORT.fullmatch(text):
        return MemoryRequest("export", "json" if (match[1] or "").lower() == "json" else "md")
    if match := _REMEMBER.fullmatch(text):
        if content := _unquote(match[1]):
            if content not in {"这条", "那条", "这个", "那个", "上面的话", "我说的话"}:
                return MemoryRequest("remember", content=content)
        return None
    if match := _CORRECTION.fullmatch(text):
        raw_target = match[1].strip()
        qualified = _QUALIFIED_CORRECTION_TARGET.fullmatch(raw_target)
        target = _natural_target(qualified[1] if qualified else raw_target)
        content = _unquote(match[2])
        unquoted = content == match[2].strip()
        if target and content and not _QUOTES.search(content) and (
            not unquoted or not (_AMBIGUOUS.search(content) or re.search(r"[，,；;？?]", content))
        ):
            return MemoryRequest("fix", target, content)
        return None
    if match := _POSTFIX.fullmatch(text):
        raw, verb = match.groups()
    elif match := _VERB.fullmatch(text):
        verb, raw = match.groups()
    else:
        return None
    if raw and (target := _natural_target(raw)):
        return MemoryRequest(_ACTIONS[verb], target)
    return MemoryRequest("offer_preview") if raw is None and _ACTIONS[verb] in {"erase", "preview"} else None


def parse_user_command(prompt: str) -> tuple[str, str] | None:
    """仅识别占据整条用户消息的动作，引用、解释和带前后文的消息照常交给宿主。"""
    text = prompt.strip()
    if match := COMMAND.fullmatch(text):
        return match[1], match[2].strip()
    if match := APPROVAL.fullmatch(text):
        return ("approve" if match[1] == "确认擦除" else "cancel"), match[2]
    match = EXPLICIT_ERASE.fullmatch(text)
    if match is None:
        return None
    if match["literal"]:
        return "erase", "contains:" + match["literal"]
    if match["reference"] or match["current"]:
        return "erase", "conflicts:" + (match["reference"] or "@current")
    return "erase", match["id"]


def _visible(store: MemoryStore, memory_id: str, project_id: str | None) -> Memory:
    memory = store.get_memory(memory_id)
    if memory is None or not scope_allows(memory, project_id):
        raise ValueError(_NOT_FOUND)
    return memory


def _select(
    store: MemoryStore, selector: str, project_id: str | None, *, session_id: str | None = None,
) -> list[Memory]:
    mode, _, value = selector.partition(":")
    if mode == "contains" and value.strip():
        matches = store.memories_containing((value,), project_id=project_id)
    elif mode == "conflicts" and (value == "@current" or SELECTOR_ID.fullmatch(value)):
        relations = [
            conflict for conflict in store.list_conflicts()
            if conflict.status in (ConflictStatus.CONFIRMED, ConflictStatus.RESOLVED)
            and conflict.resolution_type == ResolutionType.DIRECT_CONFLICT
        ]
        if value == "@current":
            source_ids = {conflict.source_id for conflict in relations}
            possible = [
                memory for memory in store.visible_memories(project_id)
                if session_id and memory.source_conversation_id == session_id and memory.id in source_ids
                and memory.status in (MemoryStatus.ACTIVE, MemoryStatus.AGING)
            ]
            if len(possible) != 1:
                raise ValueError("当前对话无法唯一确定「新记忆」，请指定它的记忆 ID。")
            newer = possible[0]
        else:
            newer = _visible(store, value, project_id)
        matches = []
        for conflict in relations:
            if conflict.source_id == newer.id:
                older = store.get_memory(conflict.target_id)
                if older is not None and older.id != newer.id and scope_allows(older, project_id):
                    matches.append(older)
    else:
        ids = [item.strip() for item in selector.split(",")]
        if not all(SELECTOR_ID.fullmatch(item) for item in ids):
            raise ValueError(_SELECTION_HELP)
        matches = [_visible(store, item, project_id) for item in dict.fromkeys(ids)]
    if not matches:
        raise ValueError(_NOT_FOUND)
    return list({memory.id: memory for memory in matches}.values())


def _snapshot(memory: Memory) -> str:
    fields = (memory.id, memory.scope, memory.content, memory.source_quote, memory.status.value, memory.created_at)
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


def _ticket(store: MemoryStore, token: str) -> Path:
    if TOKEN.fullmatch(token) is None:
        raise ValueError("擦除预览标识无效。")
    return store.data_dir / "erase-previews" / f"{token}.json"


def propose_erase(
    store: MemoryStore, selector: str, *, project_id: str | None
) -> dict[str, Any]:
    """模型入口只生成预览。短效标识绑定范围及快照，文件不存正文。"""
    try:
        memories = _select(store, selector, project_id)
    except ValueError as exc:
        return {"status": "rejected", "message": str(exc)}
    token = uuid4().hex
    path = _ticket(store, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump({
            "project_id": project_id,
            "expires_at": int(time.time()) + PREVIEW_TTL,
            "targets": {memory.id: _snapshot(memory) for memory in memories},
        }, output)
    return {
        "status": "preview",
        "token": token,
        "target_count": len(memories),
        "targets": [
            {"memory_id": memory.id, "scope": memory.scope, "content": memory.content}
            for memory in memories
        ],
        "message": (
            f"向用户展示要永久擦除的具体记忆及范围。用户可另发「确认擦除 {token}」"
            f"或「取消擦除 {token}」。预览有效 10 分钟。"
        ),
    }


def _consume_preview(
    store: MemoryStore, token: str, project_id: str | None, *, cancel: bool
) -> list[Memory]:
    path = _ticket(store, token)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError("擦除预览不存在、已取消或已使用。") from exc
    if snapshot["project_id"] != project_id:
        raise ValueError("擦除预览仅能在原项目处理。")
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise ValueError("擦除预览已使用。") from exc
    if cancel:
        return []
    if snapshot["expires_at"] <= int(time.time()):
        raise ValueError("擦除预览已过期。")
    memories = [_visible(store, mid, project_id) for mid in snapshot["targets"]]
    if any(_snapshot(memory) != snapshot["targets"][memory.id] for memory in memories):
        raise ValueError("目标有变化，请重新生成擦除预览。")
    return memories


def _erase(
    store: MemoryStore, memories: list[Memory], project_id: str | None, *,
    expected_revisions: dict[str, str] | None = None,
) -> dict[str, Any]:
    targets = tuple(memory.id for memory in memories)
    project_scope = f"project:{project_id}"
    pending = delete_receipt.prepare_single_receipt(store.data_dir, targets[0]) if expected_revisions else None
    try:
        outcome = erase.perform_erase(
            store,
            store.data_dir,
            memory_ids=targets,
            include_working=False,
            profile_store=ProfileStore(store.data_dir / config.PROFILE_FILENAME),
            clear_profile=all(memory.scope == "global" for memory in memories),
            conversation_only=True,
            visibility_project_id=project_id,
            expected_memories={memory.id: memory for memory in memories},
            expected_revisions=expected_revisions,
            working_project_id=project_id if any(memory.scope == project_scope for memory in memories) else None,
        )
    except ValueError:
        if pending is not None and store.get_memory(targets[0]) is not None:
            pending.unlink(missing_ok=True)
        raise
    receipt = delete_receipt.build_receipt(
        scope=delete_receipt.SCOPE_SINGLE if len(targets) == 1 else delete_receipt.SCOPE_SELECTION,
        targets=targets,
        deleted=outcome.deleted,
        audit=outcome.audit,
        verification=outcome.verification,
        honest_limits=(
            erase.HONESTY_CLAUSE
            + " 模型供应商日志由供应商管理。会话擦除仅清理由授权目标标识或项目归属证实的派生记录；"
            "精确重述可以计为残留，同义改写须另行明确选择。"
        ),
    )
    delete_receipt.write_receipt(receipt, store.data_dir)
    return {"status": "erased", **receipt.as_dict()}


def embed_correction(content: str) -> list[float]:
    """和终端控制面使用同一个编码器。"""
    from March7.embedder import Embedder

    return Embedder().embed_one(content)


def apply_memory_action(
    store: MemoryStore, memory: Memory, action: str, *, content: str = "",
    expected_revision: str | None = None, host: str = "panel",
) -> dict[str, Any]:
    """使用已核对的目标做单条处置，存储层在事务内复核版本。"""
    profile = ProfileStore(store.data_dir / config.PROFILE_FILENAME)
    memory_id = memory.id
    options = {"expected_revision": expected_revision} if expected_revision is not None else {}
    if action == "restore":
        if memory.status != MemoryStatus.ARCHIVED:
            raise ValueError("只能恢复已归档的记忆；已被纠正或合并的旧条保留在历史中。")
        if not store.transition_memory(memory, MemoryStatus.ACTIVE, **options):
            raise ValueError("记忆状态已变化，请重新查看。")
        profile.restore_visibility(memory_id)
        return {"status": "restored", "message": f"已恢复记忆 {memory_id}。"}
    if memory.status not in (MemoryStatus.ACTIVE, MemoryStatus.AGING):
        raise ValueError("该记忆已失效，请指定当前有效的记忆。")
    phrases = store.unshared_profile_phrases(memory.scope, (memory.content, memory.source_quote))
    profile_digests = profile.matching_digests(phrases)
    if action == "fix":
        new_id = str(uuid4())
        if expected_revision is not None:
            options["host"] = host
        if not store.fix_memory(memory, new_id, content, embed_correction(content), **options):
            raise ValueError("记忆已变化，请重新查看后纠正。")
        profile.suppress(memory_id, profile_digests)
        message = f"已纠正 {memory_id}，旧条保留为历史。当前记忆 ID {new_id}。"
        if profile_digests:
            message += "同源旧画像已停止注入，本轮以上述纠正为准。"
        return {"status": "corrected", "memory_id": new_id, "message": message}
    if action not in {"archive", "expire"}:
        raise ValueError("未知的记忆处置动作。")
    if not store.transition_memory(memory, MemoryStatus.ARCHIVED, **options):
        raise ValueError("记忆状态已变化，请重新查看。")
    profile.suppress(memory_id, profile_digests)
    status = "expired" if action == "expire" else "archived"
    return {"status": status, "message": f"记忆 {memory_id} 已停止召回，仍可查历史。"}


def _manage_memory(
    store: MemoryStore, action: str, argument: str, project_id: str | None,
) -> dict[str, Any]:
    memory_id, _, rest = argument.partition(" ")
    if not SELECTOR_ID.fullmatch(memory_id):
        raise ValueError(_SELECTION_HELP)
    store.archive_due_memories()
    memory = _visible(store, memory_id, project_id)
    if action == "history":
        if rest:
            raise ValueError("查看历史需要单条记忆 ID。")
        return {"status": "history", "memory": memory.model_dump(mode="json")}
    if action == "restore":
        if rest:
            raise ValueError("只能恢复已归档的记忆；已被纠正或合并的旧条保留在历史中。")
        return apply_memory_action(store, memory, action)
    if memory.status not in (MemoryStatus.ACTIVE, MemoryStatus.AGING):
        raise ValueError("该记忆已失效，请指定当前有效的记忆。")
    if action == "fix":
        content = rest.removeprefix(":: ").strip()
        if not rest.startswith(":: ") or not content:
            raise ValueError("请用「/March7 fix 记忆ID :: 新事实」提供纠正的原话。")
        return apply_memory_action(store, memory, action, content=content)
    if action == "expire":
        if memory.scope == "global":
            raise ValueError("到期管理适用于当前项目记忆。全局记忆可显式归档。")
        if rest:
            try:
                deadline = datetime.fromisoformat(rest)
                if deadline.utcoffset() is None:
                    raise ValueError
                timestamp = int(deadline.timestamp())
            except (ValueError, OverflowError, OSError) as exc:
                raise ValueError("请使用带时区的截止日期时间，如 2026-12-01T18:00:00+08:00。") from exc
            if timestamp > int(time.time()):
                profile = ProfileStore(store.data_dir / config.PROFILE_FILENAME)
                phrases = store.unshared_profile_phrases(memory.scope, (memory.content, memory.source_quote))
                profile_digests = profile.matching_digests(phrases)
                if not store.schedule_memory_expiry(memory, timestamp):
                    raise ValueError("记忆状态已变化，请重新查看。")
                profile.suppress(memory.id, profile_digests, timestamp)
                return {"status": "scheduled", "message": f"记忆 {memory_id} 将在指定时间停止召回。"}
    elif rest:
        raise ValueError("归档需要单条记忆 ID。")
    return apply_memory_action(store, memory, action)


def _event_key(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def _turn_path(data_dir: Path, host: str, session_id: str) -> Path:
    return data_dir / "conversation-state" / (_event_key(host, session_id) + ".json")


def _read_turn(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and state["expires_at"] > time.time():
            return state
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return {}
    return {}


def _write_turn(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _take_turn(path: Path) -> dict[str, Any]:
    """确认、取消或后续入站只能有一个先领取上一轮状态。"""
    taken = path.with_name(f"{uuid4().hex}.consumed")
    try:
        path.replace(taken)
    except FileNotFoundError:
        return {}
    try:
        return _read_turn(taken)
    finally:
        taken.unlink(missing_ok=True)


def clear_conversation_turn(
    data_dir: Path, host: str, session_id: str, *, event_id: str = "", prompt: str = "",
) -> None:
    """非记忆轮也使上一轮的指代和预览失效。"""
    db_path = data_dir / config.DB_FILENAME
    if event_id and db_path.is_file():
        with closing(MemoryStore(db_path)) as store:
            store.init_schema()
            store.dialogue_receive(
                _event_key(host, session_id), None, _event_key("dialogue", host, session_id, event_id), None,
                prompt_key=dialogue.source_key(host, session_id, prompt),
            )
    path = _turn_path(data_dir, host, session_id)
    state = _take_turn(path)
    if state.get("kind") == "offer":
        _ticket_path(path, state["nonce"]).unlink(missing_ok=True)


def _ticket_path(path: Path, nonce: str) -> Path:
    if not TOKEN.fullmatch(nonce):
        raise ValueError("会话预览无效。")
    return path.with_name(f"{nonce}.proposal")


def _detail(store: MemoryStore, memory_id: str, project_id: str | None) -> ManagementDetail:
    detail = store.management_detail(memory_id)
    if detail is None or not scope_allows(detail.memory, project_id):
        raise ValueError(_NOT_FOUND)
    return detail


def _save_selection(
    store: MemoryStore, path: Path, project_id: str | None, memory_ids: list[str], kind: str,
) -> list[Memory]:
    details = [_detail(store, memory_id, project_id) for memory_id in memory_ids]
    _write_turn(path, {
        "kind": kind,
        "project_id": project_id,
        "expires_at": int(time.time()) + PREVIEW_TTL,
        "items": [[detail.memory.id, detail.revision] for detail in details],
    })
    return [
        detail.memory.model_copy(update={"source_quote": detail.source.get("quote") or detail.memory.source_quote})
        if detail.source else detail.memory
        for detail in details
    ]


def _offer_preview(path: Path, project_id: str | None) -> dict[str, Any]:
    nonce = uuid4().hex
    _write_turn(path, {
        "kind": "offer", "nonce": nonce, "project_id": project_id,
        "expires_at": int(time.time()) + PREVIEW_TTL,
    })
    return {
        "status": "preview_available",
        "message": (
            "还未找到明确的删除目标，库未改动。可以请用户先查找并指出具体记忆。"
            "如确需根据本轮原话查找近义目标，先查找，至多提议一条。"
            "使用 March7_resolve 的 preview_erase，conflict_id 传 JSON 对象，"
            f"context 填 {path.stem}.{nonce}，selector 填找到的一条可见记忆 ID。"
            "把工具返回的具体目标展示给用户，不要展示内部标识或声称已删除。"
        ),
    }


def propose_conversation_erase(
    store: MemoryStore, context: str, selector: str, *, project_id: str | None,
) -> dict[str, Any]:
    """模型只能为本轮原生删除请求登记单条预览，不能执行或覆盖。"""
    if not re.fullmatch(r"[a-f0-9]{64}\.[a-f0-9]{32}", context) or not SELECTOR_ID.fullmatch(selector):
        return {"status": "rejected", "message": "请使用本轮宿主提供的预览上下文和单条记忆 ID。"}
    key, nonce = context.split(".")
    path = store.data_dir / "conversation-state" / f"{key}.json"
    state = _read_turn(path)
    if state.get("nonce") != nonce or state.get("project_id") != project_id:
        return {"status": "rejected", "message": "没有来自本会话及项目的有效删除请求。"}
    try:
        detail = _detail(store, selector, project_id)
        if not store.claim_conversation_operation(_event_key("erase-preview", nonce)):
            raise ValueError("本轮已提出删除预览。请回复已展示的候选，或重新查找。")
        _write_turn(_ticket_path(path, nonce), {
            "kind": "preview", "items": [[selector, detail.revision]], "expires_at": state["expires_at"],
        })
    except ValueError as exc:
        return {"status": "rejected", "message": str(exc)}
    result = {"status": "preview", "memories": [detail.memory], "message": _PREVIEW_HELP}
    return {"status": "preview", "message": render_user_result(result)}


_PREVIEW_HELP = "请先核对以下永久删除目标。要删除可说「确认删除」，要保留可说「取消删除」。"


def _ref_index(target: str) -> int | None:
    if _REFERENCE.fullmatch(target):
        return -1
    if match := _NUMBERED.fullmatch(target):
        number = match[1]
        return int(number) - 1 if number.isascii() else "一二三四五".index(number)
    return None


def _selected_detail(
    store: MemoryStore, state: dict[str, Any], project_id: str | None, index: int,
) -> ManagementDetail:
    items = state.get("items", [])
    if (index == -1 and len(items) != 1) or (index >= 0 and index >= len(items)):
        raise ValueError("上一轮没有可唯一确定的这条记忆。请先查找，再说第几条或具体文字。")
    memory_id, revision = items[index]
    detail = _detail(store, memory_id, project_id)
    if detail.revision != revision:
        raise ValueError("上一轮的记忆已变化，请重新查找并核对。")
    return detail


def _remember(content: str, project_id: str | None, host: str, session_id: str, event_id: str) -> dict[str, Any]:
    from March7.judge import RULE_PROJECT_IDENTITY_REQUIRED
    from March7.server import last_extraction_failed, write_with_context

    result = write_with_context(
        "用户：" + content, conversation_id=session_id, host=host, source_session_id=session_id,
        source_message_id=event_id, project_id=project_id, attested_text=content,
    )
    if last_extraction_failed():
        return {"status": "failed", "message": "补记失败，记忆判定服务没有完成，请稍后再试。"}
    if result.written_count or result.updated_l0_count:
        return {
            "status": "remembered",
            "message": f"已补记 {result.written_count} 条长期记忆，更新 {result.updated_l0_count} 项画像。",
        }
    if result.rejected:
        if any(item.rule == RULE_PROJECT_IDENTITY_REQUIRED for item in result.rejected):
            return {"status": "not_written", "message": "这条属于项目，本轮缺少可信项目身份。请核对目录后重新补记。"}
        return {"status": "not_written", "message": "本轮未新增记忆。请查找原记录或核对待补记的事实。"}
    return {"status": "not_written", "message": "本轮没有新增记忆。可查找现有记录核对是否已记。"}


def _library_read(store: MemoryStore, request: MemoryRequest) -> dict[str, Any]:
    if request.action == "status":
        stats = store.stats()
        return {
            "status": "library_status",
            "message": f"记忆库有 {stats.memory_count} 条记忆，其中 {stats.recallable_count} 条可召回。",
        }
    if request.action == "receipt":
        current = delete_receipt.latest_receipt(store.data_dir)
        if current is None:
            return {"status": "not_found", "message": "当前库没有擦除回执。"}
        return {"status": "receipt", "message": (
            f"最近的擦除回执为 {current['receipt_id']}，"
            f"删除 {current['deleted']['memories']} 条，验证状态 {current['verification']['verdict']}。"
        )}
    from March7.export import export_json, export_markdown

    profile = ProfileStore(store.data_dir / config.PROFILE_FILENAME).load()
    export = export_json if request.target == "json" else export_markdown
    path = str(export(store, profile, output_dir=store.data_dir))
    count = store.stats().memory_count
    return {"status": "exported", "path": path, "memory_count": count, "message": f"已导出 {count} 条记忆。文件在 {path}。"}


def _handle_natural(
    store: MemoryStore, request: MemoryRequest, *,
    project_id: str | None, host: str, session_id: str, event_id: str,
) -> dict[str, Any]:
    path = _turn_path(store.data_dir, host, session_id)
    state = _take_turn(path)
    proposal = None
    if state.get("kind") == "offer":
        proposal = _ticket_path(path, state["nonce"])
    if state.get("project_id") != project_id:
        state = {}
    action = request.action
    try:
        if action in {"diagnostics", "why_write", "why_recall"}:
            report = diagnostics.runtime_snapshot(store, project_id, host=host, session_id=session_id)
            mode = {"why_write": "write", "why_recall": "recall"}.get(action, "all")
            return {"status": "library_diagnostics", "message": diagnostics.explain_runtime(report, include=mode)}
        if action in {"status", "export", "receipt"}:
            return _library_read(store, request)
        if action == "remember":
            return _remember(request.content, project_id, host, session_id, event_id)
        if action == "cancel":
            if state.get("kind") not in {"preview", "offer"}:
                raise ValueError("本会话没有可取消的删除预览。请核对当前库状态。")
            return {"status": "cancelled", "message": "已取消本会话的删除预览。"}
        if action == "offer_preview":
            return _offer_preview(path, project_id)
        if action == "confirm":
            if state.get("kind") == "offer" and proposal is not None:
                state = _read_turn(proposal)
            if state.get("kind") != "preview":
                raise ValueError("本会话没有待确认的删除预览。请先核对具体目标。")
            detail = _selected_detail(store, state, project_id, -1)
            return _erase(store, [detail.memory], project_id, expected_revisions={detail.memory.id: detail.revision})
        return _handle_selection(store, request, state, path, project_id, host)
    finally:
        if proposal is not None:
            proposal.unlink(missing_ok=True)


def _handle_selection(
    store: MemoryStore, request: MemoryRequest, state: dict[str, Any], path: Path, project_id: str | None,
    host: str,
) -> dict[str, Any]:
    store.archive_due_memories()
    action = request.action
    index = _ref_index(request.target)
    if index is not None:
        detail = _selected_detail(store, state, project_id, index)
        candidates = [detail.memory]
    else:
        candidates = store.memories_containing((request.target,), project_id=project_id)
        if action in {"lookup", "fix", "archive"}:
            candidates = [memory for memory in candidates if memory.status in _ACTIVE]
        elif action == "restore":
            candidates = [memory for memory in candidates if memory.status == MemoryStatus.ARCHIVED]
        elif action == "history_lookup":
            candidates = [memory for memory in candidates if memory.status not in _ACTIVE]
    if not candidates:
        if action in {"erase", "preview"}:
            return _offer_preview(path, project_id)
        return {"status": "not_found", "message": "当前可见记忆中没有匹配这段文字的记录。"}
    if len(candidates) > _MAX_CANDIDATES:
        return {"status": "candidates", "message": f"匹配到 {len(candidates)} 条，请补充关键词以缩小范围。"}
    if len(candidates) > 1 or action in {"lookup", "history_lookup"}:
        return {
            "status": "candidates" if len(candidates) > 1 else "found",
            "memories": _save_selection(store, path, project_id, [memory.id for memory in candidates], "selection"),
            "message": "请按第几条或具体文字指定目标。" if len(candidates) > 1 else "找到了以下记忆。",
        }
    if action == "preview":
        return {
            "status": "preview", "message": _PREVIEW_HELP,
            "memories": _save_selection(store, path, project_id, [candidates[0].id], "preview"),
        }
    if index is None:
        detail = _detail(store, candidates[0].id, project_id)
        if _snapshot(detail.memory) != _snapshot(candidates[0]):
            raise ValueError("目标已变化，请重新查找并核对。")
    memory = detail.memory
    if action == "erase":
        return _erase(store, [memory], project_id, expected_revisions={memory.id: detail.revision})
    result = apply_memory_action(
        store, memory, action, content=request.content, expected_revision=detail.revision, host=host,
    )
    _save_selection(store, path, project_id, [result.get("memory_id", memory.id)], "selection")
    return result


def _maintenance_command(
    store: MemoryStore, request: MemoryRequest, project_id: str | None, host: str, session_id: str,
    context: str | None = None,
) -> dict[str, Any]:
    label = request.target
    if label == "当前项目" and not project_id:
        raise ValueError("缺少可信项目身份。请核对当前目录后再做项目体检。")
    scope = f"project:{project_id}" if label == "当前项目" else "global"
    owner = maintenance.fingerprint((host, session_id, project_id))
    key = request.content
    action = request.action
    if action == "maintenance_quick_preview":
        groups = [item.key for item in store.maintenance_check(scope).findings if item.kind == "duplicate"]
        if len(groups) == 1:
            action, key = "maintenance_preview", groups[0]
        else:
            action, key = "maintenance_check", "1"
    if action == "maintenance_auto":
        enabled = key == "开启"
        store.maintenance_set_auto(scope, enabled)
        if enabled:
            store.maintenance_check(scope, automatic_only=True)
        return {"status": action, "message": f"已{key}{label}记忆自动体检。"}
    if action == "maintenance_check":
        report = store.maintenance_check(scope)
        findings = report.findings
        offset = (int(key) - 1) * 6
        library = diagnostics.library_identifier(store.data_dir)
        lines = [
            f"{label}记忆体检：{len(findings)} 组，本次检查 {report.examined} 条；自动体检{'开启' if report.auto else '关闭'}。",
            f"当前库编号 {library}。",
        ]
        for finding in findings[offset:offset + 6]:
            name = maintenance.FINDING_NAMES[finding.kind]
            if finding.reason == "旧条项目归属待核对":
                lines.append("待复核：有旧条的项目归属需在本机面板核对。")
            elif finding.kind == "duplicate":
                lines.append(f"{name} {finding.key}，含 {len(finding.ids)} 条。可说「预览{label}记忆整理 {finding.key}」。")
            else:
                lines.append(f"{name}：{finding.reason}（{len(finding.ids)} 条）。")
        if offset + 6 < len(findings):
            lines.append(f"下一页可说「检查{label}记忆 第{int(key) + 1}页」。")
        return {"status": "maintenance_report", "message": "\n".join(lines)}
    if action == "maintenance_history":
        batches = store.maintenance_history(scope)
        if not batches:
            return {"status": action, "message": f"{label}范围暂无整理回执。"}
        lines = [f"{label}最近整理回执。"]
        for batch in batches:
            mark = "已撤销" if batch.state == "undone" else "撤销操作" if batch.kind == "undo" else "已执行"
            lines.append(f"{batch.id}（{mark}）。")
        return {"status": action, "message": "\n".join(lines)}
    if action in {"maintenance_preview", "maintenance_undo"}:
        batch = store.maintenance_preview(scope, owner, key, undo=action == "maintenance_undo", context=context)
        lines = [
            f"{label}记忆整理预览 {batch.id}，十分钟有效。",
            *[f"{target.id}：{target.before} → {target.after}" for target in batch.targets],
            "可在本机面板核对条目和原话。",
            f"同意请说「确认{label}记忆整理 {batch.id}」，取消请说「取消{label}记忆整理 {batch.id}」。",
        ]
        if context:
            code = dialogue.confirmation_code(batch.id)
            lines.append(f"聊天里也可以说「就按这个方案执行 {code}」或「取消这份方案 {code}」。")
        return {"status": "maintenance_preview", "id": batch.id, "kind": batch.kind,
                "targets": [target.id for target in batch.targets], "message": "\n".join(lines)}
    batch = store.maintenance_finish(scope, owner, key, cancel=action == "maintenance_cancel")
    return _maintenance_result(batch, label)


def _maintenance_result(batch: maintenance.Batch, label: str) -> dict[str, Any]:
    changed = sum(target.before != target.after for target in batch.targets)
    if batch.state == "applied":
        message = f"已{'撤销整理，恢复' if batch.kind == 'undo' else '整理'} {changed} 条。回执 {batch.id}。"
        if batch.kind in {"merge", "plan"}:
            message += f"可用「预览撤销{label}记忆整理 {batch.id}」核对撤销。"
    else:
        message = {"cancelled": "已取消此批预览。", "stale": "预览已过期或当前范围已变化，请重新检查和预览。",
                   "undone": "这批已经撤销。"}[batch.state]
    return {"status": "maintenance_" + batch.state, "id": batch.id, "kind": batch.kind,
            "changed": changed if batch.state == "applied" else 0, "message": message}


def handle_user_command(
    store: MemoryStore, prompt: str, *, project_id: str | None,
    session_id: str | None = None, event_id: str | None = None, host: str | None = None,
    offer_dialogue: bool = False,
) -> dict[str, Any] | None:
    """处理从宿主入站事件取得的用户原话，供宿主回显结果。"""
    command = parse_user_command(prompt)
    request = match_memory_request(prompt) if command is None else None
    nonce = None
    if offer_dialogue and host and session_id and event_id:
        authorization = dialogue.decision(prompt) if command is None and request is None else None
        decision, expected = (authorization[0], (None, authorization[1])) if authorization else (None, None)
        if request is not None and request.action in {"maintenance_apply", "maintenance_cancel"}:
            decision = "confirm" if request.action == "maintenance_apply" else "cancel"
            scope = f"project:{project_id or ''}" if request.target == "当前项目" else "global"
            expected = (scope, request.content)
        try:
            nonce, batch = store.dialogue_receive(
                _event_key(host, session_id), project_id, _event_key("dialogue", host, session_id, event_id), decision, expected,
                prompt_key=dialogue.source_key(host, session_id, prompt),
            )
        except ValueError as exc:
            return {"status": "rejected", "message": str(exc)}
        except (OSError, RuntimeError, sqlite3.Error):
            _LOGGER.exception("宿主会话授权未完成")
            return {"status": "failed", "message": "本轮未能核实操作，请重新核对。"}
        if decision is not None:
            if batch is None and authorization is not None:
                return {"status": "rejected", "message": "当前库和会话没有这张待确认的草稿。请核对编号或重新预览。"}
            if batch is not None:
                clear_conversation_turn(store.data_dir, host, session_id)
                return _maintenance_result(batch, "个人" if batch.scope == "global" else "当前项目")
    if command is None and request is None:
        if host and session_id:
            clear_conversation_turn(store.data_dir, host, session_id)
        return {"status": "dialogue_context", "context": nonce, "message": dialogue.hint(nonce)} if nonce else None
    maintenance_action = request is not None and request.action.startswith("maintenance_")
    replayable = request is not None and request.action in {"maintenance_apply", "maintenance_cancel"}
    if host and session_id and event_id and not replayable:
        if not store.claim_conversation_operation(_event_key(host, session_id, event_id)):
            return {"status": "duplicate", "message": "这个宿主操作已处理。"}
    try:
        if request is not None:
            if not host or not session_id or not event_id:
                raise ValueError("宿主没有提供本轮用户消息来源。请使用本机面板。")
            if maintenance_action:
                clear_conversation_turn(store.data_dir, host, session_id)
                return _maintenance_command(store, request, project_id, host, session_id, nonce)
            return _handle_natural(
                store, request, project_id=project_id, host=host, session_id=session_id, event_id=event_id,
            )
        if host and session_id:
            clear_conversation_turn(store.data_dir, host, session_id)
        action, selector = command
        if action in ("approve", "cancel"):
            memories = _consume_preview(store, selector, project_id, cancel=action == "cancel")
            return _erase(store, memories, project_id) if memories else {"status": "cancelled"}
        if action == "erase":
            return _erase(store, _select(store, selector, project_id, session_id=session_id), project_id)
        return _manage_memory(store, action, selector, project_id)
    except ValueError as exc:
        return {"status": "rejected", "message": str(exc)}
    except (OSError, RuntimeError):
        _LOGGER.exception("对话记忆操作未能完成")
        return {"status": "failed", "message": "操作未能核实完成。请在本机面板核对记忆或擦除回执。"}



def render_user_result(result: dict[str, Any]) -> str:
    """宿主只回显操作结果。擦除正文不进入本轮的附加上下文。"""
    status = result["status"]
    if status == "erased":
        checked = "可自动检查范围内无残留" if result["verification"]["clean"] else "检查发现残留或扫描失败"
        return (
            f"【March7 操作结果】擦除 {result['deleted']['memories']} 条，{checked}。"
            f"回执 {result['receipt_id']}。{result['honest_limits']}"
        )
    if status == "history":
        from March7.injection import format_recall_context

        memory = Memory.model_validate(result["memory"])
        content = format_recall_context([memory])
        return f"【March7 历史查询】{memory.id}（{memory.status.value}）\n{content}"
    if result.get("memories"):
        from March7.injection import format_recall_context

        items = [
            f"第 {index} 条（{'个人' if memory.scope == 'global' else '当前项目'}，{memory.status.value}）\n"
            + format_recall_context([memory])
            for index, memory in enumerate(result["memories"], 1)
        ]
        return "【March7 操作结果】" + result["message"] + "\n" + "\n".join(items)
    return "【March7 操作结果】" + result.get("message", "已取消擦除。")


def operation_reply_context(result: dict[str, Any]) -> str:
    if result["status"] == "dialogue_context":
        return result["message"]
    if result["status"].startswith("maintenance_"):
        return (
            render_user_result(result)
            + "\n请照实转述这个本机结果，保留所列编号和用户可发送的指令。"
            "预览只供用户核对。确认或取消必须由用户另发，不要代表用户操作。"
        )
    if result["status"] == "erased":
        return (
            render_user_result(result)
            + "\n请按上面的实际结果简短回复。只转述擦除条数、回执、核验及范围。"
            "禁止复述、概括或引用已擦除的目标正文和原话，包括来自早先对话或预览的文字。"
        )
    if result["status"] == "not_found":
        return render_user_result(result) + "\n请如实说明当前库未找到，不要用早先聊天或预览里的正文补答。"
    return (
        render_user_result(result)
        + "\n请按上面的实际结果回复用户。确认操作状态及目标，"
        "仅在本轮结果明确提供预览入口时可以查找并发起预览。"
        "若结果是失败、未写入、未找到或仅预览，请如实说明。"
    )
