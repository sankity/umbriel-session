"""Per-window editor targets, including multiple windows sharing one PID."""

import json
import shlex
from pathlib import Path
from unittest.mock import patch

import pytest

from umbriel_session import cli, daemon, mapping, vscode
from umbriel_session.config import Config
from umbriel_session.models import WindowEntry
from umbriel_session.restore import assign_entries


@pytest.fixture
def code_root(tmp_path):
    root = tmp_path / "Code"
    (root / "User" / "workspaceStorage").mkdir(parents=True)
    with patch.object(vscode, "_user_data_dir", return_value=root), patch.object(
        vscode, "_active_storage_dirs", return_value=set()
    ):
        yield root


def store(root, key, data):
    directory = root / "User" / "workspaceStorage" / key
    directory.mkdir()
    (directory / "workspace.json").write_text(json.dumps(data))
    return directory


def test_two_folders_shared_pid(code_root):
    store(code_root, "a", {"folder": "file:///projects/umbriel-session"})
    store(code_root, "b", {"folder": "file:///projects/umbriel"})
    commands = [
        mapping.resolve_command(
            "com.microsoft.VSCode", 42, {}, {"com.microsoft.vscode": "code"},
            title=f"{folder} - Visual Studio Code",
        )
        for folder in ("umbriel-session", "umbriel")
    ]
    assert commands == [
        "code --new-window --folder-uri file:///projects/umbriel-session",
        "code --new-window --folder-uri file:///projects/umbriel",
    ]


def test_title_with_file_and_encoded_spaces(code_root):
    store(code_root, "a", {"folder": "file:///projects/my%20project"})
    cmd = vscode.window_command("code", 42, "● main.py - my project - Visual Studio Code", "code")
    assert shlex.split(cmd) == ["code", "--new-window", "--folder-uri", "file:///projects/my%20project"]


def test_multi_root_workspace(code_root):
    store(code_root, "a", {"workspace": "file:///projects/team.code-workspace"})
    cmd = vscode.window_command("code", 42, "team (Workspace) - Visual Studio Code", "code")
    assert "--file-uri file:///projects/team.code-workspace" in cmd


def test_remote_folder(code_root):
    store(code_root, "a", {"folder": "vscode-remote://ssh-remote+host/projects/remote"})
    cmd = vscode.window_command("code", 42, "remote [SSH: host] - Visual Studio Code", "code")
    assert "--folder-uri vscode-remote://ssh-remote+host/projects/remote" in cmd


def test_ambiguous_names_not_guessed(code_root, caplog):
    store(code_root, "a", {"folder": "file:///first/umbriel"})
    store(code_root, "b", {"folder": "file:///second/umbriel"})
    assert vscode.window_command("code", 42, "umbriel - Visual Studio Code", "code") == "code --new-window"
    assert "launch_by_title" in caplog.text


def test_active_database_disambiguates_history(code_root):
    store(code_root, "old", {"folder": "file:///old/umbriel"})
    active = store(code_root, "active", {"folder": "file:///current/umbriel"})
    with patch.object(vscode, "_active_storage_dirs", return_value={active}):
        cmd = vscode.window_command("code", 42, "umbriel - Visual Studio Code", "code")
    assert cmd.endswith("--folder-uri file:///current/umbriel")


def test_windows_state_format(code_root):
    directory = code_root / "User" / "globalStorage"
    directory.mkdir()
    (directory / "storage.json").write_text(json.dumps({"windowsState": {
        "openedWindows": [{"folder": "file:///projects/one"}],
        "lastActiveWindow": {"workspace": {"configPath": "/projects/two.code-workspace"}},
    }}))
    assert "--folder-uri file:///projects/one" in vscode.window_command("code", 42, "one - Visual Studio Code", "code")
    assert "--file-uri file:///projects/two.code-workspace" in vscode.window_command("code", 42, "two - Visual Studio Code", "code")


def test_launch_override_wins(code_root):
    store(code_root, "a", {"folder": "file:///projects/one"})
    assert mapping.resolve_command("code", 42, {"code": "custom-code /explicit"}, {}, title="one") == "custom-code /explicit"


def test_missing_or_malformed_metadata(code_root):
    directory = code_root / "User" / "globalStorage"
    directory.mkdir()
    (directory / "storage.json").write_text("not json")
    assert vscode.window_command("code", 42, "one - Visual Studio Code", "code") == "code --new-window"


def test_uri_is_shell_quoted(code_root):
    store(code_root, "a", {"folder": "file:///projects/a'b"})
    cmd = vscode.window_command("code", 42, "a'b - Visual Studio Code", "code")
    assert shlex.split(cmd)[-1] == "file:///projects/a'b"


def test_other_apps_unchanged():
    assert vscode.window_command("kitty", 42, "term", "kitty") == "kitty"


def test_transient_and_wrong_titles_rejected():
    command = "code --new-window --folder-uri file:///projects/one"
    assert not vscode.command_matches_title(command, "Visual Studio Code")
    assert not vscode.command_matches_title(command, "two - Visual Studio Code")
    assert vscode.command_matches_title(command, "main.py - one - Visual Studio Code")
    assert vscode.command_matches_title("kitty", "term")


@pytest.mark.parametrize("module", [cli, daemon])
def test_manual_and_daemon_capture_resolve_each_window(code_root, monkeypatch, module):
    for folder in ("one", "two"):
        store(code_root, folder, {"folder": f"file:///projects/{folder}"})
    windows = [
        {"id": folder, "app_id": "com.microsoft.VSCode", "pid": 42,
         "title": f"{folder} - Visual Studio Code", "workspace": "eDP-1:1",
         "output": "eDP-1"}
        for folder in ("one", "two")
    ]
    workspaces = [{"id": "eDP-1:1", "index": 1, "output": "eDP-1", "name": "1",
                   "named": False, "layout": "scrolling", "layout_override": False}]
    saved = []
    monkeypatch.setattr(module, "_query_windows", lambda: windows)
    monkeypatch.setattr(module, "_query_workspaces", lambda: workspaces)
    monkeypatch.setattr(module, "build_desktop_map", lambda: {"com.microsoft.vscode": "code"})
    monkeypatch.setattr(module, "save_session", lambda s, *args: saved.append(s))
    if module is cli:
        monkeypatch.setattr(cli, "_require_compositor", lambda: None)
        cli._capture_now(Config(), None)
    else:
        daemon.capture(Config())
    assert [entry.cmd for entry in saved[0].windows] == [
        f"code --new-window --folder-uri file:///projects/{folder}" for folder in ("one", "two")
    ]


def test_matching_waits_for_workspace_title(code_root):
    entry = WindowEntry.from_live({"app_id": "code", "title": "one - Visual Studio Code"})
    entry.cmd = "code --new-window --folder-uri file:///projects/one"
    pending = [(entry, entry.cmd)]
    live = {"new": {"app_id": "code", "title": "Visual Studio Code"}}
    assert assign_entries(live, pending, {}, strict_titles=True) == {}
    live["new"]["title"] = "two - Visual Studio Code"
    assert assign_entries(live, pending, {}, strict_titles=True) == {}
    live["new"]["title"] = "main.py - one - Visual Studio Code"
    assert assign_entries(live, pending, {}, strict_titles=True) == {"new": entry}


def test_same_title_from_different_app_does_not_claim_entry():
    entry = WindowEntry.from_live({"app_id": "code", "title": "same"})
    assert assign_entries({"other": {"app_id": "kitty", "title": "same"}}, [(entry, "code")], {}) == {}


def test_open_database_discovery(tmp_path):
    proc = tmp_path / "proc"
    storage = tmp_path / "Code" / "User" / "workspaceStorage"
    active = storage / "active"
    active.mkdir(parents=True)
    for pid, child in ((42, "43"), (43, "")):
        task = proc / str(pid) / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(child)
        (proc / str(pid) / "fd").mkdir()
    (proc / "43" / "fd" / "10").symlink_to(active / "state.vscdb")
    original = Path
    with patch.object(vscode, "Path", side_effect=lambda value: original(str(value).replace("/proc/", f"{proc}/", 1))):
        assert vscode._active_storage_dirs(42, storage) == {active}
