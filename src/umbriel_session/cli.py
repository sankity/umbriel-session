"""Command line interface for umbriel-session."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import CONFIG_DIR, CONFIG_FILE, load_config
from .daemon import run_daemon
from .mapping import build_desktop_map, resolve_command
from .models import Session
from .restore import restore_session, umbriel_alive
from .session import (
    build_session,
    clear_all_sessions,
    clear_session,
    filter_raw_windows,
    list_sessions,
    load_session,
    parse_compositor_windows,
    parse_compositor_workspaces,
    save_session,
)
from .terminal import get_terminal_cwd

console = Console()
log = logging.getLogger(__name__)


def _require_compositor() -> None:
    if not umbriel_alive():
        console.print("[red]Umbriel compositor is not running.[/red]")
        raise SystemExit(1)


def _query_windows() -> list[dict]:
    result = subprocess.run(
        ["umbriel", "windows", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"umbriel windows failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _query_workspaces() -> list[dict]:
    result = subprocess.run(
        ["umbriel", "workspaces", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"umbriel workspaces failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _capture_now(cfg, profile: str | None) -> Session:
    _require_compositor()
    raw_windows = _query_windows()
    raw_workspaces = _query_workspaces()
    raw_windows = filter_raw_windows(raw_windows, cfg)
    desktop_map = build_desktop_map()
    entries = parse_compositor_windows(raw_windows, raw_workspaces)
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
    save_session(session, profile)
    return session


def cmd_save(args) -> None:
    cfg = load_config()
    session = _capture_now(cfg, args.profile)
    if not session.windows and not args.force_empty:
        console.print("[yellow]No windows detected. Use --force-empty to save anyway.[/yellow]")
        raise SystemExit(1)
    console.print(
        f"[green]Saved[/green] {len(session.windows)} window(s) "
        f"to profile [bold]{args.profile or 'default'}[/bold]"
    )


def cmd_restore(args) -> None:
    cfg = load_config()
    if not args.dry_run:
        _require_compositor()
    try:
        report = restore_session(
            profile=args.profile,
            dry_run=args.dry_run,
            workspaces=args.workspace,
            exclude=args.exclude,
            adjust_clients_only=args.adjust_clients_only,
            cfg=cfg,
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)

    table = Table(title=f"Restore: {args.profile or 'default'}")
    table.add_column("App")
    table.add_column("Workspace")
    table.add_column("Status")
    ok_status = ("OK", "DRY_RUN", "RESTORED", "EXISTING")
    for entry, status in report:
        colour = "green" if status in ok_status else "yellow"
        if status == "EXTRA":
            where = entry.workspace.id
        else:
            where = f"{entry.workspace.output}:{entry.workspace.index}"
        table.add_row(
            entry.app_id,
            where,
            f"[{colour}]{status}[/{colour}]",
        )
    console.print(table)


def cmd_status(args) -> None:
    session = load_session(args.profile)
    if session is None:
        console.print("[yellow]No saved session.[/yellow]")
        raise SystemExit(1)

    table = Table(title=f"Session: {args.profile or 'default'}")
    table.add_column("App")
    table.add_column("Title")
    table.add_column("Workspace")
    table.add_column("State")
    for w in session.windows:
        state = []
        if w.floating:
            state.append("float")
        if w.fullscreen:
            state.append("full")
        if w.maximized:
            state.append("max")
        if w.pinned:
            state.append("pin")
        table.add_row(
            w.app_id,
            w.title[:40],
            f"{w.workspace.output}:{w.workspace.index}",
            " ".join(state) or "—",
        )
    console.print(table)


def cmd_list(_args) -> None:
    sessions = list_sessions()
    if not sessions:
        console.print("[yellow]No saved sessions.[/yellow]")
        return
    for label, path in sessions:
        console.print(f"  [bold]{label}[/bold] — {path}")


def cmd_diff(args) -> None:
    session = load_session(args.profile)
    if session is None:
        console.print("[yellow]No saved session.[/yellow]")
        raise SystemExit(1)
    _require_compositor()
    active = {w["app_id"] for w in _query_windows()}
    saved = {w.app_id for w in session.windows}
    for app_id in sorted(saved - active):
        console.print(f"  [yellow]missing[/yellow] {app_id}")
    for app_id in sorted(active - saved):
        console.print(f"  [cyan]extra[/cyan]   {app_id}")
    if active == saved:
        console.print("  [green]in sync[/green]")


def cmd_clear(args) -> None:
    if args.all:
        count = clear_all_sessions()
        console.print(f"[green]Deleted[/green] {count} session(s)")
    elif clear_session(args.profile):
        console.print(f"[green]Deleted[/green] profile {args.profile or 'default'}")
    else:
        console.print("[yellow]No such session.[/yellow]")


def cmd_command(args) -> None:
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / args.name
    if not args.cmd:
        if target.exists():
            target.unlink()
            console.print(f"[green]Removed[/green] bridge {args.name}")
        else:
            console.print("[yellow]No such bridge.[/yellow]")
        return
    if shutil.which(args.name) and not target.exists():
        console.print(f"[red]'{args.name}' already exists in PATH.[/red]")
        raise SystemExit(1)
    target.write_text(f"#!/bin/sh\n{args.cmd}\n")
    target.chmod(0o755)
    console.print(f"[green]Created[/green] bridge {args.name}")


def cmd_daemon(args) -> None:
    cfg = load_config()
    _require_compositor()
    console.print("[green]Daemon started[/green]")
    run_daemon(cfg, args.interval)


def cmd_install_hooks(_args) -> None:
    service_dir = (
        Path.home() / ".config" / "systemd" / "user" / "umbriel.service.d"
    )
    service_dir.mkdir(parents=True, exist_ok=True)
    dropin = service_dir / "umbriel-session.conf"
    binary = shutil.which("umbriel-session") or "/usr/bin/umbriel-session"
    dropin.write_text(
        "[Service]\n"
        f"ExecStartPost=-{binary} restore\n"
    )
    console.print(f"[green]Installed[/green] drop-in at {dropin}")
    console.print("Run: systemctl --user daemon-reload")


def cmd_config(_args) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        console.print(f"[yellow]Config already exists at {CONFIG_FILE}[/yellow]")
        return
    CONFIG_FILE.write_text(
        "[general]\n"
        "restore_delay_seconds = 0.5\n"
        "window_wait_timeout = 10.0\n"
        "restore_floating = true\n"
        "restore_fullscreen = true\n"
        "restore_cwd = true\n"
        "load_time = 60\n"
        "spawn_interval = 0.3\n"
        "debounce_seconds = 2.0\n"
        "\n"
        "[ignore]\n"
        'classes = ["mpv", "vlc", "waybar", "dunst", "hyprpaper", "swaybg", "rofi", "wofi"]\n'
        'content_types = ["video", "game"]\n'
        "\n"
        "[launch]\n"
        '# "zen" = "flatpak run org.zen_browser.zen"\n'
    )
    console.print(f"[green]Created[/green] {CONFIG_FILE}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="umbriel-session",
        description="Session save and restore for the Umbriel Wayland compositor.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("save", help="Snapshot the current session.")
    p.add_argument("--profile", "-p", default=None)
    p.add_argument("--force-empty", action="store_true")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("restore", help="Restore a saved session.")
    p.add_argument("--profile", "-p", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--workspace", "-w", type=int, action="append", default=None)
    p.add_argument("--exclude", "-e", action="append", default=None)
    p.add_argument("--adjust-clients-only", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("status", help="Show the saved session.")
    p.add_argument("--profile", "-p", default=None)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="List saved profiles.")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("diff", help="Compare active vs saved.")
    p.add_argument("--profile", "-p", default=None)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("clear", help="Delete saved sessions.")
    p.add_argument("--profile", "-p", default=None)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("command", help="Create or remove a bridge command.")
    p.add_argument("name")
    p.add_argument("cmd", nargs="?", default="")
    p.set_defaults(func=cmd_command)

    p = sub.add_parser("daemon", help="Run the auto-save daemon.")
    p.add_argument("--interval", "-i", type=float, default=None)
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("install-hooks", help="Install the autostart hook.")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("config", help="Create a default config file.")
    p.set_defaults(func=cmd_config)

    return parser


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
