"""Metadata fixtures only: never launch applications or read real profiles."""

import json
import shlex
from unittest.mock import patch

import pytest

from umbriel_session import electron, vscode


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


def vaults(root, data, *, flatpak=False):
    config = root / ".var/app/md.obsidian.Obsidian/config" if flatpak else root / "config"
    directory = config / "obsidian"
    directory.mkdir(parents=True)
    (directory / "obsidian.json").write_text(json.dumps({"vaults": data}))


@pytest.mark.parametrize("app_id", ["obsidian", "md.obsidian.Obsidian"])
def test_obsidian_unique_vault(isolated_profiles, app_id):
    vaults(isolated_profiles, {
        "alpha-id": {"path": "/notes/Alpha", "ts": 1, "open": True},
        "beta-id": {"path": "/notes/Beta", "ts": 2, "open": True},
    })
    command = electron.window_command(app_id, 42, "note - Alpha - Obsidian v1.14.4", "obsidian")
    assert shlex.split(command) == ["obsidian", "obsidian://open?vault=alpha-id"]
    assert "--new-window" not in command
    assert electron.session_launch_key(app_id, command) is None


def test_obsidian_id_encoded_and_quoted(isolated_profiles):
    vaults(isolated_profiles, {"id & 'special'": {"path": "/notes/My vault"}})
    command = electron.window_command("obsidian", 42, "My vault - Obsidian", "obsidian")
    assert shlex.split(command)[-1] == "obsidian://open?vault=id%20%26%20%27special%27"


def test_obsidian_absolute_title_can_disambiguate(isolated_profiles):
    vaults(isolated_profiles, {
        "first": {"path": "/first/Notes"}, "second": {"path": "/second/Notes"},
    })
    command = electron.window_command("obsidian", 42, "/second/Notes", "obsidian")
    assert shlex.split(command)[-1] == "obsidian://open?vault=second"


def test_obsidian_ambiguous_never_picks_latest_or_open(isolated_profiles, caplog):
    vaults(isolated_profiles, {
        "first": {"path": "/first/Notes", "ts": 1, "open": False},
        "second": {"path": "/second/Notes", "ts": 99, "open": True},
    })
    assert electron.window_command("obsidian", 42, "note - Notes - Obsidian", "obsidian") == "obsidian"
    assert "cannot uniquely resolve" in caplog.text


def test_obsidian_note_name_does_not_match_another_vault(isolated_profiles):
    vaults(isolated_profiles, {
        "first": {"path": "/notes/Alpha"}, "second": {"path": "/notes/Beta"},
    })
    command = electron.window_command("obsidian", 42, "Alpha - Beta - Obsidian", "obsidian")
    assert shlex.split(command)[-1] == "obsidian://open?vault=second"
    assert electron.window_command("obsidian", 42, "Alphabet - Obsidian", "obsidian") == "obsidian"


@pytest.mark.parametrize("data", [[], None, {"x": None}, {"x": {"path": "relative"}}, {"x": {"path": 1}}])
def test_obsidian_invalid_metadata(isolated_profiles, data):
    vaults(isolated_profiles, data)
    assert electron.window_command("obsidian", 42, "Notes - Obsidian", "obsidian") == "obsidian"


def test_obsidian_missing_and_corrupt_metadata(isolated_profiles):
    assert electron.window_command("obsidian", 42, "Notes", "obsidian") == "obsidian"
    directory = isolated_profiles / "config/obsidian"
    directory.mkdir(parents=True)
    (directory / "obsidian.json").write_text("not json")
    assert electron.window_command("obsidian", 42, "Notes", "obsidian") == "obsidian"


def test_obsidian_explicit_command_not_overridden(isolated_profiles):
    vaults(isolated_profiles, {"first": {"path": "/notes/Notes"}})
    command = "obsidian 'obsidian://open?vault=explicit'"
    assert electron.window_command("obsidian", 42, "Notes", command) == command


@pytest.mark.parametrize("command", [
    "obsidian --user-data-dir=/custom/profile",
    "obsidian --user-data-dir /custom/profile",
    "flatpak run --env=XDG_CONFIG_HOME=/custom md.obsidian.Obsidian",
])
def test_obsidian_custom_profiles_do_not_borrow_default_vault_ids(isolated_profiles, command):
    vaults(isolated_profiles, {"first": {"path": "/notes/Notes"}})
    vaults(isolated_profiles, {"second": {"path": "/sandbox/Notes"}}, flatpak=True)
    assert electron.window_command("obsidian", 42, "Notes - Obsidian", command) == command


def test_obsidian_flatpak_profile(isolated_profiles):
    vaults(isolated_profiles, {"wrong": {"path": "/native/Notes"}})
    vaults(isolated_profiles, {"right": {"path": "/sandbox/Notes"}}, flatpak=True)
    command = "flatpak run md.obsidian.Obsidian"
    result = electron.window_command("obsidian", 42, "Notes - Obsidian", command)
    assert shlex.split(result)[-1] == "obsidian://open?vault=right"


def test_editor_dispatch_only_recognized():
    with patch.object(vscode, "window_command", return_value="workspace command") as adapter:
        assert electron.window_command("Cursor", 42, "project", "cursor") == "workspace command"
        adapter.assert_called_once_with("Cursor", 42, "project", "cursor")
        for app in ("kitty", "random-electron", "Antigravity", "discord", "slack"):
            assert electron.window_command(app, 42, "project", app) == app
        assert adapter.call_count == 1


@pytest.mark.parametrize("app_id,command,canonical", [
    ("Discord", "discord", "discord"),
    ("com.discordapp.Discord", "/usr/bin/discord --url --", "discord"),
    ("Slack", "slack", "slack"),
    ("com.slack.Slack", "slack --disable-gpu", "slack"),
    ("signal-desktop", "signal-desktop", "signal"),
    ("org.signal.Signal", "signal-desktop", "signal"),
    ("md.obsidian.Obsidian", "obsidian", "obsidian"),
    ("Joplin", "joplin-desktop", "joplin"),
    ("net.cozic.joplin_desktop", "joplin-desktop", "joplin"),
    ("Bitwarden", "bitwarden-desktop", "bitwarden"),
    ("com.bitwarden.desktop", "bitwarden-desktop", "bitwarden"),
])
def test_generic_defaults(app_id, command, canonical):
    assert electron.session_launch_key(app_id, command) == (canonical, shlex.join(shlex.split(command)))


@pytest.mark.parametrize("command", [
    "flatpak run com.discordapp.Discord",
    "/usr/bin/flatpak run --branch=stable --arch=x86_64 --command=discord com.discordapp.Discord",
    "flatpak run --branch stable --arch x86_64 --command discord com.discordapp.Discord",
    "flatpak run --file-forwarding com.discordapp.Discord --url --",
])
def test_flatpak_default(command):
    assert electron.session_launch_key("discord", command) == ("discord", shlex.join(shlex.split(command)))


@pytest.mark.parametrize("command", [
    "flatpak run", "flatpak kill com.discordapp.Discord",
    "flatpak run --branch", "flatpak run --branch= com.discordapp.Discord",
    "flatpak run --unknown com.discordapp.Discord",
    "flatpak run com.slack.Slack", "flatpak run com.discordapp.Discord discord://channel/1",
    "flatpak run --file-forwarding com.discordapp.Discord @@u discord://x @@",
])
def test_flatpak_not_default(command):
    assert electron.session_launch_key("discord", command) is None


@pytest.mark.parametrize("command", [
    "discord discord://channel/1", "discord --url -- discord://channel/1",
    "discord https://example.com", "discord /target", "discord --new-window",
    "discord --profile work", "discord --unknown=value", "discord --ozone-platform",
    "discord --ozone-platform --disable-gpu", "discord --disable-gpu=target",
    "discord;", "discord && discord", "discord|other", "discord >file",
    "discord # comment", "discord\nother", "discord 'unterminated", "", "''",
    "sh -c discord", "env NAME=value discord", "discord `other`", "discord $FLAGS",
])
def test_target_or_uncertain_commands_not_grouped(command):
    assert electron.session_launch_key("discord", command) is None


def test_runtime_options_and_profiles_stay_separate():
    one = "discord --ozone-platform=wayland --user-data-dir '/profiles/one profile'"
    two = "discord --ozone-platform wayland --user-data-dir /profiles/two"
    assert electron.session_launch_key("discord", one) is not None
    assert electron.session_launch_key("discord", one) != electron.session_launch_key("discord", two)
    assert electron.session_launch_key("discord", "discord") == electron.session_launch_key("Discord", "'discord'")


@pytest.mark.parametrize("app", ["code", "com.microsoft.VSCode", "codium", "cursor", "windsurf", "antigravity-ide"])
def test_editors_never_generic_grouped_even_when_configured(app):
    assert electron.session_launch_key(app, app, {app}) is None


def test_explicit_configured_app_opt_in():
    assert electron.session_launch_key("Custom.App", "custom-app") is None
    assert electron.session_launch_key("Custom.App", "custom-app", {"CUSTOM.APP"}) == ("custom.app", "custom-app")
    assert electron.session_launch_key("Custom.App", "custom-app custom://window/1", {"Custom.App"}) is None
    assert electron.session_launch_key("Custom.App", "flatpak run Custom.App", {"Custom.App"}) is not None
    assert electron.session_launch_key("custom.app.helper", "custom-app", {"custom.app"}) is None


@pytest.mark.parametrize("app_id,launcher,flatpak", [
    ("obsidian", "obsidian", False),
    ("md.obsidian.Obsidian", "/usr/bin/obsidian", False),
    ("obsidian", "flatpak run md.obsidian.Obsidian", True),
    ("md.obsidian.Obsidian", "flatpak run --branch=stable --command=obsidian md.obsidian.Obsidian", True),
])
@pytest.mark.parametrize("target", ["alpha-id", "Alpha", "alpha"])
def test_target_title_waits_for_vault_not_note(isolated_profiles, app_id, launcher, flatpak, target):
    vaults(isolated_profiles, {
        "alpha-id": {"path": "/notes/Alpha"}, "beta-id": {"path": "/notes/Beta"},
    }, flatpak=flatpak)
    command = f"{launcher} 'obsidian://open?vault={target}&file=old-note'"
    assert not electron.command_matches_title(app_id, command, "Obsidian")
    assert not electron.command_matches_title(app_id, command, "Obsidian v1.14.4")
    assert not electron.command_matches_title(app_id, command, "Alpha - Beta - Obsidian")
    assert not electron.command_matches_title(app_id, command, "note - Alphabet - Obsidian")
    assert electron.command_matches_title(app_id, command, "old-note - Alpha - Obsidian")
    assert electron.command_matches_title(app_id, command, "changed-note - Alpha - Obsidian v1.14.4")


def test_target_title_profile_isolation(isolated_profiles):
    vaults(isolated_profiles, {"same-id": {"path": "/native/Native"}})
    vaults(isolated_profiles, {"same-id": {"path": "/sandbox/Sandbox"}}, flatpak=True)
    native = "obsidian 'obsidian://open?vault=same-id'"
    flatpak = "flatpak run md.obsidian.Obsidian 'obsidian://open?vault=same-id'"
    for app_id in ("obsidian", "md.obsidian.Obsidian"):
        assert electron.command_matches_title(app_id, native, "Native - Obsidian")
        assert not electron.command_matches_title(app_id, native, "Sandbox - Obsidian")
        assert electron.command_matches_title(app_id, flatpak, "Sandbox - Obsidian")
        assert not electron.command_matches_title(app_id, flatpak, "Native - Obsidian")


@pytest.mark.parametrize("target", ["Notes", "first", "second"])
def test_target_title_duplicate_vault_names_not_guessed(isolated_profiles, target):
    vaults(isolated_profiles, {
        "first": {"path": "/one/Notes"}, "second": {"path": "/two/Notes"},
    })
    command = f"obsidian 'obsidian://open?vault={target}'"
    assert not electron.command_matches_title("obsidian", command, "note - Notes - Obsidian")
    assert electron.command_matches_title("obsidian", command, "/one/Notes") is (target == "first")


@pytest.mark.parametrize("flatpak", [False, True])
@pytest.mark.parametrize("note", ["", "note - ", "chapter - section - "])
def test_overlapping_delimited_vault_names_never_choose_shorter(isolated_profiles, flatpak, note):
    vaults(isolated_profiles, {
        "long-id": {"path": "/notes/My - Notes"},
        "short-id": {"path": "/notes/Notes"},
        "longer-id": {"path": "/notes/Team - My - Notes"},
    }, flatpak=flatpak)
    launcher = "flatpak run md.obsidian.Obsidian" if flatpak else "obsidian"
    title = f"{note}My - Notes - Obsidian v1.14.4"
    assert electron.window_command("obsidian", 42, title, launcher) == launcher
    for target in ("long-id", "short-id", "longer-id", "My%20-%20Notes", "Notes"):
        command = f"{launcher} 'obsidian://open?vault={target}'"
        assert not electron.command_matches_title("obsidian", command, title)
    longer_title = f"{note}Team - My - Notes - Obsidian"
    assert electron.window_command("obsidian", 42, longer_title, launcher) == launcher
    for target in ("longer-id", "long-id", "short-id"):
        command = f"{launcher} 'obsidian://open?vault={target}'"
        assert not electron.command_matches_title("obsidian", command, longer_title)


@pytest.mark.parametrize("flatpak", [False, True])
@pytest.mark.parametrize("note", ["", "note - ", "chapter - section - ", "Other - "])
def test_unique_delimited_vault_name_preserved(isolated_profiles, flatpak, note):
    vaults(isolated_profiles, {
        "long-id": {"path": "/notes/My - Notes"},
        "other-id": {"path": "/notes/Other"},
    }, flatpak=flatpak)
    launcher = "flatpak run md.obsidian.Obsidian" if flatpak else "obsidian"
    title = f"{note}My - Notes - Obsidian v1.14.4"
    command = electron.window_command("obsidian", 42, title, launcher)
    assert shlex.split(command) == shlex.split(launcher) + ["obsidian://open?vault=long-id"]
    assert electron.command_matches_title("obsidian", command, title)
    assert electron.command_matches_title("obsidian", command, "changed - note - My - Notes - Obsidian")
    named = f"{launcher} 'obsidian://open?vault=My%20-%20Notes'"
    assert electron.command_matches_title("obsidian", named, title)
    wrong = f"{launcher} 'obsidian://open?vault=other-id'"
    assert not electron.command_matches_title("obsidian", wrong, title)


def test_note_separators_and_partial_vault_suffix_do_not_claim_another_vault(isolated_profiles):
    vaults(isolated_profiles, {
        "actual-id": {"path": "/notes/TeamNotes"},
        "short-id": {"path": "/notes/Notes"},
        "note-id": {"path": "/notes/Chapter - Draft"},
    })
    title = "Chapter - Draft - TeamNotes - Obsidian"
    command = electron.window_command("obsidian", 42, title, "obsidian")
    assert shlex.split(command)[-1] == "obsidian://open?vault=actual-id"
    assert electron.command_matches_title("obsidian", command, title)
    for target in ("short-id", "note-id"):
        assert not electron.command_matches_title("obsidian", f"obsidian obsidian://open?vault={target}", title)


def test_target_title_id_name_collision_is_ambiguous(isolated_profiles):
    vaults(isolated_profiles, {
        "first": {"path": "/one/Notes"}, "second": {"path": "/two/first"},
    })
    command = "obsidian 'obsidian://open?vault=first'"
    assert not electron.command_matches_title("obsidian", command, "Notes - Obsidian")
    assert not electron.command_matches_title("obsidian", command, "first - Obsidian")


def test_target_title_uri_decoding_and_no_command_rewrite(isolated_profiles):
    vaults(isolated_profiles, {"id & 'special'": {"path": "/notes/My vault"}})
    command = electron.window_command("obsidian", 42, "My vault - Obsidian", "obsidian")
    assert electron.command_matches_title("obsidian", command, "different note - My vault - Obsidian")
    named = "obsidian 'obsidian://open?vault=My%20vault'"
    assert electron.command_matches_title("obsidian", named, "different note - My vault - Obsidian")
    assert electron.window_command("obsidian", 42, "My vault - Obsidian", command) == command


@pytest.mark.parametrize("flatpak", [False, True])
def test_target_title_missing_metadata_fails_closed(isolated_profiles, flatpak):
    launcher = "flatpak run md.obsidian.Obsidian" if flatpak else "obsidian"
    command = f"{launcher} 'obsidian://open?vault=alpha-id'"
    assert not electron.command_matches_title("obsidian", command, "Alpha - Obsidian")
    # A valid profile for the other installation must not be borrowed.
    vaults(isolated_profiles, {"alpha-id": {"path": "/notes/Alpha"}}, flatpak=not flatpak)
    assert not electron.command_matches_title("obsidian", command, "Alpha - Obsidian")


@pytest.mark.parametrize("data", [None, [], {"vaults": []}, {"vaults": {"id": {"path": "relative"}}}])
def test_target_title_malformed_metadata_fails_closed(isolated_profiles, data):
    directory = isolated_profiles / "config/obsidian"
    directory.mkdir(parents=True)
    metadata = directory / "obsidian.json"
    metadata.write_text(json.dumps(data))
    command = "obsidian 'obsidian://open?vault=id'"
    assert not electron.command_matches_title("obsidian", command, "Notes - Obsidian")
    metadata.write_text("not json")
    assert not electron.command_matches_title("obsidian", command, "Notes - Obsidian")


@pytest.mark.parametrize("arguments", [
    "'obsidian://open?vault=missing'",
    "'obsidian://open?vault='",
    "'obsidian://open?vault=id&vault=id'",
    "'obsidian://open?vault=id&path=%2Fother%2Fnote'",
    "'obsidian://open?vault=id' 'obsidian://open?vault=id'",
    "--user-data-dir=/custom 'obsidian://open?vault=id'",
])
def test_target_title_uncertain_targets_fail_closed(isolated_profiles, arguments):
    vaults(isolated_profiles, {"id": {"path": "/notes/Notes"}})
    assert not electron.command_matches_title("obsidian", f"obsidian {arguments}", "Notes - Obsidian")


def test_target_title_editor_delegation():
    with patch.object(vscode, "command_matches_title", return_value=False) as adapter:
        assert not electron.command_matches_title("Cursor", "cursor --folder-uri file:///one", "one")
    adapter.assert_called_once_with("cursor --folder-uri file:///one", "one")


@pytest.mark.parametrize("app_id,command", [
    ("obsidian", "obsidian"),
    ("obsidian", "obsidian 'obsidian://choose-vault'"),
    ("obsidian", "obsidian 'obsidian://open?path=%2Fnotes%2Fnote'"),
    ("discord", "discord 'obsidian://open?vault=id'"),
    ("custom-app", "custom 'obsidian://open?vault=id'"),
])
def test_target_title_others_unfiltered(app_id, command):
    assert electron.command_matches_title(app_id, command, "Obsidian")


@pytest.mark.parametrize("app_id,command", [
    ("Slack", "slack 'slack://channel?team=one&id=two'"),
    ("com.slack.Slack", "flatpak run com.slack.Slack 'slack://channel/one'"),
    ("Discord", "discord 'discord://channel/one'"),
    ("org.signal.Signal", "signal-desktop 'sgnl://linkdevice'"),
    ("net.cozic.joplin_desktop", "joplin-desktop 'joplin://x-callback-url/openNote?id=one'"),
    ("com.bitwarden.desktop", "bitwarden-desktop 'bitwarden://target'"),
    ("obsidian", "obsidian"),
    ("md.obsidian.Obsidian", "flatpak run md.obsidian.Obsidian"),
    ("obsidian", "obsidian 'obsidian://open?path=%2Fnotes%2Fnote'"),
])
def test_requires_unique_title_independent_of_launch_key(app_id, command):
    assert electron.requires_unique_title(app_id, command)
    if "://" in command:
        assert electron.session_launch_key(app_id, command) is None


@pytest.mark.parametrize("launcher", ["obsidian", "flatpak run md.obsidian.Obsidian"])
@pytest.mark.parametrize("query", ["vault=id", "vault=", "vault", "vault=id&vault=other", "vault=id&path=%2Fother"])
def test_vault_targets_use_command_validator_even_when_invalid(launcher, query):
    command = f"{launcher} 'obsidian://open?{query}'"
    assert not electron.requires_unique_title("md.obsidian.Obsidian", command)
    # With no metadata, including a syntactically valid ID must fail closed.
    assert not electron.command_matches_title("md.obsidian.Obsidian", command, "Notes - Obsidian")


def test_requires_unique_title_configured_ids_and_unknown_apps():
    command = "custom-app 'custom://window/one'"
    assert not electron.requires_unique_title("Custom.App", command)
    assert electron.requires_unique_title("Custom.App", command, {"CUSTOM.APP"})
    assert not electron.requires_unique_title("custom.app.helper", command, {"custom.app"})
    assert not electron.requires_unique_title("kitty", "kitty")


@pytest.mark.parametrize("app_id", ["code", "com.microsoft.VSCode", "codium", "Cursor", "windsurf", "antigravity-ide"])
def test_editors_never_require_unique_title_even_when_configured(app_id):
    assert not electron.requires_unique_title(app_id, f"{app_id} --folder-uri file:///one", {app_id})


def test_malformed_vault_shell_command_cannot_enable_app_fallback():
    command = "obsidian 'obsidian://open?vault=id"
    assert electron.requires_unique_title("obsidian", command)
    assert not electron.command_matches_title("obsidian", command, "Notes - Obsidian")
