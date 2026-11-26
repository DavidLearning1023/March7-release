"""panel_entry.py — 三个安装器共用的「可双击面板入口」写入器（S17 / `ADR-0072`）

## 为什么单独一个文件

`hosts/` 不是 Python 包，三个安装器彼此独立、各自用 importlib 按路径加载自己。
入口文件的命名、内容、失败语义如果各写一遍，**必然漂**——漂了以后的症状是
「WorkBuddy 有入口、DSH 没有」，而这正是 S17 独立验收抓出来的形态。

所以实现只留这一份：三个安装器都 `_load_shared()` 加载它，传各自的
`(python, data_dir)` 进来。

## 为什么走子进程而不是 import `March7.control`

安装器可能跑在**没有 March7 依赖**的系统 Python 下（`--doctor` 就是给这种机器用的）。
用装好的解释器起一个短命子进程，安装器自己保持零依赖。

失败返回 `None`（调用方只提示、不回滚）：入口是"宿主 hooks 不可用时的降级路径"，
不是接入的必要条件。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def write_panel_entry(
    python: Path | str,
    data_dir: Path | str,
    *,
    timeout: float = 60.0,
) -> Path | None:
    """让 `panel_launcher` 写一个双击即打开面板的本机入口。

    Args:
        python: 跑 March7 的解释器（安装器已经校验过它能 import March7）。
        data_dir: 绑定库目录（写进入口脚本，双击时不再解析）。
        timeout: 子进程上限（正常 <1s；60s 是"明显卡死"的界线）。

    Returns:
        入口文件路径；写不了 / 子进程失败 ⇒ `None`。
    """
    try:
        completed = subprocess.run(
            [
                str(python),
                "-m",
                "March7.control.panel_launcher",
                "--write-shortcut",
                "--json",
                "--data-dir",
                str(data_dir),
                "--python",
                str(python),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "MARCH7_HOME": str(data_dir)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    lines = [line for line in (completed.stdout or "").strip().splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except ValueError:
        return None
    raw = payload.get("shortcut") if isinstance(payload, dict) else None
    return Path(raw) if isinstance(raw, str) and raw else None


__all__ = ["write_panel_entry"]
