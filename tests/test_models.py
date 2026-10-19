"""Tests for data models."""

import json
import tempfile
import unittest
from pathlib import Path

from umbriel_session.models import (
    Session,
    WindowEntry,
    WorkspaceEntry,
    WorkspaceRef,
    workspace_ref_from_id,
)


class TestWorkspaceRef(unittest.TestCase):
    def test_from_id_valid(self):
        ref = workspace_ref_from_id("HDMI-A-1:3")
        self.assertEqual(ref.output, "HDMI-A-1")
        self.assertEqual(ref.index, 3)
        self.assertFalse(ref.named)

    def test_from_id_invalid(self):
        self.assertIsNone(workspace_ref_from_id(""))
        self.assertIsNone(workspace_ref_from_id("foo"))
        self.assertIsNone(workspace_ref_from_id("HDMI-A-1:abc"))

    def test_roundtrip(self):
        ref = WorkspaceRef(output="eDP-1", index=2, named=True, name="CHAT")
        d = {"output": ref.output, "index": ref.index, "named": ref.named, "name": ref.name}
        ref2 = WorkspaceRef(**d)
        self.assertEqual(ref, ref2)


class TestWindowEntry(unittest.TestCase):
    def _make(self):
        return WindowEntry(
            app_id="kitty",
            title="test",
            cmd="kitty",
            cwd="/tmp",
            pid=123,
            workspace=WorkspaceRef(output="HDMI-A-1", index=1, named=False, name="1"),
            output="HDMI-A-1",
            floating=False,
            x=10, y=20, w=800, h=600,
            fullscreen=False,
            maximized=True,
            maximized_to_edges=False,
            pinned=False,
            focused=True,
            focus_order=0,
            content_type="none",
            xdg_tag="",
            xwayland=False,
            tabbed=False,
            scratchpad="",
        )

    def test_to_from_dict_roundtrip(self):
        w = self._make()
        d = w.to_dict()
        w2 = WindowEntry.from_dict(d)
        self.assertEqual(w, w2)

    def test_json_serializable(self):
        w = self._make()
        text = json.dumps(w.to_dict())
        w2 = WindowEntry.from_dict(json.loads(text))
        self.assertEqual(w, w2)


class TestSession(unittest.TestCase):
    def test_roundtrip(self):
        w = WindowEntry(
            app_id="zen", title="Zen", cmd="zen", cwd=None, pid=1,
            workspace=WorkspaceRef(output="eDP-1", index=1, named=False, name="1"),
            output="eDP-1", floating=False, x=0, y=0, w=100, h=100,
            fullscreen=False, maximized=False, maximized_to_edges=False,
            pinned=False, focused=False, focus_order=1,
            content_type="none", xdg_tag="", xwayland=False,
            tabbed=False, scratchpad="",
        )
        ws = WorkspaceEntry(
            id="eDP-1:1", name="1", named=False, index=1,
            output="eDP-1", layout="scrolling", layout_override=False,
        )
        session = Session(
            version=1, saved_at=123.45, compositor="umbriel",
            workspaces=[ws], windows=[w],
        )
        d = session.to_dict()
        session2 = Session.from_dict(d)
        self.assertEqual(session, session2)


if __name__ == "__main__":
    unittest.main()
