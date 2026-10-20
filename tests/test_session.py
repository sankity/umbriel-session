"""Tests for session save/load/clear."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umbriel_session import session, models


class TestSessionPaths(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_data = session.DATA_DIR
        session.DATA_DIR = Path(self._tmpdir) / "data"
        session.BACKUPS_DIR = session.DATA_DIR / "backups"

    def tearDown(self):
        session.DATA_DIR = self._orig_data
        session.BACKUPS_DIR = session.DATA_DIR / "backups"

    def test_get_session_path_default(self):
        p = session.get_session_path()
        self.assertEqual(p.name, "session.json")

    def test_get_session_path_profile(self):
        p = session.get_session_path("work")
        self.assertEqual(p.name, "session-work.json")

    def test_save_load_roundtrip(self):
        session.DATA_DIR.mkdir(parents=True, exist_ok=True)
        w = models.WindowEntry(
            app_id="kitty", title="k", cmd="kitty", cwd=None, pid=1,
            workspace=models.WorkspaceRef(output="HDMI-A-1", index=1, named=False, name="1"),
            output="HDMI-A-1", floating=False, x=0, y=0, w=100, h=100,
            fullscreen=False, maximized=False, maximized_to_edges=False,
            pinned=False, focused=False, focus_order=0,
            content_type="none", xdg_tag="", xwayland=False,
            tabbed=False, scratchpad="",
        )
        ws = models.WorkspaceEntry(
            id="HDMI-A-1:1", name="1", named=False, index=1,
            output="HDMI-A-1", layout="scrolling", layout_override=False,
        )
        sess = models.Session(
            version=1, saved_at=1.0, compositor="umbriel",
            workspaces=[ws], windows=[w],
        )
        path = session.save_session(sess)
        self.assertTrue(path.exists())
        loaded = session.load_session()
        self.assertEqual(loaded.windows[0].app_id, "kitty")
        self.assertEqual(loaded.workspaces[0].output, "HDMI-A-1")

    def test_clear_session(self):
        session.DATA_DIR.mkdir(parents=True, exist_ok=True)
        session.save_session(models.Session(
            version=1, saved_at=1.0, compositor="umbriel",
            workspaces=[], windows=[],
        ))
        self.assertTrue(session.get_session_path().exists())
        self.assertTrue(session.clear_session())
        self.assertFalse(session.get_session_path().exists())

    def test_list_sessions(self):
        session.DATA_DIR.mkdir(parents=True, exist_ok=True)
        session.save_session(models.Session(
            version=1, saved_at=1.0, compositor="umbriel",
            workspaces=[], windows=[],
        ), profile="work")
        sessions = session.list_sessions()
        labels = [s[0] for s in sessions]
        self.assertIn("work", labels)


class TestParseWindows(unittest.TestCase):
    def test_uses_real_index_not_serial(self):
        # A window's workspace id carries a serial, not the index. The index
        # comes from the workspace listing.
        workspaces = [
            {"id": "eDP-1:1", "output": "eDP-1", "index": 1, "named": False, "name": "1"},
            {"id": "eDP-1:4", "output": "eDP-1", "index": 2, "named": False, "name": "2"},
        ]
        raw = [
            {
                "id": "w1", "app_id": "zen", "title": "Zen", "pid": 1,
                "workspace": "eDP-1:4", "output": "eDP-1",
                "floating": False, "x": 0, "y": 0, "w": 10, "h": 10,
                "fullscreen": False, "maximized": False,
                "maximized_to_edges": False, "pinned": False,
                "focused": True, "active": True, "content_type": "none",
                "xdg_tag": "", "xwayland": False, "tabbed": False,
                "scratchpad": "",
            }
        ]
        entries = session.parse_compositor_windows(raw, workspaces)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].workspace.index, 2)
        self.assertEqual(entries[0].workspace.id, "eDP-1:4")

    def test_named_workspace_keeps_name(self):
        workspaces = [
            {"id": "eDP-1:7", "output": "eDP-1", "index": 1, "named": True, "name": "CHAT"},
        ]
        raw = [
            {
                "id": "w1", "app_id": "zen", "title": "Zen", "pid": 1,
                "workspace": "eDP-1:7", "output": "eDP-1",
                "floating": False, "x": 0, "y": 0, "w": 10, "h": 10,
                "fullscreen": False, "maximized": False,
                "maximized_to_edges": False, "pinned": False,
                "focused": False, "active": False, "content_type": "none",
                "xdg_tag": "", "xwayland": False, "tabbed": False,
                "scratchpad": "",
            }
        ]
        entries = session.parse_compositor_windows(raw, workspaces)
        self.assertTrue(entries[0].workspace.named)
        self.assertEqual(entries[0].workspace.name, "CHAT")

    def test_window_on_unknown_workspace_is_skipped(self):
        raw = [
            {
                "id": "w1", "app_id": "zen", "title": "Zen", "pid": 1,
                "workspace": "eDP-1:9", "output": "eDP-1",
                "floating": False, "x": 0, "y": 0, "w": 10, "h": 10,
                "fullscreen": False, "maximized": False,
                "maximized_to_edges": False, "pinned": False,
                "focused": False, "active": False, "content_type": "none",
                "xdg_tag": "", "xwayland": False, "tabbed": False,
                "scratchpad": "",
            }
        ]
        self.assertEqual(session.parse_compositor_windows(raw, []), [])


if __name__ == "__main__":
    unittest.main()
