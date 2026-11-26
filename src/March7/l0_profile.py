"""l0_profile.py — L0 画像读写（内部代号：六相冰）
见 docs/NAMING.md —— 封存着最核心的那块冰晶 = 身份锚。
对应 ~/.March7/profile.json（人类可直接读写的 JSON，字段照抄 Cyrene L0Profile）。

**本模块是「AI 推断的一律进不了画像」这条产品承诺的落地点**（CONVENTIONS §9.1），
因此只有两条写入路径，且两条都要求用户原话背书：

1. `apply_candidate` —— 走 Judge 管线（`March7_write`）的 L0 候选；
   **双保险**：即便 `judge.post_filter` 漏网，本层再校验一次
   （`certainty == "explicit"` 且 `attribution == "user_explicit"`）。
   可写字段**不含 `nickname`**（T12 卡关键约束④：nickname 留给受控入口，
   否则等于给模型开一条绕过画像护栏的路）。
2. `apply_user_update` —— 宿主侧「用户明确要求改画像」的受控入口；
   绕过 LLM Judge，但**必须**由用户原话（`source_quote`）背书。

⚠️ 本模块**零 MCP 依赖**（CONVENTIONS §2 红线）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from March7.schemas import L0Profile, MemoryCandidate, MemoryLayer

logger = logging.getLogger(__name__)

# L0 画像的字符串字段（isPinned / updatedAt 是 bool / int，不接受文本更新）
_STRING_FIELDS: tuple[str, ...] = (
    "nickname",
    "preferredName",
    "occupation",
    "longTermInterests",
    "language",
    "permanentNote",
)

# Judge 管线（apply_candidate）可写的字段：**不含 nickname**。
# 与 judge.py 的 `_L0_FIELD_DESCRIPTIONS` 同口径（上游同样把 nickname 排除在
# AI 可写字段之外），留给 apply_user_update 这条受控入口。
_JUDGE_WRITABLE_FIELDS: frozenset[str] = frozenset(
    f for f in _STRING_FIELDS if f != "nickname"
)

# 受控入口（apply_user_update）可写的字段：全部字符串字段（含 nickname）。
_USER_WRITABLE_FIELDS: frozenset[str] = frozenset(_STRING_FIELDS)


def _judge_constants() -> tuple[tuple[str, ...], str]:
    """取 judge 模块的两个常量（绝对词黑名单 + 拒绝理由前缀）。

    **为什么延迟导入**：`judge.py` 依赖本模块（`from March7.l0_profile import ProfileStore`），
    本模块顶层再 import judge 会成环。延迟到调用时导入则无环，且常量仍只有一份
    （judge.py 是唯一定义处，本模块不复制黑名单——复制即第二真相源）。
    """
    from March7.judge import ABSOLUTE_WORDS, REJECT_REASON_PREFIX

    return ABSOLUTE_WORDS, REJECT_REASON_PREFIX


def _profile_digest(field: str, value: str) -> str:
    encoded = json.dumps((field, value.strip()), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def profile_view(profile: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """读画像的轻量视图；撤下标记只有摘要和生效时间，不打开记忆库。"""
    hidden: set[str] = set()
    now = int(time.time())
    for marker in (data_dir / "profile-suppressions").glob("*.json"):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if payload["effective_at"] <= now:
                hidden.update(payload["digests"])
        except FileNotFoundError:
            continue  # 与恢复同时发生，恢复后本文件不应继续生效
        except (OSError, ValueError, TypeError, KeyError):
            logger.exception("无法核对画像撤下标记，本轮跳过画像显示")
            return {}
    return {
        field: value for field, value in profile.items()
        if not isinstance(value, str) or _profile_digest(field, value) not in hidden
    }


class ProfileStore:
    """L0 画像的持久层：读写 profile.json。"""

    def __init__(self, profile_path: Path) -> None:
        """绑定 profile.json 路径（生产 ~/.March7/profile.json；开发 ~/.March7-dev/）。

        目录不存在则创建（与 `store.MemoryStore` 同惯例）；**不读盘、不校验内容**
        ——构造期不做 IO，出错留给 `load()` 统一处理。
        """
        self._path = Path(profile_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """profile.json 的完整路径。"""
        return self._path

    def load(self) -> L0Profile:
        """读取画像；文件不存在或损坏时返回空画像（不抛错——画像缺失不是故障）。

        损坏的三种情形都走同一条降级路径（记 ERROR 日志 + 返回空画像）：
        文件读不出来 / JSON 非法 / 字段不合法。
        ⚠️ `L0Profile` **不加** `extra="forbid"`（ADR-0015 决策 2）：用户手改过
        profile.json、多带一个键是**正常**的，不能因此读不出来。
        """
        if not self._path.exists():
            return L0Profile()
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("profile.json 读取失败（%s），按空画像处理：%s", self._path, exc)
            return L0Profile()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.error("profile.json 不是合法 JSON（%s），按空画像处理：%s", self._path, exc)
            return L0Profile()
        if not isinstance(data, dict):
            logger.error(
                "profile.json 顶层不是对象（%s），按空画像处理", type(data).__name__
            )
            return L0Profile()
        try:
            return L0Profile.model_validate(data)
        except ValidationError as exc:
            logger.error("profile.json 字段不合法，按空画像处理：%s", exc)
            return L0Profile()

    def recall_view(self) -> L0Profile:
        """画像工具的可召回视图；文件仍保留原值供用户核对。"""
        return L0Profile.model_validate(profile_view(self.load().model_dump(), self._path.parent))

    def matching_digests(self, phrases: tuple[str | None, ...]) -> tuple[str, ...]:
        """只关联完整相等的画像快照，避免按词片段撤下其他事实。"""
        if not self._path.is_file():
            return ()
        profile = self.load()
        candidates = {phrase.strip() for phrase in phrases if phrase and phrase.strip()}
        digests = []
        for field in _STRING_FIELDS:
            value = getattr(profile, field)
            if value.strip() in candidates:
                digests.append(_profile_digest(field, value))
        return tuple(digests)

    def _suppression_path(self, memory_id: str) -> Path:
        digest = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()
        return self._path.parent / "profile-suppressions" / f"{digest}.json"

    def suppress(self, memory_id: str, digests: tuple[str, ...], effective_at: int = 0) -> None:
        """在画像旁登记原值摘要，原画像和原话仍留在其各自历史来源。"""
        if not digests:
            return
        marker = self._suppression_path(memory_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_suffix(f".{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps({"digests": digests, "effective_at": effective_at}), encoding="utf-8")
            tmp.replace(marker)
        finally:
            tmp.unlink(missing_ok=True)

    def restore_visibility(self, memory_id: str) -> None:
        """撤掉该条的标记，其他记忆产生的遮盖仍然独立生效。"""
        self._suppression_path(memory_id).unlink(missing_ok=True)

    def save(self, profile: L0Profile) -> None:
        """落盘（原样写入；`isPinned` 字段随 JSON 原样保留）。

        ⚠️ **口径说明**：`updatedAt` 的刷新发生在**真正改画像的写入路径**上
        （`apply_candidate` / `apply_user_update`），本方法只负责**原样持久化**。
        这样「存进去什么，读出来就是什么」的往返语义成立，调用方想写哪个时间戳
        就是哪个时间戳（`apply_*` 写的是「画像内容变化的时刻」= 现在）。

        写入是**原子**的：先写同目录临时文件再 `replace` ⇒ 写一半崩溃不会留下
        半截 profile.json（画像是用户手改的文件，不能被写坏）。
        """
        payload = json.dumps(profile.model_dump(), ensure_ascii=False, indent=2)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(self._path)

    def apply_candidate(self, candidate: MemoryCandidate) -> L0Profile:
        """把一条已通过四条硬过滤（含规则②双条件）的 L0 候选并入画像并落盘。

        只接受 layer == L0 的候选；字段名合法性在写入前校验——
        **AI 推断的信息（certainty != explicit 或 attribution != user_explicit）
        一律进不了画像**，此为双保险（过滤在 judge.post_filter，校验在此）。

        两种**拒收**（不抛错、不写盘，记 WARNING 后返回当前画像）：
        - 双条件不满足（AI 推断 / 归因不是用户自述）；
        - 字段名非法（含 `nickname`——它只走 `apply_user_update` 这条受控入口）。
        拒收不抛异常是刻意的：Judge 管线里一条坏候选不该让整轮写入失败。
        """
        if candidate.layer is not MemoryLayer.L0:
            raise ValueError(
                f"apply_candidate 只接受 L0 候选，实际 layer={candidate.layer!r}。"
            )
        field = candidate.field
        if not field or field not in _JUDGE_WRITABLE_FIELDS:
            logger.warning(
                "画像写入跳过：字段 %r 不是 Judge 可写字段（合法字段：%s）。"
                "nickname 只走 apply_user_update 受控入口。",
                field,
                "、".join(sorted(_JUDGE_WRITABLE_FIELDS)),
            )
            return self.load()
        if not (candidate.certainty == "explicit" and candidate.attribution == "user_explicit"):
            logger.warning(
                "画像写入跳过：字段 %r 不满足双条件（certainty=%r、attribution=%r）"
                "——AI 推断的信息永远进不了画像。",
                field,
                candidate.certainty,
                candidate.attribution,
            )
            return self.load()
        value = candidate.summary.strip()
        if not value:
            logger.warning("画像写入跳过：字段 %r 的 summary 为空。", field)
            return self.load()

        profile = self.load()
        setattr(profile, field, value)
        profile.updatedAt = int(time.time())
        self.save(profile)
        logger.info("画像字段 %r 已更新（候选来源：Judge 管线）", field)
        return profile

    def apply_user_update(self, updates: dict[str, str], source_quote: str) -> L0Profile:
        """宿主侧「用户明确要求改画像」的**受控入口**（护栏，ADR-0003）。

        与 `apply_candidate` 的区别：这条路径绕过 LLM Judge（不需要模型判断），
        但**必须**由用户原话背书，因此强制要求 `source_quote`：

        1. `source_quote` 为空或仅空白 → 拒绝（ValueError），理由带
           「宁可漏记，不可误记」前缀；
        2. `updates` 的新值里出现绝对词黑名单（judge.ABSOLUTE_WORDS）中的词、
           而**用户原话里没有这个词** → 拒绝（ValueError）。
           这正是 LLM 把「以后都不要再提黄金」总结成「永远不聊黄金」的典型陷阱：
           新值里的「永远」在原话中不存在 ⇒ 是模型加上去的确定性，不收。
           （与 judge.post_filter 规则④同一条口径：**词必须在用户原话里出现过**。）
        3. 字段名必须是可更新的画像字段（nickname/preferredName/occupation/
           longTermInterests/language/permanentNote），否则拒绝；
           `isPinned` / `updatedAt` 不是文本字段，不接受更新。

        Raises:
            ValueError: 上述任一护栏不通过（理由一律带「宁可漏记，不可误记」前缀）。
        """
        absolute_words, prefix = _judge_constants()

        quote = (source_quote or "").strip()
        if not quote:
            raise ValueError(
                f"{prefix}用户明确要求修改画像时必须提供 source_quote（用户原话）"
                "——没有原话背书的画像修改一律拒绝。"
            )
        if not updates:
            raise ValueError(f"{prefix}没有要更新的画像字段（updates 为空）。")

        for field in updates:
            if field not in _USER_WRITABLE_FIELDS:
                raise ValueError(
                    f"{prefix}字段 {field!r} 不是可更新的画像字段"
                    f"（合法字段：{'、'.join(_STRING_FIELDS)}）。"
                )

        for field, value in updates.items():
            text = str(value)
            for word in absolute_words:
                if word in text and word not in quote:
                    raise ValueError(
                        f"{prefix}字段 {field!r} 的新值含绝对词「{word}」而用户原话里没有"
                        "——把不确定说成了确定，不收。"
                    )

        profile = self.load()
        for field, value in updates.items():
            setattr(profile, field, str(value))
        profile.updatedAt = int(time.time())
        self.save(profile)
        logger.info("画像字段已更新（受控入口）：%s", "、".join(sorted(updates)))
        return profile

    def fields_containing(self, needles: tuple[str, ...] | list[str]) -> list[str]:
        """画像里**含任一 needle** 的字段名（erase 审计的第 14 处判据）。

        ## 为什么画像也要算进删除面

        `profile.json` 是**明文 JSON**：`permanentNote` 完全可能是
        「他在做紫晶计划，别告诉别人」。用户要求删掉那条记忆时，
        这一段留在画像里等于"删了结论、留了摘要"。

        ## 为什么要单独一个方法，而不是让 `erase.py` 自己读文件

        `profile.json` 的读取有降级路径（文件坏 / JSON 非法 / 字段不合法 ⇒ 空画像，
        见 `load()`）。`erase.py` 重复实现一遍就会漂——而且它漂的方式很危险：
        扫描侧宽容、真实读取侧严格，就会出现「审计说清了、注入还在」。

        Args:
            needles: 要比对的串（记忆 id / 正文）。

        Returns:
            命中的字段名列表（**只给字段名，不给值**——值就是被删的内容）。
        """
        cleaned = [n for n in needles if n]
        if not cleaned:
            return []
        profile = self.load()
        hits: list[str] = []
        for field in ("nickname", "preferredName", "occupation", "longTermInterests", "language", "permanentNote"):
            value = str(getattr(profile, field, "") or "")
            if value and any(n in value for n in cleaned):
                hits.append(field)
        return hits

    def erase_fields(self, fields: tuple[str, ...] | list[str]) -> list[str]:
        """把命中的画像字段**清空**并落盘，返回真正被清掉的字段名。

        ## 为什么是"清空"而不是"删掉键"

        `L0Profile` 的六个文本字段默认值都是 `""`（见 `schemas.py`），
        所以"清空"就是回到未设置状态——与"从来没记过"等价。
        删掉键反而会让 `model_validate` 走默认值，效果相同但要处理
        `extra` 字段的兼容（`L0Profile` 刻意没有 `extra="forbid"`），
        多一层没必要的分支。

        ## 为什么不清 `updatedAt` / `isPinned`

        - `updatedAt` 是**这次动作**的时间戳（画像被改过了，理应更新）；
        - `isPinned` 是用户的 UI 偏好，不是个人数据（"置顶"不泄露任何内容）。

        Args:
            fields: 要清空的字段名（来自 `fields_containing()`）。

        Returns:
            真正被清掉的字段名（原本就是空的字段不算"清掉"）。
        """
        allowed = {"nickname", "preferredName", "occupation", "longTermInterests", "language", "permanentNote"}
        targets = [f for f in fields if f in allowed]
        if not targets:
            return []
        profile = self.load()
        cleared: list[str] = []
        for field in targets:
            if str(getattr(profile, field, "") or "").strip():
                setattr(profile, field, "")
                cleared.append(field)
        if not cleared:
            return []
        profile.updatedAt = int(time.time())
        self.save(profile)
        logger.info("画像字段已被 erase 清空：%s", "、".join(cleared))
        return cleared
