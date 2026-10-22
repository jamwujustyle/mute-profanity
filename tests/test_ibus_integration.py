"""Real D-Bus/IBus checks; run with ``tools/test-ibus.sh``.

Ordinary unittest discovery skips these checks. The runner creates private bus
sockets and temporary XDG directories, without changing desktop input sources.
No window, keyboard device or normal user input is used.
"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys
import time
import traceback
import unittest


_RUN = os.environ.get("MUTE_IBUS_INTEGRATION") == "1"


@unittest.skipUnless(_RUN, "Run tools/test-ibus.sh for isolated real-IBus checks")
class IBusIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Refuse to attach to an ordinary desktop bus even if the opt-in is set.
        root = Path(os.environ.get("MUTE_IBUS_TEST_ROOT", "/missing")).resolve()
        if (not root.is_dir() or not root.name.startswith("mute-ibus-test.")
                or root.parent != Path("/tmp")
                or os.environ.get("IBUS_ADDRESS") != f"unix:path={root}/ibus.sock"
                or os.environ.get("MUTE_IBUS_PRIVATE_DBUS") != "1"):
            raise RuntimeError("Integration checks require the private tools/test-ibus.sh runner")
        for variable, child in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                                ("XDG_DATA_HOME", "data"), ("XDG_RUNTIME_DIR", "runtime")):
            if os.environ.get(variable) != str(root / child):
                raise RuntimeError(f"{variable} must point inside the temporary test directory")

        import gi

        gi.require_version("IBus", "1.0")
        from gi.repository import Gio, GLib, IBus
        from mute_profanity import config
        from mute_profanity.engine import BUS_NAME, ENGINES, Factory, Runtime

        cls.Gio, cls.GLib, cls.IBus, cls.config = Gio, GLib, IBus, config
        cls.callback_errors = []
        previous_hook = sys.excepthook
        cls.addClassCleanup(setattr, sys, "excepthook", previous_hook)

        def callback_error(error_type, error, trace):
            cls.callback_errors.append("".join(traceback.format_exception(error_type, error, trace)))

        # PyGObject otherwise prints virtual-method exceptions and lets the test
        # continue, which could conceal an adapter lifecycle failure.
        sys.excepthook = callback_error
        IBus.init()
        cls.bus = IBus.Bus.new()
        if not cls.bus.is_connected():
            raise RuntimeError("Cannot connect to the isolated IBus daemon")
        # Check the installed XML discovery path before dynamic registration can
        # conceal a missing/invalid component or an incorrect keyboard layout.
        discovered = {description.get_name(): description for description in cls.bus.list_engines()}
        for name, language, layout in zip(ENGINES, ("en", "ru"), ("us", "ru")):
            if name not in discovered:
                raise AssertionError(f"The component registry did not discover {name}")
            if discovered[name].get_layout() != layout or discovered[name].get_language() != language:
                raise AssertionError(f"The discovered {name} has the wrong language or layout")
        cls.runtime = Runtime(popup=False)
        cls.factory = Factory(cls.bus, cls.runtime)
        if not cls.bus.request_name(BUS_NAME, 0):
            raise RuntimeError("Cannot own the test input method's private IBus name")
        component = IBus.Component.new(BUS_NAME, "Isolated Mute integration checks", "0.1",
                                       "MIT", "", "", "/bin/false", "")
        for name, language, layout in zip(ENGINES, ("en", "ru"), ("us", "ru")):
            component.add_engine(IBus.EngineDesc.new(name, name, "Integration test", language,
                                                   "MIT", "", "", layout))
        if not cls.bus.register_component(component):
            raise RuntimeError("Cannot register the isolated test component")
        cls.context = GLib.MainContext.default()

    @classmethod
    def tearDownClass(cls):
        for engine in tuple(cls.runtime.engines):
            engine.destroy()
        cls.factory.destroy()
        for monitor in cls.runtime.monitors:
            monitor.cancel()
        if cls.callback_errors:
            raise AssertionError("Unexpected IBus callback exception:\n" + "\n".join(cls.callback_errors))

    def wait_for(self, predicate, description, timeout=2.5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(f"Timed out waiting for {description}")
            while self.context.pending():
                self.context.iteration(False)
            time.sleep(0.001)

    def drain(self, duration=0.02):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            while self.context.pending():
                self.context.iteration(False)
            time.sleep(0.001)

    def complete_async(self, start, finish):
        result = []

        def completed(source, response, *_unused):
            try:
                result.append((True, finish(source, response)))
            except Exception as error:
                result.append((False, error))

        start(completed)
        self.wait_for(lambda: bool(result), "an asynchronous IBus reply")
        successful, value = result[0]
        if not successful:
            raise value
        return value

    def rpc(self, name, parameters=None):
        return self.complete_async(
            lambda callback: self.client.call(name, parameters, self.Gio.DBusCallFlags.NONE,
                                              1500, None, callback, None),
            lambda source, response: source.call_finish(response))

    def setUp(self):
        self.config.save(self.config.Settings(instant_trigger=True, beep=False))
        self.runtime.reload()
        self.runtime.cooldown.cancel()
        self.runtime.cooldown.strikes = 0
        self.runtime.cooldown.last_hit = float("-inf")
        self.client = self.bus.create_input_context("mute-isolated-integration")
        self.commits = []
        self.preedits = []
        self.client.connect("commit-text", lambda _client, text: self.commits.append(text.get_text()))
        self.client.connect("update-preedit-text", lambda _client, text, cursor, visible:
                            self.preedits.append((text.get_text(), cursor, visible)))
        self.client.set_capabilities(int(self.IBus.Capabilite.PREEDIT_TEXT |
                                         self.IBus.Capabilite.AUXILIARY_TEXT |
                                         self.IBus.Capabilite.FOCUS))
        # An accidental preedit/commit on focus loss must also be observable.
        self.client.set_client_commit_preedit(False)
        self.client.focus_in()
        self.switch("mute-en")

    def tearDown(self):
        self.client.focus_out()
        self.client.destroy()
        # Drain destroy/focus messages before the next test changes settings.
        deadline = time.monotonic() + 0.03
        while time.monotonic() < deadline:
            while self.context.pending():
                self.context.iteration(False)
            time.sleep(0.001)
        errors = self.callback_errors[:]
        self.callback_errors.clear()
        self.assertEqual(errors, [], "Unexpected IBus callback exception:\n" + "\n".join(errors))
        self.assertTrue(all(not text and not visible for text, _cursor, visible in self.preedits),
                        "IBus lifecycle clears may be empty, but no visible preedit is permitted")

    def switch(self, name):
        self.rpc("SetEngine", self.GLib.Variant("(s)", (name,)))
        description = self.complete_async(
            lambda callback: self.client.get_engine_async(1500, None, callback, None),
            lambda source, response: source.get_engine_async_finish(response))
        self.assertEqual(description.get_name(), name)
        self.wait_for(lambda: any(engine.get_property("engine-name") == name
                                 for engine in self.runtime.engines), "the requested engine")
        self.engine = next(engine for engine in self.runtime.engines
                           if engine.get_property("engine-name") == name)
        # The daemon emits its own empty preedit reset when changing engines.
        # Measure key handling only after those lifecycle messages have settled.
        self.drain()
        self.preedits.clear()

    def press(self, keyval, state=0):
        previous = len(self.preedits)
        result = self.complete_async(
            lambda callback: self.client.process_key_event_async(keyval, 0, state, 1500,
                                                                 None, callback, None),
            lambda source, response: source.process_key_event_async_finish(response))
        self.assertEqual(self.preedits[previous:], [],
                         "Key processing must not emit preedit, including empty updates")
        return result

    def type_text(self, text, *, unicode_keyvals=False, blocked_last=False):
        for index, character in enumerate(text):
            keyval = (0x01000000 | ord(character)) if unicode_keyvals else self.IBus.unicode_to_keyval(character)
            expected = blocked_last and index == len(text) - 1
            self.assertEqual(self.press(keyval), expected,
                             "Safe characters must use their native key event; only a triggering key is suppressed")

    def boundary_mode(self):
        self.config.save(replace(self.config.load(), instant_trigger=False))
        self.runtime.reload()

    def test_safe_search_prefixes_and_enter_preserve_native_events_without_ime_updates(self):
        for prefix in ("chr", "n2"):
            with self.subTest(prefix=prefix):
                self.type_text(prefix)
                self.assertEqual(self.engine.composition.text, prefix)
                self.assertFalse(self.press(self.IBus.KEY_Return))
                self.assertEqual(self.engine.composition.text, "")
                self.assertEqual(self.commits, [])
                self.assertEqual(self.preedits, [])
        self.assertEqual(self.runtime.cooldown.remaining(), 0)

    def test_immediate_russian_rejects_only_final_letter_without_space(self):
        self.switch("mute-ru")
        self.type_text("су", unicode_keyvals=True)
        self.assertEqual(self.engine.composition.text, "су")
        self.assertEqual(self.runtime.cooldown.remaining(), 0)
        self.type_text("к", unicode_keyvals=True)
        self.assertEqual(self.engine.composition.text, "сук")
        self.assertEqual(self.runtime.cooldown.remaining(), 0)
        self.type_text("а", unicode_keyvals=True, blocked_last=True)
        self.assertEqual(self.engine.composition.text, "")
        self.assertGreater(self.runtime.cooldown.remaining(), 0)
        self.assertEqual(self.runtime.cooldown.strikes, 1)
        self.assertEqual(self.commits, [])

    def test_immediate_english_rejects_final_letter_and_shares_cooldown(self):
        original = self.engine
        self.type_text("fuc")
        self.assertEqual(self.engine.composition.text, "fuc")
        self.assertEqual(self.runtime.cooldown.remaining(), 0)
        self.type_text("k", blocked_last=True)
        self.assertEqual(self.engine.composition.text, "")
        self.assertGreater(self.runtime.cooldown.remaining(), 0)
        self.assertEqual(self.commits, [])
        self.switch("mute-ru")
        self.assertIsNot(self.engine, original)
        self.assertIs(self.engine.runtime.cooldown, original.runtime.cooldown)
        self.assertTrue(self.press(0x01000000 | ord("я")))
        self.assertTrue(self.press(self.IBus.KEY_Return))
        self.assertEqual(self.engine.composition.text, "")
        self.assertEqual(self.commits, [])

    def test_immediate_custom_language_word_reloads_and_blocks_on_last_letter(self):
        path = self.config.wordlist_dir() / "el-immediate-test.txt"
        try:
            path.write_text("βρισιά\n", encoding="utf-8")
            self.wait_for(lambda: bool(self.engine.composition.matcher.detect("βρισιά")),
                          "automatic custom-language word-list reload")
            self.type_text("βρισι", unicode_keyvals=True)
            self.assertEqual(self.engine.composition.text, "βρισι")
            self.assertEqual(self.runtime.cooldown.remaining(), 0)
            self.type_text("ά", unicode_keyvals=True, blocked_last=True)
            self.assertEqual(self.engine.composition.text, "")
            self.assertGreater(self.runtime.cooldown.remaining(), 0)
            self.assertEqual(self.commits, [])
        finally:
            path.unlink(missing_ok=True)

    def test_immediate_composed_accent_uses_custom_word_filter(self):
        path = self.config.wordlist_dir() / "fr-immediate-test.txt"
        try:
            path.write_text("café\n", encoding="utf-8")
            self.wait_for(lambda: bool(self.engine.composition.matcher.detect("café")),
                          "the accented custom word")
            self.type_text("caf")
            self.assertTrue(self.press(0xFE51))  # dead_acute
            self.assertEqual(self.engine.composition.text, "caf")
            self.assertEqual(self.runtime.cooldown.remaining(), 0)
            self.assertTrue(self.press(ord("e")))
            self.assertEqual(self.engine.composition.text, "")
            self.assertGreater(self.runtime.cooldown.remaining(), 0)
            self.assertEqual(self.commits, [])
        finally:
            path.unlink(missing_ok=True)

    def test_backspace_passes_through_and_warns_after_dictionary_reload(self):
        self.type_text("novelx")
        path = self.config.wordlist_dir() / "xx-backspace-test.txt"
        try:
            path.write_text("novel\n", encoding="utf-8")
            self.wait_for(lambda: bool(self.engine.composition.matcher.detect("novel")),
                          "the new exact custom word")
            self.assertEqual(self.engine.composition.text, "novelx")
            self.assertEqual(self.runtime.cooldown.remaining(), 0)
            self.assertFalse(self.press(self.IBus.KEY_BackSpace))
            self.assertEqual(self.engine.composition.text, "")
            self.assertGreater(self.runtime.cooldown.remaining(), 0)
            self.assertEqual(self.commits, [])
        finally:
            path.unlink(missing_ok=True)

    def test_allow_word_preserves_native_typing_but_unsafe_standalone_boundary_is_blocked(self):
        self.config.save(replace(self.config.load(), allow_words=["assembly"]))
        self.runtime.reload()
        self.type_text("ass")
        self.assertEqual(self.engine.composition.text, "ass")
        self.assertEqual(self.runtime.cooldown.remaining(), 0)
        self.type_text("embly")
        self.assertEqual(self.engine.composition.text, "assembly")
        self.assertEqual(self.runtime.cooldown.remaining(), 0)
        self.assertFalse(self.press(self.IBus.KEY_space))
        self.assertEqual(self.commits, [])
        self.type_text("ass")
        self.assertEqual(self.runtime.cooldown.remaining(), 0)
        self.assertTrue(self.press(self.IBus.KEY_space))
        self.assertEqual(self.engine.composition.text, "")
        self.assertGreater(self.runtime.cooldown.remaining(), 0)
        self.assertEqual(self.commits, [])

    def test_safe_word_and_space_never_generate_commit_or_preedit(self):
        self.type_text("hello")
        self.assertEqual(self.engine.composition.text, "hello")
        self.assertFalse(self.press(self.IBus.KEY_space))
        self.assertEqual(self.commits, [])
        self.assertEqual(self.preedits, [])
        self.assertEqual(self.engine.composition.text, "")

    def test_wordlist_reload_and_compose_commit_only_the_completed_accent(self):
        # Ordinary characters never need the composition library or IME commits.
        self.assertIsNone(self.engine.composer)
        self.type_text("caf")
        self.assertTrue(self.press(0xFE51))  # dead_acute
        self.assertTrue(self.press(ord("e")))
        self.assertEqual(self.engine.composition.text, "café")
        self.assertFalse(self.press(self.IBus.KEY_space))
        self.wait_for(lambda: bool(self.commits), "the composed accent only")
        self.assertEqual(self.commits, ["é"])
        self.commits.clear()
        path = self.config.wordlist_dir() / "fr-test.txt"
        try:
            path.write_text("café\n", encoding="utf-8")
            self.wait_for(lambda: bool(self.engine.composition.matcher.detect("café")),
                          "automatic word-list reload")
            self.type_text("caf")
            self.assertTrue(self.press(self.IBus.KEY_Multi_key))
            self.assertTrue(self.press(ord("'")))
            self.assertTrue(self.press(ord("e")))
            self.assertEqual(self.engine.composition.text, "")
            self.assertEqual(self.commits, [])
            self.assertGreater(self.runtime.cooldown.remaining(), 0)
        finally:
            path.unlink(missing_ok=True)

    def test_boundary_mode_checks_invisible_format_characters(self):
        self.boundary_mode()
        self.type_text("f\u200buck", unicode_keyvals=True)
        self.assertTrue(self.press(self.IBus.KEY_space))
        self.assertEqual(self.commits, [])

    def test_boundary_mode_suppresses_unsafe_submit_without_recommitting_text(self):
        self.boundary_mode()
        self.type_text("fuck")
        self.assertEqual(self.commits, [])
        self.assertTrue(self.press(self.IBus.KEY_space))
        self.assertEqual(self.commits, [])
        self.assertEqual(self.engine.composition.text, "")
        self.assertGreater(self.runtime.cooldown.remaining(), 0)
        self.assertTrue(self.press(self.IBus.KEY_Return))
        self.assertTrue(self.press(self.IBus.KEY_KP_Enter))
        self.assertEqual(self.commits, [])

    def test_russian_unicode_keyvals_use_native_safe_typing(self):
        self.switch("mute-ru")
        self.type_text("Привет", unicode_keyvals=True)
        self.assertFalse(self.press(self.IBus.KEY_space))
        self.assertEqual(self.commits, [])
        self.type_text("бл", unicode_keyvals=True)
        self.type_text("я", unicode_keyvals=True, blocked_last=True)
        self.assertEqual(self.commits, [])
        self.assertGreater(self.runtime.cooldown.remaining(), 0)

    def test_native_backspace_navigation_and_selection_do_not_commit_or_filter(self):
        self.type_text("hellx")
        self.assertFalse(self.press(self.IBus.KEY_BackSpace))
        self.assertEqual(self.engine.composition.text, "hell")
        self.type_text("o")
        shift = int(self.IBus.ModifierType.SHIFT_MASK)
        self.assertFalse(self.press(self.IBus.KEY_Left, shift))
        self.assertEqual(self.engine.composition.text, "")
        self.assertEqual(self.commits, [])
        self.boundary_mode()
        for key in (self.IBus.KEY_Delete, self.IBus.KEY_Left, self.IBus.KEY_Right,
                    self.IBus.KEY_Home, self.IBus.KEY_End, self.IBus.KEY_Escape):
            with self.subTest(key=key):
                self.type_text("fuck")
                self.assertFalse(self.press(key))
                self.assertEqual(self.engine.composition.text, "")
                self.assertEqual(self.runtime.cooldown.remaining(), 0)
                self.assertEqual(self.commits, [])

    def test_native_shortcuts_reset_tracking_and_safe_modified_submit_passes(self):
        self.boundary_mode()
        control = int(self.IBus.ModifierType.CONTROL_MASK)
        for character in "acvxz":
            with self.subTest(shortcut=character):
                self.type_text("fuck")
                self.assertFalse(self.press(ord(character), control))
                self.assertEqual(self.engine.composition.text, "")
                self.assertEqual(self.runtime.cooldown.remaining(), 0)
        self.type_text("hello")
        self.assertFalse(self.press(self.IBus.KEY_Return, control))
        self.assertEqual(self.engine.composition.text, "")
        self.assertEqual(self.commits, [])

    def test_unsafe_modified_submit_is_blocked_in_boundary_mode(self):
        self.boundary_mode()
        self.type_text("fuck")
        control = int(self.IBus.ModifierType.CONTROL_MASK)
        self.assertFalse(self.press(self.IBus.KEY_Control_L, control))
        self.assertEqual(self.engine.composition.text, "fuck")
        self.assertTrue(self.press(self.IBus.KEY_Return, control))
        self.assertEqual(self.engine.composition.text, "")
        self.assertGreater(self.runtime.cooldown.remaining(), 0)
        self.assertEqual(self.commits, [])

    def test_cooldown_survives_a_new_language_engine(self):
        original = self.engine
        self.type_text("fuck", blocked_last=True)
        self.switch("mute-ru")
        self.assertIsNot(self.engine, original)
        self.assertIs(self.engine.runtime.cooldown, original.runtime.cooldown)
        self.assertTrue(self.press(0x01000000 | ord("я")))
        self.assertEqual(self.engine.composition.text, "")
        self.assertTrue(self.press(self.IBus.KEY_Return))
        self.assertEqual(self.commits, [])

    def test_emergency_pause_cancels_cooldown_and_passes_input(self):
        self.type_text("fuck", blocked_last=True)
        modifiers = int(self.IBus.ModifierType.CONTROL_MASK | self.IBus.ModifierType.SHIFT_MASK)
        self.assertTrue(self.press(self.IBus.KEY_F12, modifiers))
        self.assertGreater(self.config.load().paused_until, time.time() + 290)
        self.assertEqual(self.runtime.cooldown.remaining(), 0)
        self.assertFalse(self.press(ord("a")))
        self.assertEqual(self.engine.composition.text, "")
        self.assertEqual(self.commits, [])

    def test_disabled_filter_clears_tracker_without_flush_or_preedit(self):
        self.type_text("hello")
        self.config.save(replace(self.config.load(), enabled=False))
        self.runtime.reload()
        self.type_text("fuck")
        self.assertFalse(self.press(self.IBus.KEY_Return))
        self.assertEqual(self.engine.composition.text, "")
        self.assertEqual(self.commits, [])
        self.assertEqual(self.runtime.cooldown.remaining(), 0)

    def test_password_purpose_never_tracks_or_filters(self):
        self.assert_private_bypass(self.IBus.InputPurpose.PASSWORD, 0)

    def test_pin_purpose_never_tracks_or_filters(self):
        self.assert_private_bypass(self.IBus.InputPurpose.PIN, 0)

    def test_private_hint_never_tracks_or_filters(self):
        self.assert_private_bypass(self.IBus.InputPurpose.FREE_FORM, self.IBus.InputHints.PRIVATE)

    def test_non_message_purposes_preserve_native_typing_and_submit(self):
        for name in ("DIGITS", "NUMBER", "PHONE", "URL", "EMAIL", "NAME", "TERMINAL"):
            with self.subTest(purpose=name):
                self.assert_private_bypass(getattr(self.IBus.InputPurpose, name), 0)

    def test_alpha_purpose_still_filters_ordinary_message_typing(self):
        self.client.set_content_type(self.IBus.InputPurpose.ALPHA, 0)
        self.wait_for(lambda: self.engine.get_content_type()[0] == self.IBus.InputPurpose.ALPHA,
                      "alphabetic content type")
        self.assertFalse(self.engine.private)
        self.type_text("fuck", blocked_last=True)
        self.assertGreater(self.runtime.cooldown.remaining(), 0)
        self.assertEqual(self.commits, [])

    def assert_private_bypass(self, purpose, hints):
        self.client.set_content_type(purpose, hints)
        self.wait_for(lambda: self.engine.private and tuple(self.engine.get_content_type()) == (purpose, hints),
                      "non-message/private content type")
        self.type_text("fuck")
        self.assertFalse(self.press(self.IBus.KEY_Return))
        self.assertEqual(self.engine.composition.text, "")
        self.assertEqual(self.commits, [])
        self.assertEqual(self.runtime.cooldown.remaining(), 0)

    def test_release_events_never_insert_commit_or_pause(self):
        self.type_text("hello")
        release = int(self.IBus.ModifierType.RELEASE_MASK)
        self.assertFalse(self.press(ord("o"), release))
        self.assertFalse(self.press(self.IBus.KEY_space, release))
        emergency = int(self.IBus.ModifierType.CONTROL_MASK | self.IBus.ModifierType.SHIFT_MASK)
        self.assertFalse(self.press(self.IBus.KEY_F12, release | emergency))
        self.assertEqual(self.engine.composition.text, "hello")
        self.assertEqual(self.commits, [])
        self.assertEqual(self.config.load().paused_until, 0)
        self.assertFalse(self.press(self.IBus.KEY_space))
        self.assertEqual(self.engine.composition.text, "")
        self.assertEqual(self.commits, [])

    def test_focus_out_clears_safe_tracker_without_commit(self):
        self.type_text("hello")
        self.client.focus_out()
        self.wait_for(lambda: self.engine.composition.text == "", "safe focus-out clearing")
        self.assertEqual(self.commits, [])
        self.assertEqual(self.runtime.cooldown.remaining(), 0)

    def test_focus_out_clears_unsafe_tracker_without_commit_or_penalty(self):
        self.boundary_mode()
        self.type_text("fuck")
        self.client.focus_out()
        self.wait_for(lambda: self.engine.composition.text == "", "unsafe focus-out clearing")
        self.assertEqual(self.engine.composition.text, "")
        self.assertEqual(self.commits, [])
        self.assertEqual(self.runtime.cooldown.remaining(), 0)


if __name__ == "__main__":
    unittest.main()
