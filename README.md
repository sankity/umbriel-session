# umbriel-session

Session save and restore for the [Umbriel](https://github.com/noctalia-dev/umbriel) Wayland compositor.

## Features

- Snapshots windows, workspaces, geometry, and state to JSON
- Restore by spawning apps and silently moving them to saved workspaces, keeping the previously focused window focused
- Restore horizontal scrolling-column order from saved positions and floating-window geometry
- Recover supported VS Code-family editor workspaces and Obsidian vaults
- Shared-session launch handling for supported Electron clients, with custom app-ID opt-in
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
# Optional override when automatic per-window target detection is ambiguous.
# Use --new-window so separate saved editor windows cannot reuse each other.
"umbriel-session - Visual Studio Code" = "code --new-window /home/you/path/to/umbriel-session"

[electron]
# Additional app IDs, beyond the built-in clients below.
apps = []
```

### Restoring individual windows of an Electron app

Electron applications can create several windows under one main process and
may forward subsequent launches to that instance. This behavior is app-defined:
Electron does not provide a universal per-window restore command, metadata
format, or `--new-window` flag. The main PID's command line alone does not
identify every open document or workspace.

Support is deliberately app-specific:

| Applications | Recovery capability |
| --- | --- |
| VS Code, Code Insiders, VSCodium, Cursor, Windsurf, Antigravity IDE (`antigravity-ide`) | Per-window folder / `.code-workspace` URI |
| Obsidian | Per-vault `obsidian://open?vault=<id>` URI |
| Discord, Slack, Signal, Joplin, Bitwarden | One identical default launch per application session, then title-based placement |

For supported VS Code-family editors, save reads local workspace metadata
and matches it to each window title. Open workspace databases in the app's
process tree are preferred over historical metadata. The saved command includes
`--new-window` and the folder or `.code-workspace` URI, including remote URIs.
Both manual save and the auto-save daemon use this detection. During restore,
a transient empty editor window cannot claim a saved folder before its title
has loaded.

If multiple candidate folders have the same name, metadata is unavailable, or
you use a custom title format, detection may be ambiguous. A warning asks you to
set `[launch_by_title]`; no arbitrary folder is selected. `[launch]` overrides
remain explicit commands and bypass this detection. Other Electron applications
may also need `[launch_by_title]` to reopen a specific window rather than their
default window.

Obsidian reads only vault IDs and paths from local `obsidian.json`. A unique
vault in the window title becomes an encoded, shell-quoted URI. Native and
supported Flatpak launchers use their respective profile metadata. Restore waits
for the correct vault title, so changing the note within that vault is allowed;
an unloaded `Obsidian` window cannot claim the wrong vault. Duplicate vault
names are not guessed. This reopens the vault, not a historical copy of its tabs,
notes, or pop-out windows.

For the generic clients above, a default launch is shared across saved windows.
If that app already has a live window, another default launch is not issued.
Explicit target commands remain independent. A unique app/title match is
required before placing each window, even for explicit client URI launches. The
app itself must reopen any additional windows; this policy does not fabricate
extra chat windows or restore channels, messages, account sessions, or unsaved
application data. Generic/duplicate titles produce `AMBIGUOUS` rather than a
workspace-based guess. Existing native-session duplicates are never closed.

Native and known Flatpak app IDs are recognized for the generic clients and
Obsidian. Other Electron clients can opt
in to this shared-session policy using the exact compositor `app_id`:

```toml
[electron]
apps = ["custom.chat"]
```

This opt-in is a launch/matching policy, not automatic document recovery or
runtime detection of every Electron app. Unknown app IDs retain the existing
per-window launch behavior. Unsupported launch options or wrappers are left
alone rather than assigned guessed semantics. Supported editor adapters are not
converted to shared-session launches by this option. For another app's specific
document/window, provide its supported CLI or URI via `[launch_by_title]`; do not
add `--new-window` unless that particular app documents it.

**Save a new snapshot with the intended editor folders/vaults open to record
newly supported targets.** Older snapshots containing only `code`, `cursor`, or
`obsidian` do not contain the missing target URIs. Neither save nor restore
changes the application profiles.

### Zen / Firefox native session restoration

A bare Zen or Firefox command restores the browser profile, potentially opening
several native windows. Restore launches an identical native command only once,
not once per saved window, and does not launch it again when that browser already
has live windows. Explicit per-window commands such as `zen --new-window <url>`
remain independent.

Native-restored windows are matched only by a unique saved/live page title, not
by the workspace on which they happen to map. Some Zen builds leave restored
windows titled only `Zen Browser`, even after native restoration finishes. In
that case restore reports `AMBIGUOUS` rather than guessing which window belongs
on which workspace. Exact page-to-workspace restoration is not possible through
compositor metadata alone while those titles remain indistinguishable.

The restore report includes window titles. `FAILED` means state application
failed or the final workspace did not match the target. Unmatched live windows
are reported as `EXTRA`; they are never closed automatically. This also means
extra windows already recorded by the browser's own profile are not removed by
a subsequent Umbriel restore.

### Restore waiting and cancellation

`window_wait_timeout` limits how long matching waits without a relevant window
appearing/disappearing or its title changing (default: 10 seconds). Focus,
workspace and geometry events do not reset that timer. `load_time` remains the
hard overall matching limit (default: 60 seconds), even if titles keep changing.
Once all windows match, a short bounded settling period still catches late maps.

When waiting expires, restore returns its report, including `AMBIGUOUS` or
`TIMEOUT` entries; it does not wait indefinitely for indistinguishable browser
titles. The CLI displays a restoring indicator. Ctrl+C stops the subscription
and exits with status 130 without a Python traceback; already launched apps
remain open.

### Placement limits

Restore reorders unambiguous, single-window horizontal scrolling columns by
their saved `x` positions, rather than focus history. Unrelated columns keep
their slots. Maximized columns are restored, and nonmaximized widths are restored
when live viewport/gap measurements are available. Exact scroll offset and
stacked, tabbed, or vertical layout reconstruction are not supported by this
snapshot format.

Normal floating windows regain their saved position and size using the target
output's usable area. Client size constraints and compositor size limits still
apply. Missing numeric workspaces use Umbriel's last-workspace fallback; missing
named workspaces are not silently replaced by an unrelated name.

## Session file

`~/.local/share/umbriel-session/session.json`

Each window entry stores the resolved launch command, terminal CWD,
workspace (output + index), geometry, and floating / fullscreen /
maximized / pinned state.

## License

MIT
