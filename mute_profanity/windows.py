"""Windows 10/11 keyboard guard, using only the Python standard library.

The testable Controller knows nothing about Win32. Native DLLs are loaded only
when the Windows backend starts. Hooks retain at most the current word, never
log text, and do no file access or process creation inside their callbacks.

Unlike an input method, a low-level hook sees keys already destined for an
application. It suppresses the triggering key and removes only a tracked,
contiguous prefix in the same foreground/focus/layout. Mouse activity,
shortcuts, navigation, dead keys and uncertain translations invalidate that
prefix. Pasted/IME-generated text and application-side transformations are not
verified. Native password Edit controls are bypassed; browser password fields
cannot reliably be identified through these Win32 APIs. Elevated applications
may reject SendInput from a normal user process.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import ctypes
import math
import queue
import subprocess
import sys
import threading
import time
import unicodedata

from . import config
from .core import Cooldown, Matcher, MAX_COMPOSITION
from .wordlists import WordLists
from .launch import command


MUTEX_NAME = "Local\\MuteProfanity.Background"
_STOP_EVENT = "Local\\MuteProfanity.Stop"
_RELOAD_EVENT = "Local\\MuteProfanity.Reload"
_READY_EVENT = "Local\\MuteProfanity.Ready"
_INJECTION_TAG = 0x4D555445


@dataclass(frozen=True, slots=True)
class KeyEvent:
    """One key; focus is an opaque native foreground/control/layout token."""

    text: str = ""
    key: str = "text"
    focus: object = None
    down: bool = True
    shortcut: bool = False
    injected: bool = False
    private: bool = False
    can_erase: bool = True
    emergency: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    suppress: bool = False
    erase: int = 0
    seconds: int = 0
    pause: bool = False
    reload: bool = False


class Controller:
    """Bounded policy for keys forwarded to an ordinary text field.

    ``erase`` counts only previously forwarded, single BMP characters; the
    matching current key has not been forwarded. Native code rechecks focus
    immediately before sending these Backspaces. A focus/mouse/edit reset
    forfeits deletion rather than risking text from an earlier context.
    """

    def __init__(self, settings=None, matcher=None, cooldown=None):
        settings = settings or config.Settings()
        self._rules = (settings, matcher or Matcher(settings))
        self.cooldown = cooldown or Cooldown()
        self.local_pause_until = 0.0
        self.text = ""
        self.focus = None
        self.erasable = True
        self.suspended = False

    @property
    def settings(self):
        return self._rules[0]

    def update_rules(self, settings, matcher, *, explicit_refresh=False):
        # One immutable-pair assignment; the hook never waits for a disk worker.
        self._rules = (settings, matcher)
        # Only an external settings command acknowledges a deliberate Resume.
        # Ordinary word-triggered reloads must preserve an emergency pause when
        # saving it to disk failed and the on-disk config is still unpaused.
        if explicit_refresh and settings.paused_until <= time.time():
            self.local_pause_until = 0.0
        if not settings.enabled or settings.paused_until > time.time():
            self.cooldown.cancel()

    def reset(self):
        self.text = ""
        self.focus = None
        self.erasable = True
        self.suspended = False

    def active(self, wall_time=None):
        wall_time = time.time() if wall_time is None else wall_time
        settings = self.settings
        return (settings.enabled and settings.paused_until <= wall_time
                and self.local_pause_until <= wall_time)

    def blocking(self, now=None, wall_time=None):
        return self.active(wall_time) and self.cooldown.remaining(now) > 0

    def _reject(self, event, settings, now):
        erase = (len(self.text) if self.erasable and event.can_erase
                 and not event.shortcut and self.focus == event.focus else 0)
        seconds = self.cooldown.hit(settings, now)
        self.reset()
        return Decision(True, erase, seconds, reload=True)

    def process(self, event: KeyEvent, *, now=None, wall_time=None) -> Decision:
        wall_time = time.time() if wall_time is None else wall_time
        if event.emergency and event.down:
            self.local_pause_until = wall_time + 300
            self.cooldown.cancel()
            self.reset()
            return Decision(suppress=True, pause=True)
        if not event.down:
            return Decision()
        if self.blocking(now, wall_time):
            self.reset()
            return Decision(suppress=True, reload=True)
        if not self.active(wall_time) or event.private or event.injected:
            self.reset()
            return Decision(reload=not event.private and not event.injected)
        if event.focus != self.focus:
            self.reset()
            self.focus = event.focus
        if event.key == "modifier":
            return Decision()
        settings, matcher = self._rules
        # Modifier presses alone do not move the caret. Preserve the tracked
        # word until the actual shortcut so Ctrl+Enter cannot bypass checking.
        if event.key == "boundary" or (event.text and event.text.isspace()):
            if self.text and matcher.detect(self.text):
                return self._reject(event, settings, now)
            self.reset()
            return Decision(reload=True)
        if event.shortcut:
            self.reset()
            return Decision(reload=True)
        if event.key == "backspace":
            if self.text:
                candidate = self.text[:-1]
                if (settings.instant_trigger and matcher.detect(candidate)
                        and not matcher.allows_completion(candidate)):
                    # The physical Backspace is withheld. Erase the original
                    # tracked prefix, including the character it would remove.
                    return self._reject(event, settings, now)
                self.text = candidate
            return Decision()
        if event.key in {"navigation", "dead", "unknown"}:
            self.reset()
            return Decision(reload=True)
        # A single BMP, non-combining character has a predictable Backspace
        # unit in ordinary Windows text controls. Everything else invalidates
        # deletion tracking (including ligatures and surrogate pairs).
        if (len(event.text) != 1 or ord(event.text) > 0xFFFF
                or unicodedata.category(event.text)[0] in "CMZ"):
            self.reset()
            return Decision()
        if self.suspended:
            return Decision()
        if len(self.text) >= MAX_COMPOSITION:
            self.text = ""
            self.erasable = False
            self.suspended = True
            return Decision()
        was_empty = not self.text
        candidate = self.text + event.text
        if (settings.instant_trigger and matcher.detect(candidate)
                and not matcher.allows_completion(candidate)):
            return self._reject(event, settings, now)
        self.text = candidate
        self.focus = event.focus
        self.erasable = self.erasable and event.can_erase and event.focus is not None
        return Decision(reload=was_empty)


class _Worker:
    """Disk and popup operations stay off the hook/message thread."""

    def __init__(self, controller):
        self.controller = controller
        self.messages = queue.SimpleQueue()
        self.reload_pending = threading.Event()
        self.popup = None
        self.thread = threading.Thread(target=self._run, name="Mute configuration", daemon=True)
        self.thread.start()

    def send(self, kind):
        if kind == "reload":
            if self.reload_pending.is_set():
                return
            self.reload_pending.set()
        self.messages.put(kind)

    def _close_popup(self):
        if self.popup is not None:
            if self.popup.poll() is None:
                self.popup.terminate()
            self.popup = None

    def _run(self):
        previous = None
        lists = WordLists(config.wordlist_dir())
        while True:
            kind = self.messages.get()
            try:
                if kind == "stop":
                    self._close_popup()
                    return
                if kind == "pause":
                    self._close_popup()
                    current = config.load()
                    config.save(replace(current, paused_until=self.controller.local_pause_until))
                elif kind == "penalty":
                    self._close_popup()
                    seconds = math.ceil(self.controller.cooldown.remaining())
                    if seconds > 0:
                        self.popup = subprocess.Popen(
                            command("--intervention", str(seconds)),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=0x08000000,
                        )
                elif kind in {"reload", "refresh"}:
                    current = config.load()
                    # Directory mtime catches additions/removals, file mtimes
                    # catch edits. Only word events cause these small stats.
                    try:
                        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                                          for p in sorted(config.wordlist_dir().glob("*.txt")))
                    except OSError:
                        signature = ()
                    version = (current, signature)
                    changed = version != previous
                    if changed:
                        lists.reload()
                    if changed or kind == "refresh":
                        matcher = Matcher(current, lists) if changed else self.controller._rules[1]
                        self.controller.update_rules(current, matcher, explicit_refresh=kind == "refresh")
                        if not self.controller.active():
                            self._close_popup()
                        previous = version
            except (OSError, ValueError, RuntimeError):
                # An emergency pause already took effect in memory. Disk or
                # popup failures must never make the input hook stop responding.
                pass
            finally:
                if kind == "reload":
                    self.reload_pending.clear()

    def close(self):
        self.send("stop")
        self.thread.join(timeout=2)


# Fixed-width Win32 types, including on non-Windows hosts running policy tests.
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_LONG = ctypes.c_int32
_PTR = ctypes.c_void_p
_ULONG_PTR = ctypes.c_size_t
_LRESULT = ctypes.c_ssize_t


class _POINT(ctypes.Structure):
    _fields_ = [("x", _LONG), ("y", _LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", _LONG), ("top", _LONG), ("right", _LONG), ("bottom", _LONG)]


class _KEYBOARD(ctypes.Structure):
    _fields_ = [("vkCode", _DWORD), ("scanCode", _DWORD), ("flags", _DWORD),
                ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _MOUSE(ctypes.Structure):
    _fields_ = [("pt", _POINT), ("mouseData", _DWORD), ("flags", _DWORD),
                ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", _WORD), ("wScan", _WORD), ("dwFlags", _DWORD),
                ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", _LONG), ("dy", _LONG), ("mouseData", _DWORD),
                ("dwFlags", _DWORD), ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", _DWORD), ("value", _INPUT_UNION)]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", _DWORD), ("flags", _DWORD), ("hwndActive", _PTR),
                ("hwndFocus", _PTR), ("hwndCapture", _PTR), ("hwndMenuOwner", _PTR),
                ("hwndMoveSize", _PTR), ("hwndCaret", _PTR), ("rcCaret", _RECT)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", _PTR), ("message", _DWORD), ("wParam", _ULONG_PTR),
                ("lParam", _LRESULT), ("time", _DWORD), ("pt", _POINT), ("lPrivate", _DWORD)]


def _signature(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


def _kernel():
    if sys.platform != "win32":
        raise RuntimeError("The native keyboard backend requires Windows 10 or 11")
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    _signature(library, "CreateMutexW", [_PTR, ctypes.c_int, ctypes.c_wchar_p], _PTR)
    _signature(library, "OpenMutexW", [_DWORD, ctypes.c_int, ctypes.c_wchar_p], _PTR)
    _signature(library, "CloseHandle", [_PTR], ctypes.c_int)
    _signature(library, "GetModuleHandleW", [ctypes.c_wchar_p], _PTR)
    _signature(library, "GetCurrentThreadId", [], _DWORD)
    _signature(library, "CreateEventW", [_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p], _PTR)
    _signature(library, "OpenEventW", [_DWORD, ctypes.c_int, ctypes.c_wchar_p], _PTR)
    _signature(library, "SetEvent", [_PTR], ctypes.c_int)
    _signature(library, "ResetEvent", [_PTR], ctypes.c_int)
    _signature(library, "WaitForSingleObject", [_PTR, _DWORD], _DWORD)
    _signature(library, "WaitForMultipleObjects", [_DWORD, ctypes.POINTER(_PTR), ctypes.c_int, _DWORD], _DWORD)
    return library


def is_running():
    if sys.platform != "win32":
        return False
    kernel = _kernel()
    handle = kernel.OpenMutexW(0x00100000, False, MUTEX_NAME)
    if not handle:
        return False
    kernel.CloseHandle(handle)
    return True


def is_ready():
    """Return true only after the current guard has installed both input hooks."""
    if sys.platform != "win32" or not is_running():
        return False
    kernel = _kernel()
    handle = kernel.OpenEventW(0x00100000, False, _READY_EVENT)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0  # WAIT_OBJECT_0
    finally:
        kernel.CloseHandle(handle)


def _signal_control(name):
    if sys.platform != "win32":
        return False
    kernel = _kernel()
    handle = kernel.OpenEventW(0x0002, False, name)  # EVENT_MODIFY_STATE
    if not handle:
        return False
    try:
        return bool(kernel.SetEvent(handle))
    finally:
        kernel.CloseHandle(handle)


def stop() -> bool:
    """Ask this session's guard to exit and unhook, without killing a process."""
    return _signal_control(_STOP_EVENT)


def refresh() -> bool:
    """Reload after a settings change without waiting for another typed word."""
    return _signal_control(_RELOAD_EVENT)


class _Native:
    _MODIFIERS = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0x90, 0x91,
                  0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
    _NAVIGATION = {0x1B, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                   0x2D, 0x2E, 0x5D}

    def __init__(self, controller, worker, kernel):
        self.controller, self.worker, self.kernel = controller, worker, kernel
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, _ULONG_PTR, _LRESULT)
        signatures = {
            "SetWindowsHookExW": ([ctypes.c_int, self.callback_type, _PTR, _DWORD], _PTR),
            "UnhookWindowsHookEx": ([_PTR], ctypes.c_int),
            "CallNextHookEx": ([_PTR, ctypes.c_int, _ULONG_PTR, _LRESULT], _LRESULT),
            "GetMessageW": ([ctypes.POINTER(_MSG), _PTR, _DWORD, _DWORD], ctypes.c_int),
            "TranslateMessage": ([ctypes.POINTER(_MSG)], ctypes.c_int),
            "DispatchMessageW": ([ctypes.POINTER(_MSG)], _LRESULT),
            "PeekMessageW": ([ctypes.POINTER(_MSG), _PTR, _DWORD, _DWORD, _DWORD], ctypes.c_int),
            "PostThreadMessageW": ([_DWORD, _DWORD, _ULONG_PTR, _LRESULT], ctypes.c_int),
            "GetForegroundWindow": ([], _PTR),
            "GetWindowThreadProcessId": ([_PTR, ctypes.POINTER(_DWORD)], _DWORD),
            "GetKeyboardLayout": ([_DWORD], _PTR),
            "GetGUIThreadInfo": ([_DWORD, ctypes.POINTER(_GUITHREADINFO)], ctypes.c_int),
            "GetClassNameW": ([_PTR, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
            "GetKeyState": ([ctypes.c_int], ctypes.c_int16),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_int16),
            "ToUnicodeEx": ([_DWORD, _DWORD, ctypes.POINTER(ctypes.c_ubyte),
                             ctypes.c_wchar_p, ctypes.c_int, _DWORD, _PTR], ctypes.c_int),
            "SendInput": ([_DWORD, ctypes.POINTER(_INPUT), ctypes.c_int], _DWORD),
        }
        # GetWindowLongPtrW is a macro alias to GetWindowLongW on 32-bit Windows.
        self.style_name = "GetWindowLongPtrW" if ctypes.sizeof(_PTR) == 8 else "GetWindowLongW"
        signatures[self.style_name] = ([_PTR, ctypes.c_int], _LRESULT)
        for name, (arguments, result) in signatures.items():
            _signature(self.user, name, arguments, result)
        # LL keyboard events identify left/right modifiers. Recording generic
        # aliases here would leave them held forever after the specific keyup.
        self.down = {vk for vk in self._MODIFIERS - {0x10, 0x11, 0x12}
                     if self.user.GetAsyncKeyState(vk) & 0x8000}
        self.forwarded = set()
        self.suppressed = set()
        self.mouse_forwarded = set()
        self.mouse_suppressed = set()
        self.caps = bool(self.user.GetKeyState(0x14) & 1)
        self.keyboard_hook = self.mouse_hook = None
        self.stop_event = self.reload_event = self.ready_event = None
        self.stopping = threading.Event()
        self.control_thread = None
        # Keep callback objects alive for the whole installed-hook lifetime.
        self.keyboard_callback = self.callback_type(self._keyboard)
        self.mouse_callback = self.callback_type(self._mouse)

    def _next(self, code, message, pointer):
        return self.user.CallNextHookEx(None, code, message, pointer)

    def _focus(self):
        foreground = self.user.GetForegroundWindow()
        thread = self.user.GetWindowThreadProcessId(foreground, None) if foreground else 0
        info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
        focused = info.hwndFocus if thread and self.user.GetGUIThreadInfo(thread, ctypes.byref(info)) else None
        layout = self.user.GetKeyboardLayout(thread)
        private = False
        if focused:
            name = ctypes.create_unicode_buffer(128)
            if self.user.GetClassNameW(focused, name, len(name)):
                class_name = name.value.casefold()
                if class_name == "edit" or class_name.startswith("richedit"):
                    private = bool(getattr(self.user, self.style_name)(focused, -16) & 0x20)
        return (foreground, focused, layout), private

    def _translate(self, data, layout):
        state = (ctypes.c_ubyte * 256)()
        for vk in self.down:
            if 0 <= vk < 256:
                state[vk] |= 0x80
        for generic, variants in ((0x10, (0xA0, 0xA1)), (0x11, (0xA2, 0xA3)), (0x12, (0xA4, 0xA5))):
            if any(vk in self.down for vk in variants):
                state[generic] |= 0x80
        state[0x14] = int(self.caps)
        text = ctypes.create_unicode_buffer(8)
        # NO_STATE_CHANGE (Windows 10 1607+) prevents consuming the target
        # application's dead-key state just by observing a key.
        count = self.user.ToUnicodeEx(data.vkCode, data.scanCode, state, text, len(text), 4, layout)
        if count < 0:
            return "dead", ""
        if count <= 0 or count > len(text):
            return "unknown", ""
        return "text", text[:count]

    def _erase(self, count, focus):
        if not count or count > MAX_COMPOSITION:
            return
        current, private = self._focus()
        if current != focus or private or any(vk in self.down for vk in (0x11, 0x12, 0x5B, 0x5C, 0xA2, 0xA3, 0xA4, 0xA5)):
            return
        inputs = (_INPUT * (count * 2))()
        for index in range(count * 2):
            inputs[index].type = 1  # INPUT_KEYBOARD
            inputs[index].ki = _KEYBDINPUT(wVk=0x08, dwFlags=2 if index % 2 else 0,
                                           dwExtraInfo=_INJECTION_TAG)
        # Never retry a partial insertion: that could delete unrelated text.
        self.user.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT))

    def _keyboard(self, code, message, pointer):
        if code < 0:
            return self._next(code, message, pointer)
        try:
            data = ctypes.cast(pointer, ctypes.POINTER(_KEYBOARD)).contents
            if data.flags & 0x10 and data.dwExtraInfo == _INJECTION_TAG:
                return self._next(code, message, pointer)
            vk, down = int(data.vkCode), message in (0x0100, 0x0104)
            was_down = vk in self.down
            if down:
                self.down.add(vk)
            else:
                self.down.discard(vk)
            if not down:
                # Always release keys the client saw pressed, even if a
                # cooldown began while Shift or another key was held down.
                blocked = vk in self.suppressed and vk not in self.forwarded
                self.suppressed.discard(vk)
                self.forwarded.discard(vk)
                return 1 if blocked else self._next(code, message, pointer)
            if vk == 0x14 and not was_down:
                self.caps = not self.caps
            ctrl = bool(self.down.intersection((0x11, 0xA2, 0xA3)))
            shift = bool(self.down.intersection((0x10, 0xA0, 0xA1)))
            alt = bool(self.down.intersection((0x12, 0xA4, 0xA5)))
            win = bool(self.down.intersection((0x5B, 0x5C)))
            focus, private = self._focus()
            if vk in self._MODIFIERS:
                kind, text = "modifier", ""
            elif vk == 0x08:
                kind, text = "backspace", ""
            elif vk in (0x09, 0x0D, 0x20):
                kind, text = "boundary", ""
            elif vk in self._NAVIGATION:
                kind, text = "navigation", ""
            else:
                kind, text = self._translate(data, focus[2])
            event = KeyEvent(text=text, key=kind, focus=focus, shortcut=ctrl or alt or win,
                             injected=bool(data.flags & 0x10), private=private,
                             can_erase=bool(focus[0] and focus[1]),
                             emergency=vk == 0x7B and ctrl and shift)
            decision = self.controller.process(event)
            if decision.erase:
                self._erase(decision.erase, focus)
            if decision.pause:
                self.worker.send("pause")
            elif decision.seconds:
                self.worker.send("penalty")
            if decision.reload:
                self.worker.send("reload")
            if decision.suppress:
                self.suppressed.add(vk)
                return 1
            self.forwarded.add(vk)
        except Exception:
            # Callback errors must never strand the user's keyboard or unwind
            # through a foreign C callback. No text is included in diagnostics.
            self.controller.reset()
        return self._next(code, message, pointer)

    def _mouse(self, code, message, pointer):
        if code < 0 or message == 0x0200:  # Negative code / mouse movement
            return self._next(code, message, pointer)
        try:
            data = ctypes.cast(pointer, ctypes.POINTER(_MOUSE)).contents
            if data.flags & 1 and data.dwExtraInfo == _INJECTION_TAG:
                return self._next(code, message, pointer)
            self.controller.reset()
            pairs = {0x0201: ("left", True), 0x0202: ("left", False),
                     0x0204: ("right", True), 0x0205: ("right", False),
                     0x0207: ("middle", True), 0x0208: ("middle", False),
                     0x020B: ("x", True), 0x020C: ("x", False)}
            if message in pairs:
                button, down = pairs[message]
                if not down:
                    blocked = button in self.mouse_suppressed and button not in self.mouse_forwarded
                    self.mouse_suppressed.discard(button)
                    self.mouse_forwarded.discard(button)
                    return 1 if blocked else self._next(code, message, pointer)
                if self.controller.blocking():
                    self.mouse_suppressed.add(button)
                    return 1
                self.mouse_forwarded.add(button)
            elif self.controller.blocking():
                return 1  # Wheel/tilt during the cooldown.
        except Exception:
            self.controller.reset()
        return self._next(code, message, pointer)

    def run(self):
        module = self.kernel.GetModuleHandleW(None)
        try:
            # Create the message queue before another process can request stop.
            message = _MSG()
            self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            thread_id = self.kernel.GetCurrentThreadId()
            self.stop_event = self.kernel.CreateEventW(None, False, False, _STOP_EVENT)
            self.reload_event = self.kernel.CreateEventW(None, False, False, _RELOAD_EVENT)
            self.ready_event = self.kernel.CreateEventW(None, True, False, _READY_EVENT)
            if not self.stop_event or not self.reload_event or not self.ready_event:
                raise ctypes.WinError(ctypes.get_last_error())
            self.kernel.ResetEvent(self.ready_event)
            def controls():
                handles = (_PTR * 2)(self.stop_event, self.reload_event)
                while True:
                    result = self.kernel.WaitForMultipleObjects(2, handles, False, 0xFFFFFFFF)
                    if result == 0:
                        self.stopping.set()
                        self.user.PostThreadMessageW(thread_id, 0x0012, 0, 0)  # WM_QUIT
                        return
                    if result == 1:
                        self.worker.send("refresh")
                    else:
                        return
            self.control_thread = threading.Thread(target=controls, name="Mute control", daemon=True)
            self.control_thread.start()
            self.keyboard_hook = self.user.SetWindowsHookExW(13, self.keyboard_callback, module, 0)
            if not self.keyboard_hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self.mouse_hook = self.user.SetWindowsHookExW(14, self.mouse_callback, module, 0)
            if not self.mouse_hook:
                raise ctypes.WinError(ctypes.get_last_error())
            if self.stopping.is_set():
                return 0
            if not self.kernel.SetEvent(self.ready_event):
                raise ctypes.WinError(ctypes.get_last_error())
            while True:
                result = self.user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    return 0
                self.user.TranslateMessage(ctypes.byref(message))
                self.user.DispatchMessageW(ctypes.byref(message))
        finally:
            if self.ready_event:
                self.kernel.ResetEvent(self.ready_event)
            for hook in (self.mouse_hook, self.keyboard_hook):
                if hook:
                    self.user.UnhookWindowsHookEx(hook)
            self.mouse_hook = self.keyboard_hook = None
            self.controller.reset()
            if self.stop_event:
                self.kernel.SetEvent(self.stop_event)
            if self.control_thread:
                self.control_thread.join(timeout=1)
            for handle in (self.stop_event, self.reload_event, self.ready_event):
                if handle:
                    self.kernel.CloseHandle(handle)
            self.stop_event = self.reload_event = self.ready_event = None


def main() -> int:
    """Run one background guard in the current Windows interactive session."""
    kernel = None
    mutex = None
    worker = None
    try:
        kernel = _kernel()
        ctypes.set_last_error(0)
        mutex = kernel.CreateMutexW(None, False, MUTEX_NAME)
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return 0
        settings = config.load()
        lists = WordLists(config.wordlist_dir())
        lists.reload()
        controller = Controller(settings, Matcher(settings, lists))
        worker = _Worker(controller)
        return _Native(controller, worker, kernel).run()
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as error:
        print(f"Could not start the Windows keyboard guard: {error}", file=sys.stderr)
        return 1
    finally:
        if worker:
            worker.close()
        if mutex and kernel:
            kernel.CloseHandle(mutex)
