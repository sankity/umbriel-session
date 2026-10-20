"""Tests for config loading."""

import tempfile
import unittest
from pathlib import Path

from umbriel_session import config


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._orig_file = config.CONFIG_FILE
        self._tmpdir = tempfile.mkdtemp()
        config.CONFIG_FILE = Path(self._tmpdir) / "config.toml"
        config.CONFIG_DIR = Path(self._tmpdir)

    def tearDown(self):
        config.CONFIG_FILE = self._orig_file

    def test_defaults_when_no_file(self):
        cfg = config.load_config()
        self.assertTrue(cfg.restore_floating)
        self.assertTrue(cfg.restore_cwd)
        self.assertIn("video", cfg.ignore_content_types)

    def test_parse_general(self):
        config.CONFIG_FILE.write_text(
            "[general]\n"
            "restore_delay_seconds = 1.5\n"
            "window_wait_timeout = 5.0\n"
            "restore_floating = false\n"
            "restore_cwd = false\n"
            "restore_scratchpad = true\n"
        )
        cfg = config.load_config()
        self.assertEqual(cfg.restore_delay_seconds, 1.5)
        self.assertEqual(cfg.window_wait_timeout, 5.0)
        self.assertFalse(cfg.restore_floating)
        self.assertFalse(cfg.restore_cwd)
        self.assertTrue(cfg.restore_scratchpad)

    def test_parse_ignore(self):
        config.CONFIG_FILE.write_text(
            "[ignore]\n"
            'classes = ["foo", "bar"]\n'
            'content_types = ["video"]\n'
        )
        cfg = config.load_config()
        self.assertIn("foo", cfg.ignore_classes)
        self.assertIn("bar", cfg.ignore_classes)
        self.assertEqual(cfg.ignore_content_types, {"video"})

    def test_parse_launch(self):
        config.CONFIG_FILE.write_text(
            '[launch]\n'
            '"zen" = "flatpak run org.zen_browser.zen"\n'
        )
        cfg = config.load_config()
        self.assertEqual(cfg.launch["zen"], "flatpak run org.zen_browser.zen")

    def test_parse_additional_electron_apps(self):
        config.CONFIG_FILE.write_text('[electron]\napps = ["Custom.Chat", " widget ", ""]\n')
        cfg = config.load_config()
        self.assertEqual(cfg.electron_apps, {"custom.chat", "widget"})

    def test_invalid_electron_apps_do_not_become_character_ids(self):
        config.CONFIG_FILE.write_text('[electron]\napps = "discord"\n')
        self.assertEqual(config.load_config().electron_apps, set())

    def test_defaults_have_no_custom_electron_apps(self):
        self.assertEqual(config.load_config().electron_apps, set())

    def test_invalid_toml_falls_back(self):
        config.CONFIG_FILE.write_text("this is not [valid toml\n")
        cfg = config.load_config()
        # Should not raise; defaults remain
        self.assertTrue(cfg.restore_floating)


if __name__ == "__main__":
    unittest.main()
