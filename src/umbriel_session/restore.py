"""Restore engine: spawn apps, match new windows, apply saved state."""

from __future__ import annotations

import json
import logging
import select
import subprocess
import time
from pathlib import Path

from .config import Config
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


def _ws_key(ws_id: str, ws_map: dict[str, dict]) -> tuple[str, int] | None:
    """Map a live workspace id to the (output, index) pair a saved entry uses."""
    ws = ws_map.get(ws_id)
    if not ws:
        return None
    return (ws.get("output", ""), ws.get("index", 0))


def _entry_key(entry: WindowEntry) -> tuple[str, int]:
    return (entry.workspace.output, entry.workspace.index)


def workspace_selector(live_ws: dict) -> str:
    """Selector that addresses a live workspace."""
    output = live_ws.get("output", "")
    if live_ws.get("named"):
        name = live_ws.get("name", "")
        return f'"{name}"/{output}' if output else f'"{name}"'
    return f"{live_ws.get('index', 1)}/{output}" if output else str(live_ws.get("index", 1))


def resolve_target_workspace(
    entry: WindowEntry, ws_map: dict[str, dict]
) -> dict | None:
    """Find the live workspace a saved entry belongs to.

    A saved index can point past the end of the live inventory when Umbriel
    pruned an empty dynamic workspace, so the last workspace on that output
    is the fallback.
    """
    on_output = [w for w in ws_map.values() if w.get("output") == entry.workspace.output]
    if not on_output:
        return None
    for w in on_output:
        if w.get("index") == entry.workspace.index:
            return w
    if entry.workspace.named:
        for w in on_output:
            if w.get("name") == entry.workspace.name:
                return w
    fallback = max(on_output, key=lambda w: w.get("index", 0))
    log.warning(
        "workspace %s:%s no longer exists, using %s",
        entry.workspace.output,
        entry.workspace.index,
        fallback.get("id"),
    )
    return fallback


def _send(action: str) -> bool:
    result = run_umbriel("msg", action)
    if result.returncode != 0:
        log.warning("msg %s failed: %s", action, result.stderr.strip())
        return False
    return True


def _remove_pending(
    pending: list[tuple[WindowEntry, str]], entry: WindowEntry
) -> None:
    for i, (e, _) in enumerate(pending):
        if e is entry:
            del pending[i]
            return


def _default_windows(live: dict[str, dict]) -> set[str]:
    """Windows whose title is a strict suffix of another window of the same app.

    An Electron app's blank window is titled with the app name alone, which
    is a suffix of the titled windows of the same app. Such a window is a
    default one and must not claim an entry that carries a distinctive
    prefix of its own.
    """
    defaults: set[str] = set()
    by_app: dict[str, list[str]] = {}
    for wid, window in live.items():
        by_app.setdefault(window.get("app_id", ""), []).append(wid)
    for wids in by_app.values():
        titles = {wid: live[wid].get("title", "") for wid in wids}
        for wid, title in titles.items():
            if not title:
                continue
            for other, other_title in titles.items():
                if other == wid or not other_title or title == other_title:
                    continue
                if title.endswith(other_title) or other_title.endswith(title):
                    defaults.add(wid if len(title) < len(other_title) else other)
    return defaults


def assign_entries(
    live: dict[str, dict],
    pending: list[tuple[WindowEntry, str]],
    ws_map: dict[str, dict],
) -> dict[str, WindowEntry]:
    """Assign live windows to saved entries.

    A title identifies a window inside its app, the workspace identifies it
    when the title is shared or has changed, and the app id alone is only
    used when a single entry is left for that app. Every window gets its
    title tried before any window falls back, so one window without a unique
    title cannot steal the entry that another window's title identifies.
    """
    assignment: dict[str, WindowEntry] = {}
    remaining = list(pending)
    defaults = _default_windows(live)

    # A title that matches exactly one entry identifies the window.
    for wid, window in live.items():
        title = window.get("title", "")
        if not title:
            continue
        same = [(e, c) for e, c in remaining if e.title == title]
        if len(same) == 1:
            assignment[wid] = same[0][0]
            remaining.remove(same[0])

    # The workspace identifies the window when the title no longer matches.
    for wid, window in live.items():
        if wid in assignment:
            continue
        live_key = _ws_key(window.get("workspace", ""), ws_map)
        if live_key is None:
            continue
        app_id = window.get("app_id", "")
        for pair in list(remaining):
            if pair[0].app_id != app_id or _entry_key(pair[0]) != live_key:
                continue
            if wid in defaults and pair[0].title != window.get("title", ""):
                # A default window only takes an entry titled the same way.
                continue
            assignment[wid] = pair[0]
            remaining.remove(pair)
            break

    # The app id alone, and only as many windows as there are entries left:
    # an extra window of an app must not take an entry that a real window of
    # that app still needs.
    def distance(wid: str, entry: WindowEntry) -> int:
        live_key = _ws_key(live[wid].get("workspace", ""), ws_map)
        target = _entry_key(entry)
        if live_key is None or live_key[0] != target[0]:
            return 999
        return abs(live_key[1] - target[1])

    app_ids = {w.get("app_id", "") for w in live.values()}
    for app_id in app_ids:
        waiting = [
            wid
            for wid, w in live.items()
            if wid not in assignment and w.get("app_id", "") == app_id
        ]
        if not waiting:
            continue
        combos = []
        for wid in waiting:
            for index, pair in enumerate(remaining):
                if pair[0].app_id != app_id:
                    continue
                if wid in defaults and pair[0].title != live[wid].get("title", ""):
                    continue
                combos.append((distance(wid, pair[0]), wid, index))
        if not combos:
            continue
        combos.sort()
        used_windows: set[str] = set()
        used_entries: set[int] = set()
        for _dist, wid, index in combos:
            if wid in used_windows or index in used_entries:
                continue
            used_windows.add(wid)
            used_entries.add(index)
            assignment[wid] = remaining[index][0]
        remaining = [
            pair for index, pair in enumerate(remaining) if index not in used_entries
        ]

    return assignment


def _apply_window(
    entry: WindowEntry,
    window_id: str,
    current: dict,
    cfg: Config,
    target_ws: dict | None,
) -> None:
    """Apply the saved state to a live window.

    Focus switches the visible workspace, so it is only used when the state
    actually needs it: moving the window to its workspace, or a focused-window
    action (fullscreen/maximize/pin).
    """
    on_target = False
    if target_ws is not None:
        on_target = current.get("workspace") == target_ws.get("id")
        on_output = current.get("output") == target_ws.get("output")

        needs_focus = (
            not (on_target and on_output)
            or (cfg.restore_fullscreen and entry.fullscreen and not current.get("fullscreen"))
            or (entry.maximized and not current.get("maximized"))
            or (entry.pinned and not current.get("pinned"))
        )
        if needs_focus:
            _send(f"window-focus:{window_id}")

        if not (on_target and on_output):
            _send(
                f"window-move-to-workspace-silent:{workspace_selector(target_ws)}"
            )
    else:
        needs_focus = (
            (cfg.restore_fullscreen and entry.fullscreen and not current.get("fullscreen"))
            or (entry.maximized and not current.get("maximized"))
            or (entry.pinned and not current.get("pinned"))
        )
        if needs_focus:
            _send(f"window-focus:{window_id}")

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


def _spawn_cmd(entry: WindowEntry, cfg: Config) -> str:
    """Build the command to launch for a saved window."""
    cmd = cfg.launch_by_title.get(entry.title) or entry.cmd
    flags = cfg.terminal_cwd_flags.get(entry.app_id.lower())
    if cfg.restore_cwd and entry.cwd and flags:
        style, flag = flags
        cmd = build_cwd_cmd(cmd, entry.cwd, style, flag)
    return cmd


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

    # The seat-global activation is `active`; `focused` is per-workspace
    # memory and says nothing about which window holds the keyboard.
    original_focus = next(
        (wid for wid, w in before_windows.items() if w.get("active")), None
    )
    original_workspace = next(
        (wid for wid, w in ws_map.items() if w.get("focused")), None
    )

    # Which saved entries already have a live window? The rest need one of
    # their own, launched directly onto their workspace.
    probe: list[tuple[WindowEntry, str]] = [(e, e.cmd) for e in windows]
    pre_assigned = assign_entries(before_windows, probe, ws_map)
    has_window = set(id(entry) for entry in pre_assigned.values())

    # One launch per saved entry that has no window of its own. Each launch
    # targets that entry's workspace, so the window is born in the right
    # place instead of being moved there afterwards.
    spawn_plan: list[tuple[WindowEntry, str]] = [
        (entry, entry.cmd)
        for entry in sorted(windows, key=lambda e: e.focus_order)
        if id(entry) not in has_window
    ]

    if not adjust_clients_only:
        # Atomic placement: switch to the target workspace first, so the new
        # window is born there. No focus and no move, which is what keeps the
        # restore from stealing the focus.
        by_workspace: dict[tuple[str, int], list[tuple[WindowEntry, str]]] = {}
        for entry, cmd in spawn_plan:
            key = (entry.workspace.output, entry.workspace.index)
            by_workspace.setdefault(key, []).append((entry, cmd))

        for key in sorted(
            by_workspace, key=lambda k: by_workspace[k][0][0].focus_order
        ):
            entry = by_workspace[key][0][0]
            target = resolve_target_workspace(entry, ws_map)
            if target is None:
                for e, _c in by_workspace[key]:
                    report.append((e, "MISSING"))
                continue
            _send(f"workspace-switch:{workspace_selector(target)}")
            time.sleep(cfg.spawn_interval)
            for e, cmd in by_workspace[key]:
                final_cmd = _spawn_cmd(e, cfg)
                binary = final_cmd.split()[0] if final_cmd.split() else ""
                if not Path(binary).exists() and not _which(binary):
                    report.append((e, "MISSING"))
                    continue
                if not _send(f"spawn:{final_cmd}"):
                    report.append((e, "MISSING"))
                time.sleep(cfg.spawn_interval)

    pending: list[tuple[WindowEntry, str]] = [
        (entry, entry.cmd)
        for entry in sorted(
            windows, key=lambda e: (id(e) in has_window, e.focus_order)
        )
    ]
    # wid -> (entry, spawned_here)
    matched: dict[str, tuple[WindowEntry, bool]] = {}
    spawn_entries = [entry for entry, _cmd in spawn_plan]
    deadline = time.monotonic() + cfg.load_time

    if spawn_entries and not adjust_clients_only:
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
                    if _all_matched(spawn_entries, matched):
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
                by_id = {w["id"]: w for w in event.get("data", [])}
                fresh = {
                    wid: w
                    for wid, w in by_id.items()
                    if wid not in before_ids and wid not in matched
                }
                for wid, entry in assign_entries(fresh, pending, ws_map).items():
                    matched[wid] = (entry, True)
                    _remove_pending(pending, entry)
                    _apply_window(
                        entry,
                        wid,
                        by_id[wid],
                        cfg,
                        resolve_target_workspace(entry, ws_map),
                    )
                if _all_matched(spawn_entries, matched):
                    break
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    # Match the entries that are still pending to windows that were already
    # running: the app is up, its state just has to be applied.
    live_now = snapshot_windows()
    remaining_live = {
        wid: w for wid, w in live_now.items() if wid not in matched
    }
    pairs = list(assign_entries(remaining_live, pending, ws_map).items())
    # Apply workspace by workspace. Focusing a window activates the workspace
    # it sits on, so windows that share a workspace are handled together:
    # the first focus activates it and the rest are free.
    pairs.sort(
        key=lambda pair: (
            live_now[pair[0]].get("workspace", ""),
            pair[1].focus_order,
        )
    )
    for wid, entry in pairs:
        matched[wid] = (entry, False)
        _remove_pending(pending, entry)
        _apply_window(
            entry, wid, live_now[wid], cfg, resolve_target_workspace(entry, ws_map)
        )

    # Put the active workspace and the focused window back where they were.
    # Focusing a window that the restore moved elsewhere would switch the
    # visible workspace, so it is skipped in that case.
    after_workspaces = snapshot_workspaces()
    active_workspace = next(
        (wid for wid, w in after_workspaces.items() if w.get("focused")), None
    )
    if original_workspace and active_workspace != original_workspace:
        ws = ws_map.get(original_workspace)
        if ws:
            _send(f"workspace-switch:{workspace_selector(ws)}")

    after_windows = snapshot_windows()
    active_window = next(
        (wid for wid, w in after_windows.items() if w.get("active")), None
    )
    if original_focus and active_window != original_focus:
        target_ws = ws_map.get(original_workspace, {}).get("id")
        if after_windows.get(original_focus, {}).get("workspace") == target_ws:
            _send(f"window-focus:{original_focus}")

    matched_ids = set(id(e) for e, _spawned in matched.values())
    for entry in windows:
        if id(entry) in matched_ids:
            spawned = any(
                id(e) == id(entry) and s for e, s in matched.values()
            )
            report.append((entry, "RESTORED" if spawned else "EXISTING"))
        elif not any(st == "MISSING" and id(e) == id(entry) for e, st in report):
            report.append((entry, "TIMEOUT"))

    # Windows that run but are not in the session. The restore never closes
    # anything, so they stay; the report says so.
    for wid, window in after_windows.items():
        if wid in matched:
            continue
        report.append((WindowEntry.from_live(window), "EXTRA"))

    report.sort(key=lambda pair: (pair[1] == "EXTRA", pair[0].focus_order))
    return report


def _all_matched(
    spawn_entries: list[WindowEntry],
    matched: dict[str, tuple[WindowEntry, bool]],
) -> bool:
    """True when every launched entry has a window of its own."""
    matched_ids = set(id(e) for e, _s in matched.values())
    return all(id(e) in matched_ids for e in spawn_entries)


def _which(binary: str) -> bool:
    from .mapping import _which as which

    return which(binary)
