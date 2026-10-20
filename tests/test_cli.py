"""Tests for CLI commands (mocked compositor)."""

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umbriel_session import cli, config, session


class TestCliSave(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        session.DATA_DIR = Path(self._tmpdir) / "data"
        session.BACKUPS_DIR = session.DATA_DIR / "backups"
        config.CONFIG_FILE = Path(self._tmpdir) / "config.toml"

    def tearDown(self):
        session.DATA_DIR = Path.home() / ".local" / "share" / "umbriel-session"

    @patch("umbriel_session.cli._query_windows")
    @patch("umbriel_session.cli._query_workspaces")
    @patch("umbriel_session.cli.umbriel_alive", return_value=True)
    def test_save_writes_session(self, _alive, mock_ws, mock_win):
        mock_win.return_value = [
            {
                "id": "w1", "app_id": "kitty", "title": "term", "pid": 1,
                "workspace": "HDMI-A-1:1", "output": "HDMI-A-1",
                "floating": False, "x": 0, "y": 0, "w": 100, "h": 100,
                "fullscreen": False, "maximized": False,
                "maximized_to_edges": False, "pinned": False,
                "focused": True, "content_type": "none",
                "xdg_tag": "", "xwayland": False, "tabbed": False,
                "scratchpad": "",
            }
        ]
        mock_ws.return_value = [
            {
                "id": "HDMI-A-1:1", "name": "1", "named": False,
                "index": 1, "output": "HDMI-A-1", "active": True,
                "focused": True, "occupied": True, "window_count": 1,
                "focused_window": "w1", "urgent": False,
                "layout": "scrolling", "layout_override": False,
            }
        ]

        # Patch resolve_command to return the app_id
        with patch("umbriel_session.cli.resolve_command", return_value="kitty"):
            import argparse
            args = argparse.Namespace(profile=None, force_empty=False)
            cli.cmd_save(args)

        loaded = session.load_session()
        self.assertEqual(len(loaded.windows), 1)
        self.assertEqual(loaded.windows[0].app_id, "kitty")


class TestCliRestore(unittest.TestCase):
    def test_ctrl_c_exits_cleanly_with_status_130(self):
        args = argparse.Namespace(
            dry_run=False, profile=None, workspace=None, exclude=None,
            adjust_clients_only=False,
        )
        with (
            patch.object(cli, "load_config", return_value=config.Config()),
            patch.object(cli, "_require_compositor"),
            patch.object(cli, "restore_session", side_effect=KeyboardInterrupt),
            patch.object(cli.console, "print") as output,
            self.assertRaises(SystemExit) as exc,
        ):
            cli.cmd_restore(args)
        self.assertEqual(exc.exception.code, 130)
        output.assert_called_once_with("[yellow]Restore cancelled.[/yellow]")


class TestCliStatus(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        session.DATA_DIR = Path(self._tmpdir) / "data"
        session.BACKUPS_DIR = session.DATA_DIR / "backups"
        session.DATA_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        session.DATA_DIR = Path.home() / ".local" / "share" / "umbriel-session"

    def test_status_no_session(self):
        import argparse
        args = argparse.Namespace(profile=None)
        with self.assertRaises(SystemExit):
            cli.cmd_status(args)


if __name__ == "__main__":
    unittest.main()
