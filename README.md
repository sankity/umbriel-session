# umbriel-session

Session save and restore for the [Umbriel](https://github.com/noctalia-dev/umbriel) Wayland compositor.

## Features

- Snapshots windows, workspaces, geometry, and state to JSON
- Restore by spawning apps and silently moving them to saved workspaces, keeping the previously focused window focused
- Event-driven auto-save daemon
- Bridge commands for Flatpak / Snap / Electron apps
- Named profiles, dry-run, diff, and adjust-only reconcile mode

## Installation

### User-local (no root)

```bash
./install.sh
```

### System-wide

```bash
sudo ./install.sh --system
# or via pacman:
makepkg -si
```

### Development

```bash
PYTHONPATH=src python3 -m umbriel_session.cli --help
```

## Quick start

```bash
# Save the current session
umbriel-session save

# Restore it (on the next login, or right now)
umbriel-session restore

# Preview without launching anything
umbriel-session restore --dry-run

# Auto-save daemon (event-driven)
umbriel-session daemon

# Restore automatically when the compositor starts
umbriel-session install-hooks
```

## Commands

| Command | Description |
| --- | --- |
| `save` | Snapshot the current desktop |
| `restore` | Restore a saved session |
| `status` | Show the saved session |
| `list` | List saved profiles |
| `diff` | Compare active vs saved |
| `clear` | Delete saved sessions |
| `command <name> <cmd>` | Create a bridge command in `~/.local/bin` |
| `daemon` | Auto-save background daemon |
| `install-hooks` | Install the systemd autostart hook |

## Configuration

`~/.config/umbriel-session/config.toml` — see `umbriel-session config` to create a default.

```toml
[general]
restore_delay_seconds = 0.5
window_wait_timeout = 10.0
restore_floating = true
restore_fullscreen = true
restore_cwd = true
load_time = 60
spawn_interval = 0.3
debounce_seconds = 2.0
focus_last = true

[ignore]
classes = ["mpv", "vlc", "waybar", "dunst"]
content_types = ["video", "game"]

[launch]
"zen" = "flatpak run org.zen_browser.zen"

[launch_by_title]
# Apps whose windows cannot be told apart by app id share one process
# (every Electron app does), so a window is identified by its title only.
# Map a title to the command that opens exactly that window.
"umbriel-session - Visual Studio Code" = "code /home/you/path/to/umbriel-session"
```

### Restoring individual windows of an Electron app

An Electron app runs every window in one process, so the compositor reports the
same pid for all of them and the launch command alone cannot say which window
is which. The title is the only per-window signal, and it changes as the app
does — a browser tab shows the page title, not the app name.

`umbriel-session` uses the window title to match a running window to a saved
one, and the `.desktop` entry to launch it again. When a window has to be
launched, the app's default command opens a new default window, not the one
the session recorded. Use `[launch_by_title]` for those.

## Session file

`~/.local/share/umbriel-session/session.json`

Each window entry stores the resolved launch command, terminal CWD,
workspace (output + index), geometry, and floating / fullscreen /
maximized / pinned state.

## License

MIT
