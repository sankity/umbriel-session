"""Application detection: resolve an app_id to a launch command."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\s+%[a-zA-Z]")
_DESKTOP_DIRS = (
    "{data_home}/applications",
    "/usr/local/share/applications",
    "/usr/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    "{home}/.local/share/flatpak/exports/share/applications",
)


def _desktop_search_dirs() -> list[Path]:
    data_home = os.environ.get(
        "XDG_DATA_HOME", str(Path.home() / ".local" / "share")
    )
    dirs = []
    for template in _DESKTOP_DIRS:
        path = Path(
            template.format(data_home=data_home, home=Path.home())
        ).expanduser()
        if path.is_dir():
            dirs.append(path)
    return dirs


def _parse_desktop(content: str) -> tuple[str, str, str] | None:
    name = None
    exec_line = None
    wm_class = None
    in_entry = False
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry:
            continue
        if line.startswith("Name="):
            name = line.split("=", 1)[1].strip().lower()
        elif line.startswith("Exec="):
            exec_line = line.split("=", 1)[1].strip()
        elif line.startswith("StartupWMClass="):
            wm_class = line.split("=", 1)[1].strip().lower()
        if name and exec_line and wm_class:
            break
    if not name or not exec_line:
        return None
    cleaned = _PLACEHOLDER_RE.sub("", exec_line).strip()
    if not cleaned:
        return None
    return name, cleaned, wm_class or ""


def build_desktop_map() -> dict[str, str]:
    """Map app_id (and desktop file stem) to launch command."""
    mapping: dict[str, str] = {}
    for directory in _desktop_search_dirs():
        for entry in sorted(directory.glob("*.desktop")):
            try:
                parsed = _parse_desktop(entry.read_text(errors="ignore"))
            except OSError:
                continue
            if parsed is None:
                continue
            name, cmd, wm_class = parsed
            stem = entry.stem.lower()
            mapping.setdefault(stem, cmd)
            mapping.setdefault(name, cmd)
            if wm_class:
                mapping.setdefault(wm_class, cmd)
    return mapping


def _which(binary: str) -> bool:
    if not binary:
        return False
    if os.path.sep in binary:
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return True
    return False


def _cmdline_command(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [p for p in raw.split(b"\0") if p]
    if not parts:
        return None
    return " ".join(p.decode(errors="replace") for p in parts)


def _exe_command(pid: int) -> str | None:
    try:
        exe = Path(f"/proc/{pid}/exe").resolve()
    except OSError:
        return None
    return exe.name


def resolve_command(
    app_id: str,
    pid: int,
    launch_map: dict[str, str],
    desktop_map: dict[str, str],
) -> str | None:
    """Resolve the launch command for a window.

    Order: launch map override, /proc cmdline, /proc exe, desktop map,
    raw app_id. Returns None when nothing usable is found.
    """
    override = launch_map.get(app_id.lower())
    if override:
        return override

    candidates: list[str] = []
    if pid > 0:
        cmdline = _cmdline_command(pid)
        if cmdline:
            candidates.append(cmdline)
        exe = _exe_command(pid)
        if exe:
            candidates.append(exe)
    candidates.append(app_id)

    for candidate in candidates:
        binary = candidate.split()[0] if candidate.split() else ""
        if _which(binary):
            return candidate
        desktop_cmd = desktop_map.get(app_id.lower())
        if desktop_cmd:
            return desktop_cmd

    return None
