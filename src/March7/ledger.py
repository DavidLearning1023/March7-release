"""ledger.py — 写入账本（一次写入管线一行，JSONL 追加写）。

为什么是 JSONL 而不是一张表：
- 账本是**日志**，不是记忆——不参与召回、不进 `March7_export`、也不受「永不物理删除」承诺约束；
- 追加写不需要事务，写坏了最多丢一行（记忆本体已经入库，账本只影响可观测性）；
- 形态与本地资产库的 `cost-log.jsonl`（1,771 行的实践）同源，跨项目读法一致。

为什么现在需要它（设计复盘 P1-2/P1-3）：
`MARCH7_DAILY_WRITE_CAP` 一旦生效，「今天写了几条 / 有没有被熔断」必须查得到；
否则一个静默的熔断等于把记忆功能关掉了而没人知道。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LEDGER_DIRNAME = "logs"
"""账本目录（与钩子日志同处 `<data_dir>/logs/`，一次排查只看一个目录）。"""

DECISIONS_PREFIX = "decisions"
"""召回决策账本的文件前缀：`decisions-YYYYMMDD.jsonl`。

**为什么与 `writes-*` 隔离**（S1）：两类记录的分母不同。
写入账本的分母是「处理了几轮 Stop」，召回账本的分母是「发生了几次 UPS 召回」——
混在同一个文件里，`runs` 与 `attempts` 会互相污染，`aggregate()` 也会把
召回行当成写入行加总（`written` 恒 0，看起来像"跑了很多轮但一条没写"）。
"""

RECALL_INJECTED = "injected"
RECALL_NO_HIT = "no_hit"
RECALL_ALL_OMITTED = "all_omitted"
RECALL_ALL_FILTERED = "all_filtered"

RECALL_OUTCOMES = (RECALL_INJECTED, RECALL_NO_HIT, RECALL_ALL_OMITTED, RECALL_ALL_FILTERED)
"""召回一轮的**四事件**，互斥且完备（`ADR-0037`）。

| 事件 | 判据 | 含义 |
|---|---|---|
| `no_hit` | 候选 = 0 | 检索没命中（不是"命中但没用"） |
| `all_filtered` | 候选 > 0，状态过滤后 = 0 | 命中的都被 archived/superseded/merged 挡掉 |
| `all_omitted` | 过滤后 > 0，注入文本 = 空 | 有候选但全被预算/空内容吃掉 |
| `injected` | 注入文本非空 | 正常注入 |

**为什么必须四个而不是"注入/没注入"两个**：两个事件下，「检索没命中」与
「命中了但全被过滤」在日志里长得一模一样 —— 而这两种情况的修法完全相反
（前者要改检索，后者要改状态治理）。
"""


def decisions_path(data_dir: Path, when: float | None = None, *, prefix: str = DECISIONS_PREFIX) -> Path:
    """当天的召回决策账本：`<data_dir>/logs/decisions-YYYYMMDD.jsonl`。"""
    stamp = time.strftime("%Y%m%d", time.localtime(when if when is not None else time.time()))
    return Path(data_dir) / LEDGER_DIRNAME / f"{prefix}-{stamp}.jsonl"


def append_decision(
    record: dict[str, Any], data_dir: Path, when: float | None = None, *, prefix: str = DECISIONS_PREFIX,
) -> None:
    """追加一行召回决策。**失败不抛**（记账不该影响注入），但记 WARNING。

    ⚠️ 只写 `decisions-*`，绝不落到 `writes-*`（见 `DECISIONS_PREFIX` 的算理）。
    """
    try:
        path = decisions_path(data_dir, when, prefix=prefix)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": int(when if when is not None else time.time()), **record}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 —— 记账失败必须降级，不得影响注入
        logger.warning("召回账本写入失败（不影响本轮注入）：%s", exc)


def read_decisions(
    data_dir: Path, when: float | None = None, *, prefix: str = DECISIONS_PREFIX,
) -> list[dict[str, Any]]:
    """读当天召回决策账本（坏行跳过）。文件不存在 ⇒ 空列表。"""
    path = decisions_path(data_dir, when, prefix=prefix)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("读召回账本失败（按空处理）：%s", exc)
        return []
    return rows


def classify_recall(candidates: int, filtered: int, has_text: bool) -> str:
    """按「候选 / 过滤后 / 有无文本」判四事件之一（**唯一判定处**，钩子与审计脚本共用）。

    Args:
        candidates: 检索命中的条数（过滤前）。
        filtered: 状态过滤后剩下的条数。
        has_text: 渲染出的注入文本是否非空。

    Returns:
        `RECALL_OUTCOMES` 里的一个。
    """
    if candidates <= 0:
        return RECALL_NO_HIT
    if filtered <= 0:
        return RECALL_ALL_FILTERED
    if not has_text:
        return RECALL_ALL_OMITTED
    return RECALL_INJECTED


def latest_receipt(data_dir: Path, when: float | None = None) -> dict[str, Any] | None:
    """取当天**最近一条带凭条**的注入回执（S3 的 A4「可查」入口）。

    为什么是「最近一条带凭条的」而不是「最后一行的凭条」：
    升级当天账本里会同时存在**新老两种行**（老行没有 `receipt` 字段），
    而钩子也可能在某些路径上只写四事件、不写凭条。直接取最后一行的话，
    用户问「刚才那条为什么被注入」时得到的答案是 `None`——看起来像"回执功能没生效"。

    Returns:
        `{injected, omitted, working, chars, project_id, prompt_chars, ...}`（账本行的
        `receipt` 字段原样返回）；一条都没有 ⇒ `None`（**不编空凭条**）。

    ⚠️ 只读当天文件：跨天追问"上周那轮"应当查历史账本，不该让本函数扫全目录
    （账本是按天 append 的，扫全目录在长期使用后是几百个文件）。
    """
    for row in reversed(read_decisions(data_dir, when)):
        payload = row.get("receipt")
        if isinstance(payload, dict):
            return payload
    return None


def outcome_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    """把多行决策账本按四事件加总（未知事件名计入 `unknown`，**不静默丢**）。"""
    counts: dict[str, int] = {name: 0 for name in RECALL_OUTCOMES}
    counts["unknown"] = 0
    counts["total"] = 0
    for row in records:
        outcome = str(row.get("outcome") or "")
        counts["total"] += 1
        if outcome in counts:
            counts[outcome] += 1
        else:
            counts["unknown"] += 1
    return counts


def verify_outcomes(records: list[dict[str, Any]]) -> list[str]:
    """逐行检查「事件名与明细自洽」；返回问题列表（空 = 全部自洽）。

    这是 A2 里「三态互斥」的机械判据：不是看事件名对不对，而是看**事件名与那一行的
    明细是否互相矛盾**（例如标了 `injected` 却没有注入任何 id）。
    """
    problems: list[str] = []
    for index, row in enumerate(records):
        outcome = str(row.get("outcome") or "")
        candidates = int(row.get("candidates") or 0)
        injected = list(row.get("injected") or [])
        omitted = list(row.get("omitted") or [])
        filtered = set(row.get("filtered") or []) | set(row.get("out_of_scope") or [])
        low_relevance = int(row.get("low_relevance_count") or 0)
        chars = int(row.get("chars") or 0)
        where = f"row{index}({outcome or '空事件'})"
        if outcome not in RECALL_OUTCOMES:
            problems.append(f"{where}: 事件名不在四事件内")
            continue
        if set(injected) & set(omitted):
            problems.append(f"{where}: 同一条即注入又省略")
        if len(injected) + len(omitted) + len(filtered) + low_relevance != candidates:
            problems.append(
                f"{where}: 明细条数 {len(injected)}+{len(omitted)}+{len(filtered)}+{low_relevance} ≠ 候选 {candidates}"
            )
        if outcome == RECALL_INJECTED and not (injected and chars > 0):
            problems.append(f"{where}: 标了 injected 却没有注入内容")
        if outcome == RECALL_ALL_OMITTED and (injected or chars > 0):
            problems.append(f"{where}: 标了 all_omitted 却仍有注入内容")
        if outcome == RECALL_ALL_FILTERED and (injected or omitted or candidates <= 0):
            problems.append(f"{where}: 标了 all_filtered 却有注入/省略明细或候选为 0")
        if outcome == RECALL_NO_HIT and candidates != 0:
            problems.append(f"{where}: 标了 no_hit 却有候选")
    return problems


def reconcile(records: list[dict[str, Any]], attempts: int) -> dict[str, Any]:
    """四事件之和 vs 独立分母（`attempts`，来自 `ingest_marks`）的对账结果。

    为什么分母要独立：用账本自己的行数当分母，等于「自己证明自己」——
    钩子根本没触发时，账本里一行都没有，等式 0 = 0 依然"成立"。
    分母取 `ingest_marks`（宿主侧真正处理过的用户消息数）才能暴露「丢了事件」。

    Returns:
        `{counts, total, attempts, gap, ok, problems}`；`ok` = 差额为 0 且无自洽问题。
    """
    counts = outcome_counts(records)
    problems = verify_outcomes(records)
    total = counts["total"]
    gap = int(attempts) - total
    return {
        "counts": counts,
        "total": total,
        "attempts": int(attempts),
        "gap": gap,
        "ok": gap == 0 and counts["unknown"] == 0 and not problems,
        "problems": problems,
    }


def ledger_path(data_dir: Path, when: float | None = None) -> Path:
    """当天的账本路径：`<data_dir>/logs/writes-YYYYMMDD.jsonl`。"""
    stamp = time.strftime("%Y%m%d", time.localtime(when if when is not None else time.time()))
    return Path(data_dir) / LEDGER_DIRNAME / f"writes-{stamp}.jsonl"


def append(record: dict[str, Any], data_dir: Path, when: float | None = None) -> None:
    """追加一行账本。**失败不抛**（记账不该影响写入主流程），但记 WARNING。"""
    try:
        path = ledger_path(data_dir, when)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": int(when if when is not None else time.time()), **record}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 —— 记账失败必须降级，不得影响主流程
        logger.warning("写入账本失败（不影响本次写入）：%s", exc)


def read_day(data_dir: Path, when: float | None = None) -> list[dict[str, Any]]:
    """读当天账本（坏行跳过）。文件不存在 ⇒ 空列表。"""
    path = ledger_path(data_dir, when)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("读账本失败（按空处理）：%s", exc)
        return []
    return rows


def aggregate(records: list[dict[str, Any]]) -> dict[str, int]:
    """把一天的多行账本加总（`doctor` 用）。

    ⚠️ **跳过事件行**（带 `event` 字段的行，例如 `codex_cwd_drift`）：
    它们不是"一次写入管线"，把它们算进 `runs` 会让"今天跑了几轮"虚高，
    而那个数字被 `doctor` 用来判断"钩子还在不在工作"（`ADR-0067` 决策 2）。
    """
    total: dict[str, int] = {
        "runs": 0,
        "written": 0,
        "deduped": 0,
        "rejected": 0,
        "capped": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    for row in records:
        if str(row.get("event") or "").strip():
            continue
        total["runs"] += 1
        total["written"] += int(row.get("written") or 0)
        total["deduped"] += int(row.get("deduped") or 0)
        total["rejected"] += int(row.get("rejected") or 0)
        total["capped"] += 1 if row.get("capped") else 0
        total["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
        total["completion_tokens"] += int(row.get("completion_tokens") or 0)
    return total
