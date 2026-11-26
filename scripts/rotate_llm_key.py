"""rotate_llm_key.py — 换 LLM key 时把「所有副本」一次改齐（本机运维工具）

## 为什么需要它

同一个 key 在本机**有 4 份可写副本**，只改一处等于没改：

| # | 位置 | 谁读它 | 本脚本 |
|---|---|---|---|
| 1 | 用户环境变量 `HKCU\\Environment` | 钩子脚本、`python -m March7.control`、Codex 的 MCP server（继承环境） | ✅ 改 |
| 2 | `~/.workbuddy/mcp.json` 的 `mcpServers.March7.env` | WorkBuddy 起的 MCP server（**它不继承登录环境**，只读这份） | ✅ 改 |
| 3 | `~/.dsh/march7-adapter.json` + `~/.dsh/profiles/*/cordis.patch.yml` | DSH 适配器 | 👀 只报 |
| 4 | 宿主自己的模型配置（cc-switch 等） | 宿主本体的模型调用，与 March7 无关 | 👀 不碰 |

**为什么 1 和 2 必须一起改**：WorkBuddy 的 MCP server 由宿主起进程、环境来自 `mcp.json`，
**不是**你的登录环境。只改环境变量 ⇒ WorkBuddy 侧仍拿旧 key ⇒ 每轮 401 ⇒ 钩子恒 exit 0
⇒ 界面上什么都不会响，用户只看到「它好像没记住」。这正是 `ADR-0058` 要消灭的那种静默失败。

**为什么 judge 的 key 以前没路径**：`hosts/workbuddy/install.py --set-llm` 只写
`MARCH7_LLM_MAIN_*` 三件套（见该文件 `LLM_ENV_KEYS`），judge 那一份只能手改。本脚本补上。

## 用法

> ⚠️ **仅 Windows**：第 1 处用的是 `winreg`（`HKCU\\Environment`）。
> macOS / Linux 请用 `~/.zshrc` / `~/.bashrc` 里的 `export`，本脚本不适用。

```bash
python scripts/rotate_llm_key.py --check              # 只读：列出所有副本的指纹与一致性
python scripts/rotate_llm_key.py --apply              # 换 key（隐藏输入，不落盘、不打印全值）
python scripts/rotate_llm_key.py --apply --dry-run    # 只打印计划，一个字节都不写
python scripts/rotate_llm_key.py --apply --only judge # 只换 judge（例如只换了 judge 的供应商）
```

输入方式二选一：交互（`getpass`，屏幕不显示）或 `--key-stdin`（从管道读一行，
适合脚本化；**不要**用 `--key` 命令行参数，那会进 shell 历史）。

## 三条硬约束（照 `CONVENTIONS` §2 / `ADR-0055` 决策 3）

1. **先探针、后落盘**：探针失败时一个字节都不写，你手上还是旧值。
2. **永不打印完整 key**：只打指纹 `前10…后4`。本脚本不写任何日志文件。
3. **改完必须完全退出并重开宿主**：环境变量在进程启动时被快照；宿主不重启 = 没换。

## 不做的事

- 不改 `~/.codex/config.toml`：它的 `[mcp_servers.March7.env]` 里没有 LLM key，
  Codex 侧的 March7 server 继承**用户环境变量**（所以改第 1 处就够，但要重启 Codex）。
- 不改 DSH：那两份由 `hosts/dsh/install.py` 管，本脚本只报指纹让你核对。
- 不重启任何宿主：什么时候重启由你决定。
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import winreg
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ENV_MAIN = "MARCH7_LLM_MAIN_API_KEY"
ENV_JUDGE = "MARCH7_LLM_JUDGE_API_KEY"
TARGETS = {ENV_MAIN: "main", ENV_JUDGE: "judge"}

# 与引擎同口径的 base_url / model（探针要用它们）
PAIRS = {
    ENV_MAIN: ("MARCH7_LLM_MAIN_BASE_URL", "MARCH7_LLM_MAIN_MODEL"),
    ENV_JUDGE: ("MARCH7_LLM_JUDGE_BASE_URL", "MARCH7_LLM_JUDGE_MODEL"),
}

REG_TYPES = {1: "REG_SZ", 2: "REG_EXPAND_SZ", 7: "REG_MULTI_SZ"}

EXIT_OK = 0
EXIT_PROBE = 2
EXIT_WRITE = 3
EXIT_INPUT = 4

_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


# ---------------------------------------------------------------------------
# 指纹与打印（唯一的"看 key"方式——永不打印全值）
# ---------------------------------------------------------------------------


def fingerprint(value: str | None) -> str:
    """把 key 压成 `前10…后4`；够核对"是不是同一把"，不够拿去用。"""
    text = (value or "").strip()
    if not text:
        return "（未配置）"
    if len(text) <= 16:
        return text[:2] + "…" + text[-2:]
    return f"{text[:10]}…{text[-4:]}"


def mask_free(text: str) -> str:
    """把任意文本里出现的 key 形状串换成指纹（防止意外回显整把 key）。"""
    return _KEY_RE.sub(lambda m: fingerprint(m.group(0)), text)


# ---------------------------------------------------------------------------
# 副本 1：用户环境变量（HKCU\Environment）
# ---------------------------------------------------------------------------


def read_user_env(name: str) -> tuple[str | None, str]:
    """读用户级环境变量，返回 (值, 类型名)。不存在 ⇒ (None, "未设置")。

    ⚠️ 只读 `HKCU\\Environment`，不读系统级（`HKLM`）：本项目所有 `MARCH7_*` 都在用户级，
    系统级那一份不归我们管，也不该被悄悄改。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, regtype = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None, "未设置"
    except OSError:
        return None, "读取失败"
    return str(value), REG_TYPES.get(regtype, f"类型{regtype}")


def write_user_env(name: str, value: str) -> str:
    """写用户级环境变量，**保留原有的值类型**。

    为什么不用 `setx`：① 超过 1024 字符会静默截断；② 它会把 `REG_EXPAND_SZ`
    写成 `REG_SZ`，于是 `%USERPROFILE%` 这类引用永久失效（本机实测过，别再踩）。
    这里读出原类型再按原类型写回，两种情况都不会发生。
    """
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    ) as key:
        try:
            _, regtype = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            regtype = winreg.REG_SZ
        winreg.SetValueEx(key, name, 0, regtype, value)
    return REG_TYPES.get(regtype, f"类型{regtype}")


def broadcast_env_change() -> bool:
    """广播 `WM_SETTINGCHANGE`，让之后新起的进程看到新值。

    为什么需要：只写注册表的话，Explorer 不会刷新自己的环境块 ⇒ 从开始菜单/桌面图标
    启动的宿主可能还拿旧值。已经跑着的进程无论如何要重启（这是操作系统的行为，不是 bug）。

    Returns:
        是否广播成功（失败只影响"新进程能不能立刻看到"，不影响写入本身）。
    """
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000,
            ctypes.byref(result),
        )
        return True
    except Exception:  # noqa: BLE001 —— 广播失败不该让换 key 失败
        return False


# ---------------------------------------------------------------------------
# 副本 2：WorkBuddy 的 mcp.json
# ---------------------------------------------------------------------------


def mcp_json_path() -> Path:
    return Path.home() / ".workbuddy" / "mcp.json"


def read_mcp_env() -> tuple[dict[str, str], Path | None]:
    """读 `mcpServers.March7.env`。返回 (env 字典, 文件路径)；读不到返回 ({}, None)。"""
    path = mcp_json_path()
    if not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, path
    servers = data.get("mcpServers") or data.get("servers") or {}
    for name, entry in servers.items():
        if str(name).lower() == "march7" and isinstance(entry, dict):
            env = entry.get("env") or {}
            return {str(k): str(v) for k, v in env.items()}, path
    return {}, path


def write_mcp_env(updates: dict[str, str]) -> Path:
    """只改 `mcpServers.March7.env` 里指定的键，其余原样保留；写前备份、写后校验。

    为什么不整段重写：那是 `hosts/workbuddy/install.py` 的活（它要一并管 command /
    args / 数据目录）。换 key 只该动 key——**改动面越小，越不会顺手改坏别的**。

    Returns:
        备份文件路径。
    """
    path = mcp_json_path()
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)

    servers = data.get("mcpServers") or {}
    entry = None
    for name, value in servers.items():
        if str(name).lower() == "march7" and isinstance(value, dict):
            entry = value
            break
    if entry is None:
        raise KeyError("mcp.json 里没有 mcpServers.March7 这一段（先用安装器装上）")

    env = entry.setdefault("env", {})
    for name, value in updates.items():
        env[name] = value

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

    check = json.loads(path.read_text(encoding="utf-8"))
    check_env = (check.get("mcpServers") or {}).get("March7", {}).get("env") or {}
    for name, value in updates.items():
        if check_env.get(name) != value:
            raise OSError(f"写入后校验失败：{name} 没写进去（备份在 {backup}）")
    return backup


# ---------------------------------------------------------------------------
# 副本 3/4：只报告，不修改
# ---------------------------------------------------------------------------


def _walk_json_keys(node: object, prefix: str = "") -> list[tuple[str, str]]:
    """递归收集 JSON 里"名字像 key"的字段（`*_API_KEY`），返回 (路径, 值)。"""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, str) and key.upper().endswith("API_KEY"):
                found.append((dotted, value))
            else:
                found.extend(_walk_json_keys(value, dotted))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk_json_keys(value, f"{prefix}[{index}]"))
    return found


def scan_foreign() -> list[tuple[str, str, str]]:
    """扫只读副本，返回 (标签, 名称, 指纹)。文件不存在就跳过——不臆造。"""
    rows: list[tuple[str, str, str]] = []
    home = Path.home()

    # DSH 适配器（JSON）
    adapter = home / ".dsh" / "march7-adapter.json"
    if adapter.is_file():
        try:
            data = json.loads(adapter.read_text(encoding="utf-8"))
            for where, value in _walk_json_keys(data):
                rows.append(("DSH 适配器", f"{adapter.name}:{where}", fingerprint(value)))
        except (OSError, json.JSONDecodeError):
            rows.append(("DSH 适配器", f"{adapter.name}", "（读不出来）"))

    # DSH 的 patch yml（LLM env 是明文写进去的）
    for yml in sorted((home / ".dsh" / "profiles").glob("*/cordis.patch.yml")):
        try:
            text = yml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if "API_KEY" not in stripped or ":" not in stripped:
                continue
            name = stripped.split(":", 1)[0].strip()
            # 只认"整行就是一个环境变量名"的形态：YAML 里缩进/注释/嵌套都可能混进来，
            # 不加这道门会把 `env:` 这种父键也当成一个"变量"，报出假行（实测踩过）。
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                continue
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            rows.append(("DSH 补丁", f"{yml.parent.name}/{yml.name}:{name}", fingerprint(value)))

    # Codex 的 config.toml（正常情况下没有 LLM key —— Codex 继承用户环境变量）
    codex_cfg = home / ".codex" / "config.toml"
    if codex_cfg.is_file():
        try:
            text = codex_cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        hits = [ln for ln in text.splitlines() if "API_KEY" in ln]
        if hits:
            for line in hits:
                name = line.split("=", 1)[0].strip()
                value = line.split("=", 1)[1].strip().strip("'\"") if "=" in line else ""
                rows.append(("Codex 配置", f"config.toml:{name}", fingerprint(value)))
        else:
            rows.append(("Codex 配置", "config.toml", "（无 LLM key，靠环境变量）"))
    return rows


# ---------------------------------------------------------------------------
# 探针（复用安装器的实现，不复制一份）
# ---------------------------------------------------------------------------


def _load_installer():
    """按文件路径加载 `hosts/workbuddy/install.py`。

    为什么不 `import install`：名字太通用，`sys.path` 上任何一个 `install.py` 都会撞车；
    而复制一份 `probe_llm` 就是两份真相源——探针判据改了一次、这里忘一次，就又变成静默失败。
    """
    path = REPO_ROOT / "hosts" / "workbuddy" / "install.py"
    spec = importlib.util.spec_from_file_location("march7_wb_installer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"加载不了 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(name: str, api_key: str) -> str:
    """用现有的 base_url/model 真打一次（`max_tokens=1`）。返回一行结论，失败抛异常。"""
    env, _ = read_mcp_env()
    url_name, model_name = PAIRS[name]

    base_url = (env.get(url_name) or os.environ.get(url_name) or "").strip()
    model = (env.get(model_name) or os.environ.get(model_name) or "").strip()
    if not base_url or not model:
        return "跳过（缺 base_url / model，无法探测）"

    module = _load_installer()
    module.probe_llm(base_url=base_url, api_key=api_key, model=model)
    return f"通过（{model} @ {base_url}）"


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def render_state(title: str) -> bool:
    """打印四个副本的现状；返回「可写副本是否全部一致」。"""
    print(f"== {title} ==")
    print("指纹口径：前10…后4（永不打印完整 key）\n")

    writable: list[tuple[str, str, str]] = []
    env_values: dict[str, str | None] = {}
    for name in (ENV_MAIN, ENV_JUDGE):
        value, regtype = read_user_env(name)
        env_values[name] = value
        writable.append(("环境变量", f"{name} [{regtype}]", fingerprint(value)))

    mcp_env, mcp_path = read_mcp_env()
    for name in (ENV_MAIN, ENV_JUDGE):
        writable.append(
            ("mcp.json" if mcp_path else "mcp.json（未找到）", name, fingerprint(mcp_env.get(name)))
        )

    print("可写副本：")
    for where, name, fp in writable:
        print(f"  [{where}] {name:32s} {fp}")

    print("\n只报告（本脚本不改）：")
    foreign = scan_foreign()
    if not foreign:
        print("  （没找到 DSH / Codex 的配置副本）")
    for where, name, fp in foreign:
        print(f"  [{where}] {name:46s} {fp}")

    values = [v for v in env_values.values() if v] + [
        v for v in (mcp_env.get(ENV_MAIN), mcp_env.get(ENV_JUDGE)) if v
    ]
    consistent = len(set(values)) <= 1 if values else True
    print(f"\n可写副本一致性：{'一致 ✓' if consistent else '不一致 ✗（下面这批不是同一把）'}")
    if not consistent:
        for where, name, fp in writable:
            print(f"  {fp}  ← {where} / {name}")

    # 只读副本是否落后：拿可写副本的指纹当基准，不一致就点名
    base = fingerprint(values[0]) if values else "（未配置）"
    stale = [(where, name, fp) for where, name, fp in foreign if fp not in (base, "（无 LLM key，靠环境变量）")]
    if stale:
        print(f"\n⚠️ 只读副本与可写副本不一致（可写副本 = {base}）：")
        for where, name, fp in stale:
            print(f"  {fp}  ← [{where}] {name}")
        print("  DSH 的补丁是**从环境变量重新渲染**的（`collect_llm_env`）⇒ 改完环境变量后跑：")
        print("      python hosts/dsh/install.py --apply")
        print("  不要手改 cordis.patch.yml 的管理块（它有备份与对账，手改会被下次渲染覆盖）。")
    return consistent


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def read_new_key(*, from_stdin: bool) -> str:
    """拿新 key：管道读一行，或交互隐藏输入两次（防手滑）。"""
    if from_stdin:
        raw = sys.stdin.readline().strip()
        if not raw:
            raise ValueError("stdin 没读到内容")
        return raw
    first = getpass.getpass("新 key（输入不显示）：").strip()
    second = getpass.getpass("再输一次确认：").strip()
    if first != second:
        raise ValueError("两次输入不一致")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 LLM key 的所有副本一次改齐（本机运维工具）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true", help="只读：列出副本指纹与一致性（默认）")
    parser.add_argument("--apply", action="store_true", help="换 key")
    parser.add_argument(
        "--only", choices=("both", "main", "judge"), default="both", help="只换其中一个（默认两个）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写任何东西")
    parser.add_argument("--key-stdin", action="store_true", help="从 stdin 读新 key（代替隐藏输入）")
    parser.add_argument("--no-env", action="store_true", help="不改用户环境变量")
    parser.add_argument("--no-mcp-json", action="store_true", help="不改 WorkBuddy 的 mcp.json")
    parser.add_argument("--skip-probe", action="store_true", help="不联网探测（不推荐，见 ADR-0055）")
    args = parser.parse_args(argv)

    if not args.apply:
        render_state("LLM key 副本现状")
        print("\n下一步：--apply 换 key（会先探针、再落盘、再复验）")
        return EXIT_OK

    render_state("换之前")
    names = [ENV_MAIN, ENV_JUDGE] if args.only == "both" else (
        [ENV_MAIN] if args.only == "main" else [ENV_JUDGE]
    )

    print("\n" + "=" * 60)
    try:
        new_key = read_new_key(from_stdin=args.key_stdin)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return EXIT_INPUT
    if len(new_key) < 16 or not new_key.startswith("sk-"):
        print(f"❌ 这不像一把 key（{fingerprint(new_key)}，长度 {len(new_key)}）。"
              "若是自定义前缀，请改脚本里的形状判断。", file=sys.stderr)
        return EXIT_INPUT

    old_values = {
        name: (read_user_env(name)[0] or read_mcp_env()[0].get(name)) for name in names
    }
    same = [name for name in names if (old_values.get(name) or "") == new_key]
    if same and len(same) == len(names):
        print(f"❌ 新 key 与现有的完全一样（{fingerprint(new_key)}）——看起来没换。", file=sys.stderr)
        return EXIT_INPUT

    print(f"新 key 指纹：{fingerprint(new_key)}")
    print(f"将改：{'、'.join(TARGETS[n] for n in names)}"
          f"｜环境变量 {'不改' if args.no_env else '改'}"
          f"｜mcp.json {'不改' if args.no_mcp_json else '改'}")

    if args.dry_run:
        print("\n（--dry-run：到此为止，没有写任何东西）")
        return EXIT_OK

    # ① 先探针（ADR-0055 决策 3：失败时一个字节都不写）
    if args.skip_probe:
        print("\n① 探针：跳过（--skip-probe）")
    else:
        print("\n① 探针（真打一次 max_tokens=1）：")
        for name in names:
            try:
                detail = probe(name, new_key)
            except Exception as exc:  # noqa: BLE001 —— 探针失败要原样告诉人，不能吞
                print(f"  ❌ {TARGETS[name]}：{mask_free(str(exc))}", file=sys.stderr)
                print("\n未写入任何配置，你手上还是旧值。", file=sys.stderr)
                return EXIT_PROBE
            print(f"  ✓ {TARGETS[name]}：{detail}")

    # ② 落盘
    print("\n② 写入：")
    try:
        if not args.no_env:
            for name in names:
                regtype = write_user_env(name, new_key)
                print(f"  ✓ 环境变量 {name}（{regtype}）")
            print(f"  {'✓' if broadcast_env_change() else '⚠'} 已广播环境变更（新进程才看得到）")
        else:
            print("  - 环境变量：按要求跳过")

        if not args.no_mcp_json:
            backup = write_mcp_env({name: new_key for name in names})
            print(f"  ✓ mcp.json 的 {len(names)} 个键（备份：{backup.name}）")
        else:
            print("  - mcp.json：按要求跳过")
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"❌ 写入失败：{mask_free(str(exc))}", file=sys.stderr)
        return EXIT_WRITE

    # ③ 复验（读回来，不信内存里的值）
    print("\n③ 复验（重新读盘）：")
    ok = True
    for name in names:
        value, _ = read_user_env(name)
        if args.no_env:
            print(f"  - 环境变量 {name} 未改（仍是 {fingerprint(value)}）")
            continue
        if value != new_key:
            ok = False
            print(f"  ❌ 环境变量 {name} 读回来是 {fingerprint(value)}")
            continue
        print(f"  ✓ 环境变量 {name} {fingerprint(value)}")
    if not args.no_mcp_json:
        mcp_env, _ = read_mcp_env()
        for name in names:
            value = mcp_env.get(name)
            if value != new_key:
                ok = False
                print(f"  ❌ mcp.json {name} 读回来是 {fingerprint(value)}")
            else:
                print(f"  ✓ mcp.json {name} {fingerprint(value)}")

    print("\n" + "=" * 60)
    if not ok:
        print("❌ 复验没通过：以上面标 ❌ 的为准，别当成功了。", file=sys.stderr)
        return EXIT_WRITE

    print("✅ 换完了。**必须做完下面这几件事才算生效**：")
    print("  ① 完全退出 WorkBuddy 再重开（关窗口不算，要退出进程）——它启动时快照环境变量，")
    print("     而 MCP server 是它按 mcp.json 起的子进程；")
    print("  ② 退出所有 Codex 会话/窗口再重开，并**刷新它的 MCP 环境表**：")
    print("        python hosts/codex/install.py --refresh-llm-env")
    print("     （Codex 的 MCP server **不继承**用户环境变量，只认 config.toml 里那张 env 表")
    print("      —— 少了这一步，工具面会一直返回 `Error executing tool`，而自动写入是好的。")
    print("      详见 `ADR-0068`）")
    if any(where.startswith("DSH") for where, _n, _f in scan_foreign()):
        print("  ③ 让 DSH 的补丁重新渲染（它是从环境变量取 key 的，不会自己更新）：")
        print("        python hosts/dsh/install.py --apply")
        if args.no_env:
            print("     ⚠️ 你这次跳过了环境变量 ⇒ 它渲染出来还是旧 key，先改环境变量再跑。")
    print("\n  验证：新开会话说一句关于你自己的事实，然后跑")
    print("        python -m March7.control why --last")
    print("        看到「写入 N 条 ✓」才算通；看到 failed / 401 就是没换成。")
    print("\n  ⚠️ 旧 key 已在 git 历史里（ADR-0065 §5）——请确认它已在供应商侧作废。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
