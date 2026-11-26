"""export_memories.py — 用户侧的导出 CLI（相册的"自己留一份"入口）。

为什么需要它：`March7_export` 工具的输出会进**模型上下文**，所以它默认走脱敏视图
（D6：模型看到的是脱敏后的原话）。但用户自己的本地副本应当能看到完整原文——
这个脚本就是那条"用户侧"通道：在终端里跑，不经模型。

用法：
    uv run python scripts/export_memories.py            # markdown（脱敏视图）
    uv run python scripts/export_memories.py --json     # json
    uv run python scripts/export_memories.py --verbatim # 完整原文（只给自己看）
    uv run python scripts/export_memories.py --data-dir ~/.March7-dev --out ./exports

⚠️ 默认数据目录跟随 `MARCH7_HOME`，与引擎/钩子同一口径（CONVENTIONS §2 数据红线）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from March7 import config
from March7.export import export_json, export_markdown
from March7.l0_profile import ProfileStore
from March7.store import MemoryStore


def main(argv: list[str] | None = None) -> int:
    """解析参数 → 导出 → 打印落地路径。返回退出码（0 = 成功）。"""
    parser = argparse.ArgumentParser(description="导出 March7 的全部记忆（含历史）")
    parser.add_argument(
        "--json",
        action="store_true",
        help="导出 json（默认 markdown）",
    )
    parser.add_argument(
        "--verbatim",
        action="store_true",
        help="保留原话里的绝对路径（默认脱敏；只给自己看的本地副本才该打开）",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="数据目录（默认读 MARCH7_HOME，未设则 ~/.March7）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="输出目录（默认 = 数据目录，导出文件跟着记忆库走）",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else config.resolve_data_dir()
    output_dir = Path(args.out) if args.out else data_dir
    store = MemoryStore(data_dir / config.DB_FILENAME)
    store.init_schema()
    try:
        profile = ProfileStore(data_dir / config.PROFILE_FILENAME).load()
        exporter = export_json if args.json else export_markdown
        path = exporter(store, profile, output_dir=output_dir, verbatim=args.verbatim)
    finally:
        store.close()
    print(f"导出完成：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
