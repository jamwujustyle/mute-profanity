"""Portable behavior checks; native Windows/Tk rendering needs Windows QA."""

from dataclasses import replace
import os
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mute_profanity import config, windows_ui


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self, *_args):
        return self.value

    def set(self, value):
        self.value = value


class WindowsPreferencesBehavior(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mute-windows-ui-")
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.window = windows_ui.Preferences.__new__(windows_ui.Preferences)
        self.window.flags = {field: Value(getattr(config.Settings(), field)) for field in
                             ("enabled", "instant_trigger", "escalate", "beep", "filter_ru", "filter_en")}
        self.window.editors = {"allow_words": Value("assembly\nAssembly"), "extra_words": Value("myword")}
        self.window.cooldown = Value("8")
        self.window.message = Value()
        self.window.status = Value()
        self.window.root = Mock()
        self.window.start_button = Mock()
        self.window.stop_button = Mock()
        self.window.closed = False
        self.window.process = None

    def test_save_preserves_pause_and_roundtrips_immediate_mode(self):
        pause = time.time() + 200
        config.save(replace(config.Settings(), paused_until=pause))
        self.window.flags["instant_trigger"].set(False)
        self.assertTrue(self.window._save())
        settings = config.load()
        self.assertFalse(settings.instant_trigger)
        self.assertEqual(settings.paused_until, pause)
        self.assertEqual(settings.allow_words, ["assembly"])
        self.assertEqual(settings.extra_words, ["myword"])
        self.window.flags["instant_trigger"].set(True)
        self.assertTrue(self.window._save())
        self.assertTrue(config.load().instant_trigger)

    def test_invalid_duration_does_not_overwrite_saved_settings(self):
        config.save(config.Settings(cooldown_seconds=12))
        self.window.cooldown.set("3.5")
        self.assertFalse(self.window._save())
        self.assertEqual(config.load().cooldown_seconds, 12)
        self.assertIn("whole number", self.window.message.get())

    def test_save_notifies_the_running_backend_to_reload(self):
        with patch("mute_profanity.windows.refresh", create=True, return_value=True) as refresh:
            self.assertTrue(self.window._save())
        refresh.assert_called_once()

    def test_running_process_is_distinguished_from_enabled_setting(self):
        with patch("mute_profanity.windows.is_ready", return_value=False):
            self.window._refresh_status()
        self.assertIn("not running", self.window.status.get())
        with patch("mute_profanity.windows.is_ready", return_value=True):
            self.window._refresh_status()
        self.assertIn("can close", self.window.status.get())
        config.save(config.Settings(enabled=False))
        with patch("mute_profanity.windows.is_ready", return_value=True):
            self.window._refresh_status()
        self.assertIn("filtering is disabled", self.window.status.get())

    def test_start_uses_packaged_command_builder_and_reports_failure(self):
        process = Mock(returncode=7)
        process.poll.return_value = 7
        process.communicate.return_value = ("", "Background startup failed.")
        with patch("mute_profanity.launch.command", return_value=["Mute.exe", "enable"]) as command:
            with patch.object(windows_ui.subprocess, "Popen", return_value=process) as spawn:
                self.window._start()
        command.assert_called_once_with("enable")
        self.assertEqual(spawn.call_args.args[0], ["Mute.exe", "enable"])
        self.assertTrue(config.load().enabled)
        self.window._poll_command()
        self.assertEqual(self.window.message.get(), "Background startup failed.")
        self.assertIsNone(self.window.process)

    def test_word_lists_reject_phrases_and_normalize_duplicates(self):
        self.assertEqual(windows_ui.word_lines("Example\nexample\n\n"), ["example"])
        with self.assertRaises(ValueError):
            windows_ui.word_lines("two words")


class WindowsCooldownBehavior(unittest.TestCase):
    def setUp(self):
        self.window = windows_ui.Intervention.__new__(windows_ui.Intervention)
        self.window.root = Mock()
        self.window.closed = False
        self.window.deadline = 110
        self.window.duration = 10
        self.window.countdowns = [Value()]
        self.window.progress = [Mock()]
        self.settings = config.Settings(beep=False)
        self.load = patch.object(config, "load", side_effect=lambda: self.settings)
        self.load.start()
        self.addCleanup(self.load.stop)

    def test_english_countdown_and_automatic_resume(self):
        with patch.object(windows_ui.time, "monotonic", return_value=103):
            self.window._tick()
        self.assertEqual(self.window.countdowns[0].get(), "Try again in 7 seconds.")
        self.window.root.destroy.assert_not_called()
        with patch.object(windows_ui.time, "monotonic", return_value=110):
            self.window._tick()
        self.assertTrue(self.window.closed)
        self.window.root.destroy.assert_called_once()

    def test_escape_does_not_dismiss_and_emergency_pauses(self):
        self.window._key_press(SimpleNamespace(keysym="Escape", state=0))
        self.assertFalse(self.window.closed)
        with patch.object(config, "save") as save:
            self.window._key_press(SimpleNamespace(keysym="F12", state=0x0005))
        self.assertGreater(save.call_args.args[0].paused_until, time.time() + 290)
        self.assertTrue(self.window.closed)
        self.window.root.destroy.assert_called_once()

    def test_backend_pause_dismisses_screen(self):
        self.settings = replace(self.settings, paused_until=time.time() + 300)
        with patch.object(windows_ui.time, "monotonic", return_value=103):
            self.window._tick()
        self.assertTrue(self.window.closed)
        self.window.root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
