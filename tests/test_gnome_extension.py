import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from gi.repository import GLib

from mute_profanity import gnome_modal


ASSETS = Path(__file__).resolve().parent.parent / "assets" / "gnome-extension"


class ShellSettings:
    def __init__(self, enabled=(), disabled=(), globally_disabled=True):
        self.lists = {"enabled-extensions": list(enabled), "disabled-extensions": list(disabled)}
        self.globally_disabled = globally_disabled
        self.writes = []

    def get_strv(self, key):
        return self.lists[key].copy()

    def set_strv(self, key, value):
        if key not in self.lists:
            raise AssertionError("Only extension UUID lists may be changed")
        self.writes.append(key)
        self.lists[key] = list(value)
        return True

    def set_boolean(self, *_args):
        raise AssertionError("The global user-extension switch must not change")


class GnomeExtensionTests(unittest.TestCase):
    def test_extension_metadata_and_dbus_contract_match_client(self):
        metadata = json.loads((ASSETS / "metadata.json").read_text())
        self.assertEqual(metadata["uuid"], gnome_modal.UUID)
        self.assertIn("46", metadata["shell-version"])
        source = (ASSETS / "extension.js").read_text()
        xml = re.search(r"const INTERFACE_XML = `(.+?)`;", source, re.S).group(1)
        root = ET.fromstring(xml)
        interface = root.find("interface")
        self.assertEqual(interface.get("name"), gnome_modal.INTERFACE)
        methods = {method.get("name"): [(arg.get("type"), arg.get("direction"))
                                        for arg in method.findall("arg")]
                   for method in interface.findall("method")}
        self.assertEqual(methods, {"Show": [("u", "in"), ("b", "out")], "Hide": []})

    @unittest.skipUnless(shutil.which("gjs"), "GJS parser is not installed")
    def test_extension_is_valid_gjs_es_module_syntax(self):
        # Parse with the installed SpiderMonkey/GJS engine without importing or
        # executing Shell modules. This verifies syntax, not a live modal grab.
        result = subprocess.run([
            "gjs", "-c",
            "const [, bytes] = imports.gi.GLib.file_get_contents(ARGV[0]); "
            "Reflect.parse(new TextDecoder().decode(bytes), {target: 'module'});",
            str(ASSETS / "extension.js"),
        ], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prepare_stages_complete_extension_without_enabling_host(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_DATA_HOME": temporary}):
            entry = Path(temporary) / "an app's directory" / "run.py"
            target = gnome_modal.prepare_extension(entry)
            self.assertEqual(target, Path(temporary) / "gnome-shell/extensions" / gnome_modal.UUID)
            self.assertEqual(json.loads((target / "bridge.json").read_text()),
                             {"entry_path": str(entry.resolve())})
            self.assertEqual((target / "metadata.json").read_bytes(), (ASSETS / "metadata.json").read_bytes())
            self.assertEqual((target / "extension.js").read_bytes(), (ASSETS / "extension.js").read_bytes())
            self.assertEqual((target / "bridge.json").stat().st_mode & 0o777, 0o600)

    def test_client_clamps_duration_and_uses_boolean_grab_result(self):
        with patch.object(gnome_modal, "_call", return_value=GLib.Variant("(b)", (True,))) as call:
            self.assertTrue(gnome_modal.try_show(80))
            self.assertEqual(call.call_args.args[0], "Show")
            self.assertEqual(call.call_args.args[1].unpack(), (60,))
            self.assertEqual(call.call_args.args[2], "(b)")
            self.assertTrue(gnome_modal.try_show(0.5))
            self.assertEqual(call.call_args.args[1].unpack(), (1,))
        with patch.object(gnome_modal, "_call", return_value=GLib.Variant("(b)", (False,))):
            self.assertFalse(gnome_modal.try_show(8))
        with patch.object(gnome_modal, "_call", return_value=None):
            self.assertFalse(gnome_modal.try_show(8))

    def test_invalid_duration_never_calls_shell(self):
        with patch.object(gnome_modal, "_call") as call:
            for value in (float("nan"), float("inf"), 10 ** 1000, True, "8", None):
                self.assertFalse(gnome_modal.try_show(value))
            call.assert_not_called()

    def test_transport_is_bounded_does_not_activate_shell_and_handles_unavailable_bus(self):
        from gi.repository import Gio

        connection = Mock()
        connection.call_sync.return_value = GLib.Variant("(b)", (True,))
        with patch.object(Gio, "bus_get_sync", return_value=connection):
            self.assertTrue(gnome_modal.try_show(8))
        arguments = connection.call_sync.call_args.args
        self.assertEqual(arguments[:4], (gnome_modal.BUS_NAME, gnome_modal.OBJECT_PATH,
                                         gnome_modal.INTERFACE, "Show"))
        self.assertEqual(arguments[6], Gio.DBusCallFlags.NO_AUTO_START)
        self.assertEqual(arguments[7], 400)
        with patch.object(Gio, "bus_get_sync", side_effect=GLib.Error("Unavailable")):
            self.assertFalse(gnome_modal.try_show(8))
            self.assertFalse(gnome_modal.try_hide())

    def test_hide_uses_no_payload(self):
        with patch.object(gnome_modal, "_call", return_value=GLib.Variant("()", ())) as call:
            self.assertTrue(gnome_modal.try_hide())
        call.assert_called_once_with("Hide", None, "()")

    def test_request_enable_preserves_other_uuid_lists_and_global_disable_preference(self):
        from gi.repository import Gio

        settings = ShellSettings(enabled=["first@example", "second@example"],
                                 disabled=["disabled-first@example", gnome_modal.UUID, "disabled-last@example"])
        connection = Mock()
        connection.call_sync.return_value = GLib.Variant("(b)", (True,))
        with patch.object(Gio.Settings, "new", return_value=settings), \
             patch.object(Gio.Settings, "sync"), \
             patch.object(Gio, "bus_get_sync", return_value=connection), \
             patch.object(gnome_modal, "is_available", return_value=False):
            self.assertFalse(gnome_modal.request_enable())
        self.assertEqual(settings.lists["enabled-extensions"],
                         ["first@example", "second@example", gnome_modal.UUID])
        self.assertEqual(settings.lists["disabled-extensions"],
                         ["disabled-first@example", "disabled-last@example"])
        self.assertTrue(settings.globally_disabled)
        self.assertEqual(set(settings.writes), {"enabled-extensions", "disabled-extensions"})
        arguments = connection.call_sync.call_args.args
        self.assertEqual(arguments[:4], ("org.gnome.Shell", "/org/gnome/Shell",
                                         "org.gnome.Shell.Extensions", "EnableExtension"))
        self.assertEqual(arguments[4].unpack(), (gnome_modal.UUID,))
        self.assertEqual(arguments[6], Gio.DBusCallFlags.NO_AUTO_START)
        self.assertEqual(arguments[7], gnome_modal.CALL_TIMEOUT_MS)

    def test_request_enable_does_not_duplicate_an_existing_uuid(self):
        from gi.repository import Gio

        settings = ShellSettings(enabled=["first@example", gnome_modal.UUID, "last@example"])
        with patch.object(Gio.Settings, "new", return_value=settings), \
             patch.object(Gio.Settings, "sync"), \
             patch.object(Gio, "bus_get_sync", return_value=Mock()), \
             patch.object(gnome_modal, "is_available", return_value=True):
            self.assertTrue(gnome_modal.request_enable())
        self.assertEqual(settings.lists["enabled-extensions"],
                         ["first@example", gnome_modal.UUID, "last@example"])
        self.assertEqual(settings.writes, [])

    def test_request_enable_leaves_staged_uuid_pending_when_session_bus_is_unavailable(self):
        from gi.repository import Gio

        settings = ShellSettings(enabled=["other@example"])
        with patch.object(Gio.Settings, "new", return_value=settings), \
             patch.object(Gio.Settings, "sync"), \
             patch.object(Gio, "bus_get_sync", side_effect=GLib.Error("Unavailable")):
            self.assertFalse(gnome_modal.request_enable())
        self.assertEqual(settings.lists["enabled-extensions"], ["other@example", gnome_modal.UUID])
        self.assertTrue(settings.globally_disabled)

    def test_availability_requires_the_exported_interface_and_never_shows_a_dialog(self):
        from gi.repository import Gio

        for xml, expected in ((f'<node><interface name="{gnome_modal.INTERFACE}"/></node>', True),
                              ('<node><interface name="org.freedesktop.DBus.Introspectable"/></node>', False),
                              ('<node><node name="child"/></node>', False),
                              ('malformed XML', False)):
            with self.subTest(xml=xml):
                connection = Mock()
                connection.call_sync.return_value = GLib.Variant("(s)", (xml,))
                with patch.object(Gio, "bus_get_sync", return_value=connection):
                    self.assertEqual(gnome_modal.is_available(), expected)
                arguments = connection.call_sync.call_args.args
                self.assertEqual(arguments[:4], (gnome_modal.BUS_NAME, gnome_modal.OBJECT_PATH,
                                                 "org.freedesktop.DBus.Introspectable", "Introspect"))
                self.assertIsNone(arguments[4])
                self.assertEqual(arguments[6], Gio.DBusCallFlags.NO_AUTO_START)
                self.assertEqual(arguments[7], gnome_modal.CALL_TIMEOUT_MS)
        with patch.object(Gio, "bus_get_sync", side_effect=GLib.Error("Unavailable")):
            self.assertFalse(gnome_modal.is_available())

    def test_remove_releases_modal_and_removes_only_owned_uuid_and_files(self):
        from gi.repository import Gio

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_DATA_HOME": temporary}):
            target = gnome_modal.extension_path()
            target.mkdir(parents=True)
            (target / "extension.js").write_text("test asset")
            other = target.parent / "other@example"
            other.mkdir()
            (other / "extension.js").write_text("keep this extension")
            settings = ShellSettings(enabled=["first@example", gnome_modal.UUID, "last@example"],
                                     disabled=["unrelated-disabled@example"])

            def hide():
                self.assertTrue(target.is_dir())
                return True

            with patch.object(gnome_modal, "try_hide", side_effect=hide) as hide_call, \
                 patch.object(Gio.Settings, "new", return_value=settings), \
                 patch.object(Gio.Settings, "sync"):
                gnome_modal.remove_extension()
            hide_call.assert_called_once()
            self.assertFalse(target.exists())
            self.assertEqual((other / "extension.js").read_text(), "keep this extension")
            self.assertEqual(settings.lists["enabled-extensions"], ["first@example", "last@example"])
            self.assertEqual(settings.lists["disabled-extensions"], ["unrelated-disabled@example"])
            self.assertTrue(settings.globally_disabled)


if __name__ == "__main__":
    unittest.main()
