"""User configuration for umbriel-session."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "umbriel-session"

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.toml"
DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
) / APP_NAME
BACKUPS_DIR = DATA_DIR / "backups"

DEFAULT_CONTENT_TYPE_IGNORE = {"video", "game"}

DEFAULT_TERMINAL_CWD_FLAGS = {
    "kitty": ("separate", "--directory"),
    "alacritty": ("separate", "--working-directory"),
    "foot": ("separate", "--working-directory"),
    "wezterm": ("subcommand", "start --cwd"),
    "gnome-terminal": ("separate", "--working-directory"),
    "konsole": ("separate", "--working-directory"),
    "ghostty": ("separate", "--working-directory"),
}

TERMINAL_CLASSES = set(DEFAULT_TERMINAL_CWD_FLAGS)


@dataclass
class Config:
    restore_delay_seconds: float = 0.5
    window_wait_timeout: float = 10.0
    restore_floating: bool = True
    restore_fullscreen: bool = True
    restore_cwd: bool = True
    load_time: float = 60.0
    spawn_interval: float = 0.3
    debounce_seconds: float = 2.0
    restore_scratchpad: bool = False
    restore_xwayland: bool = False
    ignore_classes: set[str] = field(default_factory=set)
    ignore_content_types: set[str] = field(
        default_factory=lambda: set(DEFAULT_CONTENT_TYPE_IGNORE)
    )
    launch: dict[str, str] = field(default_factory=dict)
    # Window title -> launch command, for apps whose windows cannot be told
    # apart by app id or pid (Electron apps share one process).
    launch_by_title: dict[str, str] = field(default_factory=dict)
    terminal_cwd_flags: dict[str, tuple[str, str]] = field(
        default_factory=lambda: dict(DEFAULT_TERMINAL_CWD_FLAGS)
    )


def _parse_terminal_flags(raw: dict) -> dict[str, tuple[str, str]]:
    out = {}
    for app_id, value in raw.items():
        style, _, flag = value.partition(" ")
        out[app_id.lower()] = (style, flag)
    return out


def load_config() -> Config:
    cfg = Config()
    if not CONFIG_FILE.exists():
        return cfg
    try:
        data = tomllib.loads(CONFIG_FILE.read_text())
    except tomllib.TOMLDecodeError:
        return cfg

    general = data.get("general", {})
    for key in (
        "restore_delay_seconds",
        "window_wait_timeout",
        "load_time",
        "spawn_interval",
        "debounce_seconds",
    ):
        if key in general:
            setattr(cfg, key, float(general[key]))
    for key in (
        "restore_floating",
        "restore_fullscreen",
        "restore_cwd",
        "restore_scratchpad",
        "restore_xwayland",
    ):
        if key in general:
            setattr(cfg, key, bool(general[key]))

    ignore = data.get("ignore", {})
    if "classes" in ignore:
        cfg.ignore_classes = {c.lower() for c in ignore["classes"]}
    if "content_types" in ignore:
        cfg.ignore_content_types = {c.lower() for c in ignore["content_types"]}

    cfg.launch = {k.lower(): v for k, v in data.get("launch", {}).items()}
    cfg.launch_by_title = dict(data.get("launch_by_title", {}))

    if "terminal_cwd_flags" in data:
        cfg.terminal_cwd_flags = _parse_terminal_flags(data["terminal_cwd_flags"])

    return cfg
