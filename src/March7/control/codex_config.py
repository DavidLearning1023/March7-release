"""读 Codex 安装配置，供原生钩子与轮末补记核对绑定。"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any


def default_config_path() -> Path:
    raw = (os.environ.get("CODEX_HOME") or "").strip()
    return (Path(raw) if raw else Path.home() / ".codex") / "config.toml"


def read_settings(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def mcp_environment(settings: dict[str, Any]) -> dict[str, str] | None:
    entry = settings.get("mcp_servers", {}).get("March7")
    return entry.get("env", {}) if entry is not None else None


def mcp_data_dir(env: dict[str, str] | None) -> Path | None:
    """有 MCP 条目时必须有明确库目录，缺失配置时由调用方提供绑定。"""
    if env is None:
        return None
    raw = env.get("MARCH7_HOME", "").strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("Codex 的 March7 配置需要绝对库目录")
    return path


def installed_notify(command: Any, script: Path, data_dir: Path) -> bool:
    """只让配置里确实登记的回调按 MCP 库运行，支持包装器转发的 JSON 命令。"""
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        return False
    for index, item in enumerate(command):
        if command[index + 1:index + 2] == ["--data-dir"] and index + 2 < len(command):
            if Path(item).resolve() == script and Path(command[index + 2]).resolve() == data_dir:
                return True
        if item.lstrip().startswith("["):
            try:
                nested = json.loads(item)
            except json.JSONDecodeError:
                continue
            if installed_notify(nested, script, data_dir):
                return True
    return False
