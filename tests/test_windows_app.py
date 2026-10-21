from contextlib import ExitStack
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from mute_profanity import windows, windows_app
from mute_profanity.config import Settings


class WindowsAppTests(unittest.TestCase):
    def setup_app(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name)
        (target / "run.py").touch()
        stack.enter_context(patch.object(windows_app, "application_dir", return_value=target))
        stack.enter_context(patch.object(windows_app.config, "load", return_value=Settings(paused_until=100)))
        save = stack.enter_context(patch.object(windows_app.config, "save"))
        startup = stack.enter_context(patch.object(windows_app, "startup"))
        stack.enter_context(patch.object(windows_app, "installed_command", return_value=["Mute.exe", "engine"]))
        stack.enter_context(patch.object(windows_app, "_installation_needs_update", return_value=False))
        stack.enter_context(patch.object(windows_app.time, "sleep"))
        return stack, save, startup

    def test_enable_launches_background_and_waits_for_hooks_not_just_mutex(self):
        stack, save, startup = self.setup_app()
        running = stack.enter_context(patch.object(windows, "is_running", return_value=False))
        ready = stack.enter_context(patch.object(windows, "is_ready", side_effect=[False, True, True]))
        refresh = stack.enter_context(patch.object(windows, "refresh", return_value=False))
        spawn = stack.enter_context(patch.object(windows_app.subprocess, "Popen"))
        stack.enter_context(patch.object(windows_app.subprocess, "DETACHED_PROCESS", 8, create=True))
        stack.enter_context(patch.object(windows_app.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True))
        self.assertEqual(windows_app.enable(), 0)
        self.assertGreaterEqual(ready.call_count, 2)
        self.assertEqual(running.call_count, 1)
        refresh.assert_called_once()
        startup.assert_called_once_with(True)
        self.assertTrue(save.call_args.args[0].enabled)
        self.assertEqual(save.call_args.args[0].paused_until, 0)
        self.assertEqual(spawn.call_args.args, (["Mute.exe", "engine"],))
        self.assertEqual(spawn.call_args.kwargs["creationflags"], 520)
        self.assertTrue(spawn.call_args.kwargs["close_fds"])

    def test_enable_existing_starting_process_waits_until_ready_without_duplicate(self):
        stack, _save, _startup = self.setup_app()
        stack.enter_context(patch.object(windows, "is_running", return_value=True))
        stack.enter_context(patch.object(windows, "is_ready", side_effect=[False, True, True]))
        stack.enter_context(patch.object(windows, "refresh", return_value=True))
        spawn = stack.enter_context(patch.object(windows_app.subprocess, "Popen"))
        self.assertEqual(windows_app.enable(), 0)
        spawn.assert_not_called()

    def test_enable_does_not_claim_success_for_live_mutex_with_no_ready_hooks(self):
        stack, _save, _startup = self.setup_app()
        stack.enter_context(patch.object(windows, "is_running", return_value=True))
        stack.enter_context(patch.object(windows, "is_ready", return_value=False))
        stack.enter_context(patch.object(windows, "refresh", return_value=True))
        stack.enter_context(patch.object(windows_app.time, "monotonic", side_effect=[0, 4]))
        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
            windows_app.enable()

    def test_disable_waits_for_process_exit_before_reporting_stopped(self):
        stack, save, startup = self.setup_app()
        stack.enter_context(patch.object(windows, "is_running", side_effect=[True, True, False, False]))
        stop = stack.enter_context(patch.object(windows, "stop", return_value=True))
        refresh = stack.enter_context(patch.object(windows, "refresh", return_value=True))
        self.assertEqual(windows_app.disable(), 0)
        self.assertFalse(save.call_args.args[0].enabled)
        self.assertEqual(save.call_args.args[0].paused_until, 100)
        startup.assert_called_once_with(False)
        stop.assert_called_once()
        refresh.assert_called_once()
        windows_app.time.sleep.assert_called_once_with(0.05)

    def test_disable_reports_unstopped_guard_but_keeps_disabled_config(self):
        stack, save, startup = self.setup_app()
        stack.enter_context(patch.object(windows, "is_running", return_value=True))
        stack.enter_context(patch.object(windows, "stop", return_value=True))
        stack.enter_context(patch.object(windows, "refresh", return_value=True))
        stack.enter_context(patch.object(windows_app.time, "monotonic", side_effect=[0, 4]))
        with self.assertRaisesRegex(RuntimeError, "has not stopped yet"):
            windows_app.disable()
        self.assertFalse(save.call_args.args[0].enabled)
        startup.assert_called_once_with(False)

    def test_disable_reports_missing_control_event_instead_of_false_success(self):
        stack, save, _startup = self.setup_app()
        stack.enter_context(patch.object(windows, "is_running", return_value=True))
        stack.enter_context(patch.object(windows, "stop", return_value=False))
        stack.enter_context(patch.object(windows, "refresh", return_value=False))
        with self.assertRaisesRegex(RuntimeError, "could not be asked to stop"):
            windows_app.disable()
        self.assertFalse(save.call_args.args[0].enabled)

    def test_disable_is_safe_when_no_process_is_running(self):
        stack, _save, _startup = self.setup_app()
        stack.enter_context(patch.object(windows, "is_running", return_value=False))
        stack.enter_context(patch.object(windows, "refresh", return_value=False))
        stop = stack.enter_context(patch.object(windows, "stop"))
        self.assertEqual(windows_app.disable(), 0)
        stop.assert_not_called()

    def test_console_doctor_prints_one_complete_report(self):
        stack, _save, _startup = self.setup_app()
        stack.enter_context(patch.object(windows, "is_running", return_value=True))
        stack.enter_context(patch.object(windows, "is_ready", return_value=True))
        stack.enter_context(patch.object(windows_app.sys, "stdout", object()))
        with patch("builtins.print") as output:
            self.assertEqual(windows_app.doctor(), 0)
        output.assert_called_once()
        self.assertIn("Background engine: running", output.call_args.args[0])
        self.assertIn("Keyboard and mouse hooks: ready", output.call_args.args[0])

    def test_windowed_doctor_shows_report_and_destroys_temporary_root(self):
        stack, _save, _startup = self.setup_app()
        stack.enter_context(patch.object(windows, "is_running", return_value=True))
        stack.enter_context(patch.object(windows, "is_ready", return_value=False))
        stack.enter_context(patch.object(windows_app.sys, "stdout", None))
        tkinter = types.ModuleType("tkinter")
        tkinter.Tk = make_root = Mock()
        messagebox = types.ModuleType("tkinter.messagebox")
        messagebox.showinfo = show = Mock()
        tkinter.messagebox = messagebox
        with patch.dict("sys.modules", {"tkinter": tkinter, "tkinter.messagebox": messagebox}):
            self.assertEqual(windows_app.doctor(), 1)
        make_root.return_value.withdraw.assert_called_once()
        make_root.return_value.destroy.assert_called_once()
        self.assertEqual(show.call_args.args[0], "Mute diagnostics")
        self.assertIn("Keyboard and mouse hooks: not ready", show.call_args.args[1])
        self.assertIs(show.call_args.kwargs["parent"], make_root.return_value)


class WindowsUpgradeTests(unittest.TestCase):
    def setup_upgrade(self, *, frozen=True, running=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source, target = root / "portable", root / "installed"
        source.mkdir()
        target.mkdir()
        wordlists = root / "wordlists"
        wordlists.mkdir()
        (wordlists / "custom.txt").write_text("preserved", encoding="utf-8")
        stack.enter_context(patch.object(windows_app, "application_dir", return_value=target))
        stack.enter_context(patch.object(windows_app.sys, "frozen", frozen, create=True))
        stack.enter_context(patch.object(windows_app.sys, "executable", str(source / "Mute.exe")))
        stack.enter_context(patch.object(windows_app, "__file__", str(source / "mute_profanity/windows_app.py")))
        stack.enter_context(patch.object(windows_app.config, "wordlist_dir", return_value=wordlists))
        stack.enter_context(patch.object(windows_app.config, "load", return_value=Settings(paused_until=100)))
        save = stack.enter_context(patch.object(windows_app.config, "save"))
        shortcut = stack.enter_context(patch.object(windows_app, "_shortcut"))
        startup = stack.enter_context(patch.object(windows_app, "startup"))
        stack.enter_context(patch.object(windows_app, "installed_command", return_value=["Mute.exe", "engine"]))
        stack.enter_context(patch.object(windows, "is_running", return_value=running))
        stack.enter_context(patch.object(windows, "is_ready", return_value=True))
        stack.enter_context(patch.object(windows, "refresh", return_value=True))
        spawn = stack.enter_context(patch.object(windows_app.subprocess, "Popen"))
        stop = stack.enter_context(patch.object(windows, "stop"))
        if frozen:
            (source / "Mute.exe").write_bytes(b"new executable")
            (target / "Mute.exe").write_bytes(b"old executable")
        else:
            for directory, version in ((source, "new"), (target, "old")):
                (directory / "mute_profanity").mkdir()
                (directory / "run.py").write_text(version + " entry", encoding="utf-8")
                (directory / "mute_profanity/windows.py").write_text(version + " module", encoding="utf-8")
        return stack, source, target, wordlists, save, shortcut, startup, spawn, stop

    def test_start_updates_an_existing_packaged_installation_when_stopped(self):
        _stack, source, target, wordlists, save, shortcut, startup, spawn, stop = self.setup_upgrade()
        self.assertEqual(windows_app.enable(), 0)
        self.assertEqual((target / "Mute.exe").read_bytes(), (source / "Mute.exe").read_bytes())
        self.assertFalse((target / "Mute.exe.update").exists())
        self.assertEqual((wordlists / "custom.txt").read_text(encoding="utf-8"), "preserved")
        self.assertTrue(save.call_args.args[0].enabled)
        shortcut.assert_called_once()
        startup.assert_called_once_with(True)
        spawn.assert_called_once()
        stop.assert_not_called()

    def test_differing_running_copy_requires_stop_before_any_settings_or_files_change(self):
        _stack, _source, target, _wordlists, save, shortcut, startup, spawn, stop = self.setup_upgrade(running=True)
        with self.assertRaisesRegex(RuntimeError, "Stop filtering first"):
            windows_app.enable()
        self.assertEqual((target / "Mute.exe").read_bytes(), b"old executable")
        self.assertFalse((target / "Mute.exe.update").exists())
        for action in (save, shortcut, startup, spawn, stop):
            action.assert_not_called()

    def test_identical_external_copy_can_start_without_overwriting_the_running_executable(self):
        stack, source, target, _wordlists, _save, shortcut, _startup, spawn, stop = self.setup_upgrade(running=True)
        (target / "Mute.exe").write_bytes((source / "Mute.exe").read_bytes())
        copy = stack.enter_context(patch.object(windows_app.shutil, "copy2"))
        self.assertEqual(windows_app.enable(), 0)
        copy.assert_not_called()
        shortcut.assert_not_called()
        spawn.assert_not_called()
        stop.assert_not_called()

    def test_start_from_the_installed_executable_does_not_compare_or_overwrite_itself(self):
        stack, _source, target, _wordlists, _save, _shortcut, _startup, spawn, _stop = self.setup_upgrade(running=True)
        stack.enter_context(patch.object(windows_app.sys, "executable", str(target / "Mute.exe")))
        compare = stack.enter_context(patch.object(windows_app.filecmp, "cmp"))
        copy = stack.enter_context(patch.object(windows_app.shutil, "copy2"))
        self.assertEqual(windows_app.enable(), 0)
        compare.assert_not_called()
        copy.assert_not_called()
        spawn.assert_not_called()

    def test_start_refreshes_existing_source_modules_and_preserves_wordlists(self):
        _stack, source, target, wordlists, _save, _shortcut, _startup, _spawn, stop = self.setup_upgrade(frozen=False)
        cache = source / "mute_profanity/__pycache__"
        cache.mkdir()
        (cache / "windows.pyc").write_bytes(b"ignored")
        self.assertEqual(windows_app.enable(), 0)
        self.assertEqual((target / "run.py").read_text(encoding="utf-8"), "new entry")
        self.assertEqual((target / "mute_profanity/windows.py").read_text(encoding="utf-8"), "new module")
        self.assertFalse((target / "mute_profanity/__pycache__").exists())
        self.assertEqual((wordlists / "custom.txt").read_text(encoding="utf-8"), "preserved")
        stop.assert_not_called()

    def test_direct_install_also_refuses_to_overwrite_a_running_older_source_installation(self):
        _stack, _source, target, _wordlists, _save, shortcut, _startup, _spawn, stop = self.setup_upgrade(frozen=False, running=True)
        with self.assertRaisesRegex(RuntimeError, "Stop filtering first"):
            windows_app.install()
        self.assertEqual((target / "mute_profanity/windows.py").read_text(encoding="utf-8"), "old module")
        shortcut.assert_not_called()
        stop.assert_not_called()

    def test_locked_executable_keeps_old_installation_and_cleans_staged_update(self):
        stack, _source, target, _wordlists, _save, shortcut, _startup, _spawn, _stop = self.setup_upgrade()
        stack.enter_context(patch.object(windows_app.os, "replace", side_effect=PermissionError("in use")))
        with self.assertRaisesRegex(RuntimeError, "Close any other installed Mute windows"):
            windows_app.install()
        self.assertEqual((target / "Mute.exe").read_bytes(), b"old executable")
        self.assertFalse((target / "Mute.exe.update").exists())
        shortcut.assert_not_called()


if __name__ == "__main__":
    unittest.main()
