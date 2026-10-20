"""Tests for restore helpers (pure logic, no compositor)."""

import unittest

from umbriel_session.models import WindowEntry, WorkspaceRef
from umbriel_session.config import Config
from umbriel_session.restore import (
    _default_windows,
    _spawn_cmd,
    assign_entries,
    resolve_target_workspace,
    workspace_selector,
)


def _entry(app_id, title="", index=1, output="HDMI-A-1", named=False, name=""):
    return WindowEntry(
        app_id=app_id, title=title, cmd=app_id, cwd=None, pid=1,
        workspace=WorkspaceRef(
            output=output, index=index, named=named, name=name, id=f"{output}:{index}"
        ),
        output=output, floating=False, x=0, y=0, w=100, h=100,
        fullscreen=False, maximized=False, maximized_to_edges=False,
        pinned=False, focused=False, focus_order=0,
        content_type="none", xdg_tag="", xwayland=False,
        tabbed=False, scratchpad="",
    )


class TestWorkspaceSelector(unittest.TestCase):
    def test_anonymous(self):
        self.assertEqual(
            workspace_selector({"output": "HDMI-A-1", "index": 1, "named": False}),
            "1/HDMI-A-1",
        )

    def test_named(self):
        self.assertEqual(
            workspace_selector(
                {"output": "HDMI-A-1", "index": 2, "named": True, "name": "CHAT"}
            ),
            '"CHAT"/HDMI-A-1',
        )

    def test_named_no_output(self):
        self.assertEqual(
            workspace_selector({"output": "", "index": 2, "named": True, "name": "CHAT"}),
            '"CHAT"',
        )


class TestResolveTargetWorkspace(unittest.TestCase):
    def setUp(self):
        # The serial in the id differs from the index: the index is what
        # Umbriel's own listing reports.
        self.ws_map = {
            "eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1, "named": False, "name": "1"},
            "eDP-1:4": {"id": "eDP-1:4", "output": "eDP-1", "index": 2, "named": False, "name": "2"},
            "eDP-1:6": {"id": "eDP-1:6", "output": "eDP-1", "index": 3, "named": False, "name": "3"},
        }

    def test_resolves_by_index_not_serial(self):
        entry = _entry("zen", index=2, output="eDP-1")
        target = resolve_target_workspace(entry, self.ws_map)
        self.assertEqual(target["id"], "eDP-1:4")
        self.assertEqual(target["index"], 2)

    def test_falls_back_to_last_workspace(self):
        entry = _entry("zen", index=9, output="eDP-1")
        target = resolve_target_workspace(entry, self.ws_map)
        self.assertEqual(target["index"], 3)

    def test_no_workspace_on_output(self):
        entry = _entry("zen", index=1, output="HDMI-A-1")
        self.assertIsNone(resolve_target_workspace(entry, self.ws_map))

    def test_named_matches_by_name(self):
        ws_map = {
            "eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1, "named": True, "name": "CHAT"},
        }
        entry = _entry("zen", index=4, output="eDP-1", named=True, name="CHAT")
        target = resolve_target_workspace(entry, ws_map)
        self.assertEqual(target["name"], "CHAT")


class TestAssignEntries(unittest.TestCase):
    def setUp(self):
        self.ws_map = {
            "eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1, "named": False, "name": "1"},
            "eDP-1:2": {"id": "eDP-1:2", "output": "eDP-1", "index": 2, "named": False, "name": "2"},
        }

    def _assign(self, live, entries):
        pending = [(e, e.cmd) for e in entries]
        return assign_entries(live, pending, self.ws_map)

    def test_match_by_title(self):
        entries = [_entry("zen", "Zen A"), _entry("zen", "Zen B")]
        live = {"w1": {"app_id": "zen", "title": "Zen B", "workspace": "eDP-1:1"}}
        assignment = self._assign(live, entries)
        self.assertEqual(assignment["w1"].title, "Zen B")

    def test_match_by_workspace_when_title_shared(self):
        entries = [
            _entry("zen", "Zen", index=1, output="eDP-1"),
            _entry("zen", "Zen", index=2, output="eDP-1"),
        ]
        live = {"w1": {"app_id": "zen", "title": "Zen", "workspace": "eDP-1:2"}}
        assignment = self._assign(live, entries)
        self.assertEqual(assignment["w1"].workspace.index, 2)

    def test_title_wins_over_workspace(self):
        # The window on workspace 2 owns the entry titled after it, even
        # though the other window sits on the entry's workspace.
        entries = [
            _entry("code", "kaenime", index=1, output="eDP-1"),
            _entry("code", "other", index=2, output="eDP-1"),
        ]
        live = {
            "w1": {"app_id": "code", "title": "other", "workspace": "eDP-1:2"},
            "w2": {"app_id": "code", "title": "kaenime", "workspace": "eDP-1:1"},
        }
        assignment = self._assign(live, entries)
        self.assertEqual(assignment["w1"].title, "other")
        self.assertEqual(assignment["w2"].title, "kaenime")

    def test_window_without_unique_title_does_not_steal(self):
        # An extra window of an app must not take an entry that a real
        # window of that app still needs.
        entries = [
            _entry("code", "kaenime", index=1, output="eDP-1"),
            _entry("code", "umbriel-session", index=1, output="eDP-1"),
        ]
        live = {
            "w1": {"app_id": "code", "title": "Visual Studio Code", "workspace": "eDP-1:1"},
            "w2": {"app_id": "code", "title": "kaenime", "workspace": "eDP-1:2"},
            "w3": {"app_id": "code", "title": "umbriel-session", "workspace": "eDP-1:1"},
        }
        assignment = self._assign(live, entries)
        self.assertNotIn("w1", assignment)
        self.assertEqual(assignment["w2"].title, "kaenime")
        self.assertEqual(assignment["w3"].title, "umbriel-session")

    def test_moved_window_is_recognised_by_app(self):
        # A window whose title changed but that is the only one of its app
        # still belongs to the saved entry.
        entries = [_entry("zen", "CachyOS", index=1, output="eDP-1")]
        live = {"w1": {"app_id": "zen", "title": "Zen Browser", "workspace": "eDP-1:2"}}
        assignment = self._assign(live, entries)
        self.assertEqual(assignment["w1"].title, "CachyOS")

    def test_extra_window_of_same_app_is_unmatched(self):
        entries = [_entry("zen", "Zen", index=1, output="eDP-1")]
        live = {
            "w1": {"app_id": "zen", "title": "Zen", "workspace": "eDP-1:1"},
            "w2": {"app_id": "zen", "title": "Zen", "workspace": "eDP-1:1"},
        }
        assignment = self._assign(live, entries)
        self.assertEqual(len(assignment), 1)

    def test_no_match(self):
        entries = [_entry("zen", "Zen A")]
        live = {"w1": {"app_id": "firefox", "title": "other", "workspace": "eDP-1:1"}}
        self.assertEqual(self._assign(live, entries), {})


class TestDefaultWindows(unittest.TestCase):
    def _live(self):
        return {
            "w1": {
                "app_id": "code",
                "title": "kaenime-api-go - Visual Studio Code",
                "workspace": "eDP-1:1",
            },
            "w2": {
                "app_id": "code",
                "title": "Visual Studio Code",
                "workspace": "eDP-1:1",
            },
        }

    def test_blank_window_is_default(self):
        defaults = _default_windows(self._live())
        self.assertEqual(defaults, {"w2"})

    def test_identical_titles_are_not_default(self):
        live = {
            "w1": {"app_id": "zen", "title": "Zen Browser", "workspace": "eDP-1:1"},
            "w2": {"app_id": "zen", "title": "Zen Browser", "workspace": "eDP-1:1"},
        }
        self.assertEqual(_default_windows(live), set())

    def test_default_window_does_not_take_titled_entry(self):
        # The blank window of an Electron app must not take the entry of a
        # titled window that is no longer running.
        entries = [
            _entry("code", "kaenime-api-go - Visual Studio Code", index=1, output="eDP-1"),
            _entry("code", "umbriel-session - Visual Studio Code", index=1, output="eDP-1"),
        ]
        live = {
            "w1": {
                "app_id": "code",
                "title": "kaenime-api-go - Visual Studio Code",
                "workspace": "eDP-1:1",
            },
            "w2": {
                "app_id": "code",
                "title": "Visual Studio Code",
                "workspace": "eDP-1:1",
            },
        }
        pending = [(e, e.cmd) for e in entries]
        ws_map = {"eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1}}
        assignment = assign_entries(live, pending, ws_map)
        self.assertEqual(assignment["w1"].title, "kaenime-api-go - Visual Studio Code")
        self.assertNotIn("w2", assignment)

    def test_default_window_takes_default_entry(self):
        entries = [_entry("code", "Visual Studio Code", index=1, output="eDP-1")]
        live = {
            "w1": {"app_id": "code", "title": "Visual Studio Code", "workspace": "eDP-1:1"},
        }
        pending = [(e, e.cmd) for e in entries]
        ws_map = {"eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1}}
        assignment = assign_entries(live, pending, ws_map)
        self.assertEqual(assignment["w1"].title, "Visual Studio Code")


class TestSpawnCmd(unittest.TestCase):
    def test_launch_by_title_wins(self):
        cfg = Config()
        cfg.launch_by_title = {"umbriel-session - Visual Studio Code": "code /path"}
        entry = _entry("code", "umbriel-session - Visual Studio Code")
        self.assertEqual(_spawn_cmd(entry, cfg), "code /path")

    def test_falls_back_to_entry_cmd(self):
        cfg = Config()
        entry = _entry("code", "whatever")
        entry.cmd = "code"
        self.assertEqual(_spawn_cmd(entry, cfg), "code")


if __name__ == "__main__":
    unittest.main()
