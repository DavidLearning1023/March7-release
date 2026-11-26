"""receipt.py — 注入回执（内部代号：**凭条**）

用户问「这条为什么被注入」时，答案必须是一个**可以对着库查**的东西，而不是
"系统觉得相关"。所以每次注入都留一张凭条：哪几条进了、为什么进、哪几条没进、
为什么没进。

## 凭条与账本的分工

- **账本**（`ledger.decisions-*.jsonl`）是**流水**：每轮一行，回答"这一轮发生了什么"；
- **凭条**（本模块的 `build_receipt`）是那一行里的一个字段：回答"具体是哪几条、为什么"。

凭条不单独落盘（否则就有两个真相源，漂一次就再也对不上）。它随账本行一起写，
读也随账本行一起读（`ledger.latest_receipt`）。

## 为什么理由是闭集而不是自由文本

`reason` 要能**分组统计**（"这个月有多少次注入是因为 global"）。自由文本一进来，
`GROUP BY reason` 立刻退化成一堆近似字符串，而看报告的人以为自己看到了分布。

⚠️ **凭条里不许出现路径**：`{id, scope, category, reason, kind}` 全是假名与枚举。
项目根只用于渲染（`privacy.redact_view`），不进凭条——`test_receipt_contains_no_paths`
钉住这件事。
"""

from __future__ import annotations

from typing import Any

from March7.injection import (
    CATEGORY_LABELS,
    InjectionRender,
    category_of,
)

REASON_SCOPE_GLOBAL = "scope_global"
"""进了：它是 `global` 记忆（跨项目事实，任何项目都该看到）。"""

REASON_SCOPE_PROJECT_MATCH = "scope_project_match"
"""进了：它的 `scope` 与本轮 `project_id` 匹配。"""

REASON_SCOPE_UNSCOPED = "scope_unscoped"
"""进了，但**没有查到 scope**（替身 / 老调用方 / 无 scope 字段）。

为什么单列一个理由而不是并进 `scope_global`：这两种情况的**可信度不同**。
`scope_global` 是"查过了，是全局事实"；`scope_unscoped` 是"没查，按老路径放过"。
混成一个会让审计时以为作用域过滤覆盖到了老路径——而它并没有。
"""

RECEIPT_INJECT_REASONS = (
    REASON_SCOPE_GLOBAL,
    REASON_SCOPE_PROJECT_MATCH,
    REASON_SCOPE_UNSCOPED,
)
"""注入理由的**闭集**（`test_injected_reason_closed_set` 钉住）。"""


def _working_entries(render: InjectionRender) -> list[dict[str, Any]]:
    """渲染里真正注入的 working 条目（带 `w<seq>` 形式的 id，与 `working_ids` 对齐）。"""
    entries: list[dict[str, Any]] = []
    ids = list(render.working_ids)
    for index, item in enumerate(render.working_items):
        if index < len(ids):
            entries.append({"id": ids[index], "kind": str(item.get("kind") or "")})
    return entries


def _inject_reason(mem: Any, categories: dict[str, str] | None) -> str:
    """这条记忆为什么进得来（只看 scope；category 不影响可见性）。"""
    scope = (getattr(mem, "scope", "") or "").strip()
    if not scope:
        return REASON_SCOPE_UNSCOPED
    if scope == "global":
        return REASON_SCOPE_GLOBAL
    return REASON_SCOPE_PROJECT_MATCH


def build_receipt(
    render: InjectionRender,
    *,
    project_id: str | None,
    prompt_chars: int,
    categories: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把一次注入的渲染结果压成一张凭条（可直接塞进账本行）。

    Args:
        render: `injection.format_injection` 的产物。
        project_id: 本轮项目假名（`None` = 没有项目身份）。
        prompt_chars: 本轮 prompt 字数（诊断用；**不含原文**）。
        categories: `{memory_id: category}`（来自 `memory_meta`），用于把类别也记进凭条。

    Returns:
        `{injected, omitted, working, chars, project_id, prompt_chars}`
        - `injected`: `[{id, scope, category, reason}]`，顺序与渲染一致；
        - `omitted`: `[{id, reason}]`，原因来自 `injection.RECALL_OMIT_REASONS`；
        - `working`: `[{id, kind}]`（working 没有 scope/category 概念）。

    ⚠️ 逐项都只有假名与枚举：**一个路径、一段原文都不进来**。
    """
    by_id: dict[str, Any] = {}
    for mem in render.memories:
        mem_id = str(getattr(mem, "id", "") or "")
        if mem_id:
            by_id[mem_id] = mem

    injected: list[dict[str, Any]] = []
    for mem_id in render.recall.injected_ids:
        mem = by_id.get(mem_id)
        injected.append(
            {
                "id": mem_id,
                "scope": (getattr(mem, "scope", "") or "") if mem is not None else "",
                "category": category_of(mem, categories) if mem is not None else None,
                "reason": _inject_reason(mem, categories) if mem is not None else REASON_SCOPE_UNSCOPED,
            }
        )
    omitted = [
        {"id": mem_id, "reason": render.recall.omitted_reasons.get(mem_id, "")}
        for mem_id in render.recall.omitted_ids
    ]
    working = [
        {
            "id": item.get("id", ""),
            "kind": str(item.get("kind") or ""),
        }
        for item in _working_entries(render)
    ]
    return {
        "injected": injected,
        "omitted": omitted,
        "working": working,
        "chars": len(render.text),
        "project_id": project_id,
        "prompt_chars": int(prompt_chars),
    }


def summarize(built: dict[str, Any] | None) -> str:
    """把凭条压成一行中文（`March7_status` / `doctor` 直接打印）。

    例：`本轮注入 2 条（画像 1 / 项目 1），省略 1 条，共 412 字`。
    空凭条 ⇒ `"本轮没有注入记忆"`（不是"无数据"——那会被误读成读不到库）。
    """
    if not built:
        return "本轮没有注入记忆"
    injected = list(built.get("injected") or [])
    if not injected:
        omitted_n = len(built.get("omitted") or [])
        return f"本轮没有注入记忆（候选里省略 {omitted_n} 条）"
    counts: dict[str, int] = {}
    for item in injected:
        label = CATEGORY_LABELS.get(str(item.get("category") or ""), "未分类")
        counts[label] = counts.get(label, 0) + 1
    breakdown = " / ".join(f"{label} {n}" for label, n in counts.items())
    return (
        f"本轮注入 {len(injected)} 条（{breakdown}），"
        f"省略 {len(built.get('omitted') or [])} 条，共 {int(built.get('chars') or 0)} 字"
    )


def status_receipt_field(data_dir: Any) -> dict[str, Any] | None:
    """读今天最近一条凭条（`March7_status` 的可查入口）。

    单独包一层而不让 server 直接碰 `ledger`：server 只该知道"有一个回执可查"，
    不该知道账本按天分文件的细节（那是 `ledger` 的内部结构）。
    """
    from March7 import ledger

    return ledger.latest_receipt(data_dir)


__all__ = [
    "REASON_SCOPE_GLOBAL",
    "REASON_SCOPE_PROJECT_MATCH",
    "REASON_SCOPE_UNSCOPED",
    "RECEIPT_INJECT_REASONS",
    "build_receipt",
    "status_receipt_field",
    "summarize",
]
