"""doctor.py — 一条命令看健康状态（ADR-0026 / 设计复盘 P1-3）。

为什么需要它：T13 的首轮 P0（包名遮蔽标准库 ⇒ 8 个工具全挂）与 T14 的 6 个静默 bug，
共同特征是**"能启动、能列工具、返回成功"**。这类故障靠验收查不出来，
只能靠一个"随时能跑、输出人话"的体检命令。

本脚本**只读**：不 init_schema、不改库、不写日志（迁移交给真正启动的引擎做）。

用法：
    uv run python scripts/doctor.py
    uv run python scripts/doctor.py --data-dir "C:/Users/xx/.March7-dev"
    uv run python scripts/doctor.py --probe-embedder   # 额外做一次编码探针（约 10s）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
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
    except OSError:
        return []
    return rows


def _stop_latency_summary(data_dir: Path, days: int = 7) -> dict:
    """近 `days` 天 Stop 钩子延迟分位（读真机日志，纯只读、可复算）。

    口径（`ADR-0036`）：只统计 `stop_ingested.elapsed_ms`（**进程级**耗时，
    含 import / 转写解析 / 模型加载 / LLM 调用），与账本 `latency_ms`
    （只含写入管线）不是一回事，两者不可横比。
    """
    import statistics
    from datetime import datetime, timedelta

    values: list[int] = []
    # 分位**只含成功 ingest 轮次**；失败/截止轮次单独记，避免把最慢的那些
    # 悄悄排除在报告之外（独立验收 P2-⑦）。
    worst: list[int] = []
    deadline = failed = 0
    with_budget = 0
    """多少条 `stop_ingested` 带 `deadline_ms` —— 新钩子副本的**版本标记**。
    旧副本（11-03 及以前）没有这个字段；引擎侧 `src/March7` 走 editable 包、
    改完即时生效，所以**不能用延迟高低推断部署状态**（独立验收 P1-A 的教训）。"""
    last_failed: int | None = None
    for offset in range(days):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
        for row in _read_jsonl(data_dir / "logs" / f"hooks-{day}.jsonl"):
            event = row.get("event")
            if event == "stop_ingested":
                ms = row.get("elapsed_ms")
                if isinstance(ms, (int, float)) and ms >= 0:
                    values.append(int(ms))
                if isinstance(row.get("deadline_ms"), (int, float)):
                    with_budget += 1
            elif event in ("stop_deadline_exceeded", "stop_extraction_failed"):
                if event == "stop_deadline_exceeded":
                    deadline += 1
                else:
                    failed += 1
                ms = row.get("elapsed_ms")
                if isinstance(ms, (int, float)) and ms >= 0:
                    worst.append(int(ms))
                ts = row.get("ts")
                if isinstance(ts, (int, float)):
                    last_failed = max(last_failed or 0, int(ts))
    values.sort()
    worst.sort()
    p90 = values[min(len(values) - 1, int(len(values) * 0.9))] if values else 0
    return {
        "n": len(values),
        "values": values,
        "median": int(statistics.median(values)) if values else 0,
        "p90": p90,
        "over60": sum(1 for v in values if v >= 60_000),
        "worst_max": worst[-1] if worst else 0,
        "worst_n": len(worst),
        "with_budget": with_budget,
        "deadline": deadline,
        "failed": failed,
        # 失败数必须带**最后一次时刻**：否则修好之后仍会一直显示「失败 13」，
        # 让人误判当前故障（T14c 在账本上栽过一次，别在钩子上再栽一次）。
        "last_failed": last_failed,
    }


def _hook_summary(data_dir: Path) -> dict[str, int]:
    """统计今天的钩子事件（钩子日志与账本同在 `logs/`，一次排查只看一个目录）。"""
    import time

    path = data_dir / "logs" / f"hooks-{time.strftime('%Y%m%d')}.jsonl"
    counts: dict[str, int] = {}
    for row in _read_jsonl(path):
        name = str(row.get("event") or "?")
        counts[name] = counts.get(name, 0) + 1
    return counts


def _detect_cuda() -> bool:
    """有没有可用的 CUDA 设备。**只查 `torch.cuda.is_available()`，不加载模型。**

    为什么这里可以 import torch 而 `Embedder` 不行：查 CUDA 只要导入 torch
    （静态 ~2.4 s），加载 BGE-M3 权重要再 2–8 s。体检要快，所以只做前者。
    torch 不可见（如只装了 pytest+pydantic+sqlite-vec 的环境）⇒ 按 CPU 处理。
    """
    try:
        import torch
    except Exception:  # noqa: BLE001 —— torch 装坏/没装都按 CPU 展示，不抛
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def _encoder_line(data_dir: Path) -> str:
    """编码器一行：型号 + 设备 + **预估等待**（`ADR-0059` 决策 5）。

    为什么这一行值得单独存在：CPU 用户第一次说话会莫名卡 10–15 秒。没有这句话，
    他的第一反应是"是不是坏了"，而实际上这只是 `import torch` + 加载权重的固定开销。
    成本 3 行代码，收益是消除一个**必然会发生的**误判。

    ⚠️ **不加载模型**：这里只读环境变量与 CUDA 可用性。真跑一次编码要 2–12 秒，
    那是 `--probe-embedder` 的事。
    """
    model = (os.environ.get("MARCH7_EMBEDDER_MODEL") or "").strip() or "BAAI/bge-m3"
    requested = (os.environ.get("MARCH7_EMBEDDER_DEVICE") or "").strip().lower()
    if requested in ("cpu", "cuda"):
        device = requested
    else:
        device = "cuda" if _detect_cuda() else "cpu"

    if device == "cuda":
        note = "每轮首次写入约 2–20 秒（含模型加载）"
    else:
        note = "无独显：每轮首次写入要多等约 10–15 秒（实测 12.1 s，其中 10 s 是 import torch）"
    return f"编码器　：{model} device={device}（{note}）"


def _vec_line(data_dir: Path) -> str:
    """`vec` 一行：`ready` / `degraded`（`ADR-0056` 决策 3）。

    为什么必须显式暴露：`sqlite-vec` 缺失时召回质量明显下降（`ADR-0025` 记录
    BM25 AND 语义 324/324 全空），但用户看到的只是"有时候想不起来"——
    **静默降级正是本项目最忌讳的失败形态**。

    ⚠️ 本函数**不建库**：`MemoryStore.__init__` 会创建文件，体检脚本不该有副作用。
    库不存在时直接读扩展可用性，不落盘。
    """
    db_path = data_dir / "March7.db"
    ready = False
    source = ""
    if db_path.exists():
        try:
            from March7.store import MemoryStore

            store = MemoryStore(db_path)
            try:
                ready = store.vec_ready
                source = "（实读库内扩展）"
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 —— 体检要给人话
            return f"vec　　 ：degraded（读库失败：{type(exc).__name__}）"
    else:
        # 库还不存在：只能查包在不在（不建库）。这一支的答案是"能不能用"，
        # 而"库里有没有索引"要等第一次写入才知道。
        try:
            import importlib.util

            ready = importlib.util.find_spec("sqlite_vec") is not None
            source = "（库未创建，按包可用性判断）"
        except Exception:  # noqa: BLE001
            ready = False

    if ready:
        return f"vec　　 ：ready{source}"
    return (
        "vec　　 ：degraded（sqlite-vec 不可用 ⇒ 召回退化为关键词匹配，"
        "「想不起昨天说的」很可能就是这个原因；用 `uv sync` 补装）"
    )


def _probe_embedder() -> int:
    """实跑两次编码，首次包含初始化和可能的权重下载。"""
    try:
        started = time.perf_counter()
        from March7.embedder import Embedder

        embedder = Embedder()
        vector = embedder.embed_one("冷启动编码探针")
        warmed = time.perf_counter()
        embedder.embed_one("同进程编码探针")
        finished = time.perf_counter()
        print(
            f"编码器　：{embedder.model_name} device={embedder.device} dim={len(vector)} ✅"
            f"｜首次编码 {warmed - started:.2f}s｜同进程第二次 {finished - warmed:.2f}s"
        )
    except Exception as exc:  # noqa: BLE001 —— 可选实跑要保留失败退出码与原因
        print(f"编码器　：❌ {type(exc).__name__}: {exc}")
        return 1
    return 0


def _windows_torch_runtime_ready() -> bool:
    """按 PyTorch Windows 加载器使用的 DLL 检查现有系统运行库。"""
    if sys.platform != "win32":
        return True

    import ctypes

    try:
        for library in ("vcruntime140.dll", "msvcp140.dll", "vcruntime140_1.dll"):
            ctypes.CDLL(library)
    except OSError:
        return False
    return True


def _preflight(data_dir: str | None, *, probe_embedder: bool) -> int:
    """安装环境探针，使用临时库，默认无需联网或加载权重。"""
    print(f"环境：{platform.system()} {platform.release()} {platform.machine()} · Python {platform.python_version()}")
    missing = [name for name in ("mcp", "openai", "pydantic") if importlib.util.find_spec(name) is None]
    if sys.version_info < (3, 12):
        missing.insert(0, "Python 3.12+")
    if missing:
        print(f"安装基础：缺 {', '.join(missing)}。请用 Python 3.12+ 运行 uv sync --locked。")
        print("完整写读：待检查")
        return 1

    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        print("安装基础：基础 Python 包可发现，临时建库待检查。")
        print("完整写读：当前 Python 的 SQLite 无法加载扩展。"
              "请运行 uv sync --locked --managed-python --python 3.12 后重查。")
        return 1

    from March7 import config
    from March7.store import MemoryStore

    data = Path(data_dir).expanduser() if data_dir else config.resolve_data_dir()
    hf_home = Path(os.environ.get("HF_HOME") or Path(
        os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    ) / "huggingface")
    hub = Path(os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE") or hf_home / "hub")
    for label, path in (("安装目录", Path(__file__).resolve().parents[1]), ("数据目录", data), ("权重缓存", hub)):
        path = path.expanduser().absolute()
        try:
            existing = next(parent for parent in (path, *path.parents) if parent.is_dir())
            print(f"可用空间（{label}所在盘）：{shutil.disk_usage(existing).free / 2**30:.1f} GiB")
        except (OSError, StopIteration) as exc:
            print(f"可用空间（{label}所在盘）：读取失败（{type(exc).__name__}）")

    try:
        with tempfile.TemporaryDirectory(prefix="March7-preflight-") as temp:
            store = MemoryStore(Path(temp) / config.DB_FILENAME)
            try:
                store.init_schema()
                vec_ready = store.vec_ready
            finally:
                store.close()
    except Exception as exc:  # noqa: BLE001 —— 区分安装前置缺项和完整写读缺项
        print(f"安装基础：临时 SQLite 建库失败（{type(exc).__name__}）。")
        print("完整写读：待检查")
        return 1

    print("安装基础：就绪（基础 Python 包可发现，临时 SQLite/FTS5 建库通过）")
    print("联网：首次下载权重使用 Hugging Face，记忆抽取访问所配模型端点。")
    missing = [name for name in ("torch", "sentence_transformers") if importlib.util.find_spec(name) is None]
    if not vec_ready:
        missing.append("可用的 sqlite-vec 扩展")
    runtime_ready = _windows_torch_runtime_ready()
    if not runtime_ready:
        missing.append("Microsoft Visual C++ Redistributable (x64)")
    if missing:
        print(f"完整写读：缺 {', '.join(missing)}。请核对平台、解释器及 uv sync --locked 的输出。")
        if not runtime_ready:
            print("请从微软安装 x64 运行库后重查：https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist/")
        return 1
    print("完整写读：依赖预检通过，实际写读需在所用宿主验证。")
    if probe_embedder:
        return _probe_embedder()
    print("可加 --probe-embedder 实测首次和同进程第二次编码。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="March7 体检")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 MARCH7_HOME 或 ~/.March7）")
    parser.add_argument("--preflight", action="store_true", help="预检安装基础与完整写读依赖")
    parser.add_argument("--probe-embedder", action="store_true", help="额外做一次编码探针（慢）")
    args = parser.parse_args(argv)
    if args.preflight:
        return _preflight(args.data_dir, probe_embedder=args.probe_embedder)

    from March7 import config, ledger

    data_dir = Path(args.data_dir) if args.data_dir else Path(
        (os.environ.get(config.ENV_DATA_DIR) or "").strip() or config.DEFAULT_DATA_DIR
    )
    db_path = data_dir / config.DB_FILENAME
    print(f"数据目录：{data_dir}")
    print(f"库文件　：{db_path}{'' if db_path.exists() else '（不存在：还没写过记忆）'}")

    # ---- 库 ----
    from March7.store import SCHEMA_VERSION, MemoryStore

    if db_path.exists():
        store = MemoryStore(db_path)
        try:
            stats = store.stats()
            print(
                f"库结构　：v{stats.schema_version}（代码支持 v{SCHEMA_VERSION}）"
            )
            print(
                f"记忆条数：{stats.memory_count} 条（可召回 {stats.recallable_count} 条）"
                f"｜库大小 {stats.db_size_bytes / 1024:.0f} KB"
            )
            cap_text = "不限" if stats.daily_write_cap == 0 else str(stats.daily_write_cap)
            flag = "⚠️ 已触顶" if stats.daily_write_cap and stats.today_writes >= stats.daily_write_cap else "正常"
            print(f"今日写入：{stats.today_writes} 条 / 上限 {cap_text}（{flag}）")
            # ---- 工作层（S2 / ADR-0038）----
            # 为什么进体检：handoff 是**显式**开关，用户忘了关就一直是 open
            # ⇒ working 会一直注入旧约束，而界面上完全看不出来。
            try:
                working_total = store.count_working_items()
                stale = store.count_stale_handoffs()
                meta_rows = store.count_memory_meta()
                meta_filled = store.count_memory_meta(with_category=True)
                print(
                    f"工作层　：working {working_total} 条｜stale_open_handoffs {stale}"
                    f"｜memory_meta {meta_rows} 行（category 已填 {meta_filled}）"
                )
                if stale:
                    print("⚠️ 有 handoff 开了超过 14 天未关闭：working 会一直注入，建议 `close`")
            except Exception as exc:  # noqa: BLE001 — 老库可能没有这些表（未迁移）
                print(f"工作层　：读失败（{type(exc).__name__}）")
        except Exception as exc:  # noqa: BLE001 — 体检脚本要给出人话，不能甩栈
            print(f"❌ 读库失败：{type(exc).__name__}: {exc}")
            return 1
        finally:
            store.close()
    else:
        print("库结构　：—")

    # ---- 编码器 / 向量能力（S11b / ADR-0059 决策 5 + ADR-0056 决策 3）----
    # 放在库信息之后、账本之前：它回答的是"这台机器上这套东西能不能好好跑"，
    # 而不是"今天写了多少"。--probe-embedder 的实跑结果会替换这一行（见文件末尾）。
    print(_vec_line(data_dir))
    print(_encoder_line(data_dir))

    # ---- 账本（今日） ----
    rows = ledger.read_day(data_dir)
    if rows:
        agg = ledger.aggregate(rows)
        failed_rows = [r for r in rows if r.get("failed")]
        # 失败数必须带**最后一次的时刻**：否则「失败 6」在修好之后还会继续显示，
        # 让人误判「修复没生效」。2026-11-01 的真实现场就是这样——6 条失败全在
        # 换模型之前，但只看数字分不出来。
        last_failed = ""
        if failed_rows:
            last_ts = max((int(r.get("ts") or 0) for r in failed_rows), default=0)
            if last_ts:
                last_failed = f"（最近一次 {time.strftime('%H:%M', time.localtime(last_ts))}）"
        print(
            f"写入账本：{agg['runs']} 次管线｜写入 {agg['written']}｜查重 {agg['deduped']}｜"
            f"拒收 {agg['rejected']}｜熔断 {agg['capped']}｜失败 {len(failed_rows)}{last_failed}｜"
            f"tokens {agg['prompt_tokens']}+{agg['completion_tokens']}"
        )
    else:
        print("写入账本：今日无记录")

    # ---- 钩子 ----
    hooks = _hook_summary(data_dir)
    if hooks:
        interesting = (
            "stop_ingested", "stop_extraction_failed", "stop_deadline_exceeded",
            "stop_budget_invalid", "ups_injected", "ups_no_hit", "ERROR",
        )
        parts = [f"{k}={hooks[k]}" for k in interesting if k in hooks]
        print(f"钩子事件：{'｜'.join(parts) if parts else '（无关键事件）'}")
        if hooks.get("ERROR"):
            print("⚠️ 钩子有 ERROR：看 logs/hooks-*.jsonl 里 event=ERROR 的 traceback")
    else:
        print("钩子事件：今日无日志（钩子可能没装或没触发）")

    # ---- Stop 延迟（`ADR-0036`）----
    # 为什么进体检：以前「每轮到底等多久」只能手工翻 jsonl 现算，
    # 而钩子注入与 Stop 在界面上双隐身 ⇒ 慢了也没人看得见。
    latency = _stop_latency_summary(data_dir)
    if latency["n"]:
        e = latency["values"]
        last = latency["last_failed"]
        failed_note = (
            f"（最近一次 {time.strftime('%m-%d %H:%M', time.localtime(last))}）" if last else ""
        )
        worst_note = (
            f"｜最坏轮次 {latency['worst_n']} 次 max {latency['worst_max'] / 1000:.1f}s"
            if latency["worst_n"] else ""
        )
        n = latency["n"]
        deployed = latency["with_budget"]
        # `deadline_ms` 是**部署版本标记**，不是性能指标。延迟低只能说明引擎侧
        # 惰性化生效（`src/March7` 走 editable 包、改完即时生效），**不能**说明
        # 钩子副本已更新 —— 这是独立验收 P1-A 抓到的那条「判据零区分力」。
        if n and deployed == n:
            budget_note = f"｜钩子预算 {deployed}/{n} 轮（新副本已生效）"
        elif deployed:
            budget_note = (
                f"｜钩子预算 {deployed}/{n} 轮（⚠️ 其余是部署前的旧副本读数；"
                "旧事件不会被追溯改写 ⇒ 看新 Stop 事件而不是看历史比例）"
            )
        else:
            # 0 轮**不能**直接断言「副本是旧的」：刚部署完、还没跑过 Stop 时也是 0/0。
            budget_note = (
                f"｜钩子预算 {deployed}/{n} 轮（日志里没有带 deadline_ms 的事件 ⇒ "
                "要么钩子副本仍是旧的（跑 install.py），要么部署后还没跑过 Stop）"
            )
        print(
            f"Stop 延迟（近 7 天）：n={n}（**分位仅含成功 ingest**）｜"
            f"min {e[0] / 1000:.1f}s / 中位 {latency['median'] / 1000:.1f}s / "
            f"p90 {latency['p90'] / 1000:.1f}s / max {e[-1] / 1000:.1f}s｜"
            f"≥60s {latency['over60']} 次｜硬截止 {latency['deadline']}｜"
            f"抽取失败 {latency['failed']}{failed_note}{worst_note}{budget_note}"
        )
        if latency["median"] >= 14_000:
            print(
                "⚠️ 中位 ≥14s ⇒ 这些轮次确实付了 BGE-M3 权重加载（通常是该轮有候选要入库）。"
                "引擎侧惰性化走 editable 包**即时生效、无需部署**；"
                "要确认钩子副本版本，只看上面的「钩子预算」行。"
            )
    else:
        # n=0 也要把失败/截止计数打出来：否则「只有失败轮次」的那天看起来
        # 像「钩子没跑」，正好是双隐身最怕的情形。
        print(
            f"Stop 延迟（近 7 天）：无 stop_ingested 记录｜"
            f"硬截止 {latency['deadline']}｜抽取失败 {latency['failed']}"
        )

    # ---- 编码器（可选） ----
    if args.probe_embedder:
        return _probe_embedder()

    return 0


if __name__ == "__main__":
    sys.exit(main())
