"""delete_receipt.py — 删除回执（内部代号：**焚稿回执**）

`ADR-0040` 决策 3：每次 `erase` 都要出一张**可查**的回执，回答四个问题——
**什么时候删的 / 删了什么范围 / 删了多少 / 验证结果**。

## 一条硬约束：回执里不许有原文

回执是**删除这件事的证明**，不是被删数据的副本。带原文的回执等于
「删了一份、又留了一份」，而且它比原库更危险：`receipts/` 是普通文件，
可以被打包、被同步、被截图。

所以回执只允许：

- **哈希**（`sha256` 前 16 位）：证明"我删的是这条"，又还原不出内容；
- **计数**（条数 / 行数 / 扫描数）；
- **枚举**（范围类型 `single` / `project` / `all`，验证结果 `clean` / `residual`）；
- **时间**（Unix 秒 + 人类可读）。

`test_receipt_contains_no_content` 用"把原文塞进 ctx 再断言回执里没有它"来钉这条。

## 为什么要单独一个文件、不塞进 `erase.py`

`erase.py` 的职责是**枚举派生面并扫描**（纯函数，无 IO 副作用）；
回执要**写文件**。混在一起会让"扫描"这个可单测的纯逻辑被迫拖上文件系统。
更重要的是：`erase.py` 会被钩子 import（要有毫秒级预算），
而回执只有用户点删除时才写——两者的性能约束差三个数量级。

## 文件名为什么是 `delete-<content_hash>.json` 而不是时间戳

`ADR-0040` 决策 1 写的是 `delete-<content_hash>.json`。用哈希做键的理由：
同一个范围被删两次（用户手滑点两次），第二张回执应当**覆盖**第一张
——它们说的是同一件事。用时间戳会留下两个文件，让人以为删了两批。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from March7.erase import ERASE_AUTO, HONESTY_CLAUSE

logger = logging.getLogger(__name__)

RECEIPTS_DIRNAME = "receipts"
"""回执目录：`<data_dir>/receipts/`（与 `logs/` 平级，不进导出）。"""

SCOPE_SINGLE = "single"
"""删一条。"""

SCOPE_PROJECT = "project"
"""按项目整批删。"""

SCOPE_SESSION = "session"
"""按会话整批删。"""

SCOPE_ALL = "all"
"""全库删除。"""

SCOPE_SELECTION = "selection"
"""由用户选择的多条记忆。"""

ERASE_SCOPES = (SCOPE_SINGLE, SCOPE_PROJECT, SCOPE_SESSION, SCOPE_ALL, SCOPE_SELECTION)
"""范围类型闭集（回执里只存这四个词之一）。"""

VERIFY_CLEAN = "clean"
VERIFY_RESIDUAL = "residual"

VERIFY_RESULTS = (VERIFY_CLEAN, VERIFY_RESIDUAL)
"""验证结果闭集。`clean` = 可自动测的面残留为 0；`residual` = 还有残留（要人看）。"""


def scope_hash(scope: str, targets: tuple[str, ...] | list[str]) -> str:
    """范围 → 稳定哈希（回执文件名与 `sealed` 字段用）。

    为什么对**范围**取哈希而不是对内容：同一批 id 再删一次应当落到同一张回执
    （见模块 docstring）。排序后拼接 ⇒ 与传入顺序无关。

    Args:
        scope: `ERASE_SCOPES` 之一。
        targets: 被删的 id / 项目假名 / 会话 id（**只用于算哈希，不回执里存明文**）。

    Returns:
        16 位 hex。
    """
    joined = "\x1f".join(sorted(str(t) for t in targets if t))
    return hashlib.sha256(f"{scope}\x1f{joined}".encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class DeleteReceipt:
    """一张删除回执（**只含哈希 / 时间 / 范围 / 条数 / 验证结果**）。

    Attributes:
        receipt_id: `delete-<scope_hash>`（文件名去掉 `.json`）。
        scope: `ERASE_SCOPES` 之一。
        target_hash: 范围哈希（`scope_hash()` 的结果；与 `receipt_id` 同源）。
        target_count: 范围内涉及的**标识**个数（几条记忆 / 几个项目）。
        deleted_at: Unix 秒。
        deleted: 各表的删除计数（`{memories, working, handoffs, ingest_marks}`）。
        verification: `erase.auto_residue()` 的产物（可自动测面的残留汇总）。
        audit: `erase.audit_deletion()` 的逐面结果（**只含 unit/计数/id 列表**）。
        honest_limits: 不可控面的说明（`ADR-0040` 决策 4 的诚实边界）。
    """

    receipt_id: str
    scope: str
    target_hash: str
    target_count: int
    deleted_at: int
    deleted: dict[str, int]
    verification: dict[str, Any]
    audit: list[dict[str, Any]]
    honest_limits: str

    @property
    def verified(self) -> bool:
        """验证是否通过（可自动测的面残留为 0）。"""
        return bool(self.verification.get("clean"))

    @property
    def verdict(self) -> str:
        """`clean` / `residual`（回执的 `verification.verdict` 同一口径）。"""
        return VERIFY_CLEAN if self.verified else VERIFY_RESIDUAL

    def as_dict(self) -> dict[str, Any]:
        """落盘形态（字段名就是用户会看见的字段名）。"""
        return {
            "receipt_id": self.receipt_id,
            "scope": self.scope,
            "target_hash": self.target_hash,
            "target_count": self.target_count,
            "deleted_at": self.deleted_at,
            "deleted_at_text": _format_ts(self.deleted_at),
            "deleted": self.deleted,
            "verification": {**self.verification, "verdict": self.verdict},
            "audit": self.audit,
            "honest_limits": self.honest_limits,
        }

    def summary(self) -> str:
        """压成三行中文（CLI 与 `March7_status` 直接打印）。

        刻意把 **verdict 放在第一行**：用户最想知道的是"删干净了吗"，
        而不是"删了几行"。
        """
        verdict_text = "可自动测的面残留 0" if self.verified else "**仍有残留，请查看 audit**"
        counts = " / ".join(f"{k} {v}" for k, v in self.deleted.items() if v)
        return (
            f"删除回执 {self.receipt_id}（{self.scope}，{self.deleted_at_text}）\n"
            f"  验证：{verdict_text}"
            f"（扫描 {self.verification.get('auto_rows_scanned', 0)} 行"
            f" / 共 {self.verification.get('auto_rows_total', 0)} 行）\n"
            f"  删除：{counts or '无行被删'}\n"
            f"  边界：{self.honest_limits}"
        )

    @property
    def deleted_at_text(self) -> str:
        """人类可读时间（本地时区）。"""
        return _format_ts(self.deleted_at)


def _format_ts(stamp: int) -> str:
    """Unix 秒 → `YYYY-MM-DD HH:MM:SS`（本地时区）。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(stamp)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def receipts_dir(data_dir: Path) -> Path:
    """回执目录路径（不创建）。"""
    return Path(data_dir) / RECEIPTS_DIRNAME


def build_receipt(
    *,
    scope: str,
    targets: tuple[str, ...] | list[str],
    deleted: dict[str, int],
    audit: list[dict[str, Any]],
    verification: dict[str, Any],
    when: int | None = None,
    honest_limits: str = HONESTY_CLAUSE,
) -> DeleteReceipt:
    """组装一张回执（**纯函数，不落盘**）。

    Args:
        scope: `ERASE_SCOPES` 之一。
        targets: 被删的标识（**只用于算哈希**，不会出现在回执里）。
        deleted: 各表删除计数。
        audit: `erase.audit_deletion()` 的结果。
        verification: `erase.auto_residue()` 的结果。
        when: Unix 秒；缺省 = 现在。

    Raises:
        ValueError: `scope` 不在闭集内。
    """
    if scope not in ERASE_SCOPES:
        raise ValueError(f"未知删除范围 {scope!r}（只允许 {list(ERASE_SCOPES)}）")
    digest = scope_hash(scope, targets)
    return DeleteReceipt(
        receipt_id=f"delete-{digest}",
        scope=scope,
        target_hash=digest,
        target_count=len([t for t in targets if t]),
        deleted_at=int(when if when is not None else time.time()),
        deleted=dict(deleted),
        verification=dict(verification),
        audit=audit,
        honest_limits=honest_limits,
    )


def write_receipt(receipt: DeleteReceipt, data_dir: Path) -> Path:
    """把回执写到 `<data_dir>/receipts/delete-<hash>.json`，返回路径。

    ⚠️ **写失败要响亮**（不像账本那样降级）：回执是"删除可证明"的唯一凭据，
    写不进去就等于这次删除**无法复核**。用户宁可这次删除失败重来，
    也不要"以为删了、事后查不到证据"。
    """
    directory = receipts_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{receipt.receipt_id}.json"
    _write_payload(path, receipt.as_dict())
    (directory / f"pending-{path.name}").unlink(missing_ok=True)
    logger.info("删除回执已写入 %s（verdict=%s）", path.name, receipt.verdict)
    return path


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    staged = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def prepare_single_receipt(data_dir: Path, memory_id: str) -> Path:
    """面板在删除前证明回执目录可写。中途故障保留待核验凭据。"""
    directory = receipts_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    digest = scope_hash(SCOPE_SINGLE, (memory_id,))
    receipt_id = f"delete-{digest}"
    path = directory / f"pending-{receipt_id}.json"
    stamp = int(time.time())
    _write_payload(path, {
        "receipt_id": receipt_id, "target_hash": digest, "scope": SCOPE_SINGLE, "target_count": 1,
        "deleted_at": stamp, "deleted_at_text": _format_ts(stamp), "deleted": {},
        "verification": {"verdict": "pending"}, "honest_limits": HONESTY_CLAUSE,
    })
    return path


def load_receipt(receipt_id: str, data_dir: Path) -> dict[str, Any] | None:
    """按回执 id 读回执（不存在 ⇒ `None`，**不编空回执**）。"""
    path = receipts_dir(data_dir) / f"{receipt_id}.json"
    if not path.exists():
        path = path.with_name(f"pending-{path.name}")
        if not path.exists():
            return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("删除回执 %s 读取失败（按不存在处理）：%s", receipt_id, exc)
        return None
    return data if isinstance(data, dict) else None


def list_receipts(data_dir: Path) -> list[dict[str, Any]]:
    """列出全部回执摘要（按时间倒序；坏文件跳过并记 WARNING）。

    Returns:
        每项 `{receipt_id, scope, deleted_at, deleted_at_text, verdict, target_count}`。
    """
    directory = receipts_dir(data_dir)
    if not directory.exists():
        return []
    out: list[dict[str, Any]] = []
    paths = sorted(directory.glob("delete-*.json"))
    paths.extend(
        path for path in sorted(directory.glob("pending-delete-*.json"))
        if not path.with_name(path.name.removeprefix("pending-")).exists()
    )
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("删除回执 %s 读取失败（跳过）：%s", path.name, exc)
            continue
        if not isinstance(data, dict):
            continue
        verification = data.get("verification") or {}
        out.append(
            {
                "receipt_id": data.get("receipt_id") or path.stem,
                "scope": data.get("scope"),
                "deleted_at": data.get("deleted_at"),
                "deleted_at_text": data.get("deleted_at_text"),
                "verdict": verification.get("verdict"),
                "target_count": data.get("target_count"),
            }
        )
    out.sort(key=lambda r: int(r.get("deleted_at") or 0), reverse=True)
    return out


def latest_receipt(data_dir: Path) -> dict[str, Any] | None:
    """最近一张回执（`March7_status` 与 CLI `receipts` 的入口）。"""
    rows = list_receipts(data_dir)
    if not rows:
        return None
    return load_receipt(str(rows[0]["receipt_id"]), data_dir)


__all__ = [
    "ERASE_SCOPES",
    "RECEIPTS_DIRNAME",
    "SCOPE_ALL",
    "SCOPE_PROJECT",
    "SCOPE_SESSION",
    "SCOPE_SINGLE",
    "VERIFY_CLEAN",
    "VERIFY_RESIDUAL",
    "VERIFY_RESULTS",
    "DeleteReceipt",
    "build_receipt",
    "latest_receipt",
    "list_receipts",
    "load_receipt",
    "receipts_dir",
    "scope_hash",
    "write_receipt",
]
