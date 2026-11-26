"""codex_notify_chain.py — 让 Codex 的 `notify` 同时调多个程序。

## 为什么需要它（真机场景，2026-11-16）

Codex 的 `notify` 是**单值**（一个命令 + 参数），不能配两条。而本机已经有
OpenAI 自带的计算机使用：

```toml
notify = ["...\\codex-computer-use.exe", "turn-ended"]
```

March7 的安装器**拒绝覆盖**它（覆盖 = 悄悄关掉用户的计算机使用功能，`ADR-0061` §4.2）。
所以要让两者共存，只能把 `notify` 指向**这个脚本**，由它依次调两个程序：

```toml
notify = ["<python>", "<本脚本>", "--", "<computer-use.exe>", "turn-ended", "--march7"]
```

## 设计（三条，都是为了"不许拖累对方"）

1. **每个下游独立超时**（默认 60s）：一个卡住不许把另一个也拖死。
2. **任何一个失败都不影响其它**：全部 `try/except`，最后**恒返回 0**。
   `notify` 是旁路，非零只会让宿主打警告、污染界面。
3. **原始 argv 原样转发给第一个下游**：Codex 会在命令末尾**追加一个 JSON**，
   那个 JSON 是数据的全部来源，丢掉它等于什么都没收到。

## 用法

    python codex_notify_chain.py -- <程序1> <参数...> -- <程序2> <参数...>

`--` 分隔不同的下游命令。至少一个。

## 为什么不把 March7 的逻辑内联进来

内联就等于这份脚本变成"第二个 March7 入口"，将来 `codex_notify.py` 一改，
这里也得跟着改——两份真相源（`CONVENTIONS` §12 最忌讳的那种）。
现在它只负责**转发**，不解析、不判断。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 60
"""单个下游的超时。为什么要 60s：`codex_notify.py` 要跑一次 LLM 判分
（实测中位 18–24s，最坏 119s）。60s 会砍掉一些慢轮次，但那比"卡住界面"好——
被砍掉的那轮会在下次（或手动 `March7_write`）补上，而界面卡住是用户直接能感知的。
"""


def split_commands(argv: list[str]) -> list[list[str]]:
    """按 `--` 把参数切成若干条命令。

    ⚠️ **必须排除 JSON 载荷**（踩过一次）：Codex 追加的 JSON 可能是这样出现的——

        ... -- <程序2> <参数> -- {"type": "agent-turn-complete", ...}

    那个 `--` 后面就是数据，不是命令。若把它当命令切，会得到一条"命令"
    `['{"type":...}']`，然后 `FileNotFoundError`（实测：第二个下游直接失败）。

    判据：`{` 开头的 token 一律视为**数据**，既不当作命令名也不当作参数。
    """
    commands: list[list[str]] = []
    current: list[str] = []
    for token in argv:
        stripped = token.strip()
        if stripped.startswith("{"):
            continue  # 数据，不是命令的一部分
        if token == "--":
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _payload_args(argv: list[str]) -> list[str]:
    """挑出"数据参数"——即 Codex 追加的那个 JSON。

    为什么要挑而不是整段转发：`sys.argv` 里既有我们的 `--` 分隔符，
    也有可能被误当参数的东西。只把**看起来是 JSON 对象的那些**转给下游，
    最稳（`codex_notify.py` 自己也是这么找载荷的）。
    """
    return [token for token in argv if token.strip().startswith("{")]


def run_one(command: list[str], extra: list[str]) -> tuple[bool, str]:
    """跑一条下游命令；返回 (成功?, 说明)。**不抛**。"""
    if not command:
        return False, "空命令"
    try:
        result = subprocess.run(
            [*command, *extra],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()
            return False, f"退出码 {result.returncode}: {tail[-1] if tail else '无输出'}"
        return True, "ok"
    except subprocess.TimeoutExpired:
        return False, f"超时（>{DEFAULT_TIMEOUT_SECONDS}s）"
    except FileNotFoundError:
        return False, f"找不到程序：{command[0]}"
    except Exception as exc:  # noqa: BLE001 - 转发器：任何异常都不许冒泡
        return False, f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    """入口。**恒返回 0**（notify 是旁路，非零会污染宿主界面）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    commands = split_commands(args)
    if not commands:
        print("[March7] notify-chain: 没有配置任何下游命令，跳过", file=sys.stderr)
        return 0

    extra = _payload_args(args)
    if not extra:
        # 没有 JSON 载荷：仍然调下游（有的程序可能不需要），但不当作错误。
        print("[March7] notify-chain: 未找到 JSON 载荷（可能不是 turn-complete）", file=sys.stderr)

    for index, command in enumerate(commands, start=1):
        ok, detail = run_one(command, extra)
        if not ok:
            # 只写 stderr：stdout 会被宿主当输出展示
            print(
                f"[March7] notify-chain: 第 {index} 个下游失败（不影响其它）：{detail}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
