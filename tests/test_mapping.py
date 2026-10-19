"""Tests for app detection mapping."""

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
        self.assertEqual(result, ("test application", "test-app --flag"))

    def test_no_exec(self):
        content = "[Desktop Entry]\nName=Foo\n"
        self.assertIsNone(mapping._parse_desktop(content))

    def test_env_prefix(self):
        # _parse_desktop doesn't strip env; just checks raw
        content = "[Desktop Entry]\nName=Bar\nExec=env FOO=1 bar %U\n"
        result = mapping._parse_desktop(content)
        self.assertEqual(result[1], "env FOO=1 bar")


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


if __name__ == "__main__":
    unittest.main()
