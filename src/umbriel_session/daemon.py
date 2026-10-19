"""Event-driven auto-save daemon."""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import threading
import time

from .config import Config
from .session import (
    save_session,
    build_session,
    parse_compositor_windows,
    parse_compositor_workspaces,
    filter_raw_windows,
)
from .mapping import resolve_command, build_desktop_map
from .terminal import get_terminal_cwd

log = logging.getLogger(__name__)

UMBRIEL = "umbriel"


def _query_windows() -> list[dict]:
    result = subprocess.run(
        [UMBRIEL, "windows", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _query_workspaces() -> list[dict]:
    result = subprocess.run(
        [UMBRIEL, "workspaces", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def capture(cfg) -> None:
    """Save the current session once."""
    raw_windows = _query_windows()
    raw_workspaces = _query_workspaces()
    raw_windows = filter_raw_windows(raw_windows, cfg)
    desktop_map = build_desktop_map()
    entries = parse_compositor_windows(raw_windows)
    for entry in entries:
        entry.cmd = resolve_command(
            entry.app_id, entry.pid, cfg.launch, desktop_map
        ) or ""
        if entry.app_id.lower() in {k.lower() for k in cfg.terminal_cwd_flags}:
            entry.cwd = get_terminal_cwd(entry.pid)
    session = build_session(
        [e for e in entries if e.cmd],
        parse_compositor_workspaces(raw_workspaces),
    )
    save_session(session)


def run_daemon(cfg: Config, interval: float | None = None) -> None:
    """Run the auto-save loop. Event-driven via subscribe, or poll fallback."""
    stop = threading.Event()

    def _handle_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if interval:
        log.info("Polling every %.0fs", interval)
        while not stop.is_set():
            try:
                capture(cfg)
            except Exception:
                log.exception("save failed")
            stop.wait(interval)
        return

    log.info("Subscribing to umbriel events")
    proc = subprocess.Popen(
        [UMBRIEL, "subscribe", "windows", "workspaces"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None

    debounce_timer: threading.Timer | None = None

    def _schedule_save():
        nonlocal debounce_timer
        if stop.is_set():
            return
        debounce_timer = threading.Timer(cfg.debounce_seconds, _do_save)
        debounce_timer.daemon = True
        debounce_timer.start()

    def _do_save():
        try:
            capture(cfg)
        except Exception:
            log.exception("save failed")

    try:
        for _line in proc.stdout:
            if stop.is_set():
                break
            _schedule_save()
    finally:
        if debounce_timer:
            debounce_timer.cancel()
        proc.terminate()
        proc.wait(timeout=5)
