"""Preferences activation-state checks; run with ``tools/test-ui.sh``.

Desktop diagnostics and activation are replaced at their boundaries. The test
process never changes input sources or connects to the user's IBus daemon.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def activation_status(*, ready=False):
    return SimpleNamespace(
        code="source_active" if ready else "requires_load",
        message="Метод ввода ещё не загружен. Запустите фильтр кнопкой ниже.",
        sources=(("ibus", "mute-en"), ("ibus", "mute-ru")) if ready else (),
        global_engine="mute-en" if ready else None,
        engines_ready=ready,
        ready=ready,
    )


@unittest.skipUnless(os.environ.get("MUTE_GTK_TEST") == "1", "Run tools/test-ui.sh for isolated GTK checks")
class PreferencesActivation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mute_profanity import config, desktop, ui

        cls.config, cls.desktop, cls.ui = config, desktop, ui
        available, _ = ui.Gtk.init_check(None)
        if not available:
            raise unittest.SkipTest("No isolated graphical display is available")
        css = ui.Gtk.CssProvider()
        css.load_from_data(ui._CSS)
        ui.Gtk.StyleContext.add_provider_for_screen(ui.Gdk.Screen.get_default(), css,
                                                   ui.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        cls.context = ui.GLib.MainContext.default()
        cls.real_subprocess_new = ui.Gio.Subprocess.new

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mute-gtk-config-")
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary.name,
                                               "GSETTINGS_BACKEND": "memory"})
        environment.start()
        self.addCleanup(environment.stop)
        self.config.save(self.config.Settings(enabled=True, beep=False))
        self.status_mock = self.mock_boundary(self.desktop, "status", return_value=activation_status(), create=True)
        self.disable_mock = self.mock_boundary(self.desktop, "disable")
        self.popen_mock = self.mock_boundary(self.ui.subprocess, "Popen")
        self.spawn_mock = self.mock_boundary(self.ui.Gio.Subprocess, "new")
        self.quit_mock = self.mock_boundary(self.ui.Gtk, "main_quit")
        self.callback_errors = []
        previous_hook = sys.excepthook
        self.addCleanup(setattr, sys, "excepthook", previous_hook)
        sys.excepthook = lambda kind, error, trace: self.callback_errors.append(
            "".join(traceback.format_exception(kind, error, trace)))
        self.window = self.ui.Preferences()
        self.window.show_all()
        self.drain()

    def mock_boundary(self, target, name, **kwargs):
        operation = patch.object(target, name, **kwargs)
        result = operation.start()
        self.addCleanup(operation.stop)
        return result

    def tearDown(self):
        if self.window is not None:
            self.window.destroy()
            self.drain()
        self.assertEqual(self.callback_errors, [], "\n".join(self.callback_errors))
        self.disable_mock.assert_not_called()
        self.popen_mock.assert_not_called()

    def drain(self):
        while self.context.pending():
            self.context.iteration(False)

    def wait_for(self, condition, description):
        deadline = time.monotonic() + 3
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"Timed out waiting for {description}")
            self.drain()
            time.sleep(0.001)
        self.drain()

    def test_enabled_setting_requires_actual_activation(self):
        self.assertTrue(self.config.load().enabled)
        self.assertEqual(self.window.status.get_text(), self.status_mock.return_value.message)
        self.assertTrue(self.window.setup_button.get_visible())
        self.assertTrue(self.window.setup_button.get_sensitive())
        self.spawn_mock.assert_not_called()
        screenshot = os.environ.get("MUTE_GTK_SCREENSHOT")
        if screenshot:
            pixbuf = self.ui.Gdk.pixbuf_get_from_window(
                self.window.get_window(), 0, 0, self.window.get_allocated_width(),
                self.window.get_allocated_height())
            pixbuf.savev(str(Path(screenshot)), "png", [], [])

    def test_active_source_reports_background_operation(self):
        self.status_mock.return_value = activation_status(ready=True)
        self.window._refresh_status()
        self.assertIn("работает в фоне", self.window.status.get_text())
        self.assertIn("можно закрыть", self.window.status.get_text())
        self.assertFalse(self.window.setup_button.get_visible())
        self.spawn_mock.assert_not_called()

    def test_closing_preferences_does_not_disable_background_filter(self):
        self.status_mock.return_value = activation_status(ready=True)
        self.window._refresh_status()
        self.window.destroy()
        self.window = None
        self.drain()
        self.quit_mock.assert_called_once()
        self.spawn_mock.assert_not_called()
        self.disable_mock.assert_not_called()
        self.assertTrue(self.config.load().enabled)

    def test_activation_failure_is_reported_without_claiming_ready(self):
        failure = "Не удалось загрузить метод ввода (тест)."

        def local_failure(arguments, flags):
            self.assertEqual(arguments[-1], "enable")
            self.assertTrue(arguments[-2].endswith("run.py"))
            # Exercise Gio's real asynchronous completion, while replacing the
            # desktop-changing command with an inert local child process.
            return self.real_subprocess_new(
                ["/usr/bin/python3", "-c", "import sys; sys.stderr.write(sys.argv[1]); sys.exit(7)", failure], flags)

        self.spawn_mock.side_effect = local_failure
        self.window.setup_button.emit("clicked")
        self.assertTrue(self.window.setup_pending)
        self.assertFalse(self.window.setup_button.get_sensitive())
        self.wait_for(lambda: not self.window.setup_pending, "activation failure feedback")
        self.spawn_mock.assert_called_once()
        self.assertEqual(self.window.message.get_text(), failure)
        self.assertEqual(self.window.status.get_text(), self.status_mock.return_value.message)
        self.assertTrue(self.window.setup_button.get_visible())
        self.assertTrue(self.window.setup_button.get_sensitive())


if __name__ == "__main__":
    unittest.main()
