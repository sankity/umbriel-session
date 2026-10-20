"""Tests for app detection mapping."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umbriel_session import mapping


class TestParseDesktop(unittest.TestCase):
    def test_parse(self):
        content = (
            "[Desktop Entry]\n"
            "Name=Test Application\n"
            "Exec=test-app --flag %f\n"
            "Type=Application\n"
        )
        result = mapping._parse_desktop(content)
        self.assertEqual(result, ("test application", "test-app --flag", ""))

    def test_no_exec(self):
        content = "[Desktop Entry]\nName=Foo\n"
        self.assertIsNone(mapping._parse_desktop(content))

    def test_env_prefix(self):
        content = "[Desktop Entry]\nName=Bar\nExec=env FOO=1 bar %U\n"
        result = mapping._parse_desktop(content)
        self.assertEqual(result[1], "bar")


class TestWhich(unittest.TestCase):
    def test_which_ls(self):
        self.assertTrue(mapping._which("ls"))

    def test_which_nonexistent(self):
        self.assertFalse(mapping._which("definitely_not_a_real_binary_xyz"))


class TestResolveCommand(unittest.TestCase):
    def test_launch_map_override(self):
        cmd = mapping.resolve_command(
            "zen", 0, {"zen": "flatpak run org.zen"}, {}
        )
        self.assertEqual(cmd, "flatpak run org.zen")

    def test_raw_app_id_in_path(self):
        cmd = mapping.resolve_command("ls", 0, {}, {})
        self.assertEqual(cmd, "ls")

    def test_desktop_entry_wins_over_proc(self):
        # An Electron app's /proc cmdline is the raw binary plus the path it
        # was opened with, which is not a usable way to start it again.
        cmd = mapping.resolve_command(
            "com.microsoft.VSCode",
            0,
            {},
            {"com.microsoft.vscode": "code"},
        )
        self.assertEqual(cmd, "code")

    def test_falls_back_to_proc_cmdline(self):
        with patch.object(mapping, "_cmdline_command", return_value="ls --color"), \
             patch.object(mapping, "_exe_command", return_value="ls"), \
             patch.object(mapping, "_absolutize_args", side_effect=lambda c, p: c):
            cmd = mapping.resolve_command("ls", 1, {}, {})
        self.assertEqual(cmd, "ls --color")

    def test_no_match_returns_none(self):
        with patch.object(mapping, "_cmdline_command", return_value=None), \
             patch.object(mapping, "_exe_command", return_value=None):
            cmd = mapping.resolve_command("nope_xyz", 1, {}, {})
        self.assertIsNone(cmd)


class TestStripEnv(unittest.TestCase):
    def test_strips_env_and_assignments(self):
        self.assertEqual(
            mapping._strip_env("env FOO=1 BAR=2 myapp"), "myapp"
        )

    def test_keeps_plain_command(self):
        self.assertEqual(mapping._strip_env("myapp --flag"), "myapp --flag")


class TestAbsolutizeArgs(unittest.TestCase):
    def test_relative_path_becomes_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sub", "dir"))
            with patch.object(
                mapping, "_process_cwd", return_value=tmp
            ):
                cmd = mapping._absolutize_args("app sub/dir", 1)
        self.assertEqual(cmd, f"app {os.path.join(tmp, 'sub/dir')}")

    def test_flags_untouched(self):
        cmd = mapping._absolutize_args("app --flag", 1)
        self.assertEqual(cmd, "app --flag")

    def test_absolute_path_untouched(self):
        cmd = mapping._absolutize_args("app /abs/path", 1)
        self.assertEqual(cmd, "app /abs/path")

    def test_missing_relative_path_kept(self):
        cmd = mapping._absolutize_args("app nope/missing", 1)
        self.assertEqual(cmd, "app nope/missing")


if __name__ == "__main__":
    unittest.main()
