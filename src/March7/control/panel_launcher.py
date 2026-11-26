"""panel_launcher.py — 一句话打开本机记忆面板（S17 / `ADR-0072`）

## 这个模块解决什么

用户在任何宿主里说「打开 March7 记忆面板」，要**立刻**看到自己那个库的网页，
不需要给路径、不需要模型去找仓库、也不需要模型拼一条 `python -m ...` 命令。

三段职责，刻意分开（各自可独立测试）：

| 段 | 函数 | 判据 |
|---|---|---|
| 绑定 | `resolve_binding()` | 库与解释器只来自安装绑定 / 环境变量；**cwd 不参与** |
| 进程 | `ensure_panel()` | 同一库只有一个实例；端口被占就换；陈旧状态自动替换 |
| 呈现 | `PanelResult` + `message()` | 成功说"已打开"；失败说"没有打开"+ 可点击地址 + 具体原因 |

## 三条不允许（卡里的红线，逐条对应实现）

1. **不给未绑定的库建库**：`ensure_panel` 第一件事就是检查 `March7.db` 是否存在，
   不存在直接返回失败——绝不构造 `MemoryStore`（它的构造函数会建目录 + 建库）。
2. **不拉起向量模型、不检索、不写入**：本模块只起 `control.web`（只读页面），
   不 import `retriever` / `embedder` / `judge`。
3. **不假报已打开**：`webbrowser.open` 的返回值就是判据。返回 `False`（没有可用浏览器）
   时 `opened=False`，但仍然把 `url` 交出去——服务是起着的，用户点一下就能看。

## 为什么用随机回环端口而不是固定 8765

固定端口有两个坏处：① 与用户机器上别的服务抢；② 端口被占时要么崩、要么换端口后
还把旧地址发给用户。随机端口（`port=0`，内核分配）从结构上消除了冲突；
`panel.json` 记住实际端口，重复打开直接复用。

⚠️ **`panel.json` 不是锁**：它只是一个"上次起在哪"的提示。是否真的活着由
`GET /__panel` 判定（能应答 + `data_dir` 对得上）。进程被强杀留下的残留状态
因此不会让下一次打开假成功。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from March7 import config

STATE_FILENAME = "panel.json"
"""数据目录里的面板状态文件（pid / port / data_dir）。"""

LOCK_DIRNAME = "panel.lock"
"""起服务时的互斥目录（`os.mkdir` 原子；比文件锁跨平台）。"""

HOST_KEY = "March7"
"""宿主 MCP 配置里的 server key（与 `_common.SERVER_KEY` 同名）。"""

ENV_HOST_HOME = "MARCH7_HOST_HOME"
"""宿主配置目录覆盖（测试用；默认 `~/.workbuddy`）。"""

START_TIMEOUT_SECONDS = 20.0
"""冷启等待上限。取 20s 的算理：`python -m` + 打开 SQLite 在开发机约 0.5–1.5s，
20s 是"明显卡死"的界线，而不是正常路径的预期值。"""
CHAT_START_TIMEOUT_SECONDS = 3.0
"""聊天冷启等待预算，超时后把失败交给助手回复。"""

_PROBE_TIMEOUT = 0.8
"""探活单次超时（毫秒级失败即可判定"没在听"）。"""

ENV_NO_BROWSER = "MARCH7_PANEL_NO_BROWSER"
"""非空 ⇒ **只起服务、不拉浏览器**（自动化与无桌面环境用）。

为什么需要：性能判据要求"从消息提交到列表可操作"，量这条链路就得真跑入口脚本；
每跑一次弹一个浏览器窗口，20 次就把人逼疯。设了它以后 `opened=True` 的含义是
"服务已就绪、地址可用"，**不是**"浏览器已弹出"——所以它只该出现在自动化里，
真实使用与"无浏览器"验收都不设它。
"""


def _default_opener(url: str) -> bool:
    """默认打开器：真的交给系统默认浏览器；自动化模式下直接算成功。"""
    if (os.environ.get(ENV_NO_BROWSER) or "").strip():
        return True
    try:
        if os.name == "nt":
            os.startfile(url)
            return True
        return bool(webbrowser.open(url))
    except (OSError, webbrowser.Error):
        return False


def panel_reply_context(message: str) -> str:
    """让宿主用正常助手消息复述已经发生的动作。"""
    return (
        f"【March7 本机面板结果】{message}\n"
        "这次本机打开操作已经处理。请用一句普通助手消息确认实际结果，无需调用工具、"
        "查仓库、读取记忆或再次尝试打开。只有结果为已打开时才确认成功；"
        "失败时说明原因，有备用地址就给出该地址。"
    )

PANEL_TOOL_DESCRIPTION = (
    "March7 已有本机记忆管理面板。用户明确要在浏览器打开它时，调用一次此工具。"
    "如果本轮已有【March7 本机面板结果】，直接按结果回复，即使失败也不要自动重试。"
    "只问有没有、否定或转述时不要打开。按工具结果确认，保留失败原因及备用链接。"
    "\nOpen the existing local March7 memory panel once on an explicit user request. "
    "If this turn already has a native March7 panel result, use it and do not retry."
)

_PRODUCT = r"March7\s*(?:的\s*)?"
_PANEL = rf"(?:{_PRODUCT}面板|(?:{_PRODUCT})?记忆(?:面板|(?:管理)?(?:页面|网页)))"
_OPEN = r"(?:(?:打开|开启)(?:一下)?|开一下)"
_CAN_YOU = r"(?:你\s*)?(?:能不能|可不可以|可以|能)"
_POLITE = rf"(?:请|麻烦)?\s*(?:{_CAN_YOU})?\s*(?:帮我)?\s*"
_BROWSER = r"(?:在(?:默认)?浏览器(?:里|中)?\s*)?"
_OPEN_PANEL = re.compile(
    rf"(?:{_POLITE}{_BROWSER}{_OPEN}\s*{_PANEL}(?:吗)?"
    rf"|{_POLITE}{_BROWSER}把\s*{_PANEL}\s*(?:{_OPEN}|(?:调|叫)出来)(?:吗)?"
    rf"|{_PANEL}\s*(?:[，,]\s*)?{_CAN_YOU}(?:帮我)?{_OPEN}(?:吗)?"
    rf"|{_PANEL}\s*(?:做好|上线|可以用)了吗[？?]\s*"
    rf"{_CAN_YOU}(?:帮我)?{_OPEN}(?:吗)?)[。！!？?.]?",
    re.IGNORECASE,
)
"""用户入站里"整句就是打开面板"的形态。

⚠️ **必须是 `fullmatch`**：`"打开 March7 记忆面板，然后再记一下我明天开会"`
含两个诉求，应保留原有对话路径（见 `match_open_request`）。
"""


@dataclass(frozen=True)
class Binding:
    """一次打开要用到的三样东西：解释器、库目录、来源。"""

    python: Path
    data_dir: Path
    source: str
    """`host-config` / `env` / `default`——出问题时要能说清"这个路径是哪来的"。"""


@dataclass
class PanelResult:
    """打开结果。**成功与失败都必须能自解释**（卡里"失败时假报已打开"的反面）。"""

    opened: bool
    url: str
    port: int
    pid: int
    reused: bool
    data_dir: Path
    source: str
    failure: str | None = None
    elapsed_ms: int = 0
    extra: dict = field(default_factory=dict)

    def chat_message(self) -> str:
        """面向聊天的结果，文件路径和启动日志留在本机。"""
        if self.opened:
            return "已打开 March7 记忆面板。"
        reason = (self.failure or "本机启动未完成").split("；日志尾部：", 1)[0]
        if reason.startswith("起不来面板进程："):
            reason = "本机面板进程启动失败，详情见面板日志"
        message = f"没有打开 March7 记忆面板：{reason}"
        if self.url:
            message += f"。可手动打开：{self.url}"
        return message

    def message(self) -> str:
        """给用户/模型看的一句话（中文，含地址与库位置）。"""
        if self.opened:
            suffix = "\n（复用了已运行的面板进程）" if self.reused else ""
            return (
                f"已打开 March7 记忆面板：{self.url}\n"
                f"库：{self.data_dir}（绑定来源：{self.source}）{suffix}"
            )
        lines = [f"没有打开 March7 记忆面板：{self.failure}"]
        if self.url:
            lines.append(f"可点击这个地址手动打开：{self.url}")
        lines.append(f"库：{self.data_dir}（绑定来源：{self.source}）")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        """机器可读形态（`--json`，供宿主/脚本消费）。"""
        return {
            "opened": self.opened,
            "url": self.url,
            "port": self.port,
            "pid": self.pid,
            "reused": self.reused,
            "data_dir": str(self.data_dir),
            "source": self.source,
            "failure": self.failure,
            "elapsed_ms": self.elapsed_ms,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# 绑定：库与解释器从哪来
# ---------------------------------------------------------------------------


def host_home() -> Path:
    """宿主配置目录（默认 `~/.workbuddy`；`MARCH7_HOST_HOME` 可覆盖）。"""
    override = (os.environ.get(ENV_HOST_HOME) or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".workbuddy"


def _host_entry() -> dict:
    """读宿主 `mcp.json` 里 March7 那一条（读不到 ⇒ 空字典，不抛）。"""
    path = host_home() / "mcp.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    entry = servers.get(HOST_KEY)
    return entry if isinstance(entry, dict) else {}


def resolve_binding() -> Binding:
    """解析 (解释器, 库目录)。**顺序即语义**：安装绑定优先，其次环境变量，最后默认。

    ⚠️ 刻意不看 `cwd`：用户在任意项目里说这句话，库都必须是**安装时绑定的那一个**。
    按 cwd 推导会让"在别的仓库里打开"变成"打开另一个库"——那是卡里
    「验证不会连接其他库」要防的事。
    """
    entry = _host_entry()
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    bound_dir = str(env.get(config.ENV_DATA_DIR) or "").strip()
    command = str(entry.get("command") or "").strip()

    python = Path(command) if command else Path(sys.executable)

    if bound_dir:
        return Binding(python=python, data_dir=Path(bound_dir), source="host-config")
    from_env = (os.environ.get(config.ENV_DATA_DIR) or "").strip()
    if from_env:
        return Binding(python=python, data_dir=Path(from_env), source="env")
    return Binding(python=python, data_dir=Path.home() / config.DEFAULT_DATA_DIR, source="default")


# ---------------------------------------------------------------------------
# 状态文件与探活
# ---------------------------------------------------------------------------


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_FILENAME


def read_state(data_dir: Path) -> dict | None:
    """读状态文件；不存在/坏 JSON/不是对象 ⇒ `None`（不猜）。"""
    path = state_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if type(data.get("port")) is not int or not 0 < data["port"] <= 65535:
        return None
    if type(data.get("pid")) is not int or data["pid"] <= 0:
        return None
    return data


def _panel_request(port: int, path: str, *, method: str = "GET", key: str = "") -> dict | None:
    """固定使用回环，不经代理或重定向传递本机启动凭证。"""
    connection = HTTPConnection("127.0.0.1", port, timeout=_PROBE_TIMEOUT)
    try:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            return None
        payload = json.loads(response.read(8192))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, HTTPException):
        return None
    finally:
        connection.close()


def _probe(data_dir: Path, port: int, key: str = "") -> dict | None:
    """探活同时核对服务模式与绑定的库。"""
    payload = _panel_request(port, "/__panel", key=key)
    if not payload or payload.get("ok") is not True or bool(payload.get("writable")) != bool(key):
        return None
    reported = payload.get("data_dir")
    if not isinstance(reported, str):
        return None
    try:
        if Path(reported).resolve() != Path(data_dir).resolve():
            return None
    except OSError:
        return None
    return payload


def _launch_url(port: int, key: str, url: str) -> str | None:
    if not key:
        return url
    payload = _panel_request(port, "/__launch", method="POST", key=key)
    ticket = payload.get("ticket") if payload else None
    if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
        return None
    return f"{url}#{ticket}"


def terminate_panel(pid: int) -> None:
    """尽力结束一个面板进程（失败静默——清理不该把调用方拖垮）。"""
    if not pid or pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                capture_output=True,
                timeout=2,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.kill(int(pid), 15)
    except Exception:  # noqa: BLE001 —— 清理是尽力而为
        pass


# ---------------------------------------------------------------------------
# 起服务
# ---------------------------------------------------------------------------


def _absolute_pythonpath() -> str | None:
    """把 `PYTHONPATH` 里的相对项转成绝对（子进程 cwd 变了也不会丢）。

    为什么需要：`verify.sh` 用的是 `export PYTHONPATH=src`（相对路径）。
    面板子进程的 cwd 被设成数据目录（"不依赖调用方 cwd"），相对项会解析失败
    ⇒ `import March7` 直接崩。这里在传环境之前就地转绝对。
    """
    raw = os.environ.get("PYTHONPATH")
    if not raw:
        return None
    parts: list[str] = []
    for item in raw.split(os.pathsep):
        if not item:
            continue
        path = Path(item)
        parts.append(str(path if path.is_absolute() else path.resolve()))
    return os.pathsep.join(parts) if parts else None


def _child_env(data_dir: Path) -> dict:
    """子进程环境：继承当前进程 + 明确绑定库目录 + 绝对化 PYTHONPATH。"""
    env = dict(os.environ)
    env[config.ENV_DATA_DIR] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    pythonpath = _absolute_pythonpath()
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return env


def _detach_kwargs() -> dict:
    """让面板进程**脱离调用方**（钩子进程退出后它还得在）。

    Windows 面板使用 GUI 解释器或无控制台进程；POSIX 使用独立会话。
    """
    if os.name == "nt":
        return {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    return {"start_new_session": True}


def _spawn(binding: Binding, port: int, *, desktop: bool = False, writable: bool = False) -> subprocess.Popen:
    """起一个面板子进程；日志落在 `<data_dir>/logs/panel.log`。"""
    log_dir = binding.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "panel.log"
    args = [
        "-m", "March7.control.web", "--data-dir", str(binding.data_dir),
        "--port", str(port), "--state-file", str(state_path(binding.data_dir)),
    ]
    if writable:
        args.append("--manage")
    if desktop:
        # WorkBuddy 在钩子结束时回收整棵进程树。交给当前桌面 Explorer 启动
        # 本机面板，使它的寿命独立于本次消息。
        env = _child_env(binding.data_dir)
        env.update(
            MARCH7_PANEL_EXE=str(binding.python.with_name("pythonw.exe")),
            MARCH7_PANEL_ARGS=subprocess.list2cmdline([*args, "--log-file", str(log_path)]),
        )
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='Stop'; $shell=New-Object -ComObject Shell.Application; "
            "$handle=0; $desktop=$shell.Windows().FindWindowSW(0,0,8,[ref]$handle,1); "
            "if ($null -eq $desktop) { throw 'No desktop shell' }; "
            "$desktop.Document.Application.ShellExecute("
            "$env:MARCH7_PANEL_EXE,$env:MARCH7_PANEL_ARGS,$env:MARCH7_HOME,'open',0)",
        ]
        process_options = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        env = _child_env(binding.data_dir)
        python = binding.python
        if os.name == "nt" and python.with_name("pythonw.exe").is_file():
            python = python.with_name("pythonw.exe")
            args.extend(("--log-file", str(log_path)))
        command = [str(python), *args]
        process_options = _detach_kwargs()
    log = open(log_path, "ab")
    try:
        return subprocess.Popen(
            command,
            cwd=str(binding.data_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **process_options,
        )
    finally:
        log.close()


def _wait_ready(
    binding: Binding, process: subprocess.Popen, timeout: float, *, desktop: bool = False,
    writable: bool = False,
) -> dict | None:
    """等子进程把 `/__panel` 跑通，返回它自报的身份；超时/提前退出 ⇒ `None`。

    ⚠️ **不能用 `state["pid"] == process.pid` 判定就绪**：Windows 上解释器可能是
    一层启动垫片（实测 `sys.executable` 经垫片拉起时，`Popen.pid` 与真正跑
    `http.server` 的进程不是同一个 pid）。判据只能是「状态文件出现了 + `/__panel`
    回的是这个库」。这也是 `panel.json` 不被当成锁的原因。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_state(binding.data_dir)
        if state is not None and (key := _state_key(state, writable)) is not None:
            payload = _probe(binding.data_dir, state["port"], key)
            if payload is not None:
                return {"state": state, "payload": payload}
        if process.poll() is not None and (not desktop or process.returncode != 0):
            return None
        time.sleep(0.05)
    return None


def _state_key(state: dict, writable: bool) -> str | None:
    key = state.get("manage_key", "")
    if not isinstance(key, str) or bool(key) != writable:
        return None
    return key if not key or re.fullmatch(r"[A-Za-z0-9_-]{43}", key) else None


def _tail_log(data_dir: Path, *, start: int) -> str:
    """读面板日志尾部（失败时要能说出具体故障，而不是"启动失败"四个字）。"""
    path = Path(data_dir) / "logs" / "panel.log"
    try:
        with path.open("rb") as log:
            log.seek(start)
            text = log.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-6:])


class _Lock:
    """起服务的互斥（目录创建是原子的，跨平台）。

    ⚠️ 拿不到锁不是失败：说明**别人正在起**，等一会儿直接复用它的状态即可。
    """

    def __init__(self, data_dir: Path, timeout: float = 12.0) -> None:
        self.path = Path(data_dir) / LOCK_DIRNAME
        self.timeout = timeout
        self.acquired = False

    def __enter__(self) -> "_Lock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                os.mkdir(self.path)
                self.acquired = True
                return self
            except FileExistsError:
                # 僵死锁（进程被强杀）：超过 60s 就清掉重来
                try:
                    if time.time() - self.path.stat().st_mtime > 60:
                        self.path.rmdir()
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.05)
            except OSError:
                return self

    def __exit__(self, *exc: object) -> None:
        if self.acquired:
            try:
                self.path.rmdir()
            except OSError:
                # 清理不了时，下次按残留锁的时间处理。
                return


# ---------------------------------------------------------------------------
# 对外：打开
# ---------------------------------------------------------------------------


def ensure_panel(
    binding: Binding,
    *,
    opener=None,
    timeout: float = START_TIMEOUT_SECONDS,
    preferred_port: int = 0,
    desktop: bool = False,
    writable: bool = False,
) -> PanelResult:
    """确保"这个库"的面板在跑，并打开浏览器。

    Args:
        binding: `resolve_binding()` 的结果（或测试直接构造）。
        opener: 可注入的打开器（默认 `webbrowser.open`；测试用假的）。
        timeout: 冷启等待上限。
        preferred_port: 首选端口（0 = 交给内核分配）。被占时自动回落到 0。

    Returns:
        `PanelResult`。**`opened` 只表示"浏览器这一半成功"**——
        服务起着但浏览器没起来时，`opened=False`、`url` 仍然有效。
    """
    started = time.perf_counter()
    opener = opener or _default_opener
    data_dir = binding.data_dir.expanduser().resolve()
    binding = Binding(binding.python, data_dir, binding.source)
    db_path = data_dir / config.DB_FILENAME

    if not db_path.is_file():
        return PanelResult(
            opened=False,
            url="",
            port=0,
            pid=0,
            reused=False,
            data_dir=data_dir,
            source=binding.source,
            failure=f"这个数据目录里没有记忆库（找不到 {db_path.name}），未启动面板",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def reuse_running_panel() -> PanelResult | None:
        state = read_state(data_dir)
        if state is None or (key := _state_key(state, writable)) is None:
            return None
        if _probe(data_dir, state["port"], key) is None:
            return None
        url = f"http://127.0.0.1:{state['port']}/"
        browser_url = _launch_url(state["port"], key, url)
        if browser_url is None:
            return None
        opened = bool(opener(browser_url))
        return PanelResult(
            opened=opened,
            url=url,
            port=state["port"],
            pid=state["pid"],
            reused=True,
            data_dir=data_dir,
            source=binding.source,
            failure=None if opened else "没有找到可用的默认浏览器（服务已在运行）",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            extra={"log": str(data_dir / "logs" / "panel.log")},
        )

    reused = reuse_running_panel()
    if reused is not None:
        return reused

    with _Lock(data_dir, min(12.0, timeout)) as lock:
        # 双检：等锁期间别人可能已经起好了
        reused = reuse_running_panel()
        if reused is not None:
            return reused

        if lock.acquired:
            state_path(data_dir).unlink(missing_ok=True)
            ports = [preferred_port, 0] if preferred_port and not writable else [0]
        else:
            ports = []
        failure = "另一个打开动作正在启动面板，等待超时"
        for port in ports:
            log_path = data_dir / "logs" / "panel.log"
            try:
                log_start = log_path.stat().st_size if log_path.exists() else 0
                process = _spawn(binding, port, desktop=desktop, writable=writable)
            except OSError as exc:
                failure = f"起不来面板进程：{type(exc).__name__}: {exc}"
                continue
            ready = _wait_ready(binding, process, timeout, desktop=desktop, writable=writable)
            if ready is None:
                # 结束刚起的壳与本轮写下的服务进程（两者可能不是同一个 pid）
                terminate_panel(process.pid)
                orphan = read_state(data_dir)
                if orphan and orphan.get("port"):
                    key = _state_key(orphan, writable)
                    if key is not None and _probe(data_dir, orphan["port"], key) is not None:
                        terminate_panel(int(orphan.get("pid") or 0))
                tail = _tail_log(data_dir, start=log_start)
                failure = f"面板进程没有在 {timeout:.0f}s 内就绪"
                if tail:
                    failure += f"；日志尾部：{tail}"
                continue
            actual_port = int(ready["state"].get("port") or 0)
            actual_pid = int(ready["state"].get("pid") or process.pid)
            url = f"http://127.0.0.1:{actual_port}/"
            key = _state_key(ready["state"], writable)
            browser_url = _launch_url(actual_port, key, url) if key is not None else None
            opened = browser_url is not None and bool(opener(browser_url))
            return PanelResult(
                opened=opened,
                url=url,
                port=actual_port,
                pid=actual_pid,
                reused=False,
                data_dir=data_dir,
                source=binding.source,
                failure=None if opened else "没有找到可用的默认浏览器（服务已启动）",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                extra={"log": str(data_dir / "logs" / "panel.log")},
            )

        return PanelResult(
            opened=False,
            url="",
            port=0,
            pid=0,
            reused=False,
            data_dir=data_dir,
            source=binding.source,
            failure=failure,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def open_panel(
    *, data_dir: Path | None = None, opener=None, desktop: bool = False,
    timeout: float = START_TIMEOUT_SECONDS,
) -> PanelResult:
    """入口：解析绑定 → 确保面板 → 打开浏览器。"""
    binding = (
        resolve_binding() if data_dir is None
        else Binding(python=Path(sys.executable), data_dir=Path(data_dir), source="explicit")
    )
    return ensure_panel(
        binding, opener=opener, desktop=desktop and os.name == "nt", timeout=timeout, writable=True,
    )


def open_chat_panel(data_dir: Path | None, *, desktop: bool = False) -> PanelResult:
    """对话入口只使用已绑定的绝对路径，启动失败转换为同一种结果。"""
    if data_dir is None or not data_dir.is_absolute():
        return PanelResult(False, "", 0, 0, False, Path(), "unbound", "本宿主未绑定记忆库")
    try:
        return open_panel(data_dir=data_dir, desktop=desktop, timeout=CHAT_START_TIMEOUT_SECONDS)
    except OSError as exc:
        return PanelResult(False, "", 0, 0, False, data_dir, "explicit", f"本机启动出错（{type(exc).__name__}）")


def match_open_request(text: str) -> bool:
    """整句就是"打开面板"⇒ `True`（带别的诉求一律 `False`，交给模型）。"""
    return bool(_OPEN_PANEL.fullmatch((text or "").strip()))


# ---------------------------------------------------------------------------
# 可双击的本机入口（宿主受限时的降级路径）
# ---------------------------------------------------------------------------


def shortcut_path(data_dir: Path) -> Path:
    """可双击入口落在数据目录里（不碰桌面、不碰开始菜单）。"""
    name = "March7 记忆面板.cmd" if os.name == "nt" else "March7-面板.sh"
    return Path(data_dir) / name


def write_shortcut(data_dir: Path, python: Path) -> Path | None:
    """写一个双击即打开面板的本机入口；写不了 ⇒ `None`（不抛）。

    为什么放在数据目录而不是桌面：桌面是用户的私人空间（本项目不往里放东西），
    而数据目录是"用户会去看记忆"的地方，入口跟库在一起最不容易失联。

    ⚠️ **必须同时传 `--data-dir`，不能只靠 `MARCH7_HOME`**（第 3 轮独立验收抓到）：
    `resolve_binding()` 里宿主配置优先于环境变量（那是给钩子防"继承旧路径"用的），
    所以只设 `MARCH7_HOME` 时，一个写在 A 库旁边的入口会打开宿主配置里的 B 库。
    显式参数走 `source="explicit"` 分支，从结构上压过绑定解析。

    ⚠️ Windows 批处理用 **CRLF + `chcp 65001`**：默认代码页是 GBK，
    UTF-8 写进去的中文提示会变成乱码。
    """
    target = shortcut_path(data_dir)
    if os.name == "nt":
        body = "\r\n".join(
            [
                "@echo off",
                "chcp 65001 >nul",
                "setlocal",
                f'set "MARCH7_HOME={data_dir}"',
                f'"{python}" -m March7.control.panel_launcher --data-dir "{data_dir}"',
                "if errorlevel 1 pause",
                "",
            ]
        )
    else:
        body = (
            "#!/bin/sh\n"
            f'MARCH7_HOME="{data_dir}" "{python}" -m March7.control.panel_launcher '
            f'--data-dir "{data_dir}"\n'
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="")
        if os.name != "nt":
            target.chmod(0o755)
    except OSError:
        return None
    return target


# ---------------------------------------------------------------------------
# CLI（也是快捷方式的落点）
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="March7 panel",
        description="打开本机 March7 记忆面板（复用已运行实例，端口自动分配）",
    )
    parser.add_argument("--data-dir", default=None, help="指定库目录（默认按安装绑定解析）")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    parser.add_argument(
        "--write-shortcut",
        action="store_true",
        help="只写可双击入口（安装器用），不打开面板",
    )
    parser.add_argument("--python", default=None, help="解释器（默认按安装绑定解析）")
    args = parser.parse_args(argv)

    if args.write_shortcut:
        binding = resolve_binding()
        data_dir = (Path(args.data_dir) if args.data_dir else binding.data_dir).expanduser().resolve()
        python = Path(args.python) if args.python else binding.python
        written = write_shortcut(data_dir, python)
        if args.json:
            print(json.dumps({"shortcut": str(written) if written else None}, ensure_ascii=False))
        else:
            print(f"可双击入口：{written}" if written else "写不了可双击入口（目录不可写）")
        return 0 if written else 1

    result = open_panel(data_dir=Path(args.data_dir) if args.data_dir else None)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False))
    else:
        print(result.message())
    return 0 if result.opened else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "Binding",
    "PanelResult",
    "ensure_panel",
    "host_home",
    "main",
    "match_open_request",
    "open_panel",
    "read_state",
    "resolve_binding",
    "state_path",
    "terminate_panel",
    "shortcut_path",
    "write_shortcut",
]
