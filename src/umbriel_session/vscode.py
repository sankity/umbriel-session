"""Recover per-window targets for explicitly recognized VS Code-derived editors.

Cursor/Windsurf retain VS Code's User/workspaceStorage layout. References:
https://forum.cursor.com/t/sync-of-keybindings-and-settings/31/115
https://syv-labs.gitbook.io/context-pool/ide-setup/other-ides

The installed /usr/share/applications/antigravity-ide.desktop and
/opt/antigravity-ide/resources/app/product.json identify applicationName
'antigravity-ide', nameShort 'Antigravity IDE' (VS Code 1.107.0 package).
The separate 'Antigravity' app is NOT that editor and is intentionally excluded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

_PRODUCTS = {
    "code": "Code",
    "com.microsoft.vscode": "Code",
    "code-url-handler": "Code",
    "code-insiders": "Code - Insiders",
    "com.microsoft.vscodeinsiders": "Code - Insiders",
    "vscodium": "VSCodium",
    "codium": "VSCodium",
    "com.vscodium.codium": "VSCodium",
    "cursor": "Cursor",
    "cursor-url-handler": "Cursor",
    "windsurf": "Windsurf",
    "windsurf-url-handler": "Windsurf",
    "antigravity-ide": "Antigravity IDE",
    "antigravity-ide-url-handler": "Antigravity IDE",
}


def is_editor(app_id: str) -> bool:
    """Whether this exact app ID has a supported editor metadata adapter."""
    return app_id.lower() in _PRODUCTS


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _user_data_dir(product: str, pid: int) -> Path:
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
    except (OSError, UnicodeError):
        args = []
    for index, arg in enumerate(args):
        if arg.startswith("--user-data-dir="):
            return Path(arg.split("=", 1)[1])
        if arg == "--user-data-dir" and index + 1 < len(args) and args[index + 1]:
            return Path(args[index + 1])
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / product


def _active_storage_dirs(pid: int, storage: Path) -> set[Path]:
    """Find workspace databases held by this instance's process tree.

    VS Code's storage utility process holds these open, unlike the shared main
    process argv, which only describes the first launch. Permission failures are
    harmless; persistent metadata remains a conservative fallback.
    """
    pending = [pid] if pid > 0 else []
    seen: set[int] = set()
    active: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        proc = Path(f"/proc/{current}")
        try:
            for task in (proc / "task").iterdir():
                try:
                    pending.extend(int(child) for child in (task / "children").read_text().split())
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    target = fd.readlink()
                    if target.name.startswith("state.vscdb") and target.parent.parent == storage:
                        active.add(target.parent)
                except OSError:
                    pass
        except OSError:
            pass
    return active


def _target(data: dict) -> tuple[str, str] | None:
    folder = data.get("folder") or data.get("folderUri")
    if isinstance(folder, str):
        return "--folder-uri", folder
    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        workspace = workspace.get("configPath")
    if isinstance(workspace, str):
        # Older metadata uses an absolute filename instead of a URI.
        if workspace.startswith("/"):
            workspace = Path(workspace).as_uri()
        return "--file-uri", workspace
    return None


def _matches(title: str, target: tuple[str, str]) -> bool:
    path = unquote(urlsplit(target[1]).path).rstrip("/")
    label = path.rsplit("/", 1)[-1]
    if target[0] == "--file-uri" and label.endswith(".code-workspace"):
        label = label.removesuffix(".code-workspace")
    # Default titles are either 'folder - Visual Studio Code' or
    # 'file - folder - Visual Studio Code'; do not substring-match foo/foobar.
    segments = [
        re.sub(r" \[[^\]]+\]$", "", part.strip().removeprefix("● "))
        .removesuffix(" (Workspace)")
        for part in title.split(" - ")
    ]
    return bool(label) and (label in segments or path in segments)


def command_matches_title(command: str, title: str) -> bool:
    """Reject transient empty/wrong editor windows until their workspace loads."""
    try:
        args = shlex.split(command)
    except ValueError:
        return True
    for flag in ("--folder-uri", "--file-uri"):
        if flag in args:
            index = args.index(flag)
            if index + 1 < len(args):
                return _matches(title, (flag, args[index + 1]))
    return True


def window_command(app_id: str, pid: int, title: str, command: str) -> str:
    """Add a unique saved workspace URI and force a separate editor window."""
    product = _PRODUCTS.get(app_id.lower())
    if product is None:
        return command
    root = _user_data_dir(product, pid)
    storage = root / "User" / "workspaceStorage"
    active = _active_storage_dirs(pid, storage)
    state = _read_json(root / "User" / "globalStorage" / "storage.json").get("windowsState", {})
    windows = []
    if isinstance(state, dict):
        if isinstance(state.get("openedWindows"), list):
            windows.extend(w for w in state["openedWindows"] if isinstance(w, dict))
        if isinstance(state.get("lastActiveWindow"), dict):
            windows.append(state["lastActiveWindow"])
    try:
        stored = list(storage.glob("*/workspace.json"))
    except OSError:
        stored = []
    tiers = [
        [_read_json(directory / "workspace.json") for directory in active],
        windows + [_read_json(path) for path in stored],
    ]
    for metadata in tiers:
        candidates = {
            target for data in metadata
            if (target := _target(data)) is not None and _matches(title, target)
        }
        if len(candidates) == 1:
            flag, uri = candidates.pop()
            return f"{command} --new-window {flag} {shlex.quote(uri)}"
        if len(candidates) > 1:
            break
    log.warning(
        "cannot uniquely resolve VS Code workspace for %r; "
        "configure [launch_by_title] to avoid opening an empty window", title,
    )
    return f"{command} --new-window"
