"""install.py — 把 March7 装进 WorkBuddy 宿主（S11a / `ADR-0055` / `ADR-0058` / `ADR-0059`）。

## 为什么这个脚本是「对外交付物」而不是内部工具

S11 起对外形态改为**本地测试包**（`ADR-0054`）：同学不看源码、`git clone` 下来跑一条命令。
于是安装器本身成为**用户看到的第一段代码**，它的三条失败形态都得在这里堵掉：

| 失败形态 | 后果 | 本脚本怎么堵 |
|---|---|---|
| 只写 hooks，不写 `mcp.json` | 钩子在跑、8 个工具**一个都调不到** | 一次写三处，缺一处整体回滚 |
| 静默合并已有非空库 | 记忆面板自相矛盾，**且不报错** | 非空 ⇒ 停下要 `--reuse` / `--data-dir` |
| 退出码恒 0 | 用户只能看到"好像成功了" | 五个码各有一种含义，写进 `ONBOARDING.md` |

## 用法

```bash
python hosts/workbuddy/install.py --apply                  # 三处一次写完
python hosts/workbuddy/install.py --apply --sandbox DIR    # 写进隔离目录（S11c 自证用）
python hosts/workbuddy/install.py --set-llm                # 收 key、探针、写进 mcp.json
python hosts/workbuddy/install.py --upgrade                # 备份 → 迁移 → 行数对账
python hosts/workbuddy/install.py --doctor                 # 装机前置检查（不写文件）
python hosts/workbuddy/install.py --status                 # 只看当前状态
python hosts/workbuddy/install.py --uninstall              # 回到装前状态
```

## 三条设计约束（读代码前先知道）

1. **密钥只写进 `~/.workbuddy/mcp.json` 的 `env`**（`ADR-0055` 决策 3）：不落 `.env`
   （本仓无 dotenv，落了也没人读）、不改用户级环境变量（`setx` 会截断长值并破坏
   `REG_EXPAND_SZ`，`ADR-0048` 同一条纪律）。
2. **回滚按"装前是否存在"决策**：装前有的文件 → 写回原字节；装前没有的 → 删掉。
   "留下一个空文件"会让第二次跑误判成"已经装过"。
3. **`--sandbox` 改的是三处目标的**根**，不是加一层前缀**：`Path.home()` 派生的
   `~/.workbuddy/*` 全部改到 `<sandbox>/home/.workbuddy/*`，数据目录改成
   `<sandbox>/data`。这样"沙盒里的成功"能当作"别的机器上的成功"的证据（`ADR-0059` §3.3）。

⚠️ **改完 `settings.json` 必须完全退出 WorkBuddy 再重开**（只关窗口不算，
`CONVENTIONS.md` §5 坑 5：配置在启动时快照）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
"""仓库根（本文件在 `hosts/workbuddy/` 下，往上两级）。

⚠️ **不得依赖 `.git` 存在**（`ADR-0054` 决策 4）：zip 源码包里没有 `.git`，
而 `--apply` 必须在解压目录里能跑。`parents[2]` 满足这一点。
"""

SRC_HOOKS_DIR = Path(__file__).resolve().parent / "hooks"
SRC_SKILL = Path(__file__).resolve().parent / "SKILL.md"

SKILL_DIR_NAME = "March7-memory"
"""技能目录名 = SKILL.md frontmatter 里的 `name`（宿主按 `<name>/SKILL.md` 落点）。"""

BACKUP_TAG = "bak-T14"
HOOK_FILES = ("_common.py", "session_start.py", "user_prompt_submit.py", "stop.py")
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")

PANEL_ENTRY_NAMES = ("March7 记忆面板.cmd", "March7-面板.sh")
"""安装器写进数据目录的面板入口（S17）。与 `control.panel_launcher.shortcut_path` 同源，
`tests/test_install_workbuddy.py` 有一条断言钉住两处不漂。"""

INSTALL_ARTIFACTS = frozenset(PANEL_ENTRY_NAMES + ("panel.json", "panel.lock"))
"""数据目录里的**安装/运行产物**。

为什么要单列：`check_data_dir` 的"非空"是为了拦住"静默合并旧库"（`ADR-0055` 决策 2），
而面板入口与状态文件不含任何记忆。把它们算进"非空"会产生一个自相矛盾的体验：
**卸载后重装**被自己的入口文件挡住。
"""

SERVER_KEY = "March7"
"""`mcp.json` 里 March7 server 的 key（与 `server.SERVER_NAME` / `_common.SERVER_KEY` 同名）。"""

# ---------------------------------------------------------------------------
# 退出码（`ADR-0055` 决策 6）—— 这是一份**契约**，不是内部约定
# ---------------------------------------------------------------------------

EXIT_OK = 0
"""全绿。用户该做的事：完全退出宿主再重开。"""

EXIT_DATA_DIR = 2
"""数据目录不存在 / 不可写 / 非空旧库且未给 `--reuse`。"""

EXIT_PYTHON = 3
"""解释器或 `March7` 包不可用。用户该做的事：先 `uv sync`。"""

EXIT_WRITE = 4
"""写入校验失败（**已回滚**）。用户该做的事：看 stderr 指的那一处。"""

EXIT_LLM = 5
"""LLM 探针失败。用户该做的事：检查 key / 余额 / 端点。"""


class InstallWriteError(RuntimeError):
    """某处写入或写入后校验失败 ⇒ 触发整体回滚、退出码 4。

    刻意**继承 RuntimeError 而不是 SystemExit**：回滚需要在 `main` 的统一出口里做，
    而 `SystemExit` 会从任何层级直接穿出去，让 `try/finally` 之外的东西无法介入。
    """


class ProbeError(RuntimeError):
    """LLM 探针失败 ⇒ 退出码 5，且**保留写前旧值**（`ADR-0055` 决策 3）。"""


# ---------------------------------------------------------------------------
# 宿主目录（普通模式与沙盒模式共用一套派生逻辑）
# ---------------------------------------------------------------------------


class HostPaths:
    """一次安装涉及的三处目标 + 数据目录的**唯一**派生点。

    为什么做成对象而不是一组模块级常量：`--sandbox` 要整体换根，
    而模块级常量在 import 时就算死了 —— 那会逼出"每个函数多一个参数"的写法，
    并且很容易漏掉某一处（漏掉的那处就**写到真实宿主里**了，正是 A6 要防的事）。
    """

    def __init__(self, *, sandbox: Path | None = None, data_dir: Path | None = None) -> None:
        self.is_sandbox = sandbox is not None
        if sandbox is None:
            self.home = Path.home()
            self.host_home = self.home / ".workbuddy"
            self.default_data_dir = self.home / ".March7"
        else:
            self.home = Path(sandbox) / "home"
            self.host_home = self.home / ".workbuddy"
            self.default_data_dir = Path(sandbox) / "data"
        self.data_dir = Path(data_dir) if data_dir is not None else self.default_data_dir

    @property
    def hooks_dir(self) -> Path:
        return self.host_home / "hooks"

    @property
    def skills_dir(self) -> Path:
        return self.host_home / "skills"

    @property
    def settings(self) -> Path:
        return self.host_home / "settings.json"

    @property
    def mcp_json(self) -> Path:
        return self.host_home / "mcp.json"

    @property
    def skill_dir(self) -> Path:
        return self.skills_dir / SKILL_DIR_NAME


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def repo_python() -> Path:
    """宿主钩子该用哪个解释器。

    **用仓库自己的 venv**：那里装齐了 March7 及其依赖（pydantic / sqlite-vec / torch…），
    与 MCP server 是**同一套依赖** ⇒ 钩子与 server 不可能因为依赖版本不同而行为分叉。
    用宿主自带的 Python 会缺依赖（WorkBuddy 自带的 Python 里没有 March7）。
    """
    candidates = (
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
        REPO_ROOT / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def _slash(path: Path | str) -> str:
    """路径转成 POSIX 风格（坑 3：Windows 钩子经 Git Bash 执行，路径要用 `/`）。"""
    return str(path).replace("\\", "/")


def _cmd(python: Path, script: Path) -> str:
    """生成钩子命令串：解释器与脚本都加引号（路径里有空格时不加引号会被拆开）。"""
    return '"{}" "{}"'.format(_slash(python), _slash(script))


def _read_json(path: Path) -> dict:
    """读 JSON 对象；不存在 ⇒ `{}`；坏 JSON ⇒ 抛 `InstallWriteError`（不猜、不覆盖）。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallWriteError(f"{path} 不是合法 JSON，未改动：{exc}") from exc
    if not isinstance(data, dict):
        raise InstallWriteError(f"{path} 顶层不是 JSON 对象，未改动")
    return data


def _write_json(path: Path, data: dict) -> None:
    """写 JSON（落盘前先序列化一次：序列化失败就不落盘）。"""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    json.loads(text)  # 落盘前确认可解析
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 装机前置检查（--doctor 与 --apply 共用同一套判据）
# ---------------------------------------------------------------------------


def check_no_stdlib_shadow() -> list[str]:
    """**装进宿主目录的脚本名不得与标准库同名**（T13 验收 P0-1 的同一颗地雷）。

    宿主把钩子目录放进 `sys.path`（脚本自己 `sys.path.insert`）⇒ 若脚本名与标准库模块同名
    （`profile` / `types` / `code` / `json` / `email` / `queue` …），
    `import <同名模块>` 就会命中**钩子目录里的那个** ⇒ 标准库内部引用随之断链
    （历史案例：`cProfile` → `profile.run` → torch 整条导入链崩）。
    这颗雷在宿主里发作的后果与仓库里一模一样：**所有工具一起挂、且没有报错原文**。
    """
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    if not stdlib:  # Python < 3.10 没有该属性
        return []
    return [name for name in HOOK_FILES if Path(name).stem in stdlib]


def check_python(python: Path) -> None:
    """解释器存在且能 `import March7`。

    为什么第二条也要查：`--apply` 会把钩子命令指向这个解释器；
    若它 import 不到 March7，钩子会**每轮都静默失败**（`run_hook` 兜住异常 → exit 0），
    而界面上完全看不出来。
    """
    if not python.exists():
        raise FileNotFoundError(f"找不到可用的 Python 解释器：{python}")
    check = subprocess.run(
        [str(python), "-c", "import March7, sys; sys.stdout.write(March7.__version__)"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check.returncode != 0:
        detail = (check.stderr or check.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "(无输出)"
        raise ImportError(f"{python} 里 import March7 失败：{tail}")


def check_data_dir(paths: HostPaths, *, reuse: bool) -> str:
    """数据目录三态检查；返回 `"fresh"` / `"empty"` / `"reuse"`。

    | 状态 | 判据 | 处理 |
    |---|---|---|
    | `fresh` | 不存在 | 建出来（全新安装的正常路径） |
    | `empty` | 存在但没有任何条目 | 直接用（`mkdir` 之后重跑不该莫名失败） |
    | 非空 | 存在且有内容 | **停下**，要 `--reuse` 或换 `--data-dir` |

    Raises:
        NotADirectoryError: 路径被同名**文件**占住（不可写）。
        FileExistsError: 非空且未给 `--reuse`（`ADR-0055` 决策 2 的红线）。
    """
    data_dir = paths.data_dir
    if data_dir.exists() and not data_dir.is_dir():
        raise NotADirectoryError(f"数据目录位置上是个文件，不是目录：{data_dir}")
    if not data_dir.exists():
        return "fresh"
    try:
        entries = list(data_dir.iterdir())
    except OSError as exc:
        raise NotADirectoryError(f"数据目录不可读：{data_dir}（{exc}）") from exc
    if not entries:
        return "empty"
    # 只过滤**安装/运行产物**（面板入口、状态文件），真正的内容一律照旧拦下。
    if not [entry for entry in entries if entry.name not in INSTALL_ARTIFACTS]:
        return "empty"
    if reuse:
        return "reuse"
    raise FileExistsError(
        f"数据目录已存在且非空：{data_dir}\n"
        "  为什么不自动合并：`ADR-0049` 之前的库 scope 全 global、claims 表可能缺行，"
        "半新半旧的库会让记忆面板自相矛盾**而且不报错**。\n"
        "  三个选择：\n"
        "    ① 想沿用这个库            ⇒ 加 --reuse\n"
        "    ② 想用一个新的干净库      ⇒ 加 --data-dir <另一个路径>\n"
        "    ③ 想重来                  ⇒ 自己把它移走（本脚本不删数据）"
    )


def run_doctor(paths: HostPaths, *, python: Path | None = None) -> list[tuple[str, bool, str]]:
    """装机前置检查：返回 `[(检查项, 是否通过, 说明)]`。**只读，不写任何文件**。"""
    python = python or repo_python()
    rows: list[tuple[str, bool, str]] = []

    rows.append(("宿主目录", paths.host_home.exists(), _slash(paths.host_home)))

    shadows = check_no_stdlib_shadow()
    rows.append(("脚本名不撞标准库", not shadows, "、".join(shadows) if shadows else "OK"))

    try:
        check_python(python)
        rows.append(("解释器 + March7 可导入", True, _slash(python)))
    except (FileNotFoundError, ImportError) as exc:
        rows.append(("解释器 + March7 可导入", False, str(exc)))

    try:
        state = check_data_dir(paths, reuse=True)  # 只判断可写，不用它做决定
        writable = True
        note = f"{_slash(paths.data_dir)}（{state}）"
    except (NotADirectoryError, OSError) as exc:
        writable = False
        note = str(exc)
    rows.append(("数据目录可写", writable, note))

    return rows


# ---------------------------------------------------------------------------
# 写入三处
# ---------------------------------------------------------------------------


def copy_hooks(paths: HostPaths) -> list[Path]:
    """把四个钩子脚本复制到 `<host>/hooks/`。"""
    paths.hooks_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in HOOK_FILES:
        src = SRC_HOOKS_DIR / name
        if not src.exists():
            raise InstallWriteError(f"仓库里缺少钩子脚本：{src}")
        dst = paths.hooks_dir / name
        shutil.copy2(src, dst)
        written.append(dst)
    return written


def copy_skill(paths: HostPaths) -> Path:
    """把 SKILL.md 装到 `<host>/skills/<name>/SKILL.md`（宿主的技能落点）。"""
    if not SRC_SKILL.exists():
        raise InstallWriteError(f"仓库里缺少 SKILL.md：{SRC_SKILL}")
    paths.skill_dir.mkdir(parents=True, exist_ok=True)
    dst = paths.skill_dir / "SKILL.md"
    shutil.copy2(SRC_SKILL, dst)
    return dst


def build_hooks_block(paths: HostPaths, python: Path) -> dict:
    """构造 `settings.json` 的 `hooks` 块（三个事件各一个脚本）。

    `SessionStart` **不设 matcher**：实测宿主在 `startup`（新建会话）与 `resume`
    （重开旧会话）两种情况下都会触发该事件；画像注入是**只读**的 ⇒ 重复注入天然幂等。
    """
    return {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _cmd(python, paths.hooks_dir / "session_start.py"),
                        "timeout": 60,
                    }
                ]
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _cmd(python, paths.hooks_dir / "user_prompt_submit.py"),
                        "timeout": 60,
                    }
                ]
            }
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _cmd(python, paths.hooks_dir / "stop.py"),
                        "timeout": 120,
                    }
                ]
            }
        ],
    }


def _is_our_hook(hook: dict, paths: HostPaths) -> bool:
    """这条 hook 是不是本安装器写的（按 `command` 里的脚本路径判定）。"""
    command = str(hook.get("command") or "")
    if not command:
        return False
    probe_dir = _slash(paths.hooks_dir) + "/"
    return any((probe_dir + name) in command for name in HOOK_FILES)


def install_settings(paths: HostPaths, python: Path) -> str:
    """把 `hooks` 块写进 `settings.json`；返回 `"insert"` / `"replace"` / `"already-current"`。

    两条路径：
    - 文件里**还没有** `hooks` 键 ⇒ 文本插入（最小 diff，保留用户其它配置的原始格式）；
    - 已经有了 ⇒ **先剥离我们的、再追加我们的**（`merge`），不是整体替换。

    ⚠️ **"整体替换"是本卡修掉的一个真 bug**：旧实现 `data["hooks"] = block` 会把用户
    自己的钩子一起吃掉。而宿主侧钩子是**用户配的**（他可能装了别的插件），
    静默丢掉别人的钩子 = 装一个插件弄坏另一个，且没有任何报错。
    现在改成 `merge`：剥掉指向我们脚本的条目，其余原样留下，再追加我们的组。
    这个顺序天然幂等——重跑一次的结果与跑一次相同。

    **幂等重跑不写备份**（T14 验收 P3-4）：什么都没改却每跑一次就多一份备份，
    备份目录会被无意义副本淹没，而"回滚到错的备份"是踩过的坑。
    """
    target = paths.settings
    block = build_hooks_block(paths, python)

    if not target.exists():
        _write_json(target, {"hooks": block})
        return "insert"

    original = target.read_text(encoding="utf-8")
    try:
        data = json.loads(original)
    except json.JSONDecodeError as exc:
        raise InstallWriteError(f"{target} 不是合法 JSON，未改动：{exc}") from exc
    if not isinstance(data, dict):
        raise InstallWriteError(f"{target} 顶层不是 JSON 对象，未改动")

    if "hooks" not in data:
        merged = _insert_hooks_text(original, block)
        target.write_text(merged, encoding="utf-8", newline="\n")
        mode = "insert"
    else:
        merged_hooks = merge_hooks(data.get("hooks"), block, paths)
        if merged_hooks == data.get("hooks"):
            return "already-current"
        data["hooks"] = merged_hooks
        _write_json(target, data)
        mode = "replace"

    # 复读校验：写进去的东西必须真的能被解析出来（否则等于没装）
    verify = json.loads(target.read_text(encoding="utf-8"))
    for event in HOOK_EVENTS:
        if event not in verify.get("hooks", {}):
            raise InstallWriteError(f"写入后校验失败：{event} 事件缺失")
    return mode


def merge_hooks(existing: object, block: dict, paths: HostPaths) -> dict:
    """把我们的 `block` 合并进已有 `hooks`：**先剥我们的，再追加我们的**。

    Args:
        existing: 现有的 `hooks` 值（可能是任意 JSON —— 坏形状按空处理，但**不丢**）。
        block: `build_hooks_block()` 产出的三个事件。
        paths: 用于判断"哪条是我们的"。

    Returns:
        合并后的 `hooks` 字典。用户原有的其它钩子**按原顺序保留**。

    Raises:
        InstallWriteError: `existing` 不是对象。**不猜、不覆盖**——把它替换掉等于
            静默删用户配置；把它挪到某个"备份键"里又会写进宿主的配置文件找死。
            让用户自己修那一处，是本项目一贯的"响亮失败"。
    """
    if not isinstance(existing, dict):
        raise InstallWriteError(
            "settings.json 的 hooks 不是 JSON 对象（形状不认识）。"
            "本脚本不改它——请先手工把它改成对象或删掉这一键，再重跑。"
        )

    merged: dict = {}
    for event, groups in existing.items():
        if not isinstance(groups, list):
            merged[event] = groups  # 形状不认识 ⇒ 原样留着
            continue
        kept = []
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                kept.append(group)
                continue
            remaining = [
                h for h in entries if not (isinstance(h, dict) and _is_our_hook(h, paths))
            ]
            if remaining:
                kept.append({**group, "hooks": remaining})
        if kept:
            merged[event] = kept

    for event in HOOK_EVENTS:
        merged[event] = list(merged.get(event, [])) + list(block.get(event, []))
    return merged


def _insert_hooks_text(original: str, block: dict) -> str:
    """在原文件里**插入** `hooks` 键（不整体 reformat 用户的其它配置）。

    做法与 T3 探针一致：在首个 `{` 之后插入文本片段 ⇒ 文件其余字节保持原样。
    插入后**先 json.loads 校验**，不合法就中止、不落盘。

    ⚠️ **空对象 `{}` 是特例，必须不带尾逗号**（独立验收第 3 轮抓到的真 bug）：
    朴素实现无条件插 `inner + ","`，对 `{}` 会产出 `{ "hooks": {...}, }`——
    尾逗号不是合法 JSON ⇒ `json.loads(merged)` 抛错 ⇒ 安装以**未捕获异常**退出（exit 1，
    且不是五个契约码之一）。
    触发路径有两条，都很常见：
    ① 用户卸载后再装（`uninstall` 会把文件清成 `{}`，见下）；
    ② 宿主自己生成过一个空的 `settings.json`。
    """
    text = json.dumps({"hooks": block}, ensure_ascii=False, indent=2)
    inner = text.strip()[1:-1].strip()
    brace = original.index("{")
    tail = original[brace + 1 :]
    # 剥掉 `}` 之后的内容判断"是不是空对象"：`{}` / `{  }` / `{}  \n` 都算空
    empty_object = not tail.replace("}", "").strip()
    separator = "" if empty_object else ","
    merged = original[: brace + 1] + "\n  " + inner + separator + tail
    json.loads(merged)
    return merged


def build_mcp_entry(paths: HostPaths, python: Path) -> dict:
    """构造 `mcp.json` 的 `mcpServers.March7` 条目。

    **`command` 与 `MARCH7_HOME` 一律由本机解析结果渲染**（`ADR-0055` 决策 1）——
    不从 snippet 复制。snippet 里写死过两个人名路径，换机器必然错，
    且错法是"看起来装好了"（工具列得出来、调用抛 ConfigError）。
    """
    return {
        "command": _slash(python),
        "args": ["-m", "March7"],
        "env": {
            "MARCH7_HOME": _slash(paths.data_dir),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        },
    }


def install_mcp_json(paths: HostPaths, python: Path, *, extra_env: dict | None = None) -> str:
    """把 March7 条目写进 `mcp.json`；返回 `"insert"` / `"update"` / `"already-current"`。

    保留用户其它 server 条目与其它键（只动 `mcpServers.March7`）。
    `extra_env` 用于 `--set-llm` 追加 LLM 三件套（它只改 `env`，不动 command）。

    ## 为什么显式删 `disabled`（`ADR-0046` 决策 2 的落地保证）

    工具面默认**开启**是拍过板的决定。但"默认开启"不能只靠"我们不写它"——
    如果用户机器上**残留**过一次 `"disabled": true`（历史版本、或用户自己关过），
    `{**existing, **entry}` 会把那个键原样带回来 ⇒ 装完 8 个工具**仍然调不到**，
    而安装报告会显示"已装好"。

    实测背景：本机 `~/.workbuddy/mcp.json` 里 `bilibili-mcp` 就带着 `"disabled": true`，
    说明这个字段确实会被别的工具写进同一个文件。所以这里**主动剥掉**，
    并在剥掉时明确告诉用户（他可能是有意关的）。
    """
    target = paths.mcp_json
    data = _read_json(target)
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    if not isinstance(servers, dict):
        raise InstallWriteError(f"{target} 的 mcpServers 不是对象，未改动")

    entry = build_mcp_entry(paths, python)
    existing = servers.get(SERVER_KEY)
    if isinstance(existing, dict):
        merged = {**existing, **entry}
        merged["env"] = {**entry["env"], **(existing.get("env") or {})}
        if extra_env is not None:
            merged["env"].update(extra_env)
        # 数据目录永远以本次解析为准（否则 `--set-llm` 会把旧路径又带回来）
        merged["env"]["MARCH7_HOME"] = entry["env"]["MARCH7_HOME"]
        # ADR-0046 决策 2：默认开启。残留的 disabled 必须剥掉，否则工具调不到。
        was_disabled = merged.pop("disabled", None) is not None
        if was_disabled:
            print(
                "提醒：mcp.json 里 March7 原来带 disabled 键（工具被关着），"
                "已按 ADR-0046 剥掉 ⇒ 8 个工具现在可用。\n"
                "     如果你是想关掉它，装完再手工加回 \"disabled\": true。"
            )
        mode = "already-current" if merged == existing else "update"
        servers[SERVER_KEY] = merged
    else:
        entry = build_mcp_entry(paths, python)
        if extra_env is not None:
            entry["env"].update(extra_env)
        servers[SERVER_KEY] = entry
        mode = "insert"

    if mode != "already-current":
        _write_json(target, data)

    # 复读校验：工具面全靠这一条，写歪了就等于装了个调不到的工具集
    verify = _read_json(target)
    installed = verify.get("mcpServers", {}).get(SERVER_KEY)
    if not isinstance(installed, dict):
        raise InstallWriteError("写入后校验失败：mcpServers.March7 缺失")
    if not installed.get("command"):
        raise InstallWriteError("写入后校验失败：March7 条目没有 command")
    if Path(str(installed["command"])) != python:
        raise InstallWriteError("写入后校验失败：March7 条目的 command 与预期解释器不一致")
    # ADR-0046：默认开启。这条校验是"承诺"而不是"意图"——
    # 少了它，"disabled 被带回来"这种失败会一直静默到最后一次用户点击。
    if installed.get("disabled"):
        raise InstallWriteError(
            "写入后校验失败：March7 条目仍是 disabled —— 工具调不到就等于没装"
        )
    return mode


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------


class Rollback:
    """记录"装前状态"，失败时逐项还原。

    为什么不用备份文件回滚（T14 验收 P2-1 的教训，**实跑即坏**）：
    备份是在**写之前**拍的，但第二次安装时那份备份里**已经含 hooks 块**；
    `uninstall` 原来取最新备份 ⇒ 回滚后 `hooks` 键仍在、脚本已被移走 ⇒
    宿主每轮执行一个指向不存在脚本的命令 ⇒ 非 0 退出 ⇒ **整轮熔断**（坑 2）。
    ⇒ 这里记的是**内存里的原始字节**，不是磁盘上的备份文件。
    """

    def __init__(self) -> None:
        self._files: dict[Path, bytes | None] = {}
        self._dirs: list[Path] = []

    def snapshot(self, path: Path) -> None:
        """记下文件装前的内容（`None` = 装前不存在）。只记一次。"""
        if path in self._files:
            return
        self._files[path] = path.read_bytes() if path.exists() else None

    def snapshot_dir(self, path: Path) -> None:
        """记下一个**本次可能新建**的目录（回滚时若非空则只留不删）。"""
        if path not in self._dirs:
            self._dirs.append(path)

    def restore(self) -> list[str]:
        """逐项还原；返回做了什么（给 stderr 用，用户要能看出"退到哪一步"）。"""
        actions: list[str] = []
        for path, content in reversed(list(self._files.items())):
            try:
                if content is None:
                    if path.exists():
                        path.unlink()
                        actions.append(f"删除 {path.name}（装前不存在）")
                else:
                    if not path.exists() or path.read_bytes() != content:
                        path.write_bytes(content)
                        actions.append(f"还原 {path.name}（装前内容）")
            except OSError as exc:  # noqa: BLE001 — 回滚本身失败必须报出来，不能吞
                actions.append(f"⚠️ 还原 {path.name} 失败：{exc}")
        for path in reversed(self._dirs):
            try:
                if path.exists() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                pass  # 目录留着的代价只是"多一个空目录"，不值得再抛
        return actions


# ---------------------------------------------------------------------------
# LLM 配置与探针
# ---------------------------------------------------------------------------

LLM_ENV_KEYS = ("MARCH7_LLM_MAIN_BASE_URL", "MARCH7_LLM_MAIN_API_KEY", "MARCH7_LLM_MAIN_MODEL")


def probe_llm(*, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> None:
    """发一次 `max_tokens=1` 的探针；失败抛 `ProbeError`。

    为什么必须真打一次：`--set-llm` 写完就返回成功的话，用户会拿到一个
    "装上了但每轮静默失败"的状态——而钩子层**恒 exit 0**，界面上什么都不会响
    （`CONVENTIONS` §5 坑 2）。这一发是唯一能在装机时暴露 401/402/404 的地方。

    ⚠️ 只依赖标准库（`urllib`）：安装器要在 `uv sync` **之前**就能跑
    （那时连 `openai` 都还没装）。`openai` 包不是必需路径。
    """
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001 — 读不到 body 不该掩盖状态码
            detail = "(无法读取响应体)"
        raise ProbeError(f"HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProbeError(f"连不上端点：{exc}") from exc


def set_llm(paths: HostPaths, python: Path, *, base_url: str, api_key: str, model: str) -> str:
    """写 LLM 三件套到 `mcp.json` 的 `env`，**先探针后落盘**。

    顺序是刻意的（`ADR-0055` 决策 3）：探针失败时**一个字节都不写**，
    用户手上仍是写前的旧值——比"写坏了再回滚"少一次磁盘往返，也少一次误判机会。
    """
    probe_llm(base_url=base_url, api_key=api_key, model=model)
    extra = dict(zip(LLM_ENV_KEYS, (base_url, api_key, model), strict=True))
    return install_mcp_json(paths, python, extra_env=extra)


# ---------------------------------------------------------------------------
# 升级
# ---------------------------------------------------------------------------


def upgrade(paths: HostPaths, *, python: Path | None = None) -> int:
    """升级三步：**备份 → 迁移 → 行数对账**（`ADR-0058` 决策 2）。

    为什么备份必须在迁移之前：`store._migrate` 直接 `BEGIN IMMEDIATE` 改库，
    每级有事务回滚，但**进程被杀就停在中间版本**，没有任何恢复手段
    （而 `scripts/downgrade_schema.py` 反而有 `Connection.backup()` ——
    保护装在了不常走的那条路上）。

    Returns:
        行数（`memories` 表）；库不存在 ⇒ 返回 `-1`（调用方据此不做对账）。
    """
    db_path = paths.data_dir / "March7.db"
    if not db_path.exists():
        return -1

    stamp = time.strftime("%Y%m%d%H%M%S")
    with sqlite3.connect(str(db_path)) as conn:
        from_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        before_rows = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

        backup_path = db_path.with_name(f"{db_path.name}.bak-v{from_version}-{stamp}")
        with sqlite3.connect(str(backup_path)) as backup_conn:
            conn.backup(backup_conn)

    # ⚠️ 用 `immutable=1` 只读打开备份来数行：普通 `connect` 会在备份旁边
    # 建出 `-wal` / `-shm` 两个副文件，于是「一份备份」在目录里长成三个文件。
    # 那不只是难看——用户按文件名找回时会被 `-wal` 误导成"还有没写进去的数据"。
    backup_uri = "file:{}?mode=ro&immutable=1".format(backup_path.as_posix())
    with sqlite3.connect(backup_uri, uri=True) as backup_conn:
        backup_rows = int(backup_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    # 迁移交给引擎（它自己逐级 + 每级一个事务）
    from March7.store import MemoryStore

    store = MemoryStore(db_path)
    try:
        store.init_schema()
        to_version = store.schema_version
    finally:
        store.close()

    with sqlite3.connect(str(db_path)) as conn:
        after_rows = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    print(f"备份　　：{backup_path.name}（v{from_version}，{backup_rows} 行）")
    print(f"迁移　　：v{from_version} → v{to_version}")
    print(f"对账　　：备份 {backup_rows} 行 / 现在 {after_rows} 行")
    if backup_rows != after_rows:
        raise InstallWriteError(
            f"升级对账失败：备份 {backup_rows} 行 ≠ 迁移后 {after_rows} 行。"
            f"备份在 {backup_path}，可用它恢复。"
        )
    return after_rows


# ---------------------------------------------------------------------------
# 卸载
# ---------------------------------------------------------------------------


def _strip_our_hooks(data: dict, paths: HostPaths) -> bool:
    """从 settings 里**精确剥离**本安装器写入的钩子；返回是否改动过。"""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                kept_groups.append(group)
                continue
            remaining = [
                h for h in entries if not (isinstance(h, dict) and _is_our_hook(h, paths))
            ]
            if len(remaining) != len(entries):
                changed = True
            if remaining:
                kept_groups.append({**group, "hooks": remaining})
            # 整组都是我们的 ⇒ 整组丢弃
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
            changed = True
    if not hooks:
        data.pop("hooks", None)
    return changed


def _write_or_remove(path: Path, data: dict, *, remove_when_empty: bool = True) -> None:
    """写 JSON；**清空成 `{}` 时直接删文件**，不留空壳。

    为什么这条重要（独立验收第 3 轮抓到）：`uninstall` 的目标是"回到装前状态"。
    如果装前 `settings.json` 不存在（干净的宿主），卸完却留下一个 `{}`，
    那不叫回到装前——而且它会让下一次 `--apply` 走"文件已存在"的分支，
    两条合起来就是"卸载后重装必崩"。

    ⚠️ 只在**内容为空对象**时删。用户原本就有内容的配置（哪怕只有一个键）
       必须原样留着 —— 那是他的东西，不是我们的。
    """
    if remove_when_empty and not data:
        try:
            path.unlink()
            return
        except OSError:
            pass  # 删不掉就留着空对象，不影响正确性
    _write_json(path, data)


def uninstall(paths: HostPaths) -> int:
    """还原：剥离钩子块 + 移除 `March7` server 条目 + 已装文件**移走**（不删）。

    返回 0 = 有改动或本来就没有；1 = 剥离后仍有残留（不可能，但必须响）。
    """
    if not paths.settings.exists():
        print("settings.json : 不存在，跳过")
    else:
        data = _read_json(paths.settings)
        if _strip_our_hooks(data, paths):
            _write_or_remove(paths.settings, data)
            verify = _read_json(paths.settings)
            leftover = [
                event
                for event, groups in (verify.get("hooks") or {}).items()
                for group in groups
                for hook in group.get("hooks", [])
                if _is_our_hook(hook, paths)
            ]
            if leftover:
                print(f"❌ 剥离后校验失败：仍有钩子指向 March7 脚本（{leftover}）", file=sys.stderr)
                return 1
            print("settings.json : 已剥离 March7 钩子")
        else:
            print("settings.json : 没有 March7 钩子，未改动")

    if paths.mcp_json.exists():
        data = _read_json(paths.mcp_json)
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and SERVER_KEY in servers:
            servers.pop(SERVER_KEY)
            if not servers:
                data.pop("mcpServers", None)
            _write_or_remove(paths.mcp_json, data)
            print("mcp.json      : 已移除 March7 server 条目")
        else:
            print("mcp.json      : 没有 March7 条目，未改动")

    # 文件**移走不删**：可整目录找回（T14 的既有语义，用户可能有自己的钩子副本）
    _stash(paths, "hooks", [paths.hooks_dir / name for name in HOOK_FILES])
    _stash(paths, SKILL_DIR_NAME, [paths.skill_dir])

    # 同一数据目录可由多个宿主接入，保留共用的双击入口与面板进程。
    print("提醒：还原后同样需要完全退出 WorkBuddy 再重开。")
    return 0


def _stash(paths: HostPaths, label: str, targets: list[Path]) -> None:
    """把目标移到 `hooks/march7-uninstalled-<时间戳>/`。

    **幂等**：没有任何目标存在 ⇒ 不建 stash 目录（`uninstall` 跑两次不该多出一个空目录）。
    """
    present = [p for p in targets if p.exists()]
    if not present:
        return
    stash = paths.hooks_dir / ("march7-uninstalled-%s" % time.strftime("%Y%m%d-%H%M%S"))
    stash.mkdir(parents=True, exist_ok=True)
    for path in present:
        if path.is_dir():
            shutil.move(str(path), str(stash / path.name))
        else:
            shutil.move(str(path), str(stash / path.name))
    print(f"已装 {label} 已移走（未删除）：{stash}")


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def _mcp_data_dir(paths: HostPaths) -> str:
    """宿主 MCP 配置里 March7 server 的 `MARCH7_HOME`（钩子运行时读的就是它）。"""
    try:
        data = _read_json(paths.mcp_json)
        entry = data.get("mcpServers", {}).get(SERVER_KEY, {})
        return str((entry.get("env") or {}).get("MARCH7_HOME", ""))
    except InstallWriteError:
        return ""


def show_status(paths: HostPaths) -> None:
    print("== WorkBuddy 宿主安装状态 ==")
    print("宿主目录      : %s" % _slash(paths.host_home))
    print("钩子目录      : %s" % _slash(paths.hooks_dir))
    for name in HOOK_FILES:
        path = paths.hooks_dir / name
        print("  %-24s %s" % (name, "已装" if path.exists() else "缺失"))
    skill = paths.skill_dir / "SKILL.md"
    print("SKILL.md      : %s" % ("已装 " + _slash(skill) if skill.exists() else "缺失"))
    if paths.settings.exists():
        try:
            data = _read_json(paths.settings)
            hooks = data.get("hooks")
            if hooks:
                print("settings.json : 已注入 hooks（事件：%s）" % ", ".join(sorted(hooks)))
            else:
                print("settings.json : 无 hooks 键（未注入）")
        except InstallWriteError as exc:
            print("settings.json : 读取失败：%s" % exc)
    else:
        print("settings.json : 不存在")

    entry = {}
    try:
        entry = _read_json(paths.mcp_json).get("mcpServers", {}).get(SERVER_KEY, {}) or {}
    except InstallWriteError:
        entry = {}
    if entry:
        print("mcp.json      : 已注册 March7（command=%s）" % entry.get("command", "?"))
        print("MARCH7_HOME   : %s" % (_mcp_data_dir(paths) or "(未配置)"))
        has_key = bool((entry.get("env") or {}).get("MARCH7_LLM_MAIN_API_KEY"))
        print("LLM key       : %s" % ("已配置" if has_key else "未配置（跑 --set-llm）"))
    else:
        print("mcp.json      : 没有 March7 条目（工具面调不到）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """参数表。**`ONBOARDING.md` 里提到的每个旗标都必须在这里**（测试钉住）。"""
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="把 March7 装进 WorkBuddy 宿主（S11a / ADR-0055）",
    )
    parser.add_argument("--apply", action="store_true", help="安装：一次写全三处（任一失败整体回滚）")
    parser.add_argument("--status", action="store_true", help="只看状态，不改文件")
    parser.add_argument("--uninstall", action="store_true", help="还原宿主配置（文件移走不删）")
    parser.add_argument("--upgrade", action="store_true", help="升级记忆库：备份 → 迁移 → 行数对账")
    parser.add_argument("--set-llm", action="store_true", help="配置 LLM 三件套（探针通过才落盘）")
    parser.add_argument("--doctor", action="store_true", help="装机前置检查（只读，不写文件）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 ~/.March7；沙盒下为 <sandbox>/data）")
    parser.add_argument("--reuse", action="store_true", help="允许复用已存在的非空数据目录")
    parser.add_argument("--sandbox", default=None, help="把三处目标与数据目录都写进这个隔离目录")
    parser.add_argument("--python", default=None, help="指定钩子用的解释器（默认仓库 .venv）")
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="装完不预热编码模型（默认预热，见 ADR-0059 决策 2）",
    )
    parser.add_argument("--llm-base-url", default=None, help="--set-llm：OpenAI 兼容端点")
    parser.add_argument("--llm-api-key", default=None, help="--set-llm：密钥")
    parser.add_argument("--llm-model", default=None, help="--set-llm：型号")
    return parser


def warmup_embedder(python: Path, *, timeout: float = 180.0) -> bool:
    """装完最后一步：让子进程加载一次 BGE-M3 并编码一个词（`ADR-0059` 决策 2）。

    **为什么有效**：实测第二次 `SentenceTransformer(..., local_files_only=True)` 只要 2.1 s
    （第一次带下载检查约 8 s）⇒ 权重进了 OS 文件缓存。预热不改变任何代码路径，
    只是**先把冷启动付掉**，代价放在用户还盯着安装日志的那两分钟里。

    ⚠️ **诚实边界（必须写进报告）**：预热只改善**装完后的第一轮**；
    对"每次新进程重新 `import torch`"那 10 秒**无效**——那是 Python 模块初始化，
    不是磁盘 I/O（`ADR-0059` §1.2 实测：加载时间与线程数无关，恒定 9.7 s）。

    为什么用子进程而不是在本进程里 import：本脚本要能在 `uv sync` **之前**跑
    （`--doctor` / `--set-llm` 都是），在安装器进程里 import torch 会把
    "能不能装"和"torch 装没装好"绑死。

    Returns:
        True = 预热成功；False = 预热失败。**失败绝不让安装失败**——
        预热是优化，不是安装的组成部分；把它做成硬失败等于"加速没成就装不上"。
    """
    code = (
        "import sys;"
        "from March7.embedder import Embedder;"
        "e = Embedder();"
        "v = e.embed_one('预热');"
        "sys.stdout.write('dim=%d device=%s' % (len(v), e.device))"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"预热　　：跳过（{type(exc).__name__}）——不影响使用，第一轮会慢一次")
        return False
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "(无输出)"
        print(f"预热　　：跳过（{tail}）——不影响使用，第一轮会慢一次")
        return False
    print(f"预热　　：模型已加载（{result.stdout.strip()}）")
    return True


def _load_panel_entry():
    """按路径加载 `hosts/panel_entry.py`（`hosts` 不是 Python 包）。

    ⚠️ **不要在这里再写一份**：入口的文件名、内容和失败语义必须三个安装器一致，
    复制三遍必然漂（S17 独立验收抓到的就是"只有 WorkBuddy 有入口"）。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "march7_hosts_panel_entry", Path(__file__).resolve().parents[1] / "panel_entry.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


panel_entry = _load_panel_entry()


def _apply(paths: HostPaths, python: Path, *, reuse: bool, warmup: bool = True) -> int:
    """安装主流程：前置检查 → 三处写入（带回滚）→ 报告。

    回滚粒度是**整个 `--apply`**，不是"每一处各自回滚"：留下"钩子在、工具不在"
    的半装状态，正是本项目最怕的形态（用户在界面上看到钩子日志在长，会以为一切正常）。
    """
    recursion = Rollback()
    created_dirs: list[Path] = []

    try:
        if not paths.host_home.exists():
            # 沙盒里"宿主目录还没有"是**正常起点**（模拟干净机器），建出来即可；
            # 真实宿主里没有 `~/.workbuddy` 说明 WorkBuddy 没装，必须停下。
            if paths.is_sandbox:
                paths.host_home.mkdir(parents=True, exist_ok=True)
                created_dirs.append(paths.host_home)
            else:
                raise InstallWriteError(
                    f"宿主目录不存在：{paths.host_home}（WorkBuddy 装了吗？）"
                )

        state = check_data_dir(paths, reuse=reuse)
        if state == "fresh":
            paths.data_dir.mkdir(parents=True, exist_ok=True)
            created_dirs.append(paths.data_dir)

        # 先把"装前状态"全部拍下来，再动任何一个字节
        for target in (paths.settings, paths.mcp_json):
            recursion.snapshot(target)
        for name in HOOK_FILES:
            recursion.snapshot(paths.hooks_dir / name)
        recursion.snapshot(paths.skill_dir / "SKILL.md")
        recursion.snapshot_dir(paths.skill_dir)

        written = copy_hooks(paths)
        print("钩子脚本      : %d 个 -> %s" % (len(written), _slash(paths.hooks_dir)))
        skill = copy_skill(paths)
        print("SKILL.md      : %s" % _slash(skill))

        mode = install_settings(paths, python)
        print("settings.json : %s hooks 块" % mode)

        mcp_mode = install_mcp_json(paths, python)
        print("mcp.json      : %s March7 条目" % mcp_mode)

        # 独立入口（S17）：宿主不允许 hooks 时，用户双击它也能打开面板。
        # 失败只提示、不回滚——它不是接入的必要条件。
        entry = panel_entry.write_panel_entry(python, paths.data_dir)
        if entry is not None:
            print("面板入口      : %s" % _slash(entry))
        else:
            print("面板入口      : 未生成（可在需要时手动跑 `-m March7.control.panel_launcher`）")

        # 预热放在**三处都写成功之后**：预热失败不该回滚安装（它是优化，不是安装的组成部分）
        if warmup:
            warmup_embedder(python)
        return EXIT_OK

    except InstallWriteError as exc:
        actions = recursion.restore()
        for path in created_dirs:
            try:
                if path.exists() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                pass
        print(f"❌ 安装失败，已回滚：{exc}", file=sys.stderr)
        for action in actions:
            print(f"   回滚：{action}", file=sys.stderr)
        return EXIT_WRITE
    except BaseException:
        recursion.restore()
        raise


def main(argv: list[str] | None = None) -> int:
    """入口（返回退出码，见模块文档的五个码）。"""
    args = build_parser().parse_args(argv)
    sandbox = Path(args.sandbox).expanduser().resolve() if args.sandbox else None
    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else None
    paths = HostPaths(sandbox=sandbox, data_dir=data_dir)
    python = Path(args.python) if args.python else repo_python()

    if args.status:
        show_status(paths)
        return EXIT_OK

    if args.uninstall:
        return uninstall(paths)

    if args.doctor:
        rows = run_doctor(paths, python=python)
        print("== 装机前置检查 ==")
        print("数据目录      : %s" % _slash(paths.data_dir))
        ok = True
        for name, passed, note in rows:
            print("  %-4s %-22s %s" % ("OK" if passed else "✗", name, note))
            ok = ok and passed
        return EXIT_OK if ok else EXIT_PYTHON

    # 所有会写文件的分支都要先过"宿主目录在"这一关。
    # ⚠️ **沙盒模式例外**：沙盒存在的全部意义就是模拟"一台还没有配置的干净机器"
    # （`ADR-0059` §3.3），要求 `<sandbox>/home/.workbuddy` 预先存在等于自己否掉自己。
    if sandbox is None and not paths.host_home.exists():
        print(f"❌ 宿主目录不存在：{paths.host_home}（WorkBuddy 装了吗？）", file=sys.stderr)
        return EXIT_DATA_DIR

    if args.set_llm:
        base_url = args.llm_base_url
        api_key = args.llm_api_key
        model = args.llm_model
        if not (base_url and api_key and model):
            base_url = base_url or input("base_url（如 https://api.example.com/v1）：").strip()
            api_key = api_key or input("api_key：").strip()
            model = model or input("model（如 glm-5.3-flash）：").strip()
        try:
            mode = set_llm(paths, python, base_url=base_url, api_key=api_key, model=model)
        except ProbeError as exc:
            print(
                f"❌ LLM 探针失败，未写入任何配置（旧值保留）：{exc}\n"
                "  检查：key 是否有效 / 余额 / 端点是否是 OpenAI 兼容的 /chat/completions",
                file=sys.stderr,
            )
            return EXIT_LLM
        except InstallWriteError as exc:
            print(f"❌ 写入失败：{exc}", file=sys.stderr)
            return EXIT_WRITE
        print("mcp.json      : %s（LLM 三件套已写入，探针通过）" % mode)
        return EXIT_OK

    if args.upgrade:
        try:
            rows = upgrade(paths, python=python)
        except InstallWriteError as exc:
            print(f"❌ 升级失败：{exc}", file=sys.stderr)
            return EXIT_WRITE
        if rows < 0:
            print("还没有记忆库（全新安装），跳过升级。")
        return EXIT_OK

    if args.apply:
        # 解释器检查必须在写任何东西之前（否则钩子命令会指向一个 import 不到 March7 的解释器）
        try:
            check_python(python)
        except (FileNotFoundError, ImportError) as exc:
            print(f"❌ {exc}\n  先跑 uv sync 把依赖装齐（约 8 GB，含 torch 与 BGE-M3）。", file=sys.stderr)
            return EXIT_PYTHON

        shadows = check_no_stdlib_shadow()
        if shadows:
            print(
                "❌ 以下脚本名与标准库同名，装进宿主目录会遮蔽标准库（会整条导入链崩）：%s"
                % shadows,
                file=sys.stderr,
            )
            return EXIT_WRITE

        try:
            check_data_dir(paths, reuse=args.reuse)
        except NotADirectoryError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return EXIT_DATA_DIR
        except FileExistsError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return EXIT_DATA_DIR

        print("解释器        : %s" % _slash(python))
        print("数据目录      : %s" % _slash(paths.data_dir))
        rc = _apply(paths, python, reuse=args.reuse, warmup=not args.no_warmup)
        if rc == EXIT_OK:
            print("")
            print("下一步：必须完全退出 WorkBuddy 再重开（只关窗口不算），配置才生效。")
            if sandbox is not None:
                print("沙盒模式：本次没有碰真实宿主目录（%s）。" % _slash(Path.home() / ".workbuddy"))
        return rc

    # 没有任何动作旗标 ⇒ 按安装处理（历史行为：直接跑 install.py 就是安装）
    args.apply = True
    try:
        check_python(python)
        check_data_dir(paths, reuse=args.reuse)
    except (FileNotFoundError, ImportError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return EXIT_PYTHON
    except (NotADirectoryError, FileExistsError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return EXIT_DATA_DIR

    print("解释器        : %s" % _slash(python))
    print("数据目录      : %s" % _slash(paths.data_dir))
    rc = _apply(paths, python, reuse=args.reuse, warmup=not args.no_warmup)
    if rc == EXIT_OK:
        print("")
        print("下一步：必须完全退出 WorkBuddy 再重开（只关窗口不算），配置才生效。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
