"""S18 面板的本地模板和资源。来自库的字符串只经 HTML 转义进入页面。"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from March7 import maintenance
from March7.schemas import Memory
from March7.store import ManagementDetail, MemoryStore

PAGE_SIZE = 25
STATUSES = {"active": "有效", "aging": "较少使用", "archived": "已归档", "superseded": "已被纠正或取代", "merged": "已合并"}
ACTIONS = {"fix": "纠正", "archive": "归档", "restore": "恢复", "erase": "永久擦除"}
PROFILE_FIELDS = {
    "nickname": "昵称", "preferredName": "称呼", "occupation": "职业", "longTermInterests": "长期兴趣",
    "language": "语言", "permanentNote": "长期备注",
}


def e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{name}" value="{e(value)}">'


def fmt_time(value: int | None) -> str:
    if value is None:
        return "—"
    try:
        return datetime.fromtimestamp(value, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "—"


SHELL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>March7 记忆面板</title><link rel="stylesheet" href="/panel.css">
<script src="/panel.js" defer></script></head><body>
<header class="top"><div class="frame brand"><span class="mark" aria-hidden="true">M</span>
<div><h1>March7 记忆面板</h1><span class="quiet">本机记忆管理</span></div></div></header>
<main class="frame"><nav aria-label="记忆分类" id="navigation" hidden>
<button data-go="list" data-section="personal">个人记忆</button>
<button data-go="list" data-section="project">项目</button>
<button data-go="profile">画像</button>
<button data-go="list" data-section="history">历史</button>
<button data-go="maintenance">记忆体检</button>
<button data-go="receipts">擦除回执</button>
<button data-go="diagnostics">记忆诊断</button>
<button data-go="settings-view">设置与切换库</button>
</nav><div id="feedback" role="alert" hidden></div>
<section id="view" tabindex="-1"><h2>正在打开记忆库</h2>
<p>请从 March7 的桌面入口或宿主重新打开以建立本机会话。</p></section>
<noscript>本机管理会话需要浏览器启用 JavaScript。</noscript></main></body></html>"""


CSS = """
:root { color-scheme: light; font: 16px/1.55 "Microsoft YaHei UI", "PingFang SC", sans-serif;
        color: #212840; background: #f5f6fb; }
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; }
.frame { width: min(100% - 2rem, 65rem); margin-inline: auto; }
.top { background: #1f2443; color: white; }
.brand { display: flex; align-items: center; gap: .8rem; padding-block: 1.25rem; }
.brand .quiet { color: #dadcf0; }
.mark { display: grid; place-items: center; width: 2.65rem; height: 2.65rem;
        font-size: 1.2rem; font-weight: 750; border-radius: .75rem; background: #4c448b; }
h1, h2, h3, p { margin: 0; }
h1 { font-size: 1.22rem; }
h2 { font-size: 1.58rem; line-height: 1.3; margin: 1rem 0 .45rem; }
h3 { font-size: 1.05rem; margin-block: .35rem; }
p + p, p + form, form + form, section + section { margin-top: .8rem; }
main { padding-block: 1rem 3rem; }
nav, .actions, .pages { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem; }
nav { padding-bottom: .85rem; border-bottom: 1px solid #dcdfee; }
nav button { background: transparent; border-color: transparent; }
nav button[aria-current="page"] { background: #e8e5ff; color: #332878; }
button, input, select, textarea { font: inherit; color: inherit; }
button { padding: .46rem .75rem; border: 1px solid #c8cce2; border-radius: .55rem;
         background: #fff; cursor: pointer; }
button:hover { background: #ecebfd; }
button.primary { background: #433898; color: white; border-color: #433898; }
button.primary:hover { background: #322a78; }
button.danger { color: #9b243d; border-color: #e2aab6; }
button.danger:hover { background: #fbe9ed; }
button:disabled { opacity: .6; cursor: wait; }
:focus-visible { outline: 3px solid #6350da; outline-offset: 3px; }
#view:focus { outline: none; }
input:not([type=checkbox]), select, textarea { width: 100%; padding: .5rem .65rem; background: #fff;
        border: 1px solid #c8cce2; border-radius: .45rem; min-width: 0; }
textarea { resize: vertical; white-space: pre-wrap; }
label { display: block; font-weight: 600; }
label > input, label > select, label > textarea { margin-top: .25rem; }
.filters { display: grid; grid-template-columns: minmax(10rem, 2fr) repeat(2, minmax(7rem, 1fr)) auto;
           align-items: end; gap: .65rem; margin-block: 1rem; }
.card { background: white; border: 1px solid #dce0ee; border-radius: .8rem; padding: 1rem; }
.results { display: grid; gap: .65rem; margin-block: .7rem; }
.result { display: flex; align-items: start; justify-content: space-between; gap: .75rem; }
.result button { flex: none; }
.quiet { color: #555d75; font-size: .9rem; }
.content, .quote, code, dd, .library { overflow-wrap: anywhere; word-break: normal; }
.content, .quote { white-space: pre-wrap; }
.quote { border-left: .2rem solid #aaa0e4; background: #f7f6ff; padding: .7rem .85rem; margin: .4rem 0; }
.pill { display: inline-block; color: #3a2c80; background: #ebe8ff; padding: .13rem .55rem;
        border-radius: 1rem; margin-right: .45rem; }
.detail { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: .3rem .9rem; }
dt { color: #555d75; } dd { margin: 0; }
.actions { margin-top: .9rem; }
.actions form { margin: 0; }
.warning, #feedback { background: #fff0f2; color: #84213a; border-radius: .55rem; padding: .65rem .85rem; }
#feedback { margin-top: .65rem; }
.stack { display: grid; gap: .8rem; max-width: 40rem; }
.stack form { display: grid; justify-items: start; gap: .6rem; }
.stack label { width: 100%; }
@media (max-width: 620px) {
  .frame { width: calc(100% - 1.4rem); }
  .filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .filters label:first-child { grid-column: 1 / -1; }
  .result { flex-direction: column; }
  .detail { grid-template-columns: minmax(0, 1fr); }
  .detail dd { margin-bottom: .4rem; }
}
@media (max-width: 330px) { .filters { grid-template-columns: minmax(0, 1fr); } }
"""


SCRIPT = """
'use strict';
const view = document.querySelector('#view');
const feedback = document.querySelector('#feedback');
const navigation = document.querySelector('#navigation');
let token = sessionStorage.getItem('March7-panel') || '';
let listSelection = {};
let detailSelection = {};
let busy = false;

async function request(route, fields) {
  return fetch('/api/' + route, {
    method: 'POST', cache: 'no-store', credentials: 'omit',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    body: JSON.stringify(fields),
  });
}

function locked() {
  token = '';
  sessionStorage.removeItem('March7-panel');
  navigation.hidden = true;
  view.replaceChildren();
  const title = document.createElement('h2');
  title.textContent = '请重新打开记忆面板';
  const paragraph = document.createElement('p');
  paragraph.textContent = '请使用 March7 的桌面入口，或在宿主里说「打开 March7 记忆面板」。';
  view.append(title, paragraph);
  view.focus();
}

function error(message) {
  feedback.textContent = message;
  feedback.hidden = false;
}

async function show(route, fields = {}) {
  if (busy) return;
  busy = true;
  feedback.hidden = true;
  view.setAttribute('aria-busy', 'true');
  try {
    const response = await request(route, fields);
    if (response.ok && route === 'switch') {
      const {url} = await response.json();
      const target = new URL(url);
      if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1' || !target.port ||
          target.username || target.password || target.pathname !== '/' || target.search ||
          !/^#[A-Za-z0-9_-]{43}$/.test(target.hash)) {
        throw new Error('Invalid local panel URL');
      }
      location.assign(target.href);
      return;
    }
    const body = await response.text();
    if (!response.ok) {
      if (response.status === 403) locked();
      error(body);
      return;
    }
    if (route === 'list') listSelection = fields;
    if (route === 'detail') detailSelection = fields;
    view.innerHTML = body;
    navigation.hidden = false;
    const current = route === 'list' ? (fields.section || 'personal') : route;
    if (['personal', 'project', 'history', 'profile', 'receipts', 'diagnostics', 'maintenance', 'settings-view'].includes(current)) {
      for (const button of navigation.querySelectorAll('button')) {
        if ((button.dataset.section || button.dataset.go) === current) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
      }
    }
    view.focus({preventScroll: true});
    window.scrollTo(0, 0);
  } catch {
    error('面板连接中断。请检查当前库，再决定是否重试。');
  } finally {
    busy = false;
    view.removeAttribute('aria-busy');
  }
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-go]');
  if (!button) return;
  const {go, section, id, project, page} = button.dataset;
  if (go === 'list') show(go, {section, project: listSelection.project || ''});
  else if (go === 'back') show('list', listSelection);
  else if (go === 'previous-detail') show('detail', detailSelection);
  else if (go === 'page') show('list', {...listSelection, page});
  else if (go === 'detail') show(go, {id, section, project});
  else if (go === 'diagnostics') show(go, {project: listSelection.project || ''});
  else if (go === 'maintenance') show(go, {project: project ?? listSelection.project ?? '', page: page || '1'});
  else show(go);
});

document.addEventListener('submit', (event) => {
  const form = event.target;
  if (!form.dataset.api) return;
  event.preventDefault();
  if (busy) return;
  const button = event.submitter;
  const fields = Object.fromEntries(new FormData(form, button));
  if (form.dataset.api === 'settings') {
    fields.fix = fields.fix === 'on';
    fields.erase = fields.erase === 'on';
  }
  if (button) button.disabled = true;
  show(form.dataset.api, fields).finally(() => { if (button) button.disabled = false; });
});

async function bootstrap() {
  const ticket = location.hash.slice(1);
  if (ticket) {
    history.replaceState(null, '', '/');
    try {
      const response = await request('session', {ticket});
      if (response.ok) {
        token = (await response.json()).token;
        sessionStorage.setItem('March7-panel', token);
      } else error('本机打开凭证已使用或过期。请重新打开。');
    } catch {
      error('连接不到本机面板。请重新打开。');
    }
  }
  if (token) show('list');
  else locked();
}

window.addEventListener('hashchange', bootstrap);
bootstrap();
"""


def panel_list(
    data_dir: Path, section: str, project: str, projects: list[str], labels: dict[str, str],
    search: str, status: str, page: int, total: int, rows: list[Memory],
) -> str:
    title = {"personal": "个人记忆", "project": "项目记忆", "history": "历史"}[section]
    options = ['<option value="">个人记忆</option>' if section == "history" else
               '<option value="">请选择项目</option>']
    for item in projects:
        selected = " selected" if item == project else ""
        label = f"{labels[item]} · {item}" if labels.get(item) else item
        options.append(f'<option value="{e(item)}"{selected}>{e(label)}</option>')
    project_input = (
        f'<label>项目范围<select name="project">{"".join(options)}</select></label>'
        if section != "personal" else hidden("project", "")
    )
    status_options = ['<option value="">全部状态</option>']
    for value, label in STATUSES.items():
        if (value in {"active", "aging"}) == (section != "history"):
            selected = " selected" if value == status else ""
            status_options.append(f'<option value="{value}"{selected}>{label}</option>')
    parts = [
        f'<h2>{title}</h2><p class="quiet library">当前库 · {e(data_dir)}</p>',
        f'<form class="filters" data-api="list">{hidden("section", section)}'
        f'<label>搜索<input name="search" value="{e(search)}" maxlength="300" '
        'placeholder="正文、原话或记忆 ID"></label>'
        f'{project_input}<label>状态<select name="status">{"".join(status_options)}</select></label>'
        '<button class="primary">应用筛选</button></form>',
    ]
    if section == "project" and not project:
        parts.append('<p class="card">选择项目后，点「应用筛选」。</p>')
        return "".join(parts)
    parts.append(f'<p class="quiet">共 {total} 条 · 第 {page} 页</p><div class="results">')
    if not rows:
        parts.append('<p class="card">当前条件下没有记忆。</p>')
    for row in rows:
        text = row.content if len(row.content) <= 260 else row.content[:260] + "…"
        parts.append(
            '<article class="card result"><div>'
            f'<span class="pill">{STATUSES.get(row.status.value, e(row.status.value))}</span>'
            f'<span class="quiet">{e(fmt_time(row.created_at))} · {e(row.host or "宿主未记录")}</span>'
            f'<p class="content">{e(text)}</p></div>'
            f'<button data-go="detail" data-id="{e(row.id)}" data-section="{section}" '
            f'data-project="{e(project)}" aria-label="查看记忆 {e(row.id)}">查看详情</button></article>'
        )
    parts.append('</div><div class="pages">')
    if page > 1:
        parts.append(f'<button data-go="page" data-page="{page - 1}">上一页</button>')
    if page * PAGE_SIZE < total:
        parts.append(f'<button data-go="page" data-page="{page + 1}">下一页</button>')
    parts.append('</div>')
    return "".join(parts)


def maintenance_view(
    store: MemoryStore, report: maintenance.Report, batches: tuple[maintenance.Batch, ...],
    project: str, projects: list[str], labels: dict[str, str], page: int,
    *, drafts: tuple[maintenance.Batch, ...] = (),
) -> str:
    label = "当前项目" if project else "个人"
    options = ['<option value="">个人记忆</option>']
    for item in projects:
        selected = " selected" if project == item else ""
        name = f"{labels[item]} · {item}" if labels.get(item) else item
        options.append(f'<option value="{e(item)}"{selected}>{e(name)}</option>')
    parts = [
        '<h2>记忆体检</h2>',
        f'<p class="quiet library">当前库 · {e(store.data_dir)}</p>',
        '<form class="filters" data-api="maintenance">'
        f'<label>检查范围<select name="project">{"".join(options)}</select></label>'
        '<button class="primary">检查</button></form>',
        f'<p>共 {len(report.findings)} 组 · 本次检查 {report.examined} 条 · 自动体检{"开启" if report.auto else "关闭"}</p>',
    ]
    if drafts:
        parts.append('<h3>待确认草稿</h3>')
    for draft in drafts:
        action = "撤销整理" if draft.kind == "undo" else "整理重复"
        parts.append(
            f'<article class="card"><p>{action} · <code>{e(draft.id)}</code></p>'
            + maintenance_draft_button(project, "batch", draft.id, "核对草稿") + '</article>'
        )
    def record(mid: str) -> str:
        detail = store.management_detail(mid)
        if detail is None or detail.memory.scope != report.scope:
            return ""
        memory = detail.memory
        status = memory.status.value
        section = ("project" if project else "personal") if status in {"active", "aging"} else "history"
        return (
            f'<li data-status="{e(status)}">{e(memory.content[:160])} · {e(STATUSES[status])} '
            f'<button data-go="detail" data-section="{section}" data-project="{e(project)}" data-id="{e(mid)}">'
            f'<code>{e(mid)}</code></button></li>'
        )

    page_size = 8
    start = (page - 1) * page_size
    for finding in report.findings[start:start + page_size]:
        rows = "".join(map(record, finding.ids))
        parts.append(
            f'<article class="card"><h3>{e(maintenance.FINDING_NAMES[finding.kind])}</h3>'
            f'<p>{e(finding.reason)}</p><ul>{rows}</ul>'
        )
        if finding.kind == "duplicate":
            parts.append(maintenance_draft_button(project, "group", finding.key, "预览这组"))
            parts.append(f'<p>在宿主发起预览 <code>预览{label}记忆整理 {finding.key}</code></p>')
        parts.append('</article>')
    for target, title in ((page - 1, "上一页"), (page + 1, "下一页")):
        if 1 <= target <= (len(report.findings) + page_size - 1) // page_size:
            parts.append(f'<button data-go="maintenance" data-project="{e(project)}" data-page="{target}">{title}</button>')
    parts.append('<h2>整理回执</h2>')
    if not batches:
        parts.append('<p>当前范围暂无整理回执。</p>')
    for batch in batches:
        title = "撤销整理" if batch.kind == "undo" else "已撤销" if batch.state == "undone" else "已整理"
        parts.append(
            f'<article class="card"><h3>{title} · {e(fmt_time(batch.applied_at))}</h3><p><code>{e(batch.id)}</code></p><ul>'
        )
        for target in batch.targets:
            parts.append(
                f'<li><code>{e(target.id)}</code> · {e(STATUSES[target.before])} → {e(STATUSES[target.after])}'
                f' <span class="quiet" data-status="{e(target.after)}"></span></li>'
            )
        parts.append('</ul>')
        if batch.kind in {"merge", "plan"} and batch.state == "applied":
            parts.append(maintenance_draft_button(project, "receipt", batch.id, "预览撤销"))
            parts.append(f'<p>在宿主发起撤销预览 <code>预览撤销{label}记忆整理 {e(batch.id)}</code></p>')
        elif batch.parent_id:
            parts.append(f'<p>原批次 <code>{e(batch.parent_id)}</code></p>')
        parts.append('</article>')
    return "".join(parts)


def maintenance_draft_button(project: str, key: str, value: str, label: str) -> str:
    return (
        f'<form data-api="maintenance-draft">{hidden("project", project)}{hidden(key, value)}'
        f'<button>{e(label)}</button></form>'
    )


def maintenance_draft(store: MemoryStore, batch: maintenance.Batch, project: str) -> str:
    title = "撤销整理预览" if batch.kind == "undo" else "记忆整理预览"
    parts = [
        f'<h2>{title}</h2><p class="quiet library">当前库 · {e(store.data_dir)} · {e(project or "个人")}</p>',
        f'<p><code>{e(batch.id)}</code></p><ul>',
    ]
    for target in batch.targets:
        parts.append(
            f'<li class="card"><code>{e(target.id)}</code> · {e(STATUSES[target.before])} → '
            f'{e(STATUSES[target.after])}'
        )
        detail = store.management_detail(target.id)
        if detail is not None and detail.memory.scope == batch.scope:
            quote = (detail.source or {}).get("quote") or detail.memory.source_quote
            parts.append(f'<p>{e(detail.memory.content)}</p><blockquote>{e(quote)}</blockquote>')
        parts.append('</li>')
    parts.append(
        f'</ul><form class="filters" data-api="maintenance-finish">{hidden("batch", batch.id)}'
        f'{hidden("project", project)}<button name="choice" value="confirm" class="primary">确认执行</button>'
        '<button name="choice" value="cancel">取消</button></form>'
    )
    return "".join(parts)


def maintenance_result(batch: maintenance.Batch, project: str) -> str:
    message = {
        "applied": "已撤销整理。" if batch.kind == "undo" else "已整理。",
        "cancelled": "已取消此批预览。",
        "stale": "预览已过期或记录已变化，请重新检查。",
        "undone": "这批已经撤销。",
    }[batch.state]
    return (
        f'<h2>整理结果</h2><p class="card">{message} <code>{e(batch.id)}</code></p>'
        f'<button data-go="maintenance" data-project="{e(project)}">返回记忆体检</button>'
    )


def panel_detail(
    item: ManagementDetail, selection: dict[str, str], snapshot: str,
    *, active: bool, archived: bool,
) -> str:
    mem = item.memory
    source = item.source or {}
    fields = {
        "记忆 ID": mem.id, "作用域": mem.scope, "状态": STATUSES.get(mem.status.value, mem.status.value),
        "来源宿主": mem.host or "未记录", "写入时间": fmt_time(mem.created_at),
        "来源会话": source.get("session_id"), "来源消息": source.get("message_id"),
        "来源版本": source.get("revision"), "适用期限": fmt_time(item.expires_at) if item.expires_at else "未设置",
    }
    if mem.trigger_text:
        fields["触发原话"] = mem.trigger_text
    metadata = "".join(f'<dt>{e(key)}</dt><dd>{e(value or "—")}</dd>' for key, value in fields.items())
    parts = [
        '<div class="actions"><button data-go="back">返回列表</button></div><h2>记忆详情</h2>',
        '<section class="card"><h3>记忆正文</h3>',
        f'<p class="content">{e(mem.content)}</p><h3>存库原话</h3>',
        f'<p class="quote">{e(mem.source_quote or "未记录")}</p>',
    ]
    if source.get("quote") and source["quote"] != mem.source_quote:
        parts.append(f'<h3>来源记录中的引文</h3><p class="quote">{e(source["quote"])}</p>')
    parts.append(f'<dl class="detail">{metadata}</dl></section>')
    fields_html = "".join(hidden(key, value) for key, value in selection.items()) + hidden("snapshot", snapshot)
    if active:
        parts.append(
            '<section class="card"><h3>纠正这条记忆</h3>'
            f'<form data-api="action">{fields_html}{hidden("action", "fix")}'
            '<label>用自己的话输入完整的新事实<textarea name="text" rows="4" '
            'maxlength="4000" required></textarea></label>'
            '<p class="quiet">旧条和它的原话会保留在历史中。</p>'
            '<div class="actions"><button class="primary">纠正记忆</button></div></form></section>'
        )
    actions = ["archive", "erase"] if active else ["restore", "erase"] if archived else ["erase"]
    parts.append('<div class="actions">')
    for action in actions:
        style = ' class="danger"' if action == "erase" else ""
        parts.append(
            f'<form data-api="action">{fields_html}{hidden("action", action)}{hidden("text", "")}'
            f'<button{style}>{ACTIONS[action]}这条记忆</button></form>'
        )
    parts.append('</div>')
    return "".join(parts)


def preview(action: str, memory: Memory, text: str, ticket: str) -> str:
    effect = {
        "fix": "旧条和存库原话会保留在历史中。",
        "archive": "归档后停止召回，可在历史中恢复。",
        "restore": "恢复后这条记忆重新参与召回，旧截止时间会清除。",
        "erase": "永久擦除无法撤销。面板会生成删除回执，列出验证结果和范围。",
    }[action]
    style = ' class="danger"' if action == "erase" else ' class="primary"'
    new = f'<h3>新事实</h3><p class="quote">{e(text)}</p>' if text else ""
    return (
        f'<h2>确认{ACTIONS[action]}</h2><section class="card">'
        f'<p class="quiet">{e(memory.scope)} · {e(memory.id)}</p>'
        f'<h3>目标记忆</h3><p class="quote">{e(memory.content)}</p>{new}<p>{effect}</p>'
        f'<div class="actions"><form data-api="confirm">{hidden("ticket", ticket)}'
        f'<button{style}>确认{ACTIONS[action]}</button></form>'
        f'<form data-api="cancel">{hidden("ticket", ticket)}<button>取消</button></form>'
        '</div></section>'
    )


def notice(message: str, action: str = "back") -> str:
    return (
        f'<h2>{e(message)}</h2><div class="actions">'
        f'<button data-go="{action}">{"返回记忆详情" if action == "previous-detail" else "返回列表"}</button></div>'
    )


def settings(data_dir: Path, reminders: dict[str, bool], *, saved: bool = False) -> str:
    fix = " checked" if reminders["fix"] else ""
    erase = " checked" if reminders["erase"] else ""
    status = '<p role="status">提醒设置已保存。</p>' if saved else ""
    return (
        f'<h2>设置与切换库</h2><p class="quiet library">当前库 · {e(data_dir)}</p>{status}'
        '<div class="stack"><section class="card"><h3>操作确认</h3>'
        '<p>关闭二次提醒后，点击对应操作会立即提交。</p>'
        f'<form data-api="settings"><label><input name="fix" type="checkbox"{fix}> 纠正前再次确认</label>'
        f'<label><input name="erase" type="checkbox"{erase}> 永久擦除前再次确认</label>'
        '<button class="primary">保存提醒设置</button></form></section>'
        '<section class="card"><h3>打开另一个记忆库</h3>'
        '<form data-api="switch"><label>本机数据目录的绝对路径<input name="directory" required '
        'maxlength="1024" autocomplete="off"></label><button>打开该库</button></form>'
        '<p class="quiet">宿主下次打开时仍使用其安装绑定的库。</p></section></div>'
    )


def profile_view(profile: dict[str, Any]) -> str:
    values = [(label, profile[key]) for key, label in PROFILE_FIELDS.items() if profile.get(key)]
    text = "".join(f'<dt>{e(label)}</dt><dd>{e(value)}</dd>' for label, value in values)
    body = f'<dl class="detail">{text}</dl>' if text else '<p>当前没有可用的画像字段。</p>'
    return '<h2>画像</h2><p class="quiet">此处显示目前可用于召回的画像。修改画像请用宿主的画像工具。</p>' + (
        f'<section class="card">{body}</section>'
    )


def receipt_view(receipt: dict[str, Any]) -> str:
    verification = receipt.get("verification", {})
    if verification.get("verdict") == "pending":
        return (
            '<section class="card"><h3>擦除待核验</h3>'
            f'<p><code>{e(receipt.get("receipt_id"))}</code></p>'
            f'<p>发起时间 · {e(receipt.get("deleted_at_text"))}</p>'
            '<p class="warning">擦除结果尚未确认，请检查列表和本机面板日志。</p></section>'
        )
    clean = verification.get("verdict") == "clean"
    verdict = "可验证范围内无残留" if clean else "发现残留或有项目未完成扫描"
    count = receipt.get("deleted", {}).get("memories", 0)
    limits = receipt.get("honest_limits", "")
    return (
        '<section class="card"><h3>删除回执</h3>'
        f'<p><code>{e(receipt.get("receipt_id"))}</code></p>'
        f'<p>{e(receipt.get("deleted_at_text"))} · 删除 {e(count)} 条</p>'
        f'<p class="{"" if clean else "warning"}">{verdict}</p>'
        f'<p class="quiet">自动核对 {e(verification.get("auto_surfaces", 0))} 处，'
        f'残留 {e(verification.get("auto_residue_total", 0))}，'
        f'扫描错误 {e(len(verification.get("scan_errors", [])))}。</p>'
        f'<p class="quiet">{e(limits)}</p></section>'
    )
