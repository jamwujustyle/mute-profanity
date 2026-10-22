from contextlib import redirect_stdout
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from mute_profanity import cli, config, desktop, gnome_modal, install


class CommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        config.save(config.Settings(allow_words=["example"], paused_until=123))

    def test_off_disables_saved_checking_releases_popup_and_restores_native_sources(self):
        with patch.object(desktop, "main", return_value=0) as backend, \
             patch.object(gnome_modal, "try_hide") as hide, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.switch(False), 0)
        self.assertFalse(config.load().enabled)
        self.assertEqual(config.load().paused_until, 0)
        self.assertEqual(config.load().allow_words, ["example"])
        backend.assert_called_once_with("disable")
        hide.assert_called_once_with()
        self.assertEqual(output.getvalue(), "Profanity filter: off\n")

    def test_on_clears_pause_and_enables_saved_checking(self):
        config.save(replace(config.load(), enabled=False, paused_until=9999999999))
        with patch.object(desktop, "main", return_value=0) as backend, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.switch(True), 0)
        self.assertTrue(config.load().enabled)
        self.assertEqual(config.load().paused_until, 0)
        self.assertEqual(config.load().allow_words, ["example"])
        backend.assert_called_once_with("enable")
        self.assertEqual(output.getvalue(), "Profanity filter: on\n")

    def test_status_off_needs_no_desktop_connection(self):
        config.save(replace(config.load(), enabled=False))
        with patch.object(desktop, "status") as inspect, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.status(), 0)
        inspect.assert_not_called()
        self.assertEqual(output.getvalue(), "Profanity filter: off\n")

    def test_status_reports_actual_input_guard_not_just_saved_preference(self):
        for ready, expected in ((True, "on"), (False, "off (background input guard is not active)")):
            with self.subTest(ready=ready), patch.object(desktop, "status", return_value=
                    SimpleNamespace(code="source_active" if ready else "ordinary_source", ready=ready)), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.status(), 0)
                self.assertEqual(output.getvalue(), f"Profanity filter: {expected}\n")

    def test_entrypoint_supports_short_and_existing_aliases(self):
        spec = importlib.util.spec_from_file_location("mute_command_entry", Path(__file__).parents[1] / "run.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        for name, enabled in (("on", True), ("enable", True), ("off", False), ("disable", False)):
            with self.subTest(name=name), patch.object(cli, "switch", return_value=0) as switch:
                self.assertEqual(entry.main([name]), 0)
                switch.assert_called_once_with(enabled)
        with patch.object(cli, "status", return_value=0) as status:
            self.assertEqual(entry.main(["status"]), 0)
            status.assert_called_once_with()

    def test_installed_launcher_preserves_paths_arguments_and_defaults_to_status(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "an app's folder" / "run.py"
            entry.parent.mkdir()
            entry.write_text("import json,sys; print(json.dumps(sys.argv[1:]))\n")
            target = Path(directory) / "bin/profanity"
            install.install_cli(entry, target)
            for arguments, expected in (([], ["status"]), (["off"], ["off"]),
                                        (["a b", "$(literal)"], ["a b", "$(literal)"])):
                result = subprocess.run([str(target), *arguments], capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_installer_preserves_an_unrelated_existing_command(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "profanity"
            target.write_text("another command\n")
            with self.assertRaises(ValueError):
                install.install_cli(Path(directory) / "run.py", target)
            self.assertEqual(target.read_text(), "another command\n")


if __name__ == "__main__":
    unittest.main()
