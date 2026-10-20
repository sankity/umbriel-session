"""Restore engine: spawn apps, match new windows, apply saved state."""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import subprocess
import time
from pathlib import Path

from .config import Config
from .models import Session, WindowEntry
from .session import load_session
from .terminal import build_cwd_cmd

log = logging.getLogger(__name__)

UMBRIEL = "umbriel"

# Once every launched entry has a window, keep listening for a moment so a
# late-mapping second window cannot steal focus after the restore returns.
POST_MATCH_SETTLE_SECONDS = 1.5
POST_MATCH_MAX_SECONDS = 5.0
FOCUS_RETRIES = 3


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


def _active_window(windows: dict[str, dict]) -> str | None:
    """Return the id of the seat-focused window, if any."""
    return next(
        (wid for wid, window in windows.items() if window.get("active")), None
    )


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


def _temp_workspace(
    ws_map: dict[str, dict],
    original_workspace: str | None,
    reserved: set[tuple[str, int]] | None = None,
) -> dict | None:
    """Return an empty workspace to anchor launches for the original one.

    A launch token keeps the window on the workspace that was active when the
    token was issued. Spawning an entry for the original workspace directly
    would make its window map on the workspace that already has focus, which
    steals the focus. Anchoring it to an empty workspace lets it map silently;
    the window is moved back once it exists.

    Reusing an empty unnamed workspace avoids leaving a new empty workspace
    behind after every restore.
    """
    original = ws_map.get(original_workspace or "")
    if original is None:
        return None
    output = original.get("output", "")
    same_output = [w for w in ws_map.values() if w.get("output") == output]
    if not same_output:
        return None

    reserved = reserved or set()
    empty = [
        workspace
        for workspace in same_output
        if workspace.get("id") != original_workspace
        and not workspace.get("named")
        and not workspace.get("occupied", False)
        and int(workspace.get("window_count", 0)) == 0
        and (workspace.get("output"), workspace.get("index")) not in reserved
    ]
    if empty:
        return max(empty, key=lambda workspace: int(workspace.get("index", 0)))

    index = max(int(w.get("index", 0)) for w in same_output) + 1
    return {"output": output, "index": index, "named": False, "name": str(index)}


def _focus_restore_actions(focus_id: str | None) -> list[str]:
    return [f"window-focus:{focus_id}"] if focus_id else []


def _spawn_batch_actions(
    cmd: str,
    spawn_ws: dict,
    return_ws: dict | None,
    focus_id: str | None,
) -> list[str]:
    """Actions that launch one app and return to the original workspace.

    All actions go through one batch, so the compositor applies the temporary
    workspace switch and its reversal before drawing a frame.
    """
    actions = [
        f"workspace-switch:{workspace_selector(spawn_ws)}",
        f"spawn:{cmd}",
    ]
    if return_ws is not None:
        actions.append(f"workspace-switch:{workspace_selector(return_ws)}")
    actions.extend(_focus_restore_actions(focus_id))
    return actions


def resolve_target_workspace(
    entry: WindowEntry, ws_map: dict[str, dict], avoid: str | None = None
) -> dict | None:
    """Find the live workspace a saved entry belongs to.

    A saved index can point past the end of the live inventory when Umbriel
    pruned an empty dynamic workspace, so the last workspace on that output
    is the fallback. `avoid` skips the temporary launch workspace, which is
    empty and must not become that fallback.
    """
    on_output = [
        w
        for w in ws_map.values()
        if w.get("output") == entry.workspace.output and w.get("id") != avoid
    ]
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


def _ipc_path() -> str | None:
    path = os.environ.get("UMBRIEL_SOCKET")
    if path:
        return path
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    display = os.environ.get("WAYLAND_DISPLAY")
    if runtime_dir and display:
        return f"{runtime_dir}/umbriel-{display}.sock"
    return None


def _send_sequentially(actions: list[str]) -> bool:
    ok = True
    for action in actions:
        ok = _send(action) and ok
    return ok


def _read_line(conn: socket.socket, buf: bytearray) -> bytes:
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    line, _, rest = bytes(buf).partition(b"\n")
    buf[:] = rest
    return line


def _send_many(actions: list[str]) -> bool:
    """Apply several actions in one compositor event-loop turn.

    A plain IPC connection is closed after its first reply, so parallel
    connections only batch by luck. A subscribed connection stays open and the
    server drains every buffered request before returning to its event loop,
    which makes the whole sequence atomic: no intermediate focus or workspace
    is ever drawn.
    """
    if not actions:
        return True
    if len(actions) == 1:
        return _send(actions[0])

    log.debug("batch: %s", actions)
    path = _ipc_path()
    if path is None:
        return _send_sequentially(actions)

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5.0)
    try:
        conn.connect(path)
    except OSError as exc:
        conn.close()
        log.warning("batch IPC unavailable (%s); sending sequentially", exc)
        return _send_sequentially(actions)

    buf = bytearray()
    try:
        subscribe = json.dumps({"cmd": "subscribe", "events": ["submap"]})
        conn.sendall(subscribe.encode() + b"\n")
        initial = _read_line(conn, buf)
        if not initial or b'"err"' in initial:
            raise OSError("batch subscription rejected")

        requests = b"".join(
            json.dumps({"cmd": "msg", "arg": action}).encode() + b"\n"
            for action in actions
        )
        conn.sendall(requests)

        ok = True
        responses = 0
        while responses < len(actions):
            line = _read_line(conn, buf)
            if not line:
                ok = False
                break
            if b'"event":' in line:
                continue
            responses += 1
            if b'"err"' in line:
                log.warning(
                    "msg %s failed: %s",
                    actions[responses - 1],
                    line.decode(errors="replace").strip(),
                )
                ok = False
        return ok
    except OSError as exc:
        # The requests may already have run; never resend them, spawns would
        # be duplicated. The caller reports a failed batch.
        log.warning("batch IPC failed: %s", exc)
        return False
    finally:
        conn.close()


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


def _window_actions(
    entry: WindowEntry,
    window_id: str,
    current: dict,
    cfg: Config,
    target_ws: dict | None,
) -> list[str]:
    """Return the actions needed to apply the saved state to a live window.

    Focused-window actions (fullscreen/maximize/pin) and silent moves need the
    target window focused first. They are applied before the move and the
    caller restores the original focus in the same batch.
    """
    actions: list[str] = []
    on_target = False
    on_output = False
    if target_ws is not None:
        on_target = current.get("workspace") == target_ws.get("id")
        on_output = current.get("output") == target_ws.get("output")

    move_needed = target_ws is not None and not (on_target and on_output)
    fullscreen_needed = (
        cfg.restore_fullscreen
        and entry.fullscreen
        and not current.get("fullscreen")
    )
    maximize_needed = entry.maximized and not current.get("maximized")
    pin_needed = entry.pinned and not current.get("pinned")

    if move_needed or fullscreen_needed or maximize_needed or pin_needed:
        actions.append(f"window-focus:{window_id}")

    if cfg.restore_floating and entry.floating != current.get("floating", False):
        actions.append(f"window-toggle-floating:{window_id}")
        if entry.floating:
            actions.append(f"window-move-to:center/0/0/{window_id}")

    # maximize/fullscreen/pinned are focused-window actions.
    if fullscreen_needed:
        actions.append("window-toggle-fullscreen")
    if maximize_needed:
        actions.append("window-toggle-maximize")
    if pin_needed:
        actions.append("window-toggle-pinned")

    # The silent move targets the most recently focused window, so it must
    # come after the focus action above.
    if move_needed and target_ws is not None:
        actions.append(
            f"window-move-to-workspace-silent:{workspace_selector(target_ws)}"
        )
    return actions


def _spawn_cmd(entry: WindowEntry, cfg: Config) -> str:
    """Build the command to launch for a saved window."""
    cmd = cfg.launch_by_title.get(entry.title) or entry.cmd
    flags = cfg.terminal_cwd_flags.get(entry.app_id.lower())
    if cfg.restore_cwd and entry.cwd and flags:
        style, flag = flags
        cmd = build_cwd_cmd(cmd, entry.cwd, style, flag)
    return cmd


def _clear_temp_workspace(
    temp_ws: dict | None,
    original_ws: dict | None,
    focus_id: str | None,
) -> None:
    """Move anything left on the temporary launch workspace to the original.

    A matched window is moved by `_window_actions`; this catches extra windows
    from the same app and keeps the temporary workspace from surviving with
    windows on it.
    """
    if temp_ws is None or original_ws is None:
        return
    try:
        live_ws = snapshot_workspaces()
        live = snapshot_windows()
    except RuntimeError:
        return
    temp_id = next(
        (
            wid
            for wid, workspace in live_ws.items()
            if workspace.get("output") == temp_ws.get("output")
            and workspace.get("index") == temp_ws.get("index")
            and not workspace.get("named")
        ),
        None,
    )
    if temp_id is None:
        return
    for wid, window in live.items():
        if window.get("workspace") != temp_id:
            continue
        actions = [
            f"window-focus:{wid}",
            f"window-move-to-workspace-silent:{workspace_selector(original_ws)}",
            f"workspace-switch:{workspace_selector(original_ws)}",
        ]
        _send_many(actions + _focus_restore_actions(focus_id))


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
    focus_id = _active_window(before_windows)
    original_workspace = (
        before_windows.get(focus_id, {}).get("workspace") if focus_id else None
    )
    if not original_workspace:
        original_workspace = next(
            (wid for wid, w in ws_map.items() if w.get("focused")), None
        )

    # Which saved entries already have a live window? The rest need one of
    # their own, launched directly onto their workspace.
    probe: list[tuple[WindowEntry, str]] = [(e, e.cmd) for e in windows]
    pre_assigned = assign_entries(before_windows, probe, ws_map)
    has_window = set(id(entry) for entry in pre_assigned.values())

    # One launch per saved entry that has no window of its own. Each launch
    # carries its saved workspace, so the window maps there without the
    # restore first having to visit that workspace.
    spawn_plan: list[tuple[WindowEntry, str]] = [
        (entry, entry.cmd)
        for entry in sorted(windows, key=lambda e: e.focus_order)
        if id(entry) not in has_window
    ]

    temp_ws: dict | None = None
    return_ws: dict | None = None
    avoid_ws: str | None = None
    temp_used = False
    if not adjust_clients_only:
        # Launch each missing window on its saved workspace, but return to the
        # original workspace before the app maps. The launch token keeps the
        # window on the target workspace; because the original workspace is
        # active when it maps, the compositor treats it as a silent launch.
        # Entries for the original workspace itself need a temporary anchor,
        # otherwise their windows would map on the active workspace and take
        # the focus.
        reserved = {
            (entry.workspace.output, entry.workspace.index) for entry in windows
        }
        temp_ws = _temp_workspace(ws_map, original_workspace, reserved)
        avoid_ws = temp_ws.get("id") if temp_ws else None
        return_ws = ws_map.get(original_workspace or "")

        by_workspace: dict[tuple[str, int], list[tuple[WindowEntry, str]]] = {}
        for entry, cmd in spawn_plan:
            key = (entry.workspace.output, entry.workspace.index)
            by_workspace.setdefault(key, []).append((entry, cmd))

        for key in sorted(
            by_workspace, key=lambda k: by_workspace[k][0][0].focus_order
        ):
            entry = by_workspace[key][0][0]
            target = resolve_target_workspace(entry, ws_map, avoid_ws)
            if target is None:
                for e, _c in by_workspace[key]:
                    report.append((e, "MISSING"))
                continue
            is_original = (
                original_workspace is not None
                and target.get("id") == original_workspace
            )
            if is_original and temp_ws is None:
                log.warning(
                    "no temporary workspace for %s; a new window on the "
                    "original workspace may take focus",
                    target.get("id", "unknown"),
                )
            spawn_ws = temp_ws if is_original and temp_ws is not None else target
            for e, cmd in by_workspace[key]:
                final_cmd = _spawn_cmd(e, cfg)
                binary = final_cmd.split()[0] if final_cmd.split() else ""
                if not Path(binary).exists() and not _which(binary):
                    report.append((e, "MISSING"))
                    continue
                actions = _spawn_batch_actions(
                    final_cmd, spawn_ws, return_ws or target, focus_id
                )
                if spawn_ws is temp_ws:
                    temp_used = True
                if not _send_many(actions):
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
        matched_at: float | None = None
        last_event_at = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    break
                if matched_at is not None and (
                    now - last_event_at >= POST_MATCH_SETTLE_SECONDS
                    or now - matched_at >= POST_MATCH_MAX_SECONDS
                ):
                    break
                ready, _, _ = select.select(
                    [proc.stdout], [], [], min(remaining, 0.5)
                )
                if not ready:
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

                last_event_at = time.monotonic()
                by_id = {w["id"]: w for w in event.get("data", [])}
                if focus_id is not None and focus_id not in by_id:
                    focus_id = None

                fresh = {
                    wid: w
                    for wid, w in by_id.items()
                    if wid not in before_ids and wid not in matched
                }
                for wid, entry in assign_entries(fresh, pending, ws_map).items():
                    matched[wid] = (entry, True)
                    _remove_pending(pending, entry)
                    actions = _window_actions(
                        entry,
                        wid,
                        by_id[wid],
                        cfg,
                        resolve_target_workspace(entry, ws_map, avoid_ws),
                    )
                    if actions:
                        _send_many(actions + _focus_restore_actions(focus_id))

                # A window that mapped late (or a second window the launch
                # token did not cover) must not keep the focus.
                active = _active_window(by_id)
                if focus_id and active and active != focus_id:
                    _send_many(_focus_restore_actions(focus_id))

                if _all_matched(spawn_entries, matched):
                    if matched_at is None:
                        matched_at = time.monotonic()
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    # Match the entries that are still pending to windows that were already
    # running: the app is up, its state just has to be applied.
    live_now = snapshot_windows()
    if focus_id is not None and focus_id not in live_now:
        focus_id = None
    remaining_live = {
        wid: w for wid, w in live_now.items() if wid not in matched
    }
    pairs = list(assign_entries(remaining_live, pending, ws_map).items())
    pairs.sort(
        key=lambda pair: (
            live_now[pair[0]].get("workspace", ""),
            pair[1].focus_order,
        )
    )
    for wid, entry in pairs:
        matched[wid] = (entry, False)
        _remove_pending(pending, entry)
        actions = _window_actions(
            entry,
            wid,
            live_now[wid],
            cfg,
            resolve_target_workspace(entry, ws_map, avoid_ws),
        )
        if actions:
            _send_many(actions + _focus_restore_actions(focus_id))

    # Move anything still on the temporary anchor workspace back to the
    # original one, then put the original focused window back on the seat.
    original_ws = ws_map.get(original_workspace or "")
    _clear_temp_workspace(temp_ws if temp_used else None, original_ws, focus_id)

    after_windows = snapshot_windows()
    if focus_id is not None and focus_id not in after_windows:
        focus_id = None
    for _attempt in range(FOCUS_RETRIES):
        if not focus_id or _active_window(after_windows) == focus_id:
            break
        _send_many(_focus_restore_actions(focus_id))
        time.sleep(0.05)
        after_windows = snapshot_windows()
    if focus_id and _active_window(after_windows) != focus_id:
        log.warning("could not restore focus to %s", focus_id)

    if focus_id is None and original_workspace and original_ws is not None:
        _send(f"workspace-switch:{workspace_selector(original_ws)}")

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
