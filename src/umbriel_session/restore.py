"""Restore engine: spawn apps, match new windows, apply saved state."""

from __future__ import annotations

import json
import logging
import select
import subprocess
import threading
import time
from pathlib import Path

from .config import Config
from .mapping import resolve_command
from .models import Session, WindowEntry
from .session import load_session
from .terminal import build_cwd_cmd

log = logging.getLogger(__name__)

UMBRIEL = "umbriel"


def run_umbriel(*args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [UMBRIEL, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def umbriel_alive() -> bool:
    try:
        result = run_umbriel("windows", "--json", timeout=5.0)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def snapshot_windows() -> dict[str, dict]:
    """Return current windows keyed by stable id."""
    result = run_umbriel("windows", "--json")
    if result.returncode != 0:
        raise RuntimeError(f"umbriel windows failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"malformed windows output: {exc}") from exc
    return {w["id"]: w for w in data}


def snapshot_workspaces() -> dict[str, dict]:
    """Return current workspaces keyed by their stable id."""
    result = run_umbriel("workspaces", "--json")
    if result.returncode != 0:
        raise RuntimeError(f"umbriel workspaces failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"malformed workspaces output: {exc}") from exc
    return {w["id"]: w for w in data}


def workspace_selector(window: WindowEntry) -> str:
    ref = window.workspace
    if ref.named:
        if ref.output:
            return f"{ref.name}/{ref.output}"
        return f'"{ref.name}"'
    return f"{ref.index}/{ref.output}" if ref.output else str(ref.index)


def _ws_key(ws_id: str, ws_map: dict[str, dict]) -> tuple[str, int] | None:
    """Map a live workspace id to the (output, index) pair a saved entry uses."""
    ws = ws_map.get(ws_id)
    if not ws:
        return None
    return (ws.get("output", ""), ws.get("index", 0))


def _entry_key(entry: WindowEntry) -> tuple[str, int]:
    return (entry.workspace.output, entry.workspace.index)


def _send(action: str) -> bool:
    result = run_umbriel("msg", action)
    if result.returncode != 0:
        log.warning("msg %s failed: %s", action, result.stderr.strip())
        return False
    return True


def _match_window(
    wid: str,
    windows_by_id: dict[str, dict],
    pending: list[tuple[WindowEntry, str]],
) -> tuple[WindowEntry, str] | None:
    window = windows_by_id.get(wid, {})
    app_id = window.get("app_id", "")
    title = window.get("title", "")
    for entry, cmd in pending:
        if entry.app_id and entry.app_id == app_id:
            return entry, cmd
    for entry, cmd in pending:
        if entry.title and entry.title == title:
            return entry, cmd
    return None


def _remove_pending(
    pending: list[tuple[WindowEntry, str]], entry: WindowEntry
) -> None:
    for i, (e, _) in enumerate(pending):
        if e is entry:
            del pending[i]
            return


def _pick_window(
    entry: WindowEntry,
    live: dict[str, dict],
    matched: dict[str, WindowEntry],
    ws_map: dict[str, dict],
) -> str | None:
    """Pick the live window that belongs to a saved entry.

    A window already sitting on the entry's workspace is preferred, so the
    assignment stays stable instead of depending on the order the compositor
    happens to list windows in.
    """
    candidates = [
        wid
        for wid, w in live.items()
        if wid not in matched and w.get("app_id", "") == entry.app_id
    ]
    if not candidates:
        candidates = [
            wid
            for wid, w in live.items()
            if wid not in matched and w.get("title", "") == entry.title
        ]
    if not candidates:
        return None
    target = _entry_key(entry)
    for wid in candidates:
        live_key = _ws_key(live[wid].get("workspace", ""), ws_map)
        if live_key is not None and live_key == target:
            return wid
    return candidates[0]


def _apply_window(
    entry: WindowEntry,
    window_id: str,
    current: dict,
    cfg: Config,
    ws_map: dict[str, dict],
) -> None:
    """Apply the saved state to a live window.

    Focus switches the visible workspace, so it is only used when the state
    actually needs it: moving the window to its workspace, or a focused-window
    action (fullscreen/maximize/pin).
    """
    live_key = _ws_key(current.get("workspace", ""), ws_map)
    on_target = live_key is not None and live_key == _entry_key(entry)

    needs_focus = (
        not on_target
        or (cfg.restore_fullscreen and entry.fullscreen and not current.get("fullscreen"))
        or (entry.maximized and not current.get("maximized"))
        or (entry.pinned and not current.get("pinned"))
    )
    if needs_focus:
        _send(f"window-focus:{window_id}")

    if not on_target:
        _send(f"window-move-to-workspace-silent:{workspace_selector(entry)}")

    if cfg.restore_floating and entry.floating != current.get("floating", False):
        _send(f"window-toggle-floating:{window_id}")
        _send(f"window-move-to:center/0/0/{window_id}")

    # maximize/fullscreen/pinned are focused-window actions.
    if cfg.restore_fullscreen and entry.fullscreen and not current.get("fullscreen"):
        _send("window-toggle-fullscreen")
    if entry.maximized and not current.get("maximized"):
        _send("window-toggle-maximize")
    if entry.pinned and not current.get("pinned"):
        _send("window-toggle-pinned")


def restore_session(
    profile: str | None = None,
    dry_run: bool = False,
    workspaces: list[int] | None = None,
    exclude: list[str] | None = None,
    adjust_clients_only: bool = False,
    cfg: Config | None = None,
) -> list[tuple[WindowEntry, str]]:
    """Restore a saved session. Returns per-window (entry, status) pairs."""
    cfg = cfg or Config()
    session = load_session(profile)
    if session is None:
        raise RuntimeError(
            "No saved session found. Run `umbriel-session save` first."
        )

    windows = list(session.windows)
    if workspaces is not None:
        windows = [w for w in windows if w.workspace.index in workspaces]
    if exclude:
        excluded = {e.lower() for e in exclude}
        windows = [w for w in windows if w.app_id.lower() not in excluded]
    if not windows:
        return []

    report: list[tuple[WindowEntry, str]] = []
    if dry_run:
        return [(w, "DRY_RUN") for w in windows]

    if cfg.restore_delay_seconds > 0:
        time.sleep(cfg.restore_delay_seconds)

    before_windows = snapshot_windows()
    before_ids = set(before_windows)
    try:
        ws_map = snapshot_workspaces()
    except RuntimeError:
        ws_map = {}
    before_app_counts: dict[str, int] = {}
    for w in before_windows.values():
        before_app_counts[w.get("app_id", "")] = before_app_counts.get(
            w.get("app_id", ""), 0
        ) + 1
    saved_app_counts: dict[str, int] = {}
    for entry in windows:
        saved_app_counts[entry.app_id] = saved_app_counts.get(entry.app_id, 0) + 1

    # Build spawn list: one spawn per missing window (dedup by cmd).
    spawn_list: list[tuple[WindowEntry, str]] = []
    seen_cmds: set[str] = set()
    for entry in sorted(windows, key=lambda e: e.focus_order):
        if entry.cmd in seen_cmds:
            continue
        seen_cmds.add(entry.cmd)
        if before_app_counts.get(entry.app_id, 0) >= saved_app_counts.get(
            entry.app_id, 0
        ):
            continue
        spawn_list.append((entry, entry.cmd))

    if not adjust_clients_only:
        for entry, cmd in spawn_list:
            style, flag = cfg.terminal_cwd_flags.get(
                entry.app_id.lower(), ("separate", "--working-directory")
            )
            final_cmd = cmd
            if cfg.restore_cwd and entry.cwd and entry.app_id.lower() in {
                k.lower() for k in cfg.terminal_cwd_flags
            }:
                final_cmd = build_cwd_cmd(cmd, entry.cwd, style, flag)
            binary = final_cmd.split()[0] if final_cmd.split() else ""
            if not Path(binary).exists() and not _which(binary):
                report.append((entry, "MISSING"))
                continue
            if not _send(f"spawn:{final_cmd}"):
                report.append((entry, "MISSING"))
            time.sleep(cfg.spawn_interval)

    # Event-driven adjustment: watch for new windows.
    pending: list[tuple[WindowEntry, str]] = [
        (entry, entry.cmd) for entry in windows
    ]
    matched: dict[str, WindowEntry] = {}  # wid -> entry (by identity)
    spawned_app_ids = {entry.app_id for entry, _cmd in spawn_list}
    # Save the currently focused window and active workspace so we can put
    # both back after apply (apply calls window-focus which shifts them).
    original_focus = None
    original_workspace = None
    for wid, w in before_windows.items():
        if w.get("focused"):
            original_focus = wid
            break
    for wid, w in ws_map.items():
        if w.get("focused"):
            original_workspace = wid
            break
    deadline = time.monotonic() + cfg.load_time

    if adjust_clients_only or not spawned_app_ids:
        # Nothing to spawn: match the live windows against the saved entries.
        current = snapshot_windows()
        for entry, _cmd in list(pending):
            wid = _pick_window(entry, current, matched, ws_map)
            if wid is None:
                continue
            matched[wid] = entry
            _remove_pending(pending, entry)
            _apply_window(entry, wid, current[wid], cfg, ws_map)
    else:
        proc = subprocess.Popen(
            [UMBRIEL, "subscribe", "windows"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert proc.stdout is not None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
                if not ready:
                    # All spawned apps matched?
                    matched_app_ids = {e.app_id for e in matched.values()}
                    if spawned_app_ids and spawned_app_ids <= matched_app_ids:
                        break
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") != "windows":
                    continue
                data = event.get("data", [])
                by_id = {w["id"]: w for w in data}
                for wid in by_id:
                    if wid in before_ids or wid in matched:
                        continue
                    hit = _match_window(wid, by_id, pending)
                    if hit:
                        entry, _cmd = hit
                        matched[wid] = entry
                        _remove_pending(pending, entry)
                        _apply_window(entry, wid, by_id[wid], cfg, ws_map)
                matched_app_ids = {e.app_id for e in matched.values()}
                if spawned_app_ids and spawned_app_ids <= matched_app_ids:
                    break
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    # Final pass: match remaining saved entries to existing windows (apps that
    # were already running and didn't spawn a new window). Apply their saved
    # state so already-running apps land on the right workspace.
    live_now = snapshot_windows()
    for entry, _cmd in list(pending):
        wid = _pick_window(entry, live_now, matched, ws_map)
        if wid is None:
            continue
        matched[wid] = entry
        _remove_pending(pending, entry)
        _apply_window(entry, wid, live_now[wid], cfg, ws_map)

    # Restore the workspace that was active before restore started.
    if original_workspace:
        ws = ws_map.get(original_workspace)
        if ws:
            if ws.get("named"):
                _send(f'workspace-switch:"{ws["name"]}"')
            else:
                _send(f'workspace-switch:{ws["index"]}/{ws["output"]}')

    # Focus replay. Only the most recently focused window on the active
    # workspace is focused: focusing a window on another workspace would
    # switch the visible workspace, and focusing many in a row makes the
    # focus jump around.
    if cfg.focus_last:
        entry_to_wid = {id(e): wid for wid, e in matched.items()}
        focus_order = sorted(
            windows, key=lambda e: e.focus_order, reverse=True
        )
        live_after = snapshot_windows()
        for entry in focus_order:
            wid = entry_to_wid.get(id(entry))
            if not wid:
                continue
            live = live_after.get(wid)
            if not live:
                continue
            if original_workspace and live.get("workspace") != original_workspace:
                continue
            _send(f"window-focus:{wid}")
            break

    if original_focus:
        _send(f"window-focus:{original_focus}")

    # Report: OK only if this exact entry was matched to a window.
    matched_ids = set(id(e) for e in matched.values())
    for entry in windows:
        if id(entry) in matched_ids:
            report.append((entry, "OK"))
        else:
            report.append((entry, "TIMEOUT"))
    report.sort(key=lambda pair: pair[0].focus_order)
    return report


def _which(binary: str) -> bool:
    from .mapping import _which as which

    return which(binary)
