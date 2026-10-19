"""Tests for restore helpers (pure logic, no compositor)."""

import unittest
from unittest.mock import patch

from umbriel_session.models import WindowEntry, WorkspaceRef
from umbriel_session.restore import _match_window, workspace_selector


def _entry(app_id, title="", floating=False, fullscreen=False,
           maximized=False, pinned=False):
    return WindowEntry(
        app_id=app_id, title=title, cmd=app_id, cwd=None, pid=1,
        workspace=WorkspaceRef(output="HDMI-A-1", index=1, named=False, name="1"),
        output="HDMI-A-1", floating=floating, x=0, y=0, w=100, h=100,
        fullscreen=fullscreen, maximized=maximized, maximized_to_edges=False,
        pinned=pinned, focused=False, focus_order=0,
        content_type="none", xdg_tag="", xwayland=False,
        tabbed=False, scratchpad="",
    )


class TestWorkspaceSelector(unittest.TestCase):
    def test_anonymous(self):
        e = _entry("kitty")
        self.assertEqual(workspace_selector(e), "1/HDMI-A-1")

    def test_named(self):
        e = _entry("kitty")
        e.workspace.named = True
        e.workspace.name = "CHAT"
        self.assertEqual(workspace_selector(e), "CHAT/HDMI-A-1")

    def test_named_no_output(self):
        e = _entry("kitty")
        e.workspace.named = True
        e.workspace.name = "CHAT"
        e.workspace.output = ""
        self.assertEqual(workspace_selector(e), '"CHAT"')


class TestMatchWindow(unittest.TestCase):
    def test_match_by_app_id(self):
        pending = [(_entry("zen", "Zen Browser"), "zen")]
        result = _match_window("w1", {"w1": {"app_id": "zen", "title": "X"}}, pending)
        self.assertIsNotNone(result)
        self.assertEqual(result[0].app_id, "zen")

    def test_fallback_by_title(self):
        pending = [(_entry("foo", "My Title"), "foo")]
        result = _match_window("w1", {"w1": {"app_id": "bar", "title": "My Title"}}, pending)
        self.assertIsNotNone(result)

    def test_no_match(self):
        pending = [(_entry("foo", "Foo"), "foo")]
        result = _match_window("w1", {"w1": {"app_id": "bar", "title": "Bar"}}, pending)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
