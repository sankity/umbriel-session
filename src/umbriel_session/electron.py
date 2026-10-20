"""Conservative, app-specific Electron recovery; not universal window recovery.

Obsidian URI contract: https://help.obsidian.md/Extending+Obsidian/Obsidian+URI
The vault parameter accepts a name or ID, NOT an absolute path. This is also
verified in the installed Obsidian 1.14.4 obsidian.asar/main.js URI resolver.
Use the stored ID so equal basenames cannot select an arbitrary vault.

Packaged identities (not a claim that these apps restore individual windows):
https://github.com/flathub/com.discordapp.Discord
https://github.com/flathub/com.slack.Slack
https://github.com/flathub/org.signal.Signal
https://github.com/flathub/md.obsidian.Obsidian
https://github.com/flathub/net.cozic.joplin_desktop
https://github.com/flathub/com.bitwarden.desktop
Native Discord, Obsidian and Bitwarden aliases also match installed desktop files.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import vscode

log = logging.getLogger(__name__)

_ALIASES = {
    "discord": "discord",
    "com.discordapp.discord": "discord",
    "slack": "slack",
    "com.slack.slack": "slack",
    "signal": "signal",
    "signal-desktop": "signal",
    "org.signal.signal": "signal",
    "obsidian": "obsidian",
    "md.obsidian.obsidian": "obsidian",
    "joplin": "joplin",
    "joplin-desktop": "joplin",
    "net.cozic.joplin_desktop": "joplin",
    "bitwarden": "bitwarden",
    "bitwarden-desktop": "bitwarden",
    "com.bitwarden.desktop": "bitwarden",
}

# Only launch/runtime switches with no per-window target semantics are allowed.
_RUNTIME_FLAGS = {
    "--no-sandbox", "--disable-gpu", "--disable-gpu-sandbox",
    "--enable-wayland-ime", "--enable-features", "--disable-features",
    "--ozone-platform", "--ozone-platform-hint", "--enable-logging",
    "--password-store", "--user-data-dir", "--class",
}
_VALUE_FLAGS = {
    "--enable-features", "--disable-features", "--ozone-platform",
    "--ozone-platform-hint", "--password-store", "--user-data-dir", "--class",
}


def _split(command: str) -> list[str]:
    try:
        # Refuse shell programs rather than guessing at their launch semantics.
        args = shlex.split(command)
    except ValueError:
        return []
    if any(token in {";", "&&", "||", "|", "&", ">", "<"} for token in args):
        return []
    return args


def _default_args(args: list[str], app: str) -> bool:
    if app == "discord" and args == ["--url", "--"]:
        # Discord's desktop Exec leaves these behind when %u is empty.
        return True
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--" and index == len(args) - 1:
            return True
        flag, equals, value = arg.partition("=")
        if flag not in _RUNTIME_FLAGS or (equals and (flag not in _VALUE_FLAGS or not value)):
            return False
        if not equals and flag in _VALUE_FLAGS:
            index += 1
            if index >= len(args) or args[index].startswith("-") or "://" in args[index]:
                return False
        index += 1
    return True


def _launch_args(args: list[str]) -> tuple[str | None, list[str]]:
    """Separate a Flatpak app ID from its runtime arguments, if present."""
    if Path(args[0]).name != "flatpak":
        return None, args[1:]
    if len(args) < 3 or args[1] != "run":
        return "", []
    index = 2
    while index < len(args) and args[index].startswith("--"):
        flag, equals, value = args[index].partition("=")
        if flag in {"--branch", "--arch", "--command", "--env"}:
            if not equals:
                index += 1
                if index >= len(args) or args[index].startswith("-"):
                    return "", []
            elif not value:
                return "", []
        elif flag not in {"--user", "--system", "--file-forwarding"} or equals:
            return "", []
        index += 1
    if index >= len(args):
        return "", []
    return args[index].lower(), args[index + 1:]


def session_launch_key(
    app_id: str, command: str, configured_apps: set[str] | None = None,
) -> tuple[str, str] | None:
    """Key a shared *default* launch, or return None for independent launches.

    Callers can launch each returned key once and then match observed windows.
    This cannot create/recover arbitrary Electron windows. Target commands and
    all editor commands remain independent. The command portion retains profile,
    runtime and Flatpak options, so different launch configurations stay separate.
    Explicitly configured IDs opt in to this policy, not to a metadata adapter.
    """
    app_id = app_id.lower()
    if vscode.is_editor(app_id):
        return None
    configured = {item.lower() for item in configured_apps or ()}
    app = _ALIASES.get(app_id)
    if app is None:
        if app_id not in configured:
            return None
        app = app_id
    args = _split(command)
    if not args or not args[0] or any(char in command for char in "\n\r`$;&|<>#"):
        return None
    flatpak_id, runtime = _launch_args(args)
    if flatpak_id is not None and (not flatpak_id or _ALIASES.get(flatpak_id, flatpak_id) != app):
        return None
    if not _default_args(runtime, app):
        return None
    return app, shlex.join(args)


def requires_unique_title(
    app_id: str, command: str, configured_apps: set[str] | None = None,
) -> bool:
    """Require exact unique titles independently of launch deduplication.

    Editors and explicit Obsidian vault targets use command_matches_title instead.
    Parsed vault parameters opt into that validator even when empty/invalid; they
    must never fall back to matching an arbitrary window from the same app.
    """
    app_id = app_id.lower()
    if vscode.is_editor(app_id):
        return False
    if _ALIASES.get(app_id) == "obsidian":
        for arg in _split(command)[1:]:
            if not arg.lower().startswith("obsidian://open?"):
                continue
            try:
                uri = urlsplit(arg)
                query = parse_qs(uri.query, keep_blank_values=True)
            except ValueError:
                # Unparseable targets retain strict title policy; the command
                # validator also rejects them rather than enabling fallback.
                return True
            if uri.netloc.lower() == "open" and not uri.path and "vault" in query:
                return False
    return app_id in _ALIASES or app_id in {item.lower() for item in configured_apps or ()}


def _obsidian_vaults(args: list[str]) -> dict[str, str]:
    """Read only vault IDs/paths from the launcher's supported profile."""
    if not args or any(arg.split("=", 1)[0] in {"--user-data-dir", "--env"} for arg in args[1:]):
        # A custom/sandbox profile override must not borrow native vault IDs.
        return {}
    flatpak_id, _ = _launch_args(args)
    if flatpak_id is not None and _ALIASES.get(flatpak_id) != "obsidian":
        return {}
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if flatpak_id is not None:
        # Native packages can also use md.obsidian.Obsidian as StartupWMClass;
        # use the launcher, not that app ID, to identify the sandbox profile.
        config = Path.home() / ".var" / "app" / "md.obsidian.Obsidian" / "config"
    try:
        data = json.loads((config / "obsidian" / "obsidian.json").read_text())
    except (OSError, ValueError):
        return {}
    vaults = data.get("vaults") if isinstance(data, dict) else None
    if not isinstance(vaults, dict):
        return {}
    return {
        vault_id: metadata["path"]
        for vault_id, metadata in vaults.items()
        if isinstance(vault_id, str) and vault_id and isinstance(metadata, dict)
        and isinstance(metadata.get("path"), str) and Path(metadata["path"]).is_absolute()
    }


def _obsidian_title_matches(vaults: dict[str, str], title: str) -> set[str]:
    # Remove only the application suffix: vault and note names may themselves
    # contain ' - '. Keep every full-name interpretation so overlapping vault
    # suffixes remain ambiguous rather than selecting the shorter vault.
    body, separator, app = title.strip().rpartition(" - ")
    if not separator or not app.startswith("Obsidian"):
        body = title.strip()
    return {
        vault_id for vault_id, path in vaults.items()
        if any(body == label or body.endswith(" - " + label)
               for label in {path, Path(path).name} if label)
    }


def _obsidian_command(app_id: str, title: str, command: str) -> str:
    # Preserve explicit URI/file targets, custom wrappers and uncertain options.
    if session_launch_key(app_id, command) is None:
        return command
    matches = _obsidian_title_matches(_obsidian_vaults(_split(command)), title)
    # Do not pick the latest ts, first entry, or only open=True candidate when
    # names collide: historical/open flags do not prove this window's identity.
    if len(matches) != 1:
        log.warning("cannot uniquely resolve Obsidian vault for %r; configure [launch_by_title]", title)
        return command
    uri = "obsidian://open?vault=" + quote(matches.pop(), safe="")
    return f"{command} {shlex.quote(uri)}"


def command_matches_title(app_id: str, command: str, title: str) -> bool:
    """Wait for a target's vault/workspace title, allowing note title changes.

    Obsidian vault names and IDs must resolve uniquely in this launcher's profile,
    and the live title must uniquely identify that same vault. Duplicate names
    cannot be disambiguated from a basename-only title, even with an ID URI.
    Unsupported apps and commands without an explicit vault target are unfiltered.
    """
    if vscode.is_editor(app_id):
        return vscode.command_matches_title(command, title)
    if _ALIASES.get(app_id.lower()) != "obsidian":
        return True
    args = _split(command)
    if not args:
        return "obsidian://open" not in command.lower()
    targets = []
    for arg in args[1:]:
        if not arg.lower().startswith("obsidian://open?"):
            continue
        try:
            uri = urlsplit(arg)
            query = parse_qs(uri.query, keep_blank_values=True)
        except ValueError:
            return False
        if uri.netloc.lower() == "open" and not uri.path and "vault" in query:
            if len(query["vault"]) != 1 or not query["vault"][0] or "path" in query:
                return False
            targets.append((arg, query["vault"][0]))
    if not targets:
        return True
    if len(targets) != 1 or title.strip().split(" v", 1)[0] == "Obsidian":
        return False
    uri_arg, target = targets[0]
    # Validate the underlying launcher independently of the explicit URI.
    launcher = shlex.join([arg for arg in args if arg != uri_arg])
    if session_launch_key(app_id, launcher) is None:
        return False
    vaults = _obsidian_vaults(args)
    candidates = {
        vault_id for vault_id, path in vaults.items()
        if vault_id == target or Path(path).name.upper() == target.upper()
    }
    return len(candidates) == 1 and _obsidian_title_matches(vaults, title) == candidates


def window_command(app_id: str, pid: int, title: str, command: str) -> str:
    """Recover a supported editor workspace or unique Obsidian vault target."""
    if vscode.is_editor(app_id):
        return vscode.window_command(app_id, pid, title, command)
    if _ALIASES.get(app_id.lower()) == "obsidian":
        return _obsidian_command(app_id, title, command)
    return command
