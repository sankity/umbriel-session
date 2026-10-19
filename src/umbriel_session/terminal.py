"""Terminal emulator CWD detection and launch flag building."""

from __future__ import annotations

import os
from pathlib import Path


def _descendant_shell_cwd(pid: int) -> str | None:
    """Find the CWD of the deepest descendant process of pid."""
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except OSError:
        return None
    if not children:
        return None
    deepest = None
    for child in children:
        cwd = _descendant_shell_cwd(int(child))
        if cwd:
            deepest = cwd
    if deepest:
        return deepest
    try:
        return str(Path(f"/proc/{pid}/cwd").resolve())
    except OSError:
        return None


def get_terminal_cwd(pid: int) -> str | None:
    if pid <= 0:
        return None
    return _descendant_shell_cwd(pid)


def build_cwd_cmd(cmd: str, cwd: str | None, style: str, flag: str) -> str:
    if not cwd or not Path(cwd).is_dir():
        return cmd
    if style == "separate":
        return f"{cmd} {flag} {cwd}"
    if style == "equals":
        return f"{cmd} {flag}={cwd}"
    if style == "subcommand":
        return f"{cmd} {flag} {cwd}"
    return cmd
