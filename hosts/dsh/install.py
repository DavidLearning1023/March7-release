"""install.py — 把 March7 接进 DSH：渲染补丁 → （默认只预演）落到 DSH 用户层。

要解决的问题不是"写一份配置"，而是**三个只在安装期才知道的值**：
Python 解释器、数据目录、LLM 密钥。前两个每个装一份都不同，第三个**不能进仓库**。
所以仓库里只有模板（`march7.patch.yml.in`），本脚本负责渲染。

⚠️ **这里真正要防的是"静默错库"**（`ADR-0043` §4 的头号风险）：
`MARCH7_HOME` 没写 ⇒ MCP server 回落到默认库 `~/.March7` ⇒ 它会**新建一个空库**，
而 DSH 界面上一切正常，症状只是"换工具就找不到昨天说的"。
所以本脚本的三条硬规则：
1. 数据目录**必须**能解析出来（`--data-dir` 显式给 / 读 WorkBuddy 的 `mcp.json` /
   已存在的适配器配置），解析不到就**报错退出**，绝不猜；
2. 缺 LLM 密钥**默认报错**（没有密钥时每个工具都会抛 `ConfigError`，
   而工具列表照样正常 —— 又是一个"看起来接上了"）；
3. 默认 **dry-run**（只打印），加 `--apply` 才写盘，写前**先备份**。

用法：
    python hosts/dsh/install.py                       # 预演：打印将要写入的补丁
    python hosts/dsh/install.py --apply                # 写进 ~/.dsh/profiles/desktop/cordis.patch.yml
    python hosts/dsh/install.py --apply --profile headless
    python hosts/dsh/install.py --apply --data-dir ~/.March7-pilot
    python hosts/dsh/install.py --print-env            # 只打印缺哪些环境变量
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "march7.patch.yml.in"
PLUGIN = HERE / "plugin" / "index.mjs"
BRIDGE = HERE / "bridge.py"
ADAPTER_CONFIG = HERE / "bridge.py"  # 仅用于文档；适配器配置写到 ~/.dsh 下（见下）

MANAGED_BEGIN = "# >>> March7 adapter (managed block, do not edit by hand)"
MANAGED_END = "# <<< March7 adapter"

ENV_DATA_DIR = "MARCH7_HOME"
LLM_ENV_PREFIX = "MARCH7_LLM_"
REQUIRED_LLM_ENV = (
    "MARCH7_LLM_MAIN_BASE_URL",
    "MARCH7_LLM_MAIN_API_KEY",
    "MARCH7_LLM_MAIN_MODEL",
    "MARCH7_LLM_JUDGE_BASE_URL",
    "MARCH7_LLM_JUDGE_API_KEY",
    "MARCH7_LLM_JUDGE_MODEL",
)
# 这三档是"可选但常见"的（fallback 档缺了只是没有备用链路）
OPTIONAL_LLM_ENV = (
    "MARCH7_LLM_FALLBACK_BASE_URL",
    "MARCH7_LLM_FALLBACK_API_KEY",
    "MARCH7_LLM_FALLBACK_MODEL",
)

MODE_PROFILE = "profile"
MODE_TURN = "turn"
MODE_NONE = "none"
"""注入插件三种形态，与 `plugin/index.mjs` 的 `config.mode` 一一对应。

只写一处：常量在插件与安装器各有一份字符串是**刻意的**（插件是 JS，安装器是 Python，
不共享代码）；但两边的取值范围必须一致，`tests/test_dsh_adapter.py` 有一条用例对账。
"""

DEFAULT_PROFILE = "desktop"
"""默认装进 desktop profile 的**用户层**（`~/.dsh/profiles/desktop/cordis.patch.yml`）。

为什么是 desktop 而不是 headless：desktop 是 David 日常用的那个，装在那儿才算"第二宿主"。
`headless` 只在验收与实验时用（`--patch` 直接给，不必落盘）。
⚠️ 落盘到 desktop 需要**重启 DSH** 才生效，而且本脚本无法替你验证 desktop 里的效果
（`--dump-config` 对 desktop 是拒绝的：profile 由 Electron 独占）。
"""


def host_mcp_config_path(home: Path | None = None) -> Path:
    """WorkBuddy 的 `mcp.json`（安装期的"同源锚点"：数据目录与解释器都从这里读一次）。"""
    return (home or Path.home()) / ".workbuddy" / "mcp.json"


def read_workbuddy_entry(home: Path | None = None) -> dict:
    """读 WorkBuddy `mcp.json` 里 March7 那条（读不到 / 没有 ⇒ 空字典）。

    为什么安装期读它：同一个 `MARCH7_HOME` 必须两边一致，否则"同一份记忆"就是空话。
    读一次、写进补丁，之后两边**各自独立**（不建立运行时耦合）。
    """
    try:
        with open(host_mcp_config_path(home), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    entry = servers.get("March7") if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else {}


def resolve_python(explicit: str | None, home: Path | None = None) -> str:
    """Python 解释器：显式 > WorkBuddy 条目里的 `command` > 当前解释器。"""
    if explicit:
        return explicit
    entry = read_workbuddy_entry(home)
    command = str(entry.get("command") or "").strip()
    if command:
        return command
    return sys.executable


def resolve_data_dir(explicit: str | None, home: Path | None = None) -> str | None:
    """数据目录：显式 > 环境变量 > WorkBuddy 条目 > 已存在的适配器配置；都没有 ⇒ None。"""
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    raw = (os.environ.get(ENV_DATA_DIR) or "").strip()
    if raw:
        return str(Path(raw).expanduser())
    entry = read_workbuddy_entry(home)
    env = entry.get("env") if isinstance(entry, dict) else None
    value = str((env or {}).get(ENV_DATA_DIR) or "").strip()
    if value:
        return str(Path(value).expanduser())
    existing = adapter_config_path(home)
    try:
        with open(existing, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        value = str((data or {}).get("data_dir") or "").strip()
        if value:
            return str(Path(value).expanduser())
    except Exception:
        pass
    return None


def dsh_home(home: Path | None = None) -> Path:
    """和宿主使用同一个配置根。显式 home 用于隔离验收。"""
    if home is not None:
        return home / ".dsh"
    override = os.environ.get("DSH_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".dsh"


def adapter_config_path(home: Path | None = None) -> Path:
    """适配器配置：`~/.dsh/march7-adapter.json`（bridge 与插件都读它）。"""
    return dsh_home(home) / "march7-adapter.json"


def collect_llm_env(environ: dict | None = None) -> tuple[dict[str, str], list[str]]:
    """收集要写进补丁的 LLM 环境变量 → `(已有的, 缺的必需项)`。

    只收 `MARCH7_LLM_*`：把 PATH 之类也搬进去没有意义，还会让补丁变成
    "半个环境快照"，下次换机器就对不上。
    """
    env = environ if environ is not None else dict(os.environ)
    found = {k: v for k, v in env.items() if k.startswith(LLM_ENV_PREFIX) and v}
    missing = [k for k in REQUIRED_LLM_ENV if k not in found]
    return found, missing


def _yaml_str(value: str) -> str:
    """单引号 YAML 标量（`''` 转义；Windows 反斜杠在单引号里无需转义）。"""
    return "'" + str(value).replace("'", "''") + "'"


def render_patch(
    *,
    python: str,
    data_dir: str,
    plugin: Path,
    llm_env: dict[str, str],
    mode: str = MODE_PROFILE,
) -> str:
    """渲染补丁全文（未含管理块标记；由 `merge_managed_block` 负责）。

    `mode` 决定注入插件装不装、装成哪一种（`ADR-0043` §3.2）：
    - `profile`（**默认**）= 会话开始带上画像背景 ⇒ **B 档**的落地形态；
    - `turn` = 每轮按本轮提问召回 ⇒ 实测可跑，但 **section 抛错会熔断整轮**，
      A 档判据第 3 条不过，只能当 opt-in 实验；
    - `none` = 只装 MCP 工具面（最保守）。

    ⚠️ **占位符是 `@X@` 而不是 `{{X}}`**：`{{…}}` 是 DSH/system-prompt 自己的
    变量语法，模板注释里早晚会提到它；一旦注释里出现同名 token，
    `str.replace` 会把注释也一起替换掉（第一次就是这么把 YAML 弄坏的：
    一行注释里被塞进九行 env，缩进错位，`dsh` 报 `bad indentation`）。
    现在的占位符不会和任何文档用语撞名。
    """
    template = TEMPLATE.read_text(encoding="utf-8")
    llm_lines = "\n".join(
        f"          {key}: {_yaml_str(llm_env[key])}" for key in sorted(llm_env)
    )
    if mode in (MODE_PROFILE, MODE_TURN):
        llm_lines += '\n          MARCH7_DSH_REQUIRE_SOURCE: "1"'
    bridge_model_lines = (
        "        modelEnv:\n" + "".join(
            f"          {key}: {_yaml_str(llm_env[key])}\n" for key in sorted(llm_env)
        )
        if llm_env else ""
    )
    inject_entry = (
        (
            "    - id: march7-inject\n"
            f"      name: {_yaml_str(plugin.as_uri())}\n"
            "      inject:\n"
            "        - systemPrompt\n"
            "      config:\n"
            f"        mode: {_yaml_str(mode)}\n"
            f"        python: {_yaml_str(python)}\n"
            f"        bridge: {_yaml_str(str(BRIDGE))}\n"
            f"        dataDir: {_yaml_str(data_dir)}\n"
            + bridge_model_lines
        )
        if mode in (MODE_PROFILE, MODE_TURN)
        else ""
    )
    rendered = (
        template.replace("@PYTHON@", str(python).replace("\\", "/"))
        .replace("@DATA_DIR@", str(data_dir).replace("\\", "/"))
        .replace("@PLUGIN@", plugin.as_uri())
        .replace("@INJECT_ENTRY@", inject_entry)
        .replace("@LLM_ENV@", llm_lines)
    )
    return rendered


def merge_managed_block(existing: str, block: str) -> str:
    """把 `block` 塞进 `existing` 的管理块里（**幂等**：重复执行不会叠加两份）。

    为什么需要管理块：`cordis.patch.yml` 是**用户自己也会改**的文件
    （里面已有 UI/模型配置）。整份覆盖等于把用户的改动删掉；
    只管追加则每次安装都多一份 entry（`insert` 会插两条同名 MCP server）。
    用一对标记框住"我们这部分"，重装=替换框内内容。
    """
    begin = existing.find(MANAGED_BEGIN)
    end = existing.find(MANAGED_END)
    body = f"{MANAGED_BEGIN}\n{block.rstrip()}\n{MANAGED_END}\n" if block else ""
    if begin != -1 and end != -1 and end > begin:
        after = end + len(MANAGED_END)
        if existing[after:after + 1] == "\n":
            after += 1
        return existing[:begin] + body + existing[after:]
    if not block:
        return existing
    separator = "" if existing.endswith("\n") or not existing else "\n"
    if existing and not existing.endswith("\n"):
        separator = "\n"
    return f"{existing}{separator}{body}"


def profile_patch_path(profile: str, home: Path | None = None) -> Path:
    return dsh_home(home) / "profiles" / profile / "cordis.patch.yml"


def backup_path(home: Path | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return dsh_home(home) / "_backup" / stamp



def _mask_secrets(text: str) -> str:
    """把 `sk-...` 形态的密钥打码成 `sk-9c66...3edc`（保留头尾便于核对是哪一把）。

    为什么需要（`ADR-0065`，2026-11-16 实测）：`--apply` 的输出里**原样打印**了
    完整 API key。而这些输出会：
    ① 留在终端回滚缓冲里；② 被用户复制进聊天窗口报 bug；③ 留在 CI 日志里。
    任何一条都是密钥外泄。

    `--emit` 那条路**不打码**（它的用途是重定向到仓外文件，DSH 必须读到真值），
    但它的 docstring 已写明"调用方必须重定向到仓外文件"。
    """
    import re

    def _sub(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(token) <= 14:
            return token[:4] + "***"
        return f"{token[:7]}...{token[-4:]}"

    return re.sub(r"sk-[A-Za-z0-9_\-]{12,}", _sub, text)


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="把 March7 接进 DeepSeek Harness（默认 dry-run）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="真的写盘（默认只打印）")
    action.add_argument("--uninstall", action="store_true", help="移除所选档位的 March7 管理块")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="目标 profile（默认 desktop）")
    parser.add_argument("--python", default=None, help="跑 March7 的解释器（默认取 WorkBuddy 配置）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认取 WorkBuddy 配置）")
    parser.add_argument("--print-env", action="store_true", help="只打印缺失的环境变量")
    parser.add_argument(
        "--allow-missing-keys",
        action="store_true",
        help="缺 LLM 密钥也继续（**不推荐**：工具会一个个抛 ConfigError）",
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help="**只**把补丁内容写到 stdout（可直接 `> x.yml` 交给 `dsh --patch`），不打印别的话",
    )
    parser.add_argument(
        "--mode",
        choices=(MODE_PROFILE, MODE_TURN, MODE_NONE),
        default=MODE_PROFILE,
        help="注入插件形态（默认 profile=会话背景；turn=每轮召回，实验性；none=不装）",
    )
    args = parser.parse_args(argv)

    if args.uninstall:
        target = profile_patch_path(args.profile)
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            remaining = merge_managed_block(existing, "")
            if remaining != existing:
                backup = backup_path()
                backup.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / "cordis.patch.yml")
                target.write_text(remaining, encoding="utf-8")
        print("所选档位的 March7 接入已移除。重启 DSH 后生效，本机库和共用面板入口仍可用。")
        return 0

    llm_env, missing = collect_llm_env()
    if args.print_env:
        print("必需项缺失：" + (", ".join(missing) if missing else "（无）"))
        print("已收集：" + ", ".join(sorted(llm_env)) if llm_env else "已收集：（无）")
        return 0

    data_dir = resolve_data_dir(args.data_dir)
    if not data_dir:
        print(
            "✗ 解析不到数据目录。请显式给 --data-dir，或让 WorkBuddy 的 mcp.json 里有 March7 条目。\n"
            "  为什么不猜：回落到 ~/.March7 会新建一个空生产库，而 DSH 界面一切正常——这是最坏的失败。",
            file=sys.stderr,
        )
        return 2

    if missing and not args.allow_missing_keys:
        print(
            "✗ 缺少 LLM 环境变量：" + ", ".join(missing) + "\n"
            "  为什么卡住：缺 key 时 MCP **工具照样能列出来**，但每次调用都抛 ConfigError。\n"
            "  确认要继续就加 --allow-missing-keys。",
            file=sys.stderr,
        )
        return 3

    python = resolve_python(args.python)
    patch = render_patch(
        python=python,
        data_dir=data_dir,
        plugin=PLUGIN,
        llm_env=llm_env,
        mode=args.mode,
    )

    target = profile_patch_path(args.profile)
    existing = ""
    if target.exists():
        existing = target.read_text(encoding="utf-8")
    merged = merge_managed_block(existing, patch)

    if args.emit:
        # 机器可读出口：**只有**补丁内容（含密钥，所以调用方必须重定向到仓外文件）。
        # 这条是为了让验收命令能一行拿到可用的 `--patch` 文件，而不必从人读的输出里截取。
        # 刻意输出**未合并**的 `patch` 而不是 `merged`：`--patch` 是叠加层，
        # 把目标 profile 的**已有**用户层内容再叠一遍等于同一份配置应用两次。
        sys.stdout.write(patch)
        return 0

    print(f"数据目录   : {data_dir}")
    print(f"解释器     : {python}")
    print(f"插件       : {PLUGIN.as_uri()}")
    print(f"目标 profile: {args.profile} → {target}")
    print(f"LLM 变量    : {len(llm_env)} 个（缺 {len(missing)} 个必需项）")
    print("-" * 70)
    # ⚠️ 人读输出**必须打码**（`ADR-0065`）。这份内容会进终端回滚缓冲、
    # 会被用户复制到聊天窗口报 bug、也会留在 CI 日志里。
    # `--emit` 那条路仍然输出明文，因为它的用途是重定向到仓外文件，
    # 而 DSH 必须读到真实值才能启动。
    print(_mask_secrets(merged))

    if not args.apply:
        print("-" * 70)
        print("dry-run：没有写盘。加 --apply 才会写。")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    if existing:
        backup = backup_path()
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup / "cordis.patch.yml")
        print(f"已备份原文件 → {backup / 'cordis.patch.yml'}")
    target.write_text(merged, encoding="utf-8")

    config_path = adapter_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "data_dir": data_dir,
                "python": python,
                "bridge": str(BRIDGE),
                "plugin": str(PLUGIN),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {target}")
    print(f"已写入 {config_path}")
    # 纯工具档和禁止原生事件的宿主仍可从绑定库旁双击打开。
    entry = panel_entry.write_panel_entry(python, data_dir)
    if entry is not None:
        print(f"面板入口   : {entry}")
    else:
        print("面板入口   : 未生成（可手动跑 python -m March7.control.panel_launcher）")
    print("下一步：**完全退出 DSH 再重开**（配置在启动时快照）；然后让它查一条已知事实。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
