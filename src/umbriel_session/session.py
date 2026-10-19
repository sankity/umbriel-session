"""Session save, load, and management."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from .config import BACKUPS_DIR, DATA_DIR
from .models import Session, WindowEntry, WorkspaceEntry, workspace_ref_from_id

log = logging.getLogger(__name__)

SESSION_VERSION = 1


def get_ancestor_pids(pid: int) -> set[int]:
    """Return all ancestor PIDs of the given process (including itself)."""
    ancestors: set[int] = set()
    current = pid
    seen: set[int] = set()
    while current > 1 and current not in seen:
        seen.add(current)
        ancestors.add(current)
        try:
            stat = Path(f"/proc/{current}/stat").read_text()
        except OSError:
            break
        # Format: pid (comm) state ppid ...
        # comm may contain spaces/parens; find last ')'
        idx = stat.rfind(")")
        if idx == -1:
            break
        parts = stat[idx + 1:].split()
        # parts[0] = state, parts[1] = ppid
        try:
            current = int(parts[1])
        except (IndexError, ValueError):
            break
    return ancestors


def get_session_path(profile: str | None = None) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if profile:
        safe = profile.replace("/", "_").replace(" ", "_")
        return DATA_DIR / f"session-{safe}.json"
    return DATA_DIR / "session.json"


def save_session(session: Session, profile: str | None = None) -> Path:
    path = get_session_path(profile)
    if path.exists():
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUPS_DIR / f"{path.name}.{int(time.time())}.bak"
        shutil.copy2(path, backup)
        backups = sorted(
            BACKUPS_DIR.glob(f"{path.name}.*.bak"), key=lambda p: p.stat().st_mtime
        )
        while len(backups) > 10:
            backups.pop(0).unlink(missing_ok=True)

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session.to_dict(), indent=2))
    os.chmod(tmp, 0o600)
    tmp.rename(path)
    return path


def load_session(profile: str | None = None) -> Session | None:
    path = get_session_path(profile)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return Session.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Session file {path} is corrupted: {exc}\n"
            "Run `umbriel-session clear` to delete it and start fresh."
        ) from exc


def list_sessions() -> list[tuple[str, Path]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(DATA_DIR.glob("session*.json")):
        if path.name == "session.json":
            label = "default"
        else:
            label = path.stem.removeprefix("session-")
        results.append((label, path))
    return results


def clear_session(profile: str | None = None) -> bool:
    path = get_session_path(profile)
    if path.exists():
        path.unlink()
        return True
    return False


def clear_all_sessions() -> int:
    count = 0
    for _label, path in list_sessions():
        path.unlink(missing_ok=True)
        count += 1
    return count


def build_session(
    windows: list[WindowEntry], workspaces: list[WorkspaceEntry]
) -> Session:
    return Session(
        version=SESSION_VERSION,
        saved_at=time.time(),
        compositor="umbriel",
        workspaces=workspaces,
        windows=windows,
    )


def parse_compositor_workspaces(raw: list[dict]) -> list[WorkspaceEntry]:
    return [WorkspaceEntry.from_compositor(w) for w in raw]


def parse_compositor_windows(raw: list[dict]) -> list[WindowEntry]:
    """Build WindowEntry objects from `umbriel windows --json` output.

    Windows are sorted by focus recency (focused first) and assigned a
    focus_order so restore can replay the original focus sequence.
    """
    entries = []
    for w in raw:
        ref = workspace_ref_from_id(w.get("workspace", ""))
        if ref is None:
            continue
        entries.append(WindowEntry.from_compositor(w, 0, ref))
    entries.sort(key=lambda e: (not e.focused, e.app_id))
    for order, entry in enumerate(entries):
        entry.focus_order = order
    return entries


def filter_raw_windows(raw: list[dict], cfg) -> list[dict]:
    """Apply save-side filters from config to raw window list."""
    own_ancestors = get_ancestor_pids(os.getpid())
    result = []
    for w in raw:
        app_id = (w.get("app_id") or "").lower()
        if app_id in cfg.ignore_classes:
            continue
        if (w.get("content_type") or "none").lower() in cfg.ignore_content_types:
            continue
        if w.get("pid", 0) in own_ancestors:
            continue
        if (w.get("scratchpad") or "") and not cfg.restore_scratchpad:
            continue
        if w.get("xwayland", False) and not cfg.restore_xwayland:
            continue
        result.append(w)
    return result
