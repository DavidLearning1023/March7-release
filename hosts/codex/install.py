"""install.py — Codex 宿主安装器（S14 / `ADR-0061`）

把 March7 装进 Codex：写 `notify`、MCP 工具面及面板钩子，失败整体回滚。

## 安装器接入分工

WorkBuddy 那边写三处（钩子脚本 / `settings.json` / `mcp.json`），因为它三种通道都有。
接入分工见 `ADR-0061` 和 `ADR-0073`：

| 通道 | Codex 有没有 | 我们怎么用 |
|---|---|---|
| 自动写入（Stop 等价） | ✅ `notify` | 写 `$CODEX_HOME/config.toml` 的 `notify` 数组 |
| 工具面（让模型主动查） | ✅ `mcp_servers` | 写 `$CODEX_HOME/config.toml` 的 `[mcp_servers.March7]` |
| 打开面板 | `UserPromptSubmit` | 写用户级 `hooks.json`，需宿主审阅 |
| 开局注入（画像） | 画像工具 | 靠工具面主动查 |

⚠️ **`notify` 是单值，不是数组追加**：Codex 的 `config.toml` 里 `notify` 是一个
`[exe, arg1, arg2, ...]` 的**完整命令**。若用户已经配了别的 notify（例如截图里那台
机器配了 `codex-computer-use`），我们**不能覆盖**它——覆盖 = 悄悄关掉别人的功能。

⇒ 处理方式：若已有 `notify` 且不是我们的，**拒绝安装并说明**，让用户先决定
（可以手动把两者包成一个脚本）。这比"帮你合并"更安全：合并两个外部程序的行为
不是我们能替用户决定的。

## 退出码（与 WorkBuddy 安装器同表，`ADR-0055`）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 数据目录不可用 / 非空但没 `--reuse` |
| 3 | 解释器或 March7 不可用 |
| 4 | 写入校验失败（**已回滚**）；`--doctor` 用它表示"两处数据目录不一致" |

`--set-llm` 的退出码 5 在 Codex 侧不存在：Codex 用自己的模型配置，March7 的
MCP server 也从**同一个** `config.toml` 读 env（见下），不存在"另配一份 key"。

## 用法

```bash
python hosts/codex/install.py --doctor            # 前置检查（只读）
python hosts/codex/install.py --apply             # 装（notify + MCP）
python hosts/codex/install.py --uninstall         # 卸（回到装前）
python hosts/codex/install.py --sandbox <dir>     # 装进隔离目录，不碰真实 CODEX_HOME
```
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXIT_OK = 0
EXIT_DATA_DIR = 2
EXIT_PYTHON = 3
EXIT_WRITE = 4

SERVER_KEY = "March7"
"""`[mcp_servers.March7]` 的表名。"""

NOTIFY_SCRIPT = REPO_ROOT / "hosts" / "codex" / "codex_notify.py"
PANEL_SCRIPT = REPO_ROOT / "hosts" / "codex" / "codex_panel.py"
PANEL_HOOK_LABEL = "打开 March7 记忆面板"
NOTIFY_CHAIN_SCRIPT = REPO_ROOT / "hosts" / "codex" / "codex_notify_chain.py"
"""包装脚本：让 `notify` 同时调多个程序（`notify` 是单值，不能配两条）。"""

DATA_DIR_FLAG = "--data-dir"
"""写进 `notify` 命令行的数据目录参数（`ADR-0066` §5.3 第 1 条）。

⚠️ 它必须与 `codex_notify.py` 里的同名常量**同值**：两处都是字符串字面量，
故意不互相 import（安装器不该依赖被安装脚本的内部结构）。一致性由
`test_install_codex.py::test_notify_flag_matches_the_script` 钉住。
"""

LLM_ENV_KEYS = (
    "MARCH7_LLM_MAIN_BASE_URL",
    "MARCH7_LLM_MAIN_API_KEY",
    "MARCH7_LLM_MAIN_MODEL",
    "MARCH7_LLM_JUDGE_BASE_URL",
    "MARCH7_LLM_JUDGE_API_KEY",
    "MARCH7_LLM_JUDGE_MODEL",
    "MARCH7_LLM_FALLBACK_BASE_URL",
    "MARCH7_LLM_FALLBACK_API_KEY",
    "MARCH7_LLM_FALLBACK_MODEL",
)
"""必须写进 `[mcp_servers.March7.env]` 的 LLM 变量（前 6 个必需，后 3 个可选）。

## 为什么必须写进表里，而不是"靠宿主进程继承"（2026-11-16 实测，V2 的根因）

用假 MCP server 探过 Codex 到底给子进程什么环境（`codex exec` + 一个只 dump
`os.environ` 的脚本）：

```text
变量数: 25        ← 我的 shell 有 70+ 个，含 6 个 MARCH7_LLM_*
MARCH7_HOME = C:/tmp/envprobe/data   ← 只有写在表里的那一个
（没有任何 MARCH7_LLM_*）
```

⇒ **`env` 表就是子进程的完整环境**，不继承用户变量。于是：

| 通道 | 子进程由谁起 | 拿得到 key 吗 | 结果 |
|---|---|---|---|
| `notify`（自动写入） | 宿主后端，继承完整环境 | ✅ | 正常 |
| MCP server（工具面） | 按 `env` 表起 | ❌ | `load_config()` 抛 `ConfigError` ⇒ 8 个工具全返回 `Error executing tool` |

复现（去掉这 6 个变量再起 stdio server）得到的报错与 Codex App 里**逐字一致**。
这与 `ADR-0066` 是同一个坑的两面：那次是"env 管不到 notify"，这次是
"notify 拿得到、MCP 拿不到"。
"""


def collect_llm_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """从**当前进程环境**收集 LLM 变量（缺的跳过，不编造）。

    为什么不用 `--set-llm` 那种交互式录入：换 key 已经有 `scripts/rotate_llm_key.py`
    统一改四处副本；安装器只需要把**环境里现有的值**同步进配置表，
    这样"换 key"仍然只有一个入口。
    """
    source = os.environ if env is None else env
    out: dict[str, str] = {}
    for key in LLM_ENV_KEYS:
        value = (source.get(key) or "").strip()
        if value:
            out[key] = value
    return out


RUNTIME_ENV_KEYS = (
    "HF_HOME",
    "HF_HUB_OFFLINE",
    "HF_ENDPOINT",
    "HF_HUB_DISABLE_XET",
    "TRANSFORMERS_OFFLINE",
)
"""影响**本地编码模型**（BGE-M3）能不能加载的变量（有就带上，没有就跳过）。

## 为什么它们也必须进表（2026-11-16 实测，V2 的第二半）

本机 BGE-M3 的权重在 `HF_HOME=D:\\...\\models\\hf`（不是默认 `~/.cache/huggingface`），
而且 `HF_HUB_OFFLINE=1`（没有外网）。

MCP 子进程只拿得到表里的变量 ⇒ 少了 `HF_HOME` 就会去默认缓存找 ⇒ **找不到** ⇒
`sentence-transformers` 转头去 `huggingface.co` 下载 ⇒ 本机连不通（`WinError 10060`）
⇒ `March7_recall` 卡在重试里直到超时。

⚠️ 这一条**不是"锦上添花"**：`status` 只要 LLM 变量就够了，但 `recall`（V2 真正要验的
那个工具）必须能加载编码模型。上一版只写了 LLM 变量，`status` 立刻变绿而 `recall`
仍然挂 —— 这正是"半修"的形态。
"""


def collect_runtime_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """收集编码模型相关的环境变量（只收当前环境里真正设了的）。"""
    source = os.environ if env is None else env
    out: dict[str, str] = {}
    for key in RUNTIME_ENV_KEYS:
        value = (source.get(key) or "").strip()
        if value:
            out[key] = value
    return out


class InstallWriteError(RuntimeError):
    """写入校验失败（对应退出码 4）。"""


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def codex_home() -> Path:
    """Codex 的配置根：`CODEX_HOME` → `~/.codex`。

    ⚠️ 必须尊重 `CODEX_HOME`：本机实测有**两个** codex 配置目录
    （`~/.codex` 与 `D:/DevTools/codex-home`），用户的 cc-switch 会把 `CODEX_HOME`
    指到后者。写错目录 = 装到"另一个 codex"上，而用户看不出区别。
    """
    raw = (os.environ.get("CODEX_HOME") or "").strip()
    return Path(raw) if raw else Path.home() / ".codex"


def config_path(home: Path | None = None) -> Path:
    return (home or codex_home()) / "config.toml"


def default_python() -> Path:
    """钩子用的解释器：仓库 `.venv`（存在则用），否则当前解释器。"""
    executable = Path("Scripts", "python.exe") if os.name == "nt" else Path("bin", "python")
    candidate = REPO_ROOT / ".venv" / executable
    if candidate.is_file():
        return candidate
    return Path(sys.executable)


def default_data_dir() -> Path:
    raw = (os.environ.get("MARCH7_HOME") or "").strip()
    return Path(raw) if raw else Path.home() / ".March7"


# ---------------------------------------------------------------------------
# TOML 读写：**只做行级最小改动**，不引入 toml 依赖（依赖表锁死 5+1）
# ---------------------------------------------------------------------------

_TOML_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


def toml_string(value: str) -> str:
    """把 python 字符串渲染成 TOML basic string（含反斜杠转义）。

    为什么自己写而不引 `tomli-w`：依赖表锁死（`CONVENTIONS` §2）。
    这个函数覆盖的字符集足够小（路径 + 参数），不需要通用实现。
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def toml_array(items: list[str]) -> str:
    return "[" + ", ".join(toml_string(item) for item in items) + "]"


def find_line(lines: list[str], key: str) -> int | None:
    """找**顶层** `key = ...` 的行号（跳过 `[table]` 之后的同名键）。

    为什么要区分层级：`mcp_servers` 段里也可能有 `notify` 这样的名字，
    在错误的层级上改会写坏别人的配置。
    """
    in_table = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            in_table = True
            continue
        if stripped.startswith("[["):
            in_table = True
            continue
        if in_table:
            continue
        if re.match(rf"^{re.escape(key)}\s*=", stripped):
            return index
    return None


def find_table(lines: list[str], name: str) -> tuple[int, int] | None:
    """找 `[name]` 段的 (起始行, 结束行 exclusive)。

    ⚠️ **必须精确匹配表名**，不能前缀匹配。第一版用 `stripped.startswith("[")` 判结束，
    于是 `[mcp_servers.March7.env]` 被当成 `[mcp_servers.March7]` 的**内容** ⇒
    重装时主表没被替换、`.env` 被复制了两遍（本机实测：幂等测试红）。
    这也是 TOML 的语义：`[a]` 到下一个 `[b]` 之前是 a 的内容，而 `[a.b]` 是 a 的**子表**，
    它同样终止 a 的直接键区 —— 但对"整段替换"来说，子表也必须算在同一段里。
    """
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"[{name}]":
            start = index
            continue
        if start is None:
            continue
        if not stripped.startswith("["):
            continue
        # 到了另一个表头：若是 name 的子表 ⇒ 仍属于本段；否则本段结束
        child = stripped.startswith(f"[{name}.")
        if child:
            continue
        return start, index
    if start is not None:
        return start, len(lines)
    return None


def parse_notify(line: str) -> list[str] | None:
    """从 `notify = [...]` 那行解出字符串数组（失败 ⇒ None，不猜）。"""
    match = re.search(r"=\s*\[(.*)\]\s*(?:#.*)?$", line.strip())
    if not match:
        return None
    body = match.group(1)
    try:
        parsed = json.loads("[" + body.replace("'", '"') + "]")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return None
    return parsed


def _basename(token: str) -> str:
    """取路径的文件名（把 `\\` 与 `/` 都当分隔符，Windows 两种都收）。"""
    return token.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def is_our_notify(items: list[str]) -> bool:
    """这条 notify 是不是**我们自己的完整命令**（判据：某一段的**文件名**就是我们的脚本）。

    ⚠️ **不能用子串匹配**（2026-11-16 实测踩到）：本机 `notify` 是
    computer-use 包了一层——

    ```toml
    notify = ["<cua.exe>", "turn-ended", "--previous-notify",
              "[\"<python>\",\"<chain.py>\",\"--\",…,\"<codex_notify.py>\"]"]
    ```

    我们那段藏在**第四个参数的 JSON 字符串里**。用 `"codex_notify.py" in item`
    判断会得到 True ⇒ 安装器认为"这是我们的，可以覆盖" ⇒ `--apply` 会把整行
    换成只调我们自己的命令 ⇒ **用户的计算机使用功能被静默关掉**。
    这正是本模块开头写着"绝不覆盖别人的 notify"要防的那件事，只是换了个形状。

    所以判据收窄成：**某一段的文件名恰好是我们的脚本名**（`codex_notify.py`）。
    包装在别人里面的那种形态 ⇒ 这条返回 `False`（不是"我们的行"），
    它里面有我们的命令这件事由 `find_our_command` 负责识别。
    """
    return any(_basename(item) in {NOTIFY_SCRIPT.name, NOTIFY_CHAIN_SCRIPT.name} for item in items)


def find_our_command(items: list[str]) -> list[str] | None:
    """在一条 notify 里**递归**找出我们那条命令（找不到 ⇒ `None`）。

    为什么要递归：computer-use 那类"接管 notify"的工具会把上一条命令塞进自己的
    参数里（`--previous-notify "<json 数组>"`）。此时外层不是我们的，
    但**里面那一段**是——`--doctor` 必须能看见它，否则会误报
    "旧版安装，notify 里没有 --data-dir"（本机实测就是这个误报）。

    只往下钻**能解析成字符串数组的 JSON 参数**，不猜别的编码方式。
    """
    if is_our_notify(items):
        return items
    for item in items:
        text = (item or "").strip()
        if not text.startswith("["):
            continue
        try:
            inner = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(inner, list) and all(isinstance(x, str) for x in inner):
            found = find_our_command(inner)
            if found is not None:
                return found
    return None


def table_value(lines: list[str], table: str, key: str) -> str | None:
    """取 `[table]` 段里 `key = "值"` 的值（没有 ⇒ `None`）。

    只服务 doctor 的**只读**核对，所以刻意写得比通用 TOML 解析器窄：
    只认我们自己写的那几种形状（basic string）。宁可读不出来（报"缺"），也不猜。
    """
    span = find_table(lines, table)
    if span is None:
        return None
    start, end = span
    for line in lines[start:end]:
        match = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", line)
        if not match:
            continue
        raw = re.sub(r"\s+#.*$", "", match.group(1)).strip()
        try:
            return str(json.loads(raw))
        except json.JSONDecodeError:
            return raw.strip().strip('"').strip("'")
    return None


def table_keys(lines: list[str], table: str) -> set[str]:
    """`[table]` 段里出现过的键名集合（只读，不取值）。"""
    span = find_table(lines, table)
    if span is None:
        return set()
    start, end = span
    keys: set[str] = set()
    for line in lines[start:end]:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match:
            keys.add(match.group(1))
    return keys


def check_llm_env(cfg: Path) -> tuple[bool | None, str]:
    """`[mcp_servers.March7.env]` 里的 LLM 变量齐不齐（**只报键名，不显示值**）。

    为什么这条判据必须存在：Codex 给 MCP 子进程的环境**只有表里那些**，
    不继承用户环境变量。缺一个 `MARCH7_LLM_MAIN_API_KEY` ⇒ `load_config()` 抛
    `ConfigError` ⇒ **8 个工具全返回 `Error executing tool`**（V2 的根因，
    2026-11-16 实测）。`--doctor` 不查这一项，用户就只能看到那句没有任何信息量的话。
    """
    if not cfg.exists():
        return None, "config.toml 不存在（还没装）"
    lines = cfg.read_text(encoding="utf-8").splitlines()
    present = table_keys(lines, f"mcp_servers.{SERVER_KEY}.env")
    if not present:
        return False, f"没有 `[mcp_servers.{SERVER_KEY}.env]` 表"
    required = LLM_ENV_KEYS[:6]
    missing = [key for key in required if key not in present]
    if missing:
        return False, (
            "缺 " + "、".join(missing) + "\n"
            "    Codex **不把用户环境变量传给 MCP 子进程**（实测只有表里那些）⇒ "
            "工具面会全部返回 `Error executing tool`。\n"
            "    修法：设好环境变量后跑 `--refresh-llm-env`"
        )
    optional = [key for key in LLM_ENV_KEYS[6:] if key in present]
    return True, f"必需 {len(required)} 项齐（fallback {len(optional)} 项）"


def check_runtime_env(cfg: Path) -> tuple[bool | None, str]:
    """编码模型相关的变量（`HF_HOME` 等）在不在表里（**只报键名**）。

    判据刻意不对称：**只有"本机环境里设了、表里却没有"才判 FAIL**。
    默认缓存（`~/.cache/huggingface`）的机器不需要这些变量，不该被误报。
    """
    if not cfg.exists():
        return None, "config.toml 不存在（还没装）"
    lines = cfg.read_text(encoding="utf-8").splitlines()
    present = table_keys(lines, f"mcp_servers.{SERVER_KEY}.env")
    if os.environ.get("HF_HOME") and "HF_HOME" not in present:
        return False, (
            "本机 HF_HOME 指向非默认缓存，但表里没有它 ⇒ `March7_recall` 会去\n"
            "    huggingface.co 下载权重（本机连不通）而卡住。修法：`--refresh-llm-env`"
        )
    got = [key for key in RUNTIME_ENV_KEYS if key in present]
    return True, (f"{len(got)} 项" if got else "用默认缓存位置（无需额外变量）")


def check_notify_data_dir(cfg: Path) -> tuple[bool | None, str]:
    """校验 `notify` 与 `[mcp_servers.March7.env]` 两处数据目录**是否一致**。

    `ADR-0066` §5.3 第 3 条：两处不一致 ⇒ **报错并打印两个值**，但**不自动改**
    （自动改等于替用户决定"以哪个为准"，而这两处他可能都有意为之）。

    Returns:
        `(状态, 人话说明)`：`True` = 一致；`False` = 不一致或缺失；
        `None` = 这台机器还没装（不算错，doctor 只记 WARN）。
    """
    if not cfg.exists():
        return None, "config.toml 不存在（还没装）"
    lines = cfg.read_text(encoding="utf-8").splitlines()

    index = find_line(lines, "notify")
    if index is None:
        return None, "没有 notify（还没装 March7 的自动写入通道）"
    parsed = parse_notify(lines[index])
    if not parsed:
        return None, "notify 解析不出来（不敢猜它的结构）"
    ours = find_our_command(parsed)
    if ours is None:
        return None, "notify 里没有 March7 的命令（未改动它人的配置）"

    in_notify = notify_data_dir(ours)
    in_env = table_value(lines, f"mcp_servers.{SERVER_KEY}.env", "MARCH7_HOME")

    if in_notify is None:
        return False, (
            f"notify 里没有 {DATA_DIR_FLAG}（旧版安装）⇒ 自动写入会落到默认数据目录。"
            "重跑一次 `--apply` 即可补上"
        )
    if in_env is None:
        return False, (
            f"`[mcp_servers.{SERVER_KEY}.env]` 里没有 MARCH7_HOME ⇒ 工具面会落到默认目录"
        )
    if not _same_path(in_notify, in_env):
        return False, (
            "两处数据目录**不一致**（自动写入与工具面会读写两个不同的库）：\n"
            f"    notify  {DATA_DIR_FLAG}  : {in_notify}\n"
            f"    env      MARCH7_HOME   : {in_env}\n"
            "  重跑 `--apply` 会把两处对齐（不会替你合并旧库）"
        )
    return True, f"两处一致：{in_notify}"


# ---------------------------------------------------------------------------
# 写入（含回滚）
# ---------------------------------------------------------------------------


class Rollback:
    """记下每个文件**装前的原始字节**，失败时按原样还原或删除。"""

    def __init__(self) -> None:
        self._saved: dict[Path, bytes | None] = {}

    def snapshot(self, path: Path) -> None:
        if path in self._saved:
            return
        self._saved[path] = path.read_bytes() if path.exists() else None

    def restore_all(self) -> None:
        for path, original in self._saved.items():
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            except OSError:
                pass


def update_panel_hook(
    home: Path, *, python: Path | None = None, data_dir: Path | None = None,
) -> None:
    """安装、替换或只移除自己的 UserPromptSubmit 处理器。"""
    path = home / "hooks.json"
    if python is None and not path.exists():
        return
    try:
        content = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if not isinstance(content, dict):
            raise ValueError("顶层必须是对象")
        hooks = content.setdefault("hooks", {})
        groups = hooks.get("UserPromptSubmit", [])
        if not isinstance(groups, list) or any(
            not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
            for group in groups
        ):
            raise ValueError("UserPromptSubmit 必须是钩子组列表")
    except (ValueError, AttributeError) as exc:
        raise InstallWriteError(f"{path.name} 格式无效：{exc}") from exc

    original = json.dumps(content, ensure_ascii=False)
    for group in groups[:]:
        remaining = [
            handler for handler in group["hooks"]
            if not (
                isinstance(handler, dict)
                and handler.get("statusMessage") == PANEL_HOOK_LABEL
                and "codex_panel.py" in str(handler.get("command", ""))
            )
        ]
        if len(remaining) != len(group["hooks"]):
            if remaining:
                group["hooks"] = remaining
            else:
                groups.remove(group)
    if python is not None:
        args = [str(python), str(PANEL_SCRIPT), DATA_DIR_FLAG, str(data_dir), "--config", str(config_path(home))]
        if os.name == "nt":
            command = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in args)
        else:
            command = shlex.join(args)
        groups.append({"hooks": [{
            "type": "command", "command": command, "timeout": 90, "statusMessage": PANEL_HOOK_LABEL,
        }]})
    if groups:
        hooks["UserPromptSubmit"] = groups
    else:
        hooks.pop("UserPromptSubmit", None)
    if json.dumps(content, ensure_ascii=False) != original:
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def install_panel_only(home: Path, python: Path | None, data_dir: Path | None) -> int:
    """用现有 MCP 绑定接入面板，单独升级时不用改 notify 或 MCP。"""
    try:
        cfg = config_path(home)
        settings = tomllib.loads(cfg.read_text(encoding="utf-8-sig")) if cfg.exists() else {}
        entry = settings.get("mcp_servers", {}).get(SERVER_KEY, {})
        configured_dir = entry.get("env", {}).get("MARCH7_HOME")
        configured_python = entry.get("command")
        if SERVER_KEY in settings.get("mcp_servers", {}):
            if not configured_dir or not Path(configured_dir).expanduser().is_absolute():
                print("Codex 的 March7 MCP 配置缺少有效的绝对库目录。请用 --apply --data-dir 对齐。", file=sys.stderr)
                return EXIT_DATA_DIR
            if data_dir is not None and not _same_path(
                str(data_dir.expanduser().resolve()), str(Path(configured_dir).expanduser().resolve()),
            ):
                print("--panel-only 使用 Codex 已绑定的 MCP 库。要换库，请用 --apply --data-dir。", file=sys.stderr)
                return EXIT_DATA_DIR
            data_dir = Path(configured_dir)
        elif data_dir is None:
            print("未找到 Codex 已绑定的 March7 库，请指定 --data-dir。", file=sys.stderr)
            return EXIT_DATA_DIR
        if python is None:
            python = Path(configured_python) if configured_python else default_python()
        ok, detail = check_python(python)
        if not ok:
            print(f"❌ 解释器不可用：{detail}", file=sys.stderr)
            return EXIT_PYTHON
        home.mkdir(parents=True, exist_ok=True)
        update_panel_hook(home, python=python.resolve(), data_dir=data_dir.expanduser().resolve())
    except (OSError, ValueError, AttributeError, TypeError, InstallWriteError) as exc:
        print(f"❌ 无法配置面板钩子：{exc}", file=sys.stderr)
        return EXIT_WRITE
    print("已配置面板钩子。请重新打开宿主，并在提示时审阅和信任钩子。")
    return EXIT_OK


def install_notify(
    config: Path,
    python: Path,
    data_dir: Path,
    rb: Rollback,
    *,
    chain_existing: bool = False,
) -> str:
    """写 `notify = [...]`。返回一句人话说明做了什么。

    ⚠️ **`--data-dir` 必须写在命令行里**（`ADR-0066` §5.1 方案 A）：
    `[mcp_servers.March7.env]` 只管 MCP server 那**一个**子进程，管不到 `notify`
    这个**另一个**子进程——Codex 起 notify 时继承的是它自己的进程环境，里面没有
    `MARCH7_HOME` ⇒ `codex_notify.py` 回落 `~/.March7` ⇒ **写读分叉**（实测 P0）。
    这不是 Codex 的怪癖：凡"每个通道各起一个进程"的宿主都有这个坑。

    Raises:
        InstallWriteError: 已有别人的 notify（**不覆盖**，见模块文档）。
    """
    rb.snapshot(config)
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    lines = text.splitlines()

    our_command = _notify_command(python, data_dir)
    rendered = f"notify = {toml_array(our_command)}"

    index = find_line(lines, "notify")
    if index is not None:
        existing = parse_notify(lines[index])
        if existing is None:
            raise InstallWriteError(
                f"{config} 里已有的 notify 解析不出来（第 {index + 1} 行）。"
                "不敢动它——请手工检查那一行，或先删掉它再装。"
            )
        if not is_our_notify(existing):
            nested = find_our_command(existing)
            if nested is not None:
                # ⚠️ 别人的 notify 已经把**我们**包在里面了（本机 computer-use 的形态：
                # `--previous-notify "<含 codex_notify.py 的 JSON>"`）。
                # 这时再造一条 chain 会写出**第二个**写入通道 ⇒ 同一轮记两遍
                # （幂等键相同，通常只会漏掉一次，但"通常"不是判据）。
                raise InstallWriteError(
                    "这台机器的 notify 已经把 March7 包在别人的命令里了：\n"
                    f"  {_basename(existing[0])}\n"
                    "里面那段就是 March7（`codex_notify.py`）。\n"
                    "**不会在外面再套一层**（那会写出第二个写入通道）。\n"
                    "\n两个办法：\n"
                    "  ① 保持现状，只补数据目录：手工给里面那段加上 "
                    f"`{DATA_DIR_FLAG} <你的目录>`（`--doctor` 会告诉你两处是否一致）\n"
                    "  ② 先把 notify 还原成 March7 单独一条（删掉外面那层工具的包装），"
                    "再跑 `--apply`"
                )
            if not chain_existing:
                raise InstallWriteError(
                    "这台机器的 codex 已经配了别的 notify（不是 March7 的）：\n"
                    f"  {' '.join(existing)}\n"
                    "**不会覆盖它**（覆盖等于悄悄关掉你别的功能）。\n"
                    "\n三个办法：\n"
                    "  ① 加 `--chain-existing-notify`：安装器自动写一个包装脚本，"
                    "让它依次调【你原来的】和【March7】——两个都保留（推荐）\n"
                    "  ② 手工把 notify 改成包装脚本（见 hosts/codex/README.md）\n"
                    "  ③ 如果你不需要原来那个 notify，先手工删掉那一行再装"
                )
            # 串起来：把原命令与我们的命令都塞进包装脚本。
            # ⚠️ 我们那一段**必须也带 `--data-dir`**（`ADR-0066` §7 A6-4）：
            # chain 模式下 notify 起的仍然是包装脚本 ⇒ 数据目录同样不会自己继承过来。
            command = [
                str(python).replace("\\", "/"),
                str(NOTIFY_CHAIN_SCRIPT).replace("\\", "/"),
                "--",
                *existing,
                "--",
                *our_command,
            ]
            rendered = f"notify = {toml_array(command)}"
            lines[index] = rendered
            mode = "chain"
            merged = "\n".join(lines).rstrip() + "\n"
            config.write_text(merged, encoding="utf-8")
            _verify_notify(config, le=rendered, data_dir=data_dir, expect_chain=True)
            return mode

        lines[index] = rendered
        mode = "replace"
    else:
        # 插在第一个 `[table]` 之前：TOML 要求顶层键在任何表之前
        insert_at = len(lines)
        for i, line in enumerate(lines):
            if line.strip().startswith("["):
                insert_at = i
                break
        lines.insert(insert_at, rendered)
        mode = "insert"

    merged = "\n".join(lines).rstrip() + "\n"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(merged, encoding="utf-8")
    _verify_notify(config, le=rendered, data_dir=data_dir)
    return mode


def _notify_command(python: Path, data_dir: Path) -> list[str]:
    """我们那条 notify 命令（含 `--data-dir`）。

    路径统一转正斜杠：TOML 里反斜杠是转义字符，而 Codex 与 python 都认 `/`
    （与既有写法一致，避免同一条命令里两种分隔符混着出现）。
    """
    return [
        str(python).replace("\\", "/"),
        str(NOTIFY_SCRIPT).replace("\\", "/"),
        DATA_DIR_FLAG,
        str(data_dir).replace("\\", "/"),
    ]


def notify_data_dir(items: list[str]) -> str | None:
    """从 notify 命令里取 `--data-dir` 的值（没有 ⇒ `None`，不猜）。"""
    for index, item in enumerate(items):
        if item == DATA_DIR_FLAG and index + 1 < len(items):
            value = items[index + 1].strip()
            if value and not value.startswith("-"):
                return value
    return None


def _verify_notify(
    config: Path, *, le: str, data_dir: Path | None = None, expect_chain: bool = False
) -> None:
    """写完立刻读回校验：目标行在、能解析出我们的脚本、且 `--data-dir` 是想要的那个。

    ⚠️ 只验"行里含 codex_notify.py"是不够的——那正是这次 P0 的形状：
    **配置里写对了 ≠ 那个子进程读到了**（`ADR-0066` §4）。真正的判据是
    "命令行里有没有那个参数、值对不对"。
    """
    text = config.read_text(encoding="utf-8")
    if le not in text:
        raise InstallWriteError(f"{config} 写入后校验失败：找不到刚写的那行")
    for line in text.splitlines():
        if line.strip() == le:
            parsed = parse_notify(line)
            if not parsed or not is_our_notify(parsed):
                raise InstallWriteError(f"{config} 写入后校验失败：notify 解析不出我们的脚本")
            if data_dir is not None:
                actual = notify_data_dir(parsed)
                if actual is None:
                    raise InstallWriteError(
                        f"{config} 写入后校验失败：notify 里没有 {DATA_DIR_FLAG} "
                        "（没有它，自动写入会落到默认数据目录）"
                    )
                if _same_path(actual, str(data_dir)):
                    pass
                else:
                    raise InstallWriteError(
                        f"{config} 写入后校验失败：notify 的数据目录是 {actual}，"
                        f"应为 {data_dir}"
                    )
            if expect_chain:
                # 串起来时必须有包装脚本 + 至少一个 `--` 分隔符，
                # 否则说明"原命令"被吞了（那就等于悄悄关掉了用户别的功能）。
                joined = " ".join(parsed)
                if "codex_notify_chain.py" not in joined or "--" not in parsed:
                    raise InstallWriteError(
                        f"{config} 写入后校验失败：chain 模式缺少包装脚本或分隔符"
                    )
            return
    raise InstallWriteError(f"{config} 写入后校验失败：目标行不在顶层")


def _same_path(left: str, right: str) -> bool:
    """两个路径是不是同一个（Windows 大小写不敏感；分隔符不计）。"""
    import os as _os

    return _os.path.normcase(_os.path.normpath(left)) == _os.path.normcase(
        _os.path.normpath(right)
    )


def install_mcp(
    config: Path,
    python: Path,
    data_dir: Path,
    rb: Rollback,
    *,
    llm_env: dict[str, str] | None = None,
    runtime_env: dict[str, str] | None = None,
) -> str:
    """写 `[mcp_servers.March7]` 段。返回 mode 说明。

    ⚠️ `env` 里带 `MARCH7_HOME`：MCP server 由 Codex 起进程，**不继承我们的 shell 环境**
    ⇒ 不写进 env 就会落到默认 `~/.March7`（数据红线，`CONVENTIONS` §2）。

    ⚠️ **`llm_env` 也必须写进这张表**（见 `LLM_ENV_KEYS` 的实测记录）：Codex 给
    MCP 子进程的环境是"表里的东西"，**不继承用户环境变量**。不写 ⇒ 工具面连
    `load_config()` 都过不去 ⇒ 8 个工具全返回 `Error executing tool`（V2 的根因）。

    Args:
        llm_env: 要写进去的 LLM 变量（一般来自 `collect_llm_env()`）。
            空字典 ⇒ 只写原有的三项，并在返回的 mode 里标 `no-llm`。
    """
    rb.snapshot(config)
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    lines = text.splitlines()

    block = [
        f"[mcp_servers.{SERVER_KEY}]",
        f"command = {toml_string(str(python))}",
        'args = ["-m", "March7"]',
        "",
        *_mcp_env_block(data_dir, llm_env, runtime_env),
    ]

    existing = find_table(lines, f"mcp_servers.{SERVER_KEY}")
    if existing is not None:
        start, end = existing
        lines[start:end] = block
        mode = "replace"
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
        mode = "insert"

    merged = "\n".join(lines).rstrip() + "\n"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(merged, encoding="utf-8")

    check = config.read_text(encoding="utf-8")
    # ⚠️ 校验用 `toml_string(data_dir)` 而不是裸路径：TOML 里反斜杠是**转义字符**，
    # `C:\tmp\...` 会写成 `C:\\tmp\\...`。第一版拿裸路径去比对，必然找不到
    # （本机实测：沙盒安装因此报了"段不完整"，而文件其实是对的）。
    if f"[mcp_servers.{SERVER_KEY}]" not in check:
        raise InstallWriteError(f"{config} 写入后校验失败：找不到 [mcp_servers.{SERVER_KEY}]")
    if f"MARCH7_HOME = {toml_string(str(data_dir))}" not in check:
        raise InstallWriteError(f"{config} 写入后校验失败：MARCH7_HOME 不是我写的那一行")
    # 密钥只验"键在不在"，**绝不**把值拼进报错（`ADR-0065` 打码）
    for key in (*(llm_env or {}), *(runtime_env or {})):
        if f"{key} = " not in check:
            raise InstallWriteError(
                f"{config} 写入后校验失败：{key} 没写进去（值不显示）"
            )
    return mode if llm_env else f"{mode}(no-llm)"


def _mcp_env_block(
    data_dir: Path,
    llm_env: dict[str, str] | None,
    runtime_env: dict[str, str] | None,
) -> list[str]:
    """渲染 MCP 的 `.env` 子表，供初次安装与换 key 刷新共用。"""
    block = [
        f"[mcp_servers.{SERVER_KEY}.env]",
        f"MARCH7_HOME = {toml_string(str(data_dir))}",
        'PYTHONIOENCODING = "utf-8"',
        'PYTHONUNBUFFERED = "1"',
    ]
    extras = llm_env or {}
    for key in LLM_ENV_KEYS:  # 固定顺序 ⇒ 同一份输入写出逐字节相同的文件（幂等）
        if key in extras:
            block.append(f"{key} = {toml_string(extras[key])}")
    runtime = runtime_env or {}
    for key in RUNTIME_ENV_KEYS:
        if key in runtime:
            block.append(f"{key} = {toml_string(runtime[key])}")
    return block


def refresh_mcp_env(
    config: Path,
    data_dir: Path,
    rb: Rollback,
    *,
    llm_env: dict[str, str],
    runtime_env: dict[str, str] | None = None,
) -> str:
    """只替换 `[mcp_servers.March7.env]`，**绝不改 command / args / notify**。

    V5 的假绿根因就是这里复用了整套 `install_mcp()`，把刷新命令自己的
    `sys.executable` 写成了 MCP 解释器。换 key 与选择解释器是两件事：
    前者只能更新环境变量，后者必须由安装/升级动作显式决定。
    """
    rb.snapshot(config)
    if not config.exists():
        raise InstallWriteError(f"{config} 不存在：先跑 `--apply` 装一次")
    lines = config.read_text(encoding="utf-8").splitlines()
    if find_table(lines, f"mcp_servers.{SERVER_KEY}") is None:
        raise InstallWriteError(
            f"{config} 里没有 `[mcp_servers.{SERVER_KEY}]`：先跑 `--apply` 装一次"
        )
    span = find_table(lines, f"mcp_servers.{SERVER_KEY}.env")
    if span is None:
        raise InstallWriteError(
            f"{config} 里没有 `[mcp_servers.{SERVER_KEY}.env]`：先跑 `--apply` 装一次"
        )

    start, end = span
    lines[start:end] = _mcp_env_block(data_dir, llm_env, runtime_env)
    config.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    check = config.read_text(encoding="utf-8").splitlines()
    if table_value(check, f"mcp_servers.{SERVER_KEY}.env", "MARCH7_HOME") != str(data_dir):
        raise InstallWriteError(f"{config} 刷新后校验失败：MARCH7_HOME 不是我写的那一行")
    present = table_keys(check, f"mcp_servers.{SERVER_KEY}.env")
    missing = [key for key in (*llm_env, *(runtime_env or {})) if key not in present]
    if missing:
        raise InstallWriteError(
            f"{config} 刷新后校验失败：{'、'.join(missing)} 没写进去（值不显示）"
        )
    return "refresh-env"


# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------


def check_python(python: Path) -> tuple[bool, str]:
    """解释器能不能 `import March7`（起子进程，避免本进程被 torch 拖慢）。"""
    if not python.is_file():
        return False, f"{python} 不存在（先跑 uv sync，或用 --python 指定）"
    import subprocess

    try:
        result = subprocess.run(
            [str(python), "-c", "import March7; print(March7.__version__)"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(REPO_ROOT),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        return False, tail[-1] if tail else "(无输出)"
    return True, result.stdout.strip()


def check_configured_mcp(cfg: Path) -> tuple[bool | None, str]:
    """检查配置里**真正会被宿主执行**的 MCP command / args。

    返回 `None` 表示尚未安装 March7（不是故障）；`False` 表示配置存在但无法启动。
    不能只检查安装器的候选解释器：V5 实测出现过候选解释器 `0.1.0` 可导入，
    配置里的 WorkBuddy Python 却报 `No module named March7`，doctor 仍假绿。
    """
    if not cfg.exists():
        return None, "config.toml 不存在（还没装）"
    try:
        document = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return False, f"config.toml 无法解析：{type(exc).__name__}: {exc}"

    servers = document.get("mcp_servers")
    section = servers.get(SERVER_KEY) if isinstance(servers, dict) else None
    if section is None:
        return None, f"没有 `[mcp_servers.{SERVER_KEY}]`（还没装）"
    if not isinstance(section, dict):
        return False, f"`[mcp_servers.{SERVER_KEY}]` 不是 TOML 表"

    command = section.get("command")
    args = section.get("args")
    if not isinstance(command, str) or not command.strip():
        return False, "配置里没有可执行的 command"
    if args != ["-m", "March7"]:
        return False, f'args 应为 ["-m", "March7"]，实际是 {args!r}'

    ok, detail = check_python(Path(command))
    prefix = f"{command}："
    return ok, prefix + detail


def check_data_dir(data_dir: Path, *, reuse: bool) -> tuple[bool, str]:
    """数据目录三态：fresh / empty / 非空。**非空且没 `--reuse` ⇒ 拒绝**。"""
    if not data_dir.exists():
        return True, "fresh"
    if not data_dir.is_dir():
        return False, f"{data_dir} 不是目录"
    entries = [p for p in data_dir.iterdir()]
    if not entries:
        return True, "empty"
    if reuse:
        return True, "reuse"
    return False, (
        f"数据目录已存在且非空：{data_dir}\n"
        "  March7 不会自动合并旧库（那是不可逆的）。三选一：\n"
        "    ① 想沿用这个库 ⇒ 加 --reuse\n"
        "    ② 想换个新库 ⇒ 加 --data-dir <另一个路径>\n"
        "    ③ 想从零开始 ⇒ 自己把旧目录移走（不要删，先改名）"
    )


def run_doctor(python: Path, data_dir: Path, home: Path | None = None) -> int:
    """前置检查（只读）。`home` 给了就用它——**沙盒模式必须传**。

    ⚠️ 这里曾经自己重新 `codex_home()` 一遍，于是 `--doctor --sandbox <dir>`
    读的仍是**真实** `~/.codex`（实测 2026-11-16 踩到）：沙盒里明明装好了，
    doctor 却说"notify 里没有 --data-dir"，因为它在看另一个文件。
    这和本卡要修的是同一类错——**看错目录**。
    """
    print("== Codex 装机前置检查 ==")
    ok_all = True

    home = home or codex_home()
    print(f"CODEX_HOME    : {home}")
    print(f"  {'OK  ' if home.is_dir() else 'MISS'} 目录存在")

    cfg = config_path(home)
    print(f"config.toml   : {cfg}（{'存在' if cfg.exists() else '不存在，将创建'}）")

    ok, detail = check_python(python)
    print(f"  {'OK  ' if ok else 'FAIL'} 候选解释器 + March7 可导入 {detail}")
    ok_all &= ok

    mcp_state, mcp_detail = check_configured_mcp(cfg)
    if mcp_state is None:
        print(f"  WARN 配置 MCP 解释器     {mcp_detail}")
    elif mcp_state:
        print(f"  OK   配置 MCP 解释器     {mcp_detail}")
    else:
        print(f"  FAIL 配置 MCP 解释器     {mcp_detail}")
        ok_all = False

    ok, detail = check_data_dir(data_dir, reuse=False)
    print(f"  {'OK  ' if ok else 'WARN'} 数据目录                 {data_dir}（{detail if ok else '非空'})")

    codex_exe = shutil.which("codex")
    print(f"  {'OK  ' if codex_exe else 'WARN'} codex 在 PATH 上        {codex_exe or '没找到（装不装得上不影响，但用不了）'}")

    # `ADR-0066` §5.3 第 3 条：两处数据目录必须一致，否则"自动写了、工具查不到"。
    state, detail = check_notify_data_dir(cfg)
    if state is None:
        print(f"  WARN 数据目录两处一致     {detail}")
    elif state:
        print(f"  OK   数据目录两处一致     {detail}")
    else:
        print(f"  FAIL 数据目录两处一致     {detail}")
        ok_all = False

    # 工具面能不能起来，取决于**这张表**里有没有 LLM 变量（V2 的根因）。
    llm_state, llm_detail = check_llm_env(cfg)
    if llm_state is None:
        print(f"  WARN 工具面 LLM 变量      {llm_detail}")
    elif llm_state:
        print(f"  OK   工具面 LLM 变量      {llm_detail}")
    else:
        print(f"  FAIL 工具面 LLM 变量      {llm_detail}")
        ok_all = False

    rt_state, rt_detail = check_runtime_env(cfg)
    if rt_state is None:
        print(f"  WARN 编码模型变量          {rt_detail}")
    elif rt_state:
        print(f"  OK   编码模型变量          {rt_detail}")
    else:
        print(f"  FAIL 编码模型变量          {rt_detail}")
        ok_all = False

    print()
    if ok_all:
        print("可以装：python hosts/codex/install.py --apply")
        return EXIT_OK
    print("有 FAIL 项，先修再装。")
    return EXIT_PYTHON if state is not False else EXIT_WRITE


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def do_apply(
    home: Path,
    python: Path,
    data_dir: Path,
    *,
    reuse: bool,
    warmup: bool,
    chain_existing: bool = False,
    llm_env: dict[str, str] | None = None,
    runtime_env: dict[str, str] | None = None,
) -> int:
    ok, detail = check_python(python)
    if not ok:
        print(f"❌ 解释器不可用：{detail}", file=sys.stderr)
        return EXIT_PYTHON

    ok, state = check_data_dir(data_dir, reuse=reuse)
    if not ok:
        print(f"❌ {state}", file=sys.stderr)
        return EXIT_DATA_DIR
    print(f"数据目录      : {data_dir}（{state}）")

    cfg = config_path(home)
    rb = Rollback()
    try:
        notify_mode = install_notify(cfg, python, data_dir, rb, chain_existing=chain_existing)
        print(f"notify        : {notify_mode} ⇒ {NOTIFY_SCRIPT.name}")
        print(f"                （数据目录随命令行传：{DATA_DIR_FLAG} {data_dir}）")
        mcp_mode = install_mcp(cfg, python, data_dir, rb, llm_env=llm_env, runtime_env=runtime_env)
        rb.snapshot(home / "hooks.json")
        update_panel_hook(home, python=python.resolve(), data_dir=data_dir.resolve())
        print("面板钩子      : 已配置，新会话需在宿主审阅并信任")
        print(f"mcp_servers   : {mcp_mode} March7 条目（写进 {cfg.name}）")
        print(
            f"                （LLM 变量 {len(llm_env or {})} 项 / 运行时变量 "
            f"{len(runtime_env or {})} 项：只写键名，不显示值）"
        )
        if not llm_env:
            print(
                "⚠️ 当前环境里没有 MARCH7_LLM_* ⇒ 工具面起不来（Codex 不继承用户变量）。"
                "先设好环境变量再跑一次 `--refresh-llm-env`",
                file=sys.stderr,
            )
    except InstallWriteError as exc:
        rb.restore_all()
        print(f"❌ 安装失败，已回滚：{exc}", file=sys.stderr)
        return EXIT_WRITE
    except OSError as exc:
        rb.restore_all()
        print(f"❌ 写入失败，已回滚：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_WRITE

    if warmup:
        _warmup(python)

    # 不支持或未信任钩子的客户端仍可从库旁双击打开。
    entry = panel_entry.write_panel_entry(python, data_dir)
    print()
    if entry is not None:
        print(f"面板入口：{entry}")
    else:
        print("面板入口：未生成（可手动跑 python -m March7.control.panel_launcher）")
    print("下一步：")
    print("  ① 新开一个 codex 会话（配置在启动时读，当前会话看不到变化）")
    print("  ② 说一句关于你自己的事实，例如「我是做木工的，喜欢钓鱼」")
    print("  ③ 等 5–20 秒，跑 `python -m March7.control list` 看有没有记上")
    print()
    print("⚠️ Codex 侧**没有开局注入**：画像与记忆要靠模型主动调 March7_* 工具才看得到。")
    return EXIT_OK


def _warmup(python: Path) -> None:
    """装完预热编码模型（`ADR-0059` 决策 2 的同一条理由：把冷启动付在安装时）。"""
    import subprocess

    code = (
        "import sys;"
        "from March7.embedder import Embedder;"
        "e = Embedder();"
        "v = e.embed_one('预热');"
        "sys.stdout.write('dim=%d device=%s' % (len(v), e.device))"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", code], capture_output=True, text=True, timeout=240, cwd=str(REPO_ROOT)
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"预热          : 跳过（{type(exc).__name__}）——不影响使用，第一轮会慢一次")
        return
    if result.returncode != 0:
        print("预热          : 跳过——不影响使用，第一轮会慢一次")
        return
    print(f"预热          : 模型已加载（{result.stdout.strip()}）")


def do_uninstall(home: Path) -> int:
    cfg = config_path(home)
    try:
        update_panel_hook(home)
    except (InstallWriteError, OSError) as exc:
        print(f"❌ 无法移除面板钩子：{exc}", file=sys.stderr)
        return EXIT_WRITE
    if not cfg.exists():
        print("config.toml 不存在，无需卸载")
        return EXIT_OK

    text = cfg.read_text(encoding="utf-8")
    lines = text.splitlines()

    # 1) 剥掉 notify（仅当它是我们的）
    changed = False
    index = find_line(lines, "notify")
    if index is not None:
        parsed = parse_notify(lines[index])
        if parsed and is_our_notify(parsed):
            del lines[index]
            changed = True
            print("notify        : 已移除 March7 条目")
        elif parsed:
            if find_our_command(parsed) is not None:
                # 我们是**别人命令的参数**（computer-use 的 `--previous-notify`）。
                # 动它就得改别人程序的命令行参数，那不在"卸载自己"的授权范围内。
                print(
                    "notify        : March7 被包在别人的 notify 里（如 computer-use 的 "
                    "`--previous-notify`）；**不替用户改别人的参数**。"
                    "要清掉请手工删掉那一层 JSON 里的 March7 段"
                )
            else:
                print("notify        : 不是 March7 的，未改动")
    else:
        print("notify        : 没有 March7 条目，未改动")

    # 2) 剥掉 [mcp_servers.March7] 段（含其 .env 子表）
    #    ⚠️ 顺序：**先删主表**（它现在连子表一起覆盖，见 find_table 的注释），
    #    再删可能残留的 .env（早期版本装出来的配置里两者是分开的）⇒ 两边都能清干净。
    for name in (f"mcp_servers.{SERVER_KEY}", f"mcp_servers.{SERVER_KEY}.env"):
        span = find_table(lines, name)
        if span is not None:
            start, end = span
            del lines[start:end]
            changed = True
            print(f"mcp_servers   : 已移除 {name}")
    if not changed:
        print("mcp_servers   : 没有 March7 条目，未改动")

    if changed:
        cfg.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        check = cfg.read_text(encoding="utf-8")
        if SERVER_KEY in check or "codex_notify.py" in check:
            print("❌ 卸载后仍有残留，请手工检查 config.toml", file=sys.stderr)
            return EXIT_WRITE
    print()
    print("提醒：新开一个 codex 会话才会生效（配置在启动时读）。")
    return EXIT_OK


def do_refresh_llm_env(
    home: Path,
    *,
    llm_env: dict[str, str] | None = None,
    runtime_env: dict[str, str] | None = None,
) -> int:
    """只刷新 `[mcp_servers.March7.env]` 里的 LLM 变量，**不动 notify**。

    为什么单独一个动作、而不是让 `--apply` 顺手做（2026-11-16 实测）：
    - 有些机器的 `notify` 是被**别人的工具**包着的（computer-use 的
      `--previous-notify`），`--apply` 会**拒绝**碰它（这是对的，见 §10.1），
      但用户仍然需要把 key 刷进去；
    - 换 key 之后真正要改的就是这一处，重跑整套安装是多余的风险。

    数据目录**沿用配置里已有的值**（不在这个动作里改它）；配置里没有才用默认值。
    """
    cfg = config_path(home)
    if not cfg.exists():
        print(f"❌ {cfg} 不存在：先跑 `--apply` 装一次", file=sys.stderr)
        return EXIT_DATA_DIR
    if not llm_env:
        print(
            "❌ 当前环境里没有 MARCH7_LLM_* 变量 ⇒ 没有可写入的值。\n"
            "   先把 key 设进用户环境变量（或用 scripts/rotate_llm_key.py），再重跑本命令。",
            file=sys.stderr,
        )
        return EXIT_DATA_DIR

    lines = cfg.read_text(encoding="utf-8").splitlines()
    existing = table_value(lines, f"mcp_servers.{SERVER_KEY}.env", "MARCH7_HOME")
    data_dir = Path(existing) if existing else default_data_dir()

    rb = Rollback()
    try:
        mode = refresh_mcp_env(
            cfg,
            data_dir,
            rb,
            llm_env=llm_env,
            runtime_env=runtime_env,
        )
    except InstallWriteError as exc:
        rb.restore_all()
        print(f"❌ 刷新失败，已回滚：{exc}", file=sys.stderr)
        return EXIT_WRITE
    print(
        f"mcp env       : {mode}（LLM 变量 {len(llm_env)} 项 / 运行时变量 "
        f"{len(runtime_env or {})} 项，只写键名不显示值）"
    )
    print(f"数据目录      : {data_dir}（沿用原值）")
    print()
    print("下一步：**完全退出** Codex App / 宿主再重开（MCP server 只在启动时读配置）。")
    return EXIT_OK


def _load_panel_entry():
    """按路径加载 `hosts/panel_entry.py`（`hosts` 不是包；实现只留一份）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "march7_hosts_panel_entry",
        Path(__file__).resolve().parents[1] / "panel_entry.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


panel_entry = _load_panel_entry()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 March7 装进 Codex CLI（ADR-0061）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="接入 notify、MCP 和面板钩子")
    group.add_argument("--panel-only", action="store_true", help="仅安装或更新面板钩子")
    group.add_argument("--uninstall", action="store_true", help="卸掉，回到装前")
    group.add_argument("--doctor", action="store_true", help="前置检查（只读）")
    group.add_argument(
        "--refresh-llm-env",
        action="store_true",
        dest="refresh_llm_env",
        help="只把当前环境的 MARCH7_LLM_* 刷进 MCP env 表（不动 notify，换 key 后用）",
    )

    parser.add_argument("--python", default=None, help="钩子用的解释器（默认仓库 .venv）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 ~/.March7）")
    parser.add_argument("--reuse", action="store_true", help="允许复用非空数据目录")
    parser.add_argument("--sandbox", default=None, help="装进隔离目录（不碰真实 CODEX_HOME）")
    parser.add_argument("--no-warmup", action="store_true", help="装完不预热编码模型")
    parser.add_argument(
        "--chain-existing-notify",
        action="store_true",
        help="已有别人的 notify 时不拒绝，改为写一个包装脚本让两者并存",
    )
    args = parser.parse_args(argv)

    python = Path(args.python) if args.python else default_python()
    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_dir()
    home = codex_home()

    if args.sandbox:
        base = Path(args.sandbox).expanduser().resolve()
        home = base / "home" / ".codex"
        home.mkdir(parents=True, exist_ok=True)
        if not args.data_dir:
            data_dir = base / "data"
        print(f"沙盒模式：CODEX_HOME → {home}")

    if args.doctor:
        return run_doctor(python, data_dir, home)
    if args.panel_only:
        return install_panel_only(
            home, python if args.python else None, data_dir if args.data_dir or args.sandbox else None,
        )
    if args.uninstall:
        return do_uninstall(home)
    if args.refresh_llm_env:
        return do_refresh_llm_env(
            home, llm_env=collect_llm_env(), runtime_env=collect_runtime_env()
        )
    return do_apply(
        home,
        python,
        data_dir,
        reuse=args.reuse,
        warmup=not args.no_warmup,
        chain_existing=args.chain_existing_notify,
        llm_env=collect_llm_env(),
        runtime_env=collect_runtime_env(),
    )


if __name__ == "__main__":
    sys.exit(main())
