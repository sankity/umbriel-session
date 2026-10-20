"""Integration regressions for shared Electron sessions and per-vault targets."""

import copy
import json
import shlex

import pytest

from umbriel_session import cli, daemon, restore
from umbriel_session.config import Config

from .test_restore_browser import _Clock, desktop, launches, window


def app_entries(state, app_id="slack", command="slack"):
    for e, name in zip(state["entries"], ("Alpha", "Beta")):
        e.app_id = app_id
        e.title = f"{name} - {app_id}"
        e.cmd = command
    return state["entries"]


def test_shared_electron_session_launches_once_and_matches_reversed_windows(desktop):
    alpha, beta = app_entries(desktop)
    desktop["events"] = [{"terminal": desktop["terminal"],
                           "a": window("a", alpha.title, 2, app="slack"),
                           "b": window("b", beta.title, 3, app="slack")}]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert launches(desktop) == ["spawn:slack"]
    assert desktop["windows"]["a"]["workspace"] == "eDP-1:1"
    assert desktop["windows"]["b"]["workspace"] == "eDP-1:2"
    assert sum(status == "RESTORED" for _, status in report) == 2


def test_generic_electron_titles_cannot_claim_saved_windows(desktop):
    app_entries(desktop)
    desktop["events"] = [{"terminal": desktop["terminal"],
                           "a": window("a", "Slack", 1, app="slack"),
                           "b": window("b", "Slack", 2, app="slack")}]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert launches(desktop) == ["spawn:slack"]
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2


def test_running_electron_client_is_not_launched_again(desktop):
    app_entries(desktop)
    desktop["windows"].update({"a": window("a", "Slack", 1, app="slack"),
                                "b": window("b", "Slack", 2, app="slack")})
    report = restore.restore_session(cfg=desktop["cfg"])
    assert launches(desktop) == []
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2


def test_custom_electron_app_policy_reaches_matching_and_launching(desktop):
    app_entries(desktop, "custom.chat", "custom-chat")
    desktop["cfg"].electron_apps = {"custom.chat"}
    desktop["events"] = [{"terminal": desktop["terminal"],
                           "a": window("a", "Loading", 1, app="custom.chat"),
                           "b": window("b", "Loading", 2, app="custom.chat")}]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert launches(desktop) == ["spawn:custom-chat"]
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2


def test_unidentified_electron_windows_finish_with_open_subscription(desktop, monkeypatch):
    app_entries(desktop)
    desktop["windows"].update({"a": window("a", "Slack", 1, app="slack"),
                                "b": window("b", "Slack", 2, app="slack")})
    desktop["cfg"].load_time = 60
    desktop["cfg"].window_wait_timeout = 2
    clock = _Clock()

    def idle(readers, writers, errors, timeout):
        clock.now += timeout
        return ([], [], [])

    monkeypatch.setattr(restore.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(restore.select, "select", idle)
    report = restore.restore_session(cfg=desktop["cfg"])
    assert clock.now == pytest.approx(2)
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2
    assert launches(desktop) == []
    desktop["proc"].terminate.assert_called_once()


def test_unknown_apps_keep_existing_per_window_launch_policy(desktop):
    app_entries(desktop, "custom.chat", "custom-chat")
    restore.restore_session(cfg=desktop["cfg"])
    assert launches(desktop) == ["spawn:custom-chat", "spawn:custom-chat"]


def test_explicit_electron_links_launch_independently_but_require_identity(desktop):
    app_entries(desktop)
    for e, target in zip(desktop["entries"], ("alpha", "beta")):
        desktop["cfg"].launch_by_title[e.title] = f"slack slack://{target}"
    desktop["events"] = [{"terminal": desktop["terminal"],
                           "a": window("a", "Slack", 1, app="slack"),
                           "b": window("b", "Slack", 2, app="slack")}]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert launches(desktop) == ["spawn:slack slack://alpha", "spawn:slack slack://beta"]
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2


def test_electron_matches_are_revalidated_against_all_windows(desktop):
    alpha, beta = app_entries(desktop)
    first = {"terminal": desktop["terminal"],
             "a": window("a", alpha.title, 1, app="slack"),
             "b": window("b", "Slack", 2, app="slack")}
    later = copy.deepcopy(first)
    later["a"]["title"] = beta.title
    later["b"]["title"] = beta.title
    desktop["events"] = [first, later]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2


@pytest.fixture
def obsidian_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    directory = tmp_path / "obsidian"
    directory.mkdir()
    (directory / "obsidian.json").write_text(json.dumps({"vaults": {
        "alpha-id": {"path": "/notes/Alpha"}, "beta-id": {"path": "/notes/Beta"},
    }}))


def test_obsidian_targets_do_not_match_unloaded_or_wrong_vault(desktop, obsidian_profile):
    alpha, beta = app_entries(desktop, "obsidian", "obsidian")
    for e, name in zip((alpha, beta), ("Alpha", "Beta")):
        e.title = f"old note - {name} - Obsidian"
        e.cmd = f"obsidian obsidian://open?vault={name.lower()}-id"
    generic = {"terminal": desktop["terminal"],
               "a": window("a", "Obsidian", 2, app="obsidian"),
               "b": window("b", "Obsidian", 3, app="obsidian")}
    loaded = copy.deepcopy(generic)
    loaded["a"]["title"] = "new note - Alpha - Obsidian v1.14.4"
    loaded["b"]["title"] = "other note - Beta - Obsidian v1.14.4"
    desktop["events"] = [generic, loaded]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert launches(desktop) == [
        "spawn:obsidian obsidian://open?vault=alpha-id",
        "spawn:obsidian obsidian://open?vault=beta-id",
    ]
    assert desktop["windows"]["a"]["workspace"] == "eDP-1:1"
    assert desktop["windows"]["b"]["workspace"] == "eDP-1:2"
    assert sum(status == "RESTORED" for _, status in report) == 2


@pytest.mark.parametrize("module", [cli, daemon])
def test_manual_and_daemon_save_record_individual_obsidian_vaults(obsidian_profile, monkeypatch, module):
    raw_windows = [
        {"id": name, "app_id": "obsidian", "pid": 42,
         "title": f"note - {name} - Obsidian", "workspace": "eDP-1:1", "output": "eDP-1"}
        for name in ("Alpha", "Beta")
    ]
    workspaces = [{"id": "eDP-1:1", "index": 1, "output": "eDP-1", "name": "1",
                   "named": False, "layout": "scrolling", "layout_override": False}]
    saved = []
    monkeypatch.setattr(module, "_query_windows", lambda: raw_windows)
    monkeypatch.setattr(module, "_query_workspaces", lambda: workspaces)
    monkeypatch.setattr(module, "build_desktop_map", lambda: {"obsidian": "obsidian"})
    monkeypatch.setattr(module, "save_session", lambda s, *args: saved.append(s))
    if module is cli:
        monkeypatch.setattr(cli, "_require_compositor", lambda: None)
        cli._capture_now(Config(), None)
    else:
        daemon.capture(Config())
    assert [shlex.split(e.cmd) for e in saved[0].windows] == [
        ["obsidian", "obsidian://open?vault=alpha-id"],
        ["obsidian", "obsidian://open?vault=beta-id"],
    ]
