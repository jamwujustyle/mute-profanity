"""Transparent IBus key guard. Safe keys keep their native application flow."""

from dataclasses import replace
import math
import signal
import sys
import time
import unicodedata

import gi
gi.require_version("IBus", "1.0")
from gi.repository import Gio, GLib, GObject, IBus

from . import config
from .core import Composition, Cooldown, Matcher
from .compose import Composer, is_compose_start
from .wordlists import WordLists
from .launch import command
from .gnome_modal import try_show, try_hide

ENGINES = ("mute-en", "mute-ru")
BUS_NAME = "org.freedesktop.IBus.MuteProfanity"
SHORTCUT_MASK = int(IBus.ModifierType.CONTROL_MASK | IBus.ModifierType.MOD1_MASK |
                    IBus.ModifierType.MOD4_MASK | IBus.ModifierType.SUPER_MASK |
                    IBus.ModifierType.HYPER_MASK | IBus.ModifierType.META_MASK)
RELEASE_MASK = int(IBus.ModifierType.RELEASE_MASK)
MODIFIER_KEYS = {IBus.KEY_Shift_L, IBus.KEY_Shift_R, IBus.KEY_Control_L, IBus.KEY_Control_R,
                 IBus.KEY_Alt_L, IBus.KEY_Alt_R, IBus.KEY_Super_L, IBus.KEY_Super_R,
                 IBus.KEY_Meta_L, IBus.KEY_Meta_R, IBus.KEY_Caps_Lock,
                 IBus.KEY_Num_Lock, IBus.KEY_ISO_Level3_Shift}


class Runtime:
    def __init__(self, *, popup=True):
        self.settings = config.load()
        self.lists = WordLists(config.wordlist_dir())
        self.lists.reload()
        self.matcher = Matcher(self.settings, self.lists)
        self.cooldown = Cooldown()
        self.engines = set()
        self.popup = popup
        self.popup_pid = None
        self.reload_pending = False
        self.monitors = []
        for directory in (config.config_dir(), config.wordlist_dir()):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
                Gio.FileMonitorFlags.NONE, None)
            monitor.connect("changed", self._changed)
            self.monitors.append(monitor)

    def _changed(self, *_args):
        if not self.reload_pending:
            self.reload_pending = True
            GLib.idle_add(self.reload)

    def reload(self):
        self.reload_pending = False
        self.settings = config.load()
        self.lists.reload()
        self.matcher = Matcher(self.settings, self.lists)
        if not self.settings.enabled or self.settings.paused_until > time.time():
            self.cooldown.cancel()
            try_hide()
        for engine in tuple(self.engines):
            engine.composition.settings = self.settings
            engine.composition.matcher = self.matcher
        return GLib.SOURCE_REMOVE

    def active(self):
        return self.settings.enabled and self.settings.paused_until <= time.time()

    def pause(self):
        self.cooldown.cancel()
        try_hide()
        self.settings = replace(self.settings, paused_until=time.time() + 300)
        try:
            config.save(self.settings)
        except OSError:
            # The emergency shortcut must work even with a full/read-only disk.
            pass
        for engine in tuple(self.engines):
            engine.composition.clear()
            engine.reset_compose()
            engine.update_auxiliary_text(IBus.Text.new_from_string("Пауза на 5 минут"), True)

    def intervene(self, seconds):
        if self.popup and try_show(seconds):
            return
        if not self.popup or self.popup_pid is not None:
            return
        try:
            pid, *_ = GLib.spawn_async(
                command("--intervention", str(seconds)),
                flags=GLib.SpawnFlags.DO_NOT_REAP_CHILD |
                      GLib.SpawnFlags.STDOUT_TO_DEV_NULL | GLib.SpawnFlags.STDERR_TO_DEV_NULL)
            self.popup_pid = pid
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, self._popup_closed)
        except GLib.Error:
            # IBus auxiliary feedback still works without a graphical dialog.
            pass

    def _popup_closed(self, pid, _status):
        GLib.spawn_close_pid(pid)
        self.popup_pid = None


class MuteEngine(IBus.Engine):
    __gtype_name__ = "MuteProfanityEngine"

    def __init__(self, runtime, **kwargs):
        super().__init__(**kwargs)
        self.runtime = runtime
        self.composition = Composition(runtime.matcher, runtime.settings, runtime.cooldown)
        self.private = False
        self.capabilities = 0
        self.composer = None
        self.tick_id = 0
        runtime.engines.add(self)

    def reset_compose(self):
        if self.composer is not None:
            self.composer.reset()

    def after_edit(self):
        unsafe = self.composition.unsafe()
        if (self.runtime.settings.instant_trigger and unsafe
                and not self.runtime.matcher.allows_completion(self.composition.text)):
            return self.finish()
        return False

    def _feedback(self):
        remaining = self.runtime.cooldown.remaining()
        count = math.ceil(remaining)
        message = f"Watch your language. Try again in {count} {'second' if count == 1 else 'seconds'}."
        self.update_auxiliary_text(IBus.Text.new_from_string(message), remaining > 0)
        if remaining <= 0:
            self.tick_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def finish(self):
        result = self.composition.finish()
        if result.blocked:
            self.runtime.intervene(result.seconds)
            self._feedback()
            if not self.tick_id:
                self.tick_id = GLib.timeout_add_seconds(1, self._feedback)
        # Safe text has already reached the application through its original
        # key events. Re-committing it would replace autocomplete/selection.
        return result.blocked

    def do_process_key_event(self, keyval, keycode, state):
        if state & RELEASE_MASK:
            return False
        emergency = int(IBus.ModifierType.CONTROL_MASK | IBus.ModifierType.SHIFT_MASK)
        if keyval == IBus.KEY_F12 and state & emergency == emergency:
            self.runtime.pause()
            return True
        if self.private:
            return False
        if not self.runtime.active():
            self.reset_compose()
            self.composition.clear()
            return False
        if keyval in MODIFIER_KEYS:
            return False
        shortcut = bool(state & SHORTCUT_MASK)
        if self.runtime.cooldown.remaining() > 0:
            self._feedback()
            # Permit window management, navigation, editing and clipboard shortcuts.
            # Text and submit keys remain blocked in this input method.
            if shortcut and keyval not in (IBus.KEY_Return, IBus.KEY_KP_Enter):
                return False
            char = IBus.keyval_to_unicode(keyval)
            return bool(char or keyval in (IBus.KEY_Return, IBus.KEY_KP_Enter, IBus.KEY_Tab))
        if shortcut:
            self.reset_compose()
            if keyval in (IBus.KEY_Return, IBus.KEY_KP_Enter):
                return self.finish() if self.composition.text else False
            self.composition.clear()
            return False
        char = None
        composed = False
        if is_compose_start(keyval) or (self.composer and self.composer.composing):
            if self.composer and keyval in (IBus.KEY_Escape, IBus.KEY_BackSpace):
                self.reset_compose()
                return True
            try:
                if self.composer is None:
                    self.composer = Composer()
                status, composed_text = self.composer.feed(keyval)
            except RuntimeError:
                self.update_auxiliary_text(IBus.Text.new_from_string(
                    "Для Compose нужна системная библиотека libxkbcommon."), True)
                return True
            if status == "composing":
                return True
            if status == "composed":
                char = composed_text
                composed = True
                self.reset_compose()
            elif status == "cancelled":
                self.reset_compose()
            elif is_compose_start(keyval):
                return True
        if keyval == IBus.KEY_Escape:
            self.composition.clear()
            return False
        if keyval == IBus.KEY_BackSpace and self.composition.text:
            self.composition.backspace()
            self.after_edit()
            return False
        if keyval in (IBus.KEY_Delete, IBus.KEY_Left, IBus.KEY_Right, IBus.KEY_Home, IBus.KEY_End):
            self.composition.clear()
            return False
        if char is None:
            char = IBus.keyval_to_unicode(keyval)
        if composed and char and char.isspace():
            if not self.finish():
                self.commit_text(IBus.Text.new_from_string(char))
            return True
        if char and all(not c.isspace() and unicodedata.category(c) not in {"Cc", "Cs", "Cn"}
                        for c in char):
            if not self.composition.insert(char):
                # Validate a full buffer before any part can reach the client.
                if self.finish():
                    return True
                self.composition.insert(char)
            if self.after_edit():
                return True
            if composed:
                self.commit_text(IBus.Text.new_from_string(char))
                return True
            return False
        # Validate the tracked word without generating IME commits or preedit.
        return self.finish() if self.composition.text else False

    def do_set_content_type(self, purpose, hints):
        self.private = (purpose not in (IBus.InputPurpose.FREE_FORM, IBus.InputPurpose.ALPHA)
                        or bool(hints & IBus.InputHints.PRIVATE))
        self.composition.clear()
        self.reset_compose()

    def do_set_capabilities(self, caps):
        self.capabilities = caps

    def do_focus_in(self):
        self.composition.clear()
        self.reset_compose()
        purpose, hints = self.get_content_type()
        self.private = (purpose not in (IBus.InputPurpose.FREE_FORM, IBus.InputPurpose.ALPHA)
                        or bool(hints & IBus.InputHints.PRIVATE))
        if self.runtime.cooldown.remaining() > 0 and not self.private:
            self._feedback()

    def do_focus_out(self):
        # Never commit, delete or penalize text while focus is moving. The
        # native application owns its contents, selection and autocomplete.
        self.composition.clear()
        self.private = False
        self.reset_compose()

    def do_reset(self):
        self.composition.clear()
        self.reset_compose()

    def do_disable(self):
        self.composition.clear()
        self.reset_compose()

    def do_destroy(self):
        if self.tick_id:
            GLib.source_remove(self.tick_id)
            self.tick_id = 0
        self.runtime.engines.discard(self)
        self.composition.clear()
        if self.composer is not None:
            self.composer.close()
            self.composer = None
        IBus.Engine.do_destroy(self)


class Factory(IBus.Factory):
    def __init__(self, bus, runtime):
        super().__init__(object_path=IBus.PATH_FACTORY, connection=bus.get_connection())
        self.runtime = runtime
        self.serial = 0

    def do_create_engine(self, name):
        if name not in ENGINES:
            raise ValueError("Unknown input method")
        self.serial += 1
        return MuteEngine(self.runtime, engine_name=name,
                          object_path=f"/org/freedesktop/IBus/MuteProfanity/Engine/{self.serial}",
                          connection=self.get_connection())


def main():
    IBus.init()
    bus = IBus.Bus.new()
    if not bus.is_connected():
        print("IBus недоступен. Запустите приложение в сеансе GNOME; run.py doctor — диагностика.",
              file=sys.stderr)
        return 1
    runtime = Runtime()
    factory = Factory(bus, runtime)
    loop = GLib.MainLoop()
    bus.connect("disconnected", lambda *_: loop.quit())
    if not bus.request_name(BUS_NAME, 0):
        print("Не удалось зарегистрировать метод ввода.", file=sys.stderr)
        return 1
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (loop.quit(), False)[1])
    loop.run()
    factory.destroy()
    return 0
