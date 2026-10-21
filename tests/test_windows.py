import ctypes
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from mute_profanity.config import Settings
from mute_profanity.core import MAX_COMPOSITION
from mute_profanity.windows import (
    Controller, KeyEvent, _INPUT, _KEYBOARD, _MOUSE, _Native,
    _INJECTION_TAG, _READY_EVENT, is_ready, is_running, refresh, stop,
)


class WindowsPolicyTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller(Settings(escalate=False))
        self.focus = (1, 2, 3)

    def key(self, text="", key="text", **kwargs):
        values = dict(text=text, key=key, focus=self.focus)
        values.update(kwargs)
        return self.controller.process(KeyEvent(**values), now=10, wall_time=100)

    def type(self, text):
        return [self.key(char) for char in text]

    def test_instant_suppresses_last_key_erases_only_forwarded_prefix(self):
        self.assertTrue(all(not d.suppress for d in self.type("fuc")))
        decision = self.key("k")
        self.assertTrue(decision.suppress)
        self.assertEqual(3, decision.erase)
        self.assertEqual(8, decision.seconds)
        self.assertEqual("", self.controller.text)
        self.assertTrue(self.key("a").suppress)
        self.assertTrue(self.key(key="boundary").suppress)

    def test_boundary_mode_removes_complete_word_and_blocks_submit(self):
        self.controller = Controller(Settings(instant_trigger=False, escalate=False))
        self.assertTrue(all(not d.suppress for d in self.type("блядь")))
        decision = self.key(key="boundary")
        self.assertTrue(decision.suppress)
        self.assertEqual(5, decision.erase)
        self.assertGreater(decision.seconds, 0)

    def test_modified_submit_checks_boundary_mode_and_deferred_allowed_prefix(self):
        for settings, text in ((Settings(instant_trigger=False, escalate=False), "fuck"),
                               (Settings(allow_words=["assembly"], escalate=False), "ass")):
            with self.subTest(text=text):
                self.controller = Controller(settings)
                self.assertTrue(all(not decision.suppress for decision in self.type(text)))
                self.assertFalse(self.key(key="modifier", shortcut=True).suppress)
                self.assertEqual(self.controller.text, text)
                decision = self.key(key="boundary", shortcut=True)
                self.assertTrue(decision.suppress)
                self.assertGreater(decision.seconds, 0)
                self.assertEqual(decision.erase, 0)

    def test_safe_boundary_backspace_repeats_and_metadata(self):
        self.type("hello")
        self.assertFalse(self.key(key="backspace").suppress)
        self.assertEqual("hell", self.controller.text)
        self.assertFalse(self.key(key="boundary").suppress)
        self.assertEqual("", self.controller.text)
        self.type("fuuuc")
        self.assertEqual(5, self.key("k").erase)

    def test_backspace_immediately_rechecks_changed_rules_and_erases_original_prefix(self):
        from mute_profanity.core import Matcher

        self.type("novelx")
        settings = Settings(extra_words=["novel"], escalate=False)
        self.controller.update_rules(settings, Matcher(settings))
        decision = self.key(key="backspace")
        self.assertTrue(decision.suppress)
        self.assertEqual(decision.erase, 6)
        self.assertGreater(decision.seconds, 0)
        self.assertEqual(self.controller.text, "")

    def test_focus_mouse_navigation_and_shortcut_invalidate_deletion(self):
        for reset in ("focus", "mouse", "navigation", "shortcut", "dead", "unknown"):
            with self.subTest(reset=reset):
                self.controller = Controller(Settings(escalate=False))
                self.type("fuc")
                if reset == "focus":
                    self.key("k", focus=(4, 5, 6))
                elif reset == "mouse":
                    self.controller.reset()
                    self.key("k")
                else:
                    self.key(key=reset if reset != "shortcut" else "text", shortcut=reset == "shortcut")
                    self.key("k")
                self.assertEqual("k", self.controller.text)
                self.assertEqual(0, self.controller.cooldown.until)

    def test_unknown_focus_and_uncertain_translation_never_delete(self):
        self.type("fuc")
        decision = self.key("k", can_erase=False)
        self.assertTrue(decision.suppress)
        self.assertEqual(0, decision.erase)
        for uncertain in ("é\u0301", "😀", "\u0301", "ab", "\ud800"):
            with self.subTest(uncertain=repr(uncertain)):
                self.controller = Controller()
                self.type("fuc")
                self.key(uncertain)
                self.assertEqual("", self.controller.text)
        self.controller = Controller()
        for char in "fuc":
            self.key(char, focus=None)
        self.assertEqual(0, self.key("k", focus=None).erase)

    def test_private_injected_disabled_and_paused_text_is_not_retained(self):
        for flag in ("private", "injected"):
            self.type("fuc")
            self.assertFalse(self.key("k", **{flag: True}).suppress)
            self.assertEqual("", self.controller.text)
        for settings in (Settings(enabled=False), Settings(paused_until=101)):
            self.controller = Controller(settings)
            self.assertTrue(all(not d.suppress for d in self.type("fuck")))
            self.assertEqual("", self.controller.text)

    def test_emergency_pause_cancels_and_remains_effective_after_stale_reload(self):
        self.type("fuc")
        self.key("k")
        decision = self.key(key="unknown", emergency=True)
        self.assertTrue(decision.pause)
        self.assertTrue(decision.suppress)
        self.assertFalse(self.controller.blocking(now=10, wall_time=100))
        self.controller.update_rules(Settings(), self.controller._rules[1])
        self.assertTrue(all(not d.suppress for d in self.type("fuck")))
        self.assertEqual("", self.controller.text)

    def test_explicit_resume_refresh_clears_local_emergency_pause(self):
        self.key(key="unknown", emergency=True)
        self.assertGreater(self.controller.local_pause_until, 100)
        self.controller.update_rules(Settings(), self.controller._rules[1], explicit_refresh=True)
        self.assertEqual(self.controller.local_pause_until, 0)
        self.type("fuc")
        self.assertTrue(self.key("k").suppress)

    def test_explicit_refresh_of_still_paused_settings_does_not_resume(self):
        self.key(key="unknown", emergency=True)
        settings = Settings(paused_until=time.time() + 300)
        self.controller.update_rules(settings, self.controller._rules[1], explicit_refresh=True)
        self.assertGreater(self.controller.local_pause_until, 100)
        self.assertFalse(self.controller.active(wall_time=100))

    def test_key_releases_are_not_suppressed_and_cooldown_expires(self):
        self.type("fuc")
        self.key("k")
        self.assertFalse(self.key("k", down=False).suppress)
        result = self.controller.process(KeyEvent("a", focus=self.focus), now=19, wall_time=100)
        self.assertFalse(result.suppress)
        self.assertEqual("a", self.controller.text)

    def test_allowlisted_completion_and_bounded_long_words(self):
        self.controller = Controller(Settings(allow_words=["assembly"]))
        self.assertTrue(all(not d.suppress for d in self.type("assembly")))
        self.assertFalse(self.key(key="boundary").suppress)
        self.controller = Controller()
        self.type("a" * (MAX_COMPOSITION + 500))
        self.assertLessEqual(len(self.controller.text), MAX_COMPOSITION)
        self.assertTrue(self.controller.suspended)
        self.assertFalse(self.key(key="boundary").suppress)
        self.assertFalse(self.controller.suspended)

    def test_non_windows_import_and_native_structure_sizes(self):
        import sys
        if sys.platform != "win32":
            self.assertFalse(is_running())
            self.assertFalse(is_ready())
            self.assertFalse(stop())
            self.assertFalse(refresh())
        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(40 if pointer == 8 else 28, ctypes.sizeof(_INPUT))
        self.assertEqual(24 if pointer == 8 else 20, ctypes.sizeof(_KEYBOARD))
        self.assertEqual(32 if pointer == 8 else 24, ctypes.sizeof(_MOUSE))


class NativeCallbackTests(unittest.TestCase):
    """Exercise actual callback methods with Win32 boundary calls replaced."""

    def setUp(self):
        self.native = _Native.__new__(_Native)
        self.native.controller = Controller(Settings(escalate=False))
        self.native.worker = Mock()
        self.native.user = Mock()
        self.native.user.CallNextHookEx.return_value = 0
        self.native.down = set()
        self.native.forwarded = set()
        self.native.suppressed = set()
        self.native.mouse_forwarded = set()
        self.native.mouse_suppressed = set()
        self.native.caps = False
        self.native._focus = Mock(return_value=((1, 2, 3), False))
        self.native._translate = Mock(side_effect=lambda data, layout: ("text", chr(data.vkCode).lower()))
        self.native._erase = Mock()

    def key(self, vk, down=True, flags=0, extra=0):
        data = _KEYBOARD(vkCode=vk, scanCode=30, flags=flags, dwExtraInfo=extra)
        return self.native._keyboard(0, 0x0100 if down else 0x0101, ctypes.addressof(data))

    def mouse(self, message):
        data = _MOUSE()
        return self.native._mouse(0, message, ctypes.addressof(data))

    def test_typing_block_pair_erase_and_own_injection_passthrough(self):
        for vk in map(ord, "FUC"):
            self.assertEqual(0, self.key(vk))
            self.assertEqual(0, self.key(vk, down=False))
        self.assertEqual(1, self.key(ord("K")))
        self.native._erase.assert_called_once_with(3, (1, 2, 3))
        self.native.worker.send.assert_any_call("penalty")
        self.assertEqual(1, self.key(ord("K"), down=False))
        self.assertEqual(0, self.key(8, flags=0x10, extra=_INJECTION_TAG))
        self.assertEqual(0, self.key(8, down=False, flags=0x10, extra=_INJECTION_TAG))

    def test_releases_of_previously_forwarded_keys_are_allowed_during_freeze(self):
        self.assertEqual(0, self.key(0xA0))  # Shift, held before cooldown.
        self.native.controller.cooldown.hit(self.native.controller.settings)
        self.assertEqual(0, self.key(0xA0, down=False))
        self.assertEqual(1, self.key(ord("A")))
        self.assertEqual(1, self.key(ord("A"), down=False))

    def test_mouse_clicks_and_wheel_gate_but_old_button_release_and_movement_pass(self):
        self.assertEqual(0, self.mouse(0x0201))
        self.native.controller.cooldown.hit(self.native.controller.settings)
        self.assertEqual(0, self.mouse(0x0202))
        self.assertEqual(1, self.mouse(0x0201))
        self.assertEqual(1, self.mouse(0x0202))
        self.assertEqual(1, self.mouse(0x020A))
        self.assertEqual(0, self.mouse(0x0200))

    def test_emergency_and_callback_fail_open(self):
        self.native.controller.cooldown.hit(self.native.controller.settings)
        self.key(0xA2)  # Control
        self.key(0xA0)  # Shift
        self.assertEqual(1, self.key(0x7B))  # F12
        self.assertFalse(self.native.controller.blocking())
        self.native.worker.send.assert_any_call("pause")
        self.native.controller.local_pause_until = 0
        self.native.down.clear()
        self.native._translate.side_effect = RuntimeError("native translation failure")
        self.native.controller.text = "prefix"
        self.assertEqual(0, self.key(ord("A")))
        self.assertEqual("", self.native.controller.text)

    def test_negative_hook_code_bypasses_pointer_access(self):
        self.assertEqual(0, self.native._keyboard(-1, 0x100, 0))
        self.assertEqual(0, self.native._mouse(-1, 0x201, 0))

    def test_ctrl_enter_rejects_pending_word_without_injecting_modified_backspace(self):
        self.native.controller = Controller(Settings(instant_trigger=False, escalate=False))
        for vk in map(ord, "FUCK"):
            self.assertEqual(self.key(vk), 0)
            self.assertEqual(self.key(vk, down=False), 0)
        self.assertEqual(self.key(0xA2), 0)
        self.assertEqual(self.key(0x0D), 1)
        self.native.worker.send.assert_any_call("penalty")
        self.native._erase.assert_not_called()
        self.assertEqual(self.key(0xA2, down=False), 0)

    def test_native_erase_refuses_to_delete_with_control_held(self):
        self.native.down.add(0xA2)
        _Native._erase(self.native, 3, (1, 2, 3))
        self.native.user.SendInput.assert_not_called()

    def test_matching_backspace_is_suppressed_and_original_text_alone_is_erased(self):
        from mute_profanity.core import Matcher

        for vk in map(ord, "NOVELX"):
            self.assertEqual(self.key(vk), 0)
            self.assertEqual(self.key(vk, down=False), 0)
        settings = Settings(extra_words=["novel"], escalate=False)
        self.native.controller.update_rules(settings, Matcher(settings))
        self.assertEqual(self.key(0x08), 1)
        self.native._erase.assert_called_once_with(6, (1, 2, 3))
        self.assertEqual(self.key(0x08, down=False), 1)

    def test_modifier_held_at_startup_has_no_stuck_generic_alias(self):
        from mute_profanity import windows

        user = Mock()
        held = {0x10, 0x11, 0x12, 0xA0, 0xA2, 0xA4}
        user.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk in held else 0
        user.GetKeyState.return_value = 0
        with patch.object(windows.ctypes, "WinDLL", return_value=user, create=True), \
             patch.object(windows.ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
            native = _Native(Controller(), Mock(), Mock())
        self.assertEqual(native.down.intersection({0x10, 0x11, 0x12}), set())
        self.assertTrue({0xA0, 0xA2, 0xA4} <= native.down)
        self.native.down = native.down
        for vk in (0xA0, 0xA2, 0xA4):
            self.key(vk, down=False)
        self.assertFalse(self.native.down)
        self.key(ord("A"))
        self.assertEqual(self.native.controller.text, "a")


class WindowsReadinessTests(unittest.TestCase):
    def test_readiness_requires_a_live_mutex_and_signaled_event(self):
        from mute_profanity import windows

        kernel = Mock()
        kernel.OpenMutexW.return_value = 11
        kernel.OpenEventW.return_value = 22
        kernel.WaitForSingleObject.return_value = 0
        with patch.object(windows.sys, "platform", "win32"), patch.object(windows, "_kernel", return_value=kernel):
            self.assertTrue(windows.is_ready())
            kernel.OpenEventW.assert_called_with(0x00100000, False, _READY_EVENT)
            kernel.WaitForSingleObject.assert_called_with(22, 0)
            kernel.CloseHandle.assert_any_call(11)
            kernel.CloseHandle.assert_any_call(22)
            kernel.WaitForSingleObject.return_value = 258  # WAIT_TIMEOUT
            self.assertFalse(windows.is_ready())
            kernel.OpenEventW.return_value = 0
            self.assertFalse(windows.is_ready())
            kernel.OpenMutexW.return_value = 0
            self.assertFalse(windows.is_ready())

    def native(self):
        from mute_profanity import windows

        user = Mock()
        user.GetAsyncKeyState.return_value = 0
        user.GetKeyState.return_value = 0
        kernel = Mock()
        kernel.CreateEventW.side_effect = [101, 102, 103]
        user.SetWindowsHookExW.side_effect = [201, 202]
        user.GetMessageW.return_value = 0
        with patch.object(windows.ctypes, "WinDLL", return_value=user, create=True), \
             patch.object(windows.ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
            native = _Native(Controller(), Mock(), kernel)
        return native, user, kernel

    def test_ready_is_published_after_both_hooks_and_reset_before_unhook(self):
        from mute_profanity import windows

        native, user, kernel = self.native()
        events = []

        def set_event(handle):
            if handle == 103:
                self.assertEqual((native.keyboard_hook, native.mouse_hook), (201, 202))
                events.append("ready")
            return 1

        kernel.SetEvent.side_effect = set_event
        kernel.ResetEvent.side_effect = lambda handle: events.append("reset") if handle == 103 else None
        user.UnhookWindowsHookEx.side_effect = lambda _handle: events.append("unhook")
        with patch.object(windows.threading, "Thread"):
            self.assertEqual(native.run(), 0)
        self.assertEqual(events, ["reset", "ready", "reset", "unhook", "unhook"])
        self.assertIsNone(native.ready_event)
        kernel.CreateEventW.assert_any_call(None, True, False, _READY_EVENT)
        kernel.CloseHandle.assert_any_call(103)

    def test_failed_mouse_hook_never_publishes_readiness_and_cleans_keyboard_hook(self):
        from mute_profanity import windows

        native, user, kernel = self.native()
        user.SetWindowsHookExW.side_effect = [201, 0]
        with patch.object(windows.threading, "Thread"), \
             patch.object(windows.ctypes, "get_last_error", return_value=5, create=True), \
             patch.object(windows.ctypes, "WinError", side_effect=lambda code: OSError(code, "denied"), create=True):
            with self.assertRaises(OSError):
                native.run()
        self.assertNotIn(((103,), {}), [(call.args, call.kwargs) for call in kernel.SetEvent.call_args_list])
        user.UnhookWindowsHookEx.assert_called_once_with(201)
        kernel.CloseHandle.assert_any_call(103)

    def test_control_refresh_is_explicit_and_stop_during_startup_never_claims_ready(self):
        from mute_profanity import windows

        native, user, kernel = self.native()
        kernel.WaitForMultipleObjects.side_effect = [1, 0]
        with patch.object(windows.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            self.assertEqual(native.run(), 0)
        native.worker.send.assert_called_once_with("refresh")
        self.assertTrue(native.stopping.is_set())
        self.assertNotIn(103, [call.args[0] for call in kernel.SetEvent.call_args_list])


class WindowsWorkerTests(unittest.TestCase):
    def run_messages(self, controller, messages, *, save_error=None):
        from mute_profanity import windows

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(windows.threading, "Thread"), \
             patch.object(windows.config, "wordlist_dir", return_value=Path(directory)), \
             patch.object(windows.config, "load", return_value=Settings()), \
             patch.object(windows.config, "save", side_effect=save_error):
            worker = windows._Worker(controller)
            for message in [*messages, "stop"]:
                worker.messages.put(message)
            worker._run()

    def test_failed_pause_save_and_background_reload_preserve_emergency_pause(self):
        controller = Controller()
        controller.local_pause_until = time.time() + 300
        self.run_messages(controller, ["pause", "reload"], save_error=OSError("read-only"))
        self.assertFalse(controller.active())

    def test_external_settings_refresh_can_resume_an_emergency_pause(self):
        controller = Controller()
        controller.local_pause_until = time.time() + 300
        self.run_messages(controller, ["reload", "refresh"])
        self.assertTrue(controller.active())


if __name__ == "__main__":
    unittest.main()
