"""Optional, lazily loaded XKB dead-key and Compose support.

The system's locale Compose table supplies the sequences, including user
extensions supported by libxkbcommon.  Importing this module loads no native
library and allocates no native state.  Create one Composer only when a dead
key or Multi_key is actually used, and close it when the engine is destroyed.
"""

from __future__ import annotations

import ctypes
import locale


_NOTHING, _COMPOSING, _COMPOSED, _CANCELLED = range(4)
_STATUS_NAMES = ("nothing", "composing", "composed", "cancelled")
_MAX_UTF8_BUFFER = 256


def is_compose_start(keyval: int) -> bool:
    """Recognize the X11 Compose key and standard/extended dead key ranges."""
    return keyval == 0xFF20 or 0xFE50 <= keyval <= 0xFE6F or 0xFE80 <= keyval <= 0xFE8C


def _load_library() -> ctypes.CDLL:
    try:
        library = ctypes.CDLL("libxkbcommon.so.0")
        signatures = {
            "xkb_context_new": ([ctypes.c_int], ctypes.c_void_p),
            "xkb_context_unref": ([ctypes.c_void_p], None),
            "xkb_compose_table_new_from_locale": (
                [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_void_p,
            ),
            "xkb_compose_table_unref": ([ctypes.c_void_p], None),
            "xkb_compose_state_new": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_void_p),
            "xkb_compose_state_unref": ([ctypes.c_void_p], None),
            "xkb_compose_state_feed": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
            "xkb_compose_state_get_status": ([ctypes.c_void_p], ctypes.c_int),
            "xkb_compose_state_get_utf8": (
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char), ctypes.c_size_t], ctypes.c_int,
            ),
            "xkb_compose_state_reset": ([ctypes.c_void_p], None),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result
        return library
    except (OSError, AttributeError) as error:
        raise RuntimeError("Compose support requires libxkbcommon.so.0 with Compose support") from error


class Composer:
    """Own one native context, locale table, and composition state.

    ``feed`` returns a status plus completed text, without emitting partial
    sequences.  Completed/cancelled state remains observable until ``reset``
    or the next ``feed``.  A new feed automatically starts a new sequence.
    Output requiring more than 255 UTF-8 bytes is cancelled instead of being
    truncated.  ``close`` is idempotent and is safe after partial initialization.
    """

    def __init__(self, locale_name: str | None = None) -> None:
        self._library: ctypes.CDLL | None = None
        self._context: int | None = None
        self._table: int | None = None
        self._state: int | None = None
        if locale_name is not None and not isinstance(locale_name, str):
            raise TypeError("locale_name must be a string or None")
        try:
            self._library = _load_library()
            library = self._library
            self._context = library.xkb_context_new(0)
            if not self._context:
                raise RuntimeError("Could not initialize the XKB Compose context")
            try:
                preferred_locale = locale_name or locale.setlocale(locale.LC_CTYPE)
            except locale.Error:
                preferred_locale = "C.UTF-8"
            for candidate in dict.fromkeys((preferred_locale, "C.UTF-8")):
                self._table = library.xkb_compose_table_new_from_locale(
                    self._context, candidate.encode("utf-8"), 0,
                )
                if self._table:
                    break
            if not self._table:
                raise RuntimeError("Could not load the locale or C.UTF-8 Compose table")
            self._state = library.xkb_compose_state_new(self._table, 0)
            if not self._state:
                raise RuntimeError("Could not initialize the XKB Compose state")
        except Exception:
            self.close()
            raise

    @property
    def composing(self) -> bool:
        return bool(
            self._state and self._library
            and self._library.xkb_compose_state_get_status(self._state) == _COMPOSING
        )

    def feed(self, keyval: int) -> tuple[str, str]:
        if not isinstance(keyval, int) or not 0 <= keyval <= 0xFFFFFFFF:
            raise ValueError("keyval must be an unsigned 32-bit X11 keysym")
        if not self._state or not self._library:
            raise RuntimeError("Composer is closed")
        library, state = self._library, self._state
        if library.xkb_compose_state_get_status(state) in (_COMPOSED, _CANCELLED):
            library.xkb_compose_state_reset(state)
        library.xkb_compose_state_feed(state, keyval)
        status = library.xkb_compose_state_get_status(state)
        if status not in range(4):
            self.reset()
            raise RuntimeError("libxkbcommon returned an unknown Compose status")
        if status != _COMPOSED:
            return _STATUS_NAMES[status], ""
        required = library.xkb_compose_state_get_utf8(state, None, 0)
        if not 0 < required < _MAX_UTF8_BUFFER:
            self.reset()
            return "cancelled", ""
        buffer = ctypes.create_string_buffer(required + 1)
        written = library.xkb_compose_state_get_utf8(state, buffer, len(buffer))
        if written != required:
            self.reset()
            return "cancelled", ""
        try:
            text = buffer.raw[:required].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            self.reset()
            return "cancelled", ""
        return "composed", text

    def reset(self) -> None:
        if self._state and self._library:
            self._library.xkb_compose_state_reset(self._state)

    def close(self) -> None:
        library = self._library
        if library:
            if self._state:
                library.xkb_compose_state_unref(self._state)
                self._state = None
            if self._table:
                library.xkb_compose_table_unref(self._table)
                self._table = None
            if self._context:
                library.xkb_context_unref(self._context)
                self._context = None

    def __del__(self) -> None:
        self.close()
