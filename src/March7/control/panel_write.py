"""随机回环端口上的本机管理会话与单条处置入口。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from March7 import config, delete_receipt, diagnostics, maintenance
from March7.control import conversation, panel_view as view
from March7.control.cli import _known_basenames
from March7.l0_profile import ProfileStore
from March7.schemas import MemoryStatus
from March7.store import ManagementDetail, MemoryStore

logger = logging.getLogger(__name__)
SESSION_TTL = 30 * 60
TICKET_TTL = 60
SNAPSHOT_TTL = 5 * 60
CONFIRM_TTL = 2 * 60
MAX_BODY = 32768
ACTIVE = {MemoryStatus.ACTIVE, MemoryStatus.AGING}
SETTINGS_FILE = "panel-settings.json"
now: Callable[[], float] = time.monotonic


class PanelError(ValueError):
    """在浏览器上显示的错误，不包含库正文。"""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Selection:
    section: str
    project: str

    @property
    def scope(self) -> str:
        return f"project:{self.project}" if self.project else "global"

    @property
    def history(self) -> bool:
        return self.section == "history"


@dataclass(frozen=True)
class Intent:
    target: str
    selection: Selection
    snapshot: str
    generation: int
    action: str
    text: str


@dataclass
class Session:
    deadline: float
    previews: dict[str, tuple[float, Intent]] = field(default_factory=dict)
    maintenance_preview: tuple[str, str, str] | None = None


def fields(payload: dict[str, Any], allowed: set[str]) -> None:
    if set(payload) - allowed:
        raise PanelError("请求包含不支持的字段。")


def string(payload: dict[str, Any], key: str, limit: int) -> str:
    text = payload.get(key, "")
    if not isinstance(text, str) or len(text) > limit or "\0" in text:
        raise PanelError("表单内容不符合要求。")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PanelError("表单包含无法读取的字符。") from exc
    return text


class Panel:
    """写会话、双方核对的记录版本与二次确认均绑定当前库进程。"""

    def __init__(self, data_dir: Path, launch_key: str) -> None:
        self.directory = data_dir.resolve()
        db = (self.directory / config.DB_FILENAME).stat()
        self.identity = (db.st_dev, db.st_ino)
        self.launch_key = launch_key
        self.key = secrets.token_bytes(32)
        self.lock = RLock()
        self.tickets: dict[str, float] = {}
        self.sessions: dict[str, Session] = {}
        self.generations: dict[str, tuple[float, int]] = {}

    def _prune(self) -> None:
        current = now()
        self.tickets = {ticket: deadline for ticket, deadline in self.tickets.items() if deadline > current}
        self.sessions = {token: sess for token, sess in self.sessions.items() if sess.deadline > current}
        self.generations = {
            target: generation for target, generation in self.generations.items() if generation[0] > current
        }

    def same_database(self) -> bool:
        try:
            current = (self.directory / config.DB_FILENAME).stat()
        except OSError:
            return False
        return (current.st_dev, current.st_ino) == self.identity

    def issue(self, authorization: str) -> dict[str, str]:
        if not hmac.compare_digest(authorization.encode("latin1"), f"Bearer {self.launch_key}".encode("ascii")):
            raise PanelError("启动凭证无效。", HTTPStatus.FORBIDDEN)
        self._prune()
        ticket = secrets.token_urlsafe(32)
        self.tickets[ticket] = now() + TICKET_TTL
        return {"ticket": ticket}

    def activate(self, payload: dict[str, Any]) -> dict[str, str]:
        fields(payload, {"ticket"})
        self._prune()
        ticket = string(payload, "ticket", 128)
        if self.tickets.pop(ticket, 0) <= now():
            raise PanelError("本机打开凭证已使用或过期。", HTTPStatus.FORBIDDEN)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(now() + SESSION_TTL)
        return {"token": token}

    def selection(self, payload: dict[str, Any], store: MemoryStore) -> tuple[Selection, list[str]]:
        section = string(payload, "section", 12) or "personal"
        project = string(payload, "project", 500)
        if section not in {"personal", "project", "history"}:
            raise PanelError("请选择有效的记忆分区。")
        if section == "personal":
            return Selection(section, ""), []
        projects = store.management_projects()
        if project and project not in projects:
            raise PanelError("项目不存在，请重新选择。", HTTPStatus.NOT_FOUND)
        return Selection(section, project), projects

    def listing(self, payload: dict[str, Any], store: MemoryStore) -> str:
        fields(payload, {"section", "project", "search", "status", "page"})
        selection, projects = self.selection(payload, store)
        search = string(payload, "search", 300).strip()
        status = string(payload, "status", 16)
        if status and status not in view.STATUSES:
            raise PanelError("状态筛选无效。")
        try:
            page = int(payload.get("page", 1))
            if not 1 <= page <= 1_000_000:
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise PanelError("页码无效。") from exc
        total, rows = (0, [])
        if selection.section != "project" or selection.project:
            total, rows = store.management_page(
                selection.scope, history=selection.history, search=search, status=status, limit=view.PAGE_SIZE,
                offset=(page - 1) * view.PAGE_SIZE,
            )
        return view.panel_list(
            self.directory, selection.section, selection.project, projects,
            dict(_known_basenames(store)) if projects else {}, search, status, page, total, rows,
        )

    def target(self, store: MemoryStore, mid: str, selection: Selection) -> ManagementDetail:
        item = store.management_detail(mid)
        if (
            item is None or item.memory.scope != selection.scope
            or (selection.section == "project" and not selection.project)
            or (item.memory.status not in ACTIVE) != selection.history
        ):
            raise PanelError("当前范围没有这条记忆，请刷新列表。", HTTPStatus.NOT_FOUND)
        return item

    def _generation(self, target: str) -> int:
        return self.generations.get(target, (0, 0))[1]

    def _signature(
        self, token: str, selection: Selection, item: ManagementDetail, deadline: int, generation: int,
    ) -> str:
        values = [token, selection.section, selection.project, item.memory.id, item.revision, deadline, generation]
        serialized = json.dumps(values, ensure_ascii=False).encode("utf-8")
        return hmac.new(self.key, serialized, hashlib.sha256).hexdigest()

    def detail(self, payload: dict[str, Any], store: MemoryStore, token: str) -> str:
        fields(payload, {"section", "project", "id"})
        selection, _ = self.selection(payload, store)
        mid = string(payload, "id", 500)
        item = self.target(store, mid, selection)
        deadline = int(now()) + SNAPSHOT_TTL
        signature = self._signature(token, selection, item, deadline, self._generation(mid))
        return view.panel_detail(
            item, {"section": selection.section, "project": selection.project, "id": mid},
            f"{deadline}.{signature}", active=item.memory.status in ACTIVE,
            archived=item.memory.status == MemoryStatus.ARCHIVED,
        )

    def _check(self, store: MemoryStore, intent: Intent, token: str) -> ManagementDetail:
        stale = PanelError("期限、作用域或这条记忆已变化，请重新打开详情核对。", HTTPStatus.CONFLICT)
        try:
            deadline, signature = intent.snapshot.split(".", 1)
            expires = int(deadline)
            item = self.target(store, intent.target, intent.selection)
        except (PanelError, ValueError) as exc:
            raise stale from exc
        generation = self._generation(intent.target)
        expected = self._signature(token, intent.selection, item, expires, generation)
        if expires <= now() or generation != intent.generation or not hmac.compare_digest(
            signature.encode("utf-8"), expected.encode("ascii"),
        ):
            raise stale
        return item

    def reminders(self) -> dict[str, bool]:
        path = self.directory / SETTINGS_FILE
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"fix": True, "erase": True}
        except (ValueError, OSError):
            logger.warning("面板提醒设置无法读取，启用二次确认")
            return {"fix": True, "erase": True}
        if not isinstance(content, dict):
            return {"fix": True, "erase": True}
        return {key: content.get(key) is not False for key in ("fix", "erase")}

    def save_settings(self, payload: dict[str, Any]) -> str:
        if set(payload) != {"fix", "erase"} or any(type(value) is not bool for value in payload.values()):
            raise PanelError("请选择两项提醒的开关。")
        path = self.directory / SETTINGS_FILE
        staged = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            staged.write_text(json.dumps(payload), encoding="utf-8")
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)
        return view.settings(self.directory, payload, saved=True)

    def plan(self, payload: dict[str, Any], store: MemoryStore, token: str, session: Session) -> str:
        fields(payload, {"section", "project", "id", "snapshot", "action", "text"})
        mid = string(payload, "id", 500)
        selection, _ = self.selection(payload, store)
        if selection.section == "personal" and payload.get("project"):
            raise PanelError("个人记忆的操作不能指定项目。")
        action = string(payload, "action", 20)
        text = string(payload, "text", 4000).strip()
        if action not in view.ACTIONS or (action == "fix") != bool(text):
            raise PanelError("请选择单条操作。纠正时请提供完整的新事实。")
        intent = Intent(mid, selection, string(payload, "snapshot", 128), self._generation(mid), action, text)
        item = self._check(store, intent, token)
        if (
            action in {"fix", "archive"} and item.memory.status not in ACTIVE
            or action == "restore" and item.memory.status != MemoryStatus.ARCHIVED
        ):
            raise PanelError("这条记忆的状态不支持该操作。", HTTPStatus.CONFLICT)
        session.previews = {
            ticket: preview for ticket, preview in session.previews.items() if preview[0] > now()
        }
        if not self.reminders().get(action, True):
            return self.apply(store, intent, item)
        ticket = secrets.token_urlsafe(32)
        session.previews[ticket] = (now() + CONFIRM_TTL, intent)
        return view.preview(action, item.memory, text, ticket)

    def confirm(
        self, payload: dict[str, Any], store: MemoryStore, token: str, session: Session, *, cancel: bool,
    ) -> str:
        fields(payload, {"ticket"})
        record = session.previews.pop(string(payload, "ticket", 128), None)
        if record is None or record[0] <= now():
            raise PanelError("确认已使用、已取消或已过期，请重新核对。", HTTPStatus.CONFLICT)
        intent = record[1]
        if cancel:
            return view.notice("已取消操作", "previous-detail")
        return self.apply(store, intent, self._check(store, intent, token))

    def apply(self, store: MemoryStore, intent: Intent, item: ManagementDetail) -> str:
        try:
            if intent.action == "erase":
                result = conversation._erase(
                    store, [item.memory], intent.selection.project or None,
                    expected_revisions={intent.target: item.revision},
                )
                output = view.notice("已擦除这条记忆") + view.receipt_view(result)
            else:
                result = conversation.apply_memory_action(
                    store, item.memory, intent.action, content=intent.text, expected_revision=item.revision,
                )
                output = view.notice(result["message"])
                if intent.action == "fix":
                    output += (
                        f'<div class="actions"><button data-go="detail" data-id="{view.e(result["memory_id"])}" '
                        f'data-section="{intent.selection.section}" data-project="{view.e(intent.selection.project)}">'
                        '查看新版本</button><button data-go="detail" '
                        f'data-id="{view.e(intent.target)}" data-section="history" '
                        f'data-project="{view.e(intent.selection.project)}">查看旧版本</button></div>'
                    )
        except ValueError as exc:
            logger.info("面板单条操作因状态变化或输入无效而停止（%s）", intent.action)
            raise PanelError("无法完成这次操作，请重新核对目标状态和输入。", HTTPStatus.CONFLICT) from exc
        self.generations[intent.target] = (now() + SNAPSHOT_TTL, intent.generation + 1)
        return output

    def switch(self, payload: dict[str, Any]) -> dict[str, str]:
        from March7.control.panel_launcher import open_panel

        fields(payload, {"directory"})
        raw = string(payload, "directory", 1024).strip()
        directory = Path(raw)
        if not local_directory(directory):
            raise PanelError("请填写本机数据目录的绝对路径。")
        directory = directory.resolve()
        if not local_directory(directory) or not (directory / config.DB_FILENAME).is_file():
            raise PanelError("该本机目录没有可打开的 March7 记忆库。")
        if directory == self.directory:
            raise PanelError("当前已经打开这个记忆库。")
        locations: list[str] = []

        def capture(url: str) -> bool:
            locations.append(url)
            return True

        result = open_panel(data_dir=directory, opener=capture)
        if not result.opened or not locations:
            raise PanelError("无法打开另一个记忆库，请检查目录和本机面板日志。", HTTPStatus.SERVICE_UNAVAILABLE)
        return {"url": locations[0]}

    def maintenance_operation(
        self, route: str, payload: dict[str, Any], store: MemoryStore, token: str, session: Session,
    ) -> str:
        allowed = {
            "maintenance": {"page"}, "maintenance-draft": {"batch", "group", "receipt"},
            "maintenance-finish": {"batch", "choice"},
        }
        fields(payload, allowed[route] | {"project"})
        project = string(payload, "project", 128)
        projects = store.management_projects()
        if project and project not in projects:
            raise PanelError("请选择当前库里的项目。")
        store.init_maintenance()
        scope = f"project:{project}" if project else "global"
        if route == "maintenance":
            page = string(payload, "page", 4) or "1"
            if not re.fullmatch(r"[1-9][0-9]{0,3}", page):
                raise PanelError("页码无效。")
            return view.maintenance_view(
                store, store.maintenance_check(scope), store.maintenance_history(scope), project,
                projects, dict(_known_basenames(store)), int(page), drafts=store.maintenance_model_drafts(scope),
            )
        batch_id = string(payload, "batch", 32)
        if route == "maintenance-finish":
            selection = session.maintenance_preview
            if selection is None or selection[:2] != (scope, batch_id):
                raise PanelError("请在当前面板会话先打开这张整理预览。", HTTPStatus.FORBIDDEN)
            choice = string(payload, "choice", 7)
            if choice not in {"confirm", "cancel"}:
                raise PanelError("请选择确认或取消。")
            try:
                result = store.maintenance_finish(scope, selection[2], batch_id, cancel=choice == "cancel")
            except ValueError as exc:
                raise PanelError(str(exc), HTTPStatus.CONFLICT) from exc
            return view.maintenance_result(result, project)

        group, receipt = string(payload, "group", 12), string(payload, "receipt", 32)
        if sum(bool(value) for value in (batch_id, group, receipt)) != 1:
            raise PanelError("请只选择一个草稿、组号或回执。")
        if batch_id:
            store.maintenance_check(scope)
            drafts = store.maintenance_model_drafts(scope, batch_id)
            if not drafts:
                raise PanelError("当前范围没有这张可用草稿。", HTTPStatus.NOT_FOUND)
            batch, owner = drafts[0], maintenance.MODEL_DRAFT_OWNER
        else:
            owner = maintenance.fingerprint(("panel", token))
            try:
                batch = store.maintenance_preview(scope, owner, group or receipt, undo=bool(receipt))
            except ValueError as exc:
                raise PanelError(str(exc), HTTPStatus.CONFLICT) from exc
        page = view.maintenance_draft(store, batch, project)
        session.maintenance_preview = (scope, batch.id, owner)
        return page

    def dispatch(self, route: str, payload: dict[str, Any], auth: str) -> str | dict[str, str]:
        with self.lock:
            if route == "session":
                return self.activate(payload)
            self._prune()
            token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
            session = self.sessions.get(token)
            if session is None:
                raise PanelError("本机会话无效或已过期，请从宿主或桌面入口重新打开。", HTTPStatus.FORBIDDEN)
            db = self.directory / config.DB_FILENAME
            if not self.same_database():
                raise PanelError("当前记忆库已移走或更换，请从宿主或桌面入口重新打开。", HTTPStatus.CONFLICT)
            if route == "switch":
                return self.switch(payload)
            if route == "settings":
                return self.save_settings(payload)
            if route == "settings-view":
                fields(payload, set())
                return view.settings(self.directory, self.reminders())
            if route == "profile":
                fields(payload, set())
                profile = ProfileStore(self.directory / config.PROFILE_FILENAME).recall_view()
                return view.profile_view(profile.model_dump())
            if route == "receipts":
                fields(payload, set())
                receipts = []
                for row in delete_receipt.list_receipts(self.directory):
                    receipt_id = row.get("receipt_id")
                    if isinstance(receipt_id, str) and re.fullmatch(r"delete-[a-f0-9]+", receipt_id):
                        receipt = delete_receipt.load_receipt(receipt_id, self.directory)
                        if receipt is not None:
                            receipts.append(receipt)
                return "<h2>擦除回执</h2>" + (
                    '<div class="results">' + "".join(view.receipt_view(r) for r in receipts) + "</div>"
                    if receipts else '<p class="card">还没有擦除回执。</p>'
                )
            with closing(MemoryStore(db, existing_only=True)) as store:
                if route in {"maintenance", "maintenance-draft", "maintenance-finish"}:
                    return self.maintenance_operation(route, payload, store, token, session)
                if route == "diagnostics":
                    fields(payload, {"project"})
                    selected = payload.get("project")
                    project_id = selected if isinstance(selected, str) and selected in store.management_projects() else None
                    report = diagnostics.runtime_snapshot(store, project_id)
                    return '<h2>记忆诊断</h2><p class="card">' + view.e(diagnostics.explain_runtime(report)) + '</p>'
                store.archive_due_memories()
                if route == "list":
                    return self.listing(payload, store)
                if route == "detail":
                    return self.detail(payload, store, token)
                if route == "action":
                    return self.plan(payload, store, token, session)
                if route in {"confirm", "cancel"}:
                    return self.confirm(payload, store, token, session, cancel=route == "cancel")
            raise PanelError("面板地址不存在。", HTTPStatus.NOT_FOUND)


def local_directory(directory: Path) -> bool:
    if not directory.is_absolute():
        return False
    if os.name == "nt":
        import ctypes

        return len(directory.drive) == 2 and ctypes.windll.kernel32.GetDriveTypeW(directory.anchor) in (2, 3, 6)
    return not str(directory).startswith("//")


def make_handler(data_dir: Path, launch_key: str) -> type[BaseHTTPRequestHandler]:
    panel = Panel(data_dir, launch_key)
    resources = {
        "/": ("text/html", view.SHELL),
        "/panel.css": ("text/css", view.CSS),
        "/panel.js": ("text/javascript", view.SCRIPT),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "March7Panel/1.0"

        @property
        def origin(self) -> str:
            return f"http://127.0.0.1:{self.server.server_address[1]}"

        def address_allowed(self) -> bool:
            return self.headers.get_all("Host") == [self.origin.removeprefix("http://")]

        def _send(self, status: HTTPStatus, content: str | dict[str, Any], mime: str = "text/plain") -> None:
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False)
                mime = "application/json"
            body = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{mime}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; "
                             "script-src 'self'; style-src 'self'; connect-src 'self'; "
                             "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def read_post_body(self) -> bytes | None:
            """只收有界的请求体，拒绝时也能先收完再返回状态码。"""
            try:
                raw_length = self.headers.get_all("Content-Length")
                if raw_length is None or len(raw_length) != 1:
                    return None
                size = int(raw_length[0])
                if not 0 < size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                    return None
                self.connection.settimeout(5)
                body = self.rfile.read(size)
                return body if len(body) == size else None
            except (ValueError, OSError):
                return None

        def read_json(self, body: bytes | None) -> dict[str, Any]:
            if body is None or self.headers.get_content_type() != "application/json":
                raise PanelError("请求格式无效。")
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    return payload
            except (ValueError, RecursionError) as exc:
                raise PanelError("请求格式无效。") from exc
            raise PanelError("请求格式无效。")

        def do_GET(self) -> None:
            if not self.address_allowed():
                self._send(HTTPStatus.FORBIDDEN, "面板只接受本机回环地址。")
                return
            if self.path == "/__panel":
                auth = self.headers.get("Authorization", "").encode("latin1")
                own = hmac.compare_digest(auth, f"Bearer {panel.launch_key}".encode("ascii"))
                if own and panel.same_database():
                    self._send(HTTPStatus.OK, {
                        "ok": True, "data_dir": str(panel.directory), "pid": os.getpid(), "writable": True,
                    })
                elif own:
                    self._send(HTTPStatus.CONFLICT, "这个实例的记忆库已更换。")
                else:
                    self._send(HTTPStatus.FORBIDDEN, "本机启动凭证无效。")
                return
            resource = resources.get(self.path)
            if resource is None:
                self._send(HTTPStatus.NOT_FOUND, "面板地址不存在。")
                return
            self._send(HTTPStatus.OK, resource[1], resource[0])

        def do_POST(self) -> None:
            try:
                # Windows 上先读完有界请求体，避免错误响应还没送达就关闭连接。
                body = self.read_post_body()
                if not self.address_allowed():
                    raise PanelError("面板只接受本机回环地址。", HTTPStatus.FORBIDDEN)
                auth = self.headers.get("Authorization", "")
                if self.path == "/__launch":
                    if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site"):
                        raise PanelError("此地址仅供本机启动器访问。", HTTPStatus.FORBIDDEN)
                    if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                        self.read_json(body)
                    if not panel.same_database():
                        raise PanelError("这个实例的记忆库已更换。", HTTPStatus.CONFLICT)
                    with panel.lock:
                        response = panel.issue(auth)
                elif self.path.startswith("/api/"):
                    if self.headers.get_all("Origin") != [self.origin]:
                        raise PanelError("请求来源与本机面板不一致。", HTTPStatus.FORBIDDEN)
                    response = panel.dispatch(self.path.removeprefix("/api/"), self.read_json(body), auth)
                else:
                    raise PanelError("面板地址不存在。", HTTPStatus.NOT_FOUND)
            except PanelError as exc:
                self._send(exc.status, str(exc))
                return
            except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
                logger.error("本机面板操作失败（%s）", type(exc).__name__)
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, "这次操作未能完整确认。请先核对列表与擦除回执，再决定是否重试。")
                return
            self._send(HTTPStatus.OK, response, "text/html")

        def log_message(self, fmt: str, *args: object) -> None:
            """访问日志不记录请求正文、授权标识或查询内容。"""

    return Handler
