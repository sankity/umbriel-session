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


if __name__ == "__main__":
    unittest.main()
