"""Browser-owned restoration must not spawn or permanently misidentify windows."""

import copy
import json
from unittest.mock import Mock

import pytest

from umbriel_session import restore
from umbriel_session.config import Config
from umbriel_session.models import Session, WindowEntry, WorkspaceRef


AI = "AI Usage · Zed Dashboard — Zen Browser"
NEWS = "Linux Hardware Reviews & Performance Benchmarks — Zen Browser"


def entry(title, index):
    result = WindowEntry.from_live({"app_id": "zen", "title": title, "output": "eDP-1"})
    result.cmd = "/opt/zen-browser-bin/zen-bin"
    result.workspace = WorkspaceRef("eDP-1", index, False, str(index), f"eDP-1:{index}")
    result.focus_order = index
    return result


def window(wid, title, index, app="zen", active=False):
    return {"id": wid, "app_id": app, "title": title, "workspace": f"eDP-1:{index}",
            "output": "eDP-1", "active": active, "floating": False}


def test_generic_browser_titles_do_not_claim_saved_workspaces():
    pending = [(e, e.cmd) for e in (entry(AI, 1), entry(NEWS, 2))]
    live = {"a": window("a", "Zen Browser", 1), "b": window("b", "Zen Browser", 2)}
    ws = {f"eDP-1:{i}": {"output": "eDP-1", "index": i} for i in (1, 2)}
    assert restore.assign_entries(live, pending, ws, strict_titles=True) == {}
    # The second window's title is ready first. It must not make the generic
    # first window claim the remaining entry through an app/workspace fallback.
    live["b"]["title"] = AI
    assert restore.assign_entries(live, pending, ws, strict_titles=True) == {"b": pending[0][0]}
    live["a"]["title"] = NEWS
    assert restore.assign_entries(live, pending, ws, strict_titles=True) == {
        "a": pending[1][0], "b": pending[0][0],
    }


def test_duplicate_page_titles_are_ambiguous():
    pending = [(e, e.cmd) for e in (entry(AI, 1), entry(AI, 2))]
    live = {"a": window("a", AI, 1), "b": window("b", AI, 2)}
    assert restore.assign_entries(live, pending, {}, strict_titles=True) == {}


def test_native_launch_key_preserves_profiles_and_explicit_commands():
    e = entry(AI, 1)
    key = restore._native_browser_launch
    assert key(e, "zen -P work") == ("zen", "zen -P work")
    assert key(e, "zen --profile '/profiles/my profile'") != key(e, "zen -P work")
    assert key(e, "zen --new-window https://example.com") is None
    assert key(e, "zen https://example.com") is None
    assert key(e, "zen --blank-window") is None


@pytest.fixture
def desktop(monkeypatch):
    entries = [entry(AI, 1), entry(NEWS, 2)]
    saved = Session(1, 0, "umbriel", [], entries)
    terminal = window("terminal", "Terminal", 1, app="kitty", active=True)
    state = {"windows": {"terminal": terminal}, "events": [], "batches": []}
    ws = {
        f"eDP-1:{i}": {"id": f"eDP-1:{i}", "output": "eDP-1", "index": i,
                        "name": str(i), "named": False, "occupied": i == 1,
                        "focused": i == 1, "window_count": 1 if i == 1 else 0}
        for i in (1, 2, 3)
    }
    proc = Mock()

    def readline():
        if not state["events"]:
            return ""
        event = state["events"].pop(0)
        state["windows"] = copy.deepcopy(event)
        return json.dumps({"event": "windows", "data": list(event.values())}) + "\n"

    def send(actions):
        state["batches"].append(actions)
        focused = next((wid for wid, w in state["windows"].items() if w["active"]), None)
        for action in actions:
            if action.startswith("window-focus:"):
                focused = action.split(":", 1)[1]
                for wid, w in state["windows"].items():
                    w["active"] = wid == focused
            elif action.startswith("window-move-to-workspace-silent:") and focused:
                index = action.split(":", 1)[1].split("/", 1)[0]
                state["windows"][focused]["workspace"] = f"eDP-1:{index}"
        return True

    proc.stdout.readline.side_effect = readline
    monkeypatch.setattr(restore, "load_session", lambda profile: saved)
    monkeypatch.setattr(restore, "snapshot_windows", lambda: copy.deepcopy(state["windows"]))
    monkeypatch.setattr(restore, "snapshot_workspaces", lambda: copy.deepcopy(ws))
    monkeypatch.setattr(restore, "_send_many", send)
    monkeypatch.setattr(restore, "_which", lambda binary: True)
    monkeypatch.setattr(restore.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(restore.select, "select", lambda *a: ([proc.stdout], [], []))
    state["proc"] = proc
    state["entries"] = entries
    state["terminal"] = terminal
    state["cfg"] = Config(restore_delay_seconds=0, spawn_interval=0, load_time=1)
    return state


def launches(desktop):
    return [action for batch in desktop["batches"] for action in batch if action.startswith("spawn:")]


def test_native_restores_two_windows_with_one_launch_and_delayed_reversed_titles(desktop):
    generic = {"terminal": desktop["terminal"],
               "a": window("a", "Zen Browser", 2), "b": window("b", "Zen Browser", 3)}
    titled = copy.deepcopy(generic)
    titled["a"]["title"] = AI
    titled["b"]["title"] = NEWS
    desktop["events"] = [generic, titled]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert launches(desktop) == ["spawn:/opt/zen-browser-bin/zen-bin"]
    assert desktop["windows"]["a"]["workspace"] == "eDP-1:1"
    assert desktop["windows"]["b"]["workspace"] == "eDP-1:2"
    assert [(e.title, status) for e, status in report if status != "EXTRA"] == [
        (AI, "RESTORED"), (NEWS, "RESTORED"),
    ]


def test_persistent_generic_titles_report_ambiguity_not_success_or_extra_launch(desktop):
    desktop["events"] = [{"terminal": desktop["terminal"],
                           "a": window("a", "Zen Browser", 2),
                           "b": window("b", "Zen Browser", 3)}]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert len(launches(desktop)) == 1
    assert [status for _, status in report if status != "EXTRA"] == ["AMBIGUOUS", "AMBIGUOUS"]


def test_restore_with_running_generic_browser_does_not_launch_more(desktop):
    desktop["windows"].update({"a": window("a", "Zen Browser", 1),
                                "b": window("b", "Zen Browser", 2)})
    report = restore.restore_session(cfg=desktop["cfg"])
    assert launches(desktop) == []
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2


def test_explicit_browser_window_commands_are_not_deduplicated(desktop):
    for e, url in zip(desktop["entries"], ("https://example.com/one", "https://example.com/two")):
        e.cmd += f" --new-window {url}"
    restore.restore_session(cfg=desktop["cfg"])
    assert len(launches(desktop)) == 2


def test_unapplied_move_is_not_reported_as_restored(desktop, monkeypatch):
    desktop["windows"]["a"] = window("a", AI, 2)
    desktop["windows"]["b"] = window("b", NEWS, 1)
    monkeypatch.setattr(restore, "_send_many", lambda actions: True)
    report = restore.restore_session(adjust_clients_only=True, cfg=desktop["cfg"])
    assert [status for _, status in report if status != "EXTRA"] == ["FAILED", "FAILED"]


def test_native_launch_is_not_replayed_after_uncertain_batch_failure(desktop, monkeypatch):
    def fail(actions):
        desktop["batches"].append(actions)
        return False

    monkeypatch.setattr(restore, "_send_many", fail)
    restore.restore_session(cfg=desktop["cfg"])
    assert len(launches(desktop)) == 1


def test_browser_matches_revalidated_when_titles_change_or_become_duplicates(desktop):
    first = {"terminal": desktop["terminal"], "a": window("a", AI, 2),
             "b": window("b", "Zen Browser", 3)}
    later = copy.deepcopy(first)
    later["a"]["title"] = NEWS
    later["b"]["title"] = NEWS
    desktop["events"] = [first, later]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert [status for _, status in report if status != "EXTRA"] == ["AMBIGUOUS", "AMBIGUOUS"]


def test_native_uniqueness_includes_previously_matched_windows():
    e = entry(NEWS, 2)
    a, b = window("a", NEWS, 1), window("b", NEWS, 2)
    assert restore.assign_entries(
        {"b": b}, [(e, e.cmd)], {}, strict_titles=True, identity_live={"a": a, "b": b},
    ) == {}


def test_launch_override_is_used_for_both_grouping_and_identity(desktop):
    for e in desktop["entries"]:
        e.cmd += " --new-window https://example.com"
        desktop["cfg"].launch_by_title[e.title] = "/opt/zen-browser-bin/zen-bin"
    desktop["events"] = [{"terminal": desktop["terminal"],
                           "a": window("a", "Zen Browser", 2),
                           "b": window("b", "Zen Browser", 3)}]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert len(launches(desktop)) == 1
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2


def test_transient_poll_failure_retried_within_deadline(desktop, monkeypatch):
    original = restore.snapshot_windows
    calls = 0

    def snapshot():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("temporary IPC failure")
        return original()

    reads = [([], [], [])]
    monkeypatch.setattr(restore.select, "select", lambda *args: reads.pop() if reads else ([desktop["proc"].stdout], [], []))
    monkeypatch.setattr(restore, "snapshot_windows", snapshot)
    desktop["events"] = [{"terminal": desktop["terminal"],
                           "a": window("a", AI, 2), "b": window("b", NEWS, 3)}]
    report = restore.restore_session(cfg=desktop["cfg"])
    assert calls >= 3
    assert sum(status == "RESTORED" for _, status in report) == 2


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def test_open_subscription_with_generic_titles_stops_on_idle_timeout(desktop, monkeypatch, caplog):
    clock = _Clock()
    desktop["cfg"].load_time = 60
    desktop["cfg"].window_wait_timeout = 2
    desktop["events"] = [{"terminal": desktop["terminal"],
                           "a": window("a", "Zen Browser", 2),
                           "b": window("b", "Zen Browser", 3)}]
    first = True

    def select_open_pipe(readers, writers, errors, timeout):
        nonlocal first
        clock.now += timeout
        if first:
            first = False
            return ([desktop["proc"].stdout], [], [])
        return ([], [], [])

    monkeypatch.setattr(restore.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(restore.select, "select", select_open_pipe)
    report = restore.restore_session(cfg=desktop["cfg"])
    assert clock.now == pytest.approx(2.5)
    assert desktop["proc"].stdout.readline.call_count == 1  # No EOF shortcut.
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2
    assert "no window/title progress for 2s" in caplog.text
    desktop["proc"].terminate.assert_called_once()
    desktop["proc"].wait.assert_called_once_with(timeout=5)


def test_focus_geometry_event_noise_does_not_reset_idle_timeout(desktop, monkeypatch):
    clock = _Clock()
    desktop["cfg"].load_time = 60
    desktop["cfg"].window_wait_timeout = 2
    desktop["windows"].update({"a": window("a", "Zen Browser", 1),
                                "b": window("b", "Zen Browser", 2)})

    def select_noise(readers, writers, errors, timeout):
        clock.now += min(timeout, 0.25)
        return ([desktop["proc"].stdout], [], [])

    def readline_noise():
        desktop["windows"]["a"]["x"] = clock.now * 100
        desktop["windows"]["a"]["focused"] = not desktop["windows"]["a"].get("focused", False)
        return json.dumps({"event": "windows", "data": list(desktop["windows"].values())}) + "\n"

    desktop["proc"].stdout.readline.side_effect = readline_noise
    monkeypatch.setattr(restore.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(restore.select, "select", select_noise)
    report = restore.restore_session(cfg=desktop["cfg"])
    assert clock.now == pytest.approx(2)
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2
    assert launches(desktop) == []


def test_missing_windows_stop_on_idle_timeout_without_eof(desktop, monkeypatch):
    clock = _Clock()
    desktop["cfg"].load_time = 60
    desktop["cfg"].window_wait_timeout = 2

    def select_idle(readers, writers, errors, timeout):
        clock.now += timeout
        return ([], [], [])

    monkeypatch.setattr(restore.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(restore.select, "select", select_idle)
    report = restore.restore_session(cfg=desktop["cfg"])
    assert clock.now == pytest.approx(2)
    assert sum(status == "TIMEOUT" for _, status in report) == 2
    desktop["proc"].stdout.readline.assert_not_called()


def test_title_progress_extends_idle_wait_but_not_load_time(desktop, monkeypatch):
    clock = _Clock()
    desktop["cfg"].load_time = 4
    desktop["cfg"].window_wait_timeout = 1

    def select_progress(readers, writers, errors, timeout):
        clock.now += min(timeout, 0.5)
        return ([desktop["proc"].stdout], [], [])

    def readline_progress():
        desktop["windows"]["a"] = window("a", f"Loading {clock.now}", 2)
        return json.dumps({"event": "windows", "data": list(desktop["windows"].values())}) + "\n"

    desktop["proc"].stdout.readline.side_effect = readline_progress
    monkeypatch.setattr(restore.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(restore.select, "select", select_progress)
    restore.restore_session(cfg=desktop["cfg"])
    assert clock.now == pytest.approx(4)


def test_delayed_titles_can_match_before_idle_timeout(desktop, monkeypatch):
    clock = _Clock()
    desktop["cfg"].load_time = 60
    desktop["cfg"].window_wait_timeout = 1
    emitted = 0

    def select_delayed(readers, writers, errors, timeout):
        clock.now += min(timeout, 0.5)
        ready = emitted == 0 or (emitted == 1 and clock.now >= 1.5)
        return ([desktop["proc"].stdout] if ready else [], [], [])

    def readline_delayed():
        nonlocal emitted
        emitted += 1
        desktop["windows"].update({
            "a": window("a", "Zen Browser" if emitted == 1 else AI, 2),
            "b": window("b", "Zen Browser" if emitted == 1 else NEWS, 3),
        })
        return json.dumps({"event": "windows", "data": list(desktop["windows"].values())}) + "\n"

    desktop["proc"].stdout.readline.side_effect = readline_delayed
    monkeypatch.setattr(restore.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(restore.select, "select", select_delayed)
    report = restore.restore_session(cfg=desktop["cfg"])
    assert clock.now == pytest.approx(3)
    assert sum(status == "RESTORED" for _, status in report) == 2


def test_keyboard_interrupt_terminates_subscription(desktop, monkeypatch):
    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(restore.select, "select", interrupt)
    with pytest.raises(KeyboardInterrupt):
        restore.restore_session(cfg=desktop["cfg"])
    desktop["proc"].terminate.assert_called_once()
    desktop["proc"].wait.assert_called_once_with(timeout=5)


def test_send_failure_is_not_reported_as_restored(desktop, monkeypatch):
    desktop["windows"]["a"] = window("a", AI, 2)
    desktop["windows"]["b"] = window("b", NEWS, 1)
    monkeypatch.setattr(restore, "_send_many", lambda actions: False)
    report = restore.restore_session(adjust_clients_only=True, cfg=desktop["cfg"])
    assert [status for _, status in report if status != "EXTRA"] == ["FAILED", "FAILED"]
