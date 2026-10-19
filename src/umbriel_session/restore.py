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


def workspace_selector(window: WindowEntry) -> str:
    ref = window.workspace
    if ref.named:
        if ref.output:
            return f"{ref.name}/{ref.output}"
        return f'"{ref.name}"'
    return f"{ref.index}/{ref.output}" if ref.output else str(ref.index)


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


def _apply_window(entry: WindowEntry, window_id: str, cfg: Config) -> None:
    # silent workspace move targets the focused window: focus first.
    _send(f"window-focus:{window_id}")
    selector = workspace_selector(entry)
    _send(f"window-move-to-workspace-silent:{selector}")
    if cfg.restore_floating and entry.floating:
        _send(f"window-toggle-floating:{window_id}")
        _send(f"window-move-to:center/0/0/{window_id}")
    # maximize/fullscreen/pinned are focused-window actions: focus first.
    if cfg.restore_fullscreen and entry.fullscreen:
        _send(f"window-focus:{window_id}")
        _send("window-toggle-fullscreen")
    if entry.maximized:
        _send(f"window-focus:{window_id}")
        _send("window-toggle-maximize")
    if entry.pinned:
        _send(f"window-focus:{window_id}")
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

    before_windows = snapshot_windows()
    before_ids = set(before_windows)
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
    matched: dict[str, str] = {}  # wid -> app_id
    spawned_app_ids = {entry.app_id for entry, _cmd in spawn_list}
    deadline = time.monotonic() + cfg.load_time

    if adjust_clients_only:
        # Match existing windows against the saved entries.
        current = snapshot_windows()
        for wid, window in current.items():
            hit = _match_window(wid, current, pending)
            if hit:
                entry, _cmd = hit
                matched[wid] = entry.app_id
                _apply_window(entry, wid, cfg)
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
                    if spawned_app_ids <= set(matched.values()):
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
                        matched[wid] = entry.app_id
                        _apply_window(entry, wid, cfg)
                if spawned_app_ids <= set(matched.values()):
                    break
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    # Focus replay: most-recently-focused window last.
    if cfg.focus_last:
        focus_order = sorted(
            windows, key=lambda e: e.focus_order, reverse=True
        )
        for entry in focus_order:
            for wid, app_id in matched.items():
                if app_id == entry.app_id:
                    _send(f"window-focus:{wid}")
                    break

    # Report: OK when the app appeared (matched a new or existing window),
    # TIMEOUT when neither the spawn nor the adjustment produced a window.
    matched_app_ids = set(matched.values())
    for entry in windows:
        if entry.app_id in matched_app_ids:
            report.append((entry, "OK"))
        else:
            report.append((entry, "TIMEOUT"))
    report.sort(key=lambda pair: pair[0].focus_order)
    return report


def _which(binary: str) -> bool:
    from .mapping import _which as which

    return which(binary)
