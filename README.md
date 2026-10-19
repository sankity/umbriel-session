# umbriel-session

Session save and restore for the [Umbriel](https://github.com/noctalia-dev/umbriel) Wayland compositor.

## Features

- Snapshots windows, workspaces, geometry, and state to JSON
- Restore by spawning apps and silently moving them to saved workspaces
- Event-driven auto-save daemon
- Bridge commands for Flatpak / Snap / Electron apps
- Named profiles, dry-run, diff, and adjust-only reconcile mode

## Installation

```bash
pip install -e .
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
```

## Session file

`~/.local/share/umbriel-session/session.json`

Each window entry stores the resolved launch command, terminal CWD,
workspace (output + index), geometry, and floating / fullscreen /
maximized / pinned state.

## License

MIT
