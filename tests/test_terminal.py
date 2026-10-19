"""Tests for terminal CWD helpers."""

import unittest

from umbriel_session.terminal import build_cwd_cmd


class TestBuildCwdCmd(unittest.TestCase):
    def test_separate(self):
        self.assertEqual(
            build_cwd_cmd("kitty", "/tmp", "separate", "--directory"),
            "kitty --directory /tmp",
        )

    def test_equals(self):
        self.assertEqual(
            build_cwd_cmd("foot", "/tmp", "equals", "--working-directory"),
            "foot --working-directory=/tmp",
        )

    def test_subcommand(self):
        self.assertEqual(
            build_cwd_cmd("wezterm", "/tmp", "subcommand", "start --cwd"),
            "wezterm start --cwd /tmp",
        )

    def test_no_cwd(self):
        self.assertEqual(
            build_cwd_cmd("kitty", None, "separate", "--directory"),
            "kitty",
        )

    def test_bad_dir(self):
        self.assertEqual(
            build_cwd_cmd("kitty", "/nonexistent/path/xyz", "separate", "--directory"),
            "kitty",
        )


if __name__ == "__main__":
    unittest.main()
