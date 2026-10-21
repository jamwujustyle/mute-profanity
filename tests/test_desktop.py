from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from mute_profanity import desktop


class Engine:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


class Bus:
    def __init__(self, *, connected=True, names=("mute-en", "mute-ru"), selected="mute-ru"):
        self.connected = connected
        self.names = names
        self.selected = selected

    def is_connected(self):
        return self.connected

    def list_engines_async(self, _timeout, _cancel, callback, _data):
        callback(self, None)

    def list_engines_async_finish(self, _result):
        return [Engine(name) for name in self.names]

    def get_global_engine_async(self, _timeout, _cancel, callback, _data):
        callback(self, None)

    def get_global_engine_async_finish(self, _result):
        return Engine(self.selected) if self.selected is not None else None


class Settings:
    def __init__(self, sources, recent=()):
        self.values = {"sources": list(sources), "mru-sources": list(recent)}
        self.pending = {}
        self.writes = []
        self.denied = set()

    def get_value(self, key):
        return desktop.GLib.Variant("a(ss)", self.values[key])

    def delay(self):
        self.pending = {}

    def set_value(self, key, value):
        self.writes.append(key)
        if key in self.denied:
            return False
        self.pending[key] = [tuple(pair) for pair in value.unpack()]
        return True

    def apply(self):
        self.values.update(self.pending)
        self.pending = {}

    def revert(self):
        self.pending = {}


class DesktopTests(unittest.TestCase):
    def setup_desktop(self, settings, bus):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(desktop, "source_settings", return_value=settings))
        stack.enter_context(patch.object(desktop.IBus, "init"))
        factory = stack.enter_context(patch.object(desktop.IBus.Bus, "new", return_value=bus))
        stack.enter_context(patch.object(desktop.Gio.Settings, "sync"))
        stack.enter_context(patch.object(desktop, "request_enable", return_value=False))
        return stack, factory

    def test_enable_replaces_standard_layouts_deduplicates_and_keeps_others(self):
        settings = Settings([("xkb", "ru"), ("xkb", "us"), ("ibus", "mute-ru"),
                             ("xkb", "us+intl"), ("xkb", "de"), ("ibus", "pinyin")],
                            [("xkb", "us"), ("xkb", "ru"), ("ibus", "mute-en")])
        self.setup_desktop(settings, Bus())
        with patch.object(desktop.subprocess, "run") as run:
            self.assertEqual(desktop.enable(), 0)
        run.assert_not_called()
        self.assertEqual(settings.values["sources"], [("ibus", "mute-ru"), ("ibus", "mute-en"),
                                                     ("xkb", "us+intl"), ("xkb", "de"), ("ibus", "pinyin")])
        self.assertEqual(settings.values["mru-sources"], [("ibus", "mute-en"), ("ibus", "mute-ru")])

    def test_enable_adds_missing_guard_languages_without_replacing_other_variants(self):
        settings = Settings([("xkb", "us+intl"), ("xkb", "fr")])
        self.setup_desktop(settings, Bus())
        desktop.enable()
        self.assertEqual(settings.values["sources"], [("xkb", "us+intl"), ("xkb", "fr"),
                                                     ("ibus", "mute-en"), ("ibus", "mute-ru")])
        self.assertEqual(settings.values["mru-sources"], [])

    def test_disable_converts_guard_sources_and_recent_sources_preserving_order(self):
        settings = Settings([("ibus", "mute-ru"), ("xkb", "de"), ("ibus", "mute-en"), ("xkb", "ru")],
                            [("ibus", "mute-en"), ("xkb", "ru"), ("ibus", "mute-ru")])
        self.setup_desktop(settings, Bus())
        desktop.disable()
        self.assertEqual(settings.values["sources"], [("xkb", "ru"), ("xkb", "de"), ("xkb", "us")])
        self.assertEqual(settings.values["mru-sources"], [("xkb", "us"), ("xkb", "ru")])

    def test_failed_settings_write_reverts_both_keys(self):
        original = [("xkb", "us"), ("xkb", "ru")]
        settings = Settings(original, [("xkb", "ru")])
        settings.denied.add("sources")
        self.setup_desktop(settings, Bus())
        with self.assertRaises(RuntimeError):
            desktop.enable()
        self.assertEqual(settings.values["sources"], original)
        self.assertEqual(settings.values["mru-sources"], [("xkb", "ru")])
        self.assertFalse(settings.pending)

    def installed_paths(self, stack):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        component = Path(temporary.name, "component.xml")
        dropin = Path(temporary.name, "service.conf")
        component.touch()
        dropin.touch()
        stack.enter_context(patch.object(desktop, "paths", return_value={"component": component, "dropin": dropin}))

    def test_missing_registry_restarts_service_once_and_waits_for_new_bus(self):
        settings = Settings([("xkb", "us"), ("xkb", "ru")])
        stack, factory = self.setup_desktop(settings, Bus(names=()))
        self.installed_paths(stack)
        factory.side_effect = [Bus(names=()), Bus(connected=False), Bus()]
        with patch.object(desktop.subprocess, "run", return_value=Mock(returncode=0)) as run:
            desktop.enable()
        run.assert_called_once_with(["systemctl", "--user", "restart", desktop.SERVICE],
                                    capture_output=True, timeout=10, check=False)
        self.assertEqual(settings.values["sources"], list(desktop.SOURCES))

    def test_restart_failure_leaves_source_settings_untouched(self):
        settings = Settings([("xkb", "us"), ("xkb", "ru")])
        stack, _factory = self.setup_desktop(settings, Bus(names=()))
        self.installed_paths(stack)
        with patch.object(desktop.subprocess, "run", return_value=Mock(returncode=1)) as run:
            with self.assertRaises(RuntimeError):
                desktop.enable()
        run.assert_called_once()
        self.assertFalse(settings.writes)
        self.assertEqual(settings.values["sources"], list(desktop.NATIVE_SOURCES))

    def test_restart_timeout_leaves_source_settings_untouched(self):
        settings = Settings(list(desktop.NATIVE_SOURCES))
        stack, _factory = self.setup_desktop(settings, Bus(names=()))
        self.installed_paths(stack)
        with patch.object(desktop.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemctl", 10)):
            with self.assertRaises(RuntimeError):
                desktop.enable()
        self.assertFalse(settings.writes)

    def test_uninstalled_missing_registry_does_not_restart_or_mutate(self):
        settings = Settings(list(desktop.NATIVE_SOURCES))
        stack, _factory = self.setup_desktop(settings, Bus(names=()))
        stack.enter_context(patch.object(desktop, "paths", return_value={
            "component": Path("/nonexistent/component"), "dropin": Path("/nonexistent/dropin")}))
        with patch.object(desktop.subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                desktop.enable()
        run.assert_not_called()
        self.assertFalse(settings.writes)

    def test_status_for_old_configuration_is_not_active_and_does_not_write(self):
        settings = Settings(list(desktop.NATIVE_SOURCES))
        self.setup_desktop(settings, Bus(selected="xkb:us::eng"))
        with patch.object(desktop.subprocess, "run") as run:
            state = desktop.status()
        self.assertEqual(state.code, "requires_setup")
        self.assertFalse(state.ready)
        self.assertTrue(state.engines_ready)
        self.assertEqual(state.global_engine, "xkb:us::eng")
        run.assert_not_called()
        self.assertFalse(settings.writes)

    def test_status_distinguishes_missing_registry_selected_ordinary_and_guard(self):
        for bus, expected in [(Bus(names=()), "requires_load"),
                              (Bus(selected="xkb:us::eng"), "ordinary_source"),
                              (Bus(), "source_active"),
                              (Bus(connected=False), "unavailable")]:
            with self.subTest(expected=expected):
                settings = Settings(list(desktop.SOURCES))
                with patch.object(desktop, "source_settings", return_value=settings), \
                     patch.object(desktop.IBus, "init"), \
                     patch.object(desktop.IBus.Bus, "new", return_value=bus):
                    state = desktop.status()
                self.assertEqual(state.code, expected)
                self.assertEqual(state.ready, expected == "source_active")
                self.assertFalse(settings.writes)

    def test_status_without_a_selected_engine_still_reports_missing_setup(self):
        settings = Settings(list(desktop.NATIVE_SOURCES))
        bus = Bus()
        bus.get_global_engine_async_finish = Mock(side_effect=desktop.GLib.Error("No global engine"))
        self.setup_desktop(settings, bus)
        state = desktop.status()
        self.assertEqual(state.code, "requires_setup")
        self.assertIsNone(state.global_engine)

    def test_async_registry_query_processes_glib_callbacks(self):
        bus = Bus()

        def request(_timeout, _cancel, callback, _data):
            def complete():
                callback(bus, None)
                return desktop.GLib.SOURCE_REMOVE
            desktop.GLib.idle_add(complete)

        bus.list_engines_async = request
        self.assertEqual(desktop._registry_names(bus), {"mute-en", "mute-ru"})


if __name__ == "__main__":
    unittest.main()
