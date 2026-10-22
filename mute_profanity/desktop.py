"""Reversible source activation and read-only desktop readiness diagnostics."""

from dataclasses import dataclass
import os
import subprocess
import sys
import time

import gi
gi.require_version("IBus", "1.0")
from gi.repository import Gio, GLib, IBus

from .install import SERVICE, paths
from .gnome_modal import request_enable, is_available

SOURCES = (("ibus", "mute-en"), ("ibus", "mute-ru"))
NATIVE_SOURCES = (("xkb", "us"), ("xkb", "ru"))
RECONNECT_SECONDS = 6


@dataclass(frozen=True)
class Status:
    code: str
    message: str
    sources: tuple[tuple[str, str], ...] = ()
    global_engine: str | None = None
    engines_ready: bool = False

    @property
    def ready(self) -> bool:
        return self.code == "source_active"


def source_settings():
    return Gio.Settings.new("org.gnome.desktop.input-sources")


def _bus_value(bus, method: str, timeout_ms: int = 750):
    """Bound a D-Bus query and let the IBus connection process its callbacks."""
    completed = []
    cancellable = Gio.Cancellable()

    def done(source, result, *_args):
        try:
            completed.append((True, getattr(source, method + "_async_finish")(result)))
        except GLib.Error as error:
            completed.append((False, error))

    getattr(bus, method + "_async")(timeout_ms, cancellable, done, None)
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout_ms / 1000 + 0.1
    while not completed and time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        else:
            time.sleep(0.01)
    if not completed:
        cancellable.cancel()
        raise RuntimeError("IBus did not respond in time. Try enabling the filter again in your GNOME session.")
    success, value = completed[0]
    if not success:
        raise RuntimeError("Could not read the IBus status.") from value
    return value


def _registry_names(bus) -> set[str]:
    return {engine.get_name() for engine in _bus_value(bus, "list_engines")}


def _registered(bus) -> bool:
    return bus.is_connected() and {name for _, name in SOURCES} <= _registry_names(bus)


def _ready_bus():
    IBus.init()
    bus = IBus.Bus.new()
    try:
        if _registered(bus):
            return bus
    except (GLib.Error, RuntimeError):
        pass
    locations = paths()
    if not locations["component"].is_file() or not locations["dropin"].is_file():
        raise RuntimeError("Mute is not installed for this user. Run run.py install, then enable the filter.")
    try:
        result = subprocess.run(["systemctl", "--user", "restart", SERVICE],
                                capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Could not load the Mute input methods: IBus did not restart in time. "
                           "Try enabling the filter from your GNOME session, or run run.py doctor.") from error
    if result.returncode:
        raise RuntimeError("Could not restart IBus. Enable the filter from your GNOME session, "
                           "or run run.py doctor for diagnostics. Input sources were not changed.")
    # IBus.Bus.new() is a singleton. Pumping the GLib context is necessary for
    # its socket monitor to notice the new daemon and reconnect after restart.
    context = GLib.MainContext.default()
    deadline = time.monotonic() + RECONNECT_SECONDS
    while time.monotonic() < deadline:
        while context.pending() and time.monotonic() < deadline:
            context.iteration(False)
        bus = IBus.Bus.new()
        try:
            if _registered(bus):
                return bus
        except (GLib.Error, RuntimeError):
            pass
        time.sleep(0.05)
    raise RuntimeError("IBus restarted, but the Mute input methods are missing. Run run.py install and enable "
                       "the filter again; run.py doctor shows diagnostics. Input sources were not changed.")


def _mapped(pairs, *, enable: bool, ensure: bool = False):
    mapping = dict(zip(NATIVE_SOURCES, SOURCES)) if enable else dict(zip(SOURCES, NATIVE_SOURCES))
    result = []
    seen = set()
    for pair in pairs:
        mapped = mapping.get(tuple(pair), tuple(pair))
        if mapped not in seen:
            result.append(mapped)
            seen.add(mapped)
    if ensure:
        for pair in SOURCES:
            if pair not in seen:
                result.append(pair)
                seen.add(pair)
    return result


def _update_sources(*, enable: bool):
    settings = source_settings()
    updates = {
        key: _mapped(settings.get_value(key).unpack(), enable=enable, ensure=enable and key == "sources")
        for key in ("mru-sources", "sources")
    }
    # Keep source references and their recent order together, or leave both
    # untouched when GNOME disallows one of the changes.
    settings.delay()
    try:
        for key, value in updates.items():
            if not settings.set_value(key, GLib.Variant("a(ss)", value)):
                raise RuntimeError("GNOME did not allow input-source changes. The previous settings were preserved.")
        settings.apply()
    except (GLib.Error, RuntimeError):
        settings.revert()
        raise
    Gio.Settings.sync()


def enable():
    _ready_bus()
    _update_sources(enable=True)
    modal_ready = request_enable()
    print("Russian and English now use Mute. Their order and other keyboard layouts were preserved.")
    print("Super+Space switches languages. Ctrl+Shift+F12 pauses the filter for five minutes.")
    if not modal_ready:
        print("Full desktop cooldown: sign out and back in once to load the GNOME extension.")
    return 0


def disable():
    _update_sources(enable=False)
    print("Mute input sources were replaced with ordinary US/RU layouts. Their order and other layouts were preserved.")
    return 0


def status() -> Status:
    """Report actual IBus/source readiness without changing any desktop state."""
    try:
        configured = tuple(tuple(pair) for pair in source_settings().get_value("sources").unpack())
    except GLib.Error:
        return Status("unavailable", "Настройки источников ввода GNOME недоступны.")
    IBus.init()
    bus = IBus.Bus.new()
    if not bus.is_connected():
        return Status("unavailable", "IBus недоступен. Подключите фильтр в своём сеансе GNOME.", configured)
    try:
        names = _registry_names(bus)
    except (GLib.Error, RuntimeError):
        return Status("unavailable", "Состояние IBus недоступно; подключите фильтр повторно.", configured)
    registered = {name for _, name in SOURCES} <= names
    try:
        engine = _bus_value(bus, "get_global_engine")
        selected = engine.get_name() if engine is not None else None
    except (GLib.Error, RuntimeError):
        # A freshly started daemon can have a registry before GNOME selects
        # any engine. It still needs setup/loading rather than a bus repair.
        if not bus.is_connected():
            return Status("unavailable", "IBus отключён; подключите фильтр повторно.", configured)
        selected = None
    if not registered:
        return Status("requires_load", "Методы Тише ещё не загружены. Подключите фильтр: IBus будет перезапущен автоматически.",
                      configured, selected)
    if not any(pair in SOURCES for pair in configured):
        return Status("requires_setup", "Фильтр ещё не подключён: сейчас настроены обычные источники ввода.",
                      configured, selected, True)
    if selected not in {name for _, name in SOURCES}:
        return Status("ordinary_source", "Сейчас выбран источник, который обходит Тише. Выберите Тише EN/RU через Super+Space.",
                      configured, selected, True)
    return Status("source_active", "Выбран источник Тише: ввод проходит через фильтр в приложениях с IBus.",
                  configured, selected, True)


def status_message_en(state: Status) -> str:
    """English terminal wording; Status.message remains the settings UI text."""
    return {
        "source_active": "The selected Mute source filters typing in applications that support IBus.",
        "requires_load": "Mute input methods are not loaded. Enable the filter to load them.",
        "requires_setup": "The filter is not connected; ordinary input sources are configured.",
        "ordinary_source": "The selected input source bypasses Mute. Select Mute EN/RU with Super+Space.",
        "unavailable": "GNOME input sources or IBus are unavailable. Enable the filter in your GNOME session.",
    }.get(state.code, "The filter status is unknown. Run run.py doctor for diagnostics.")


def doctor():
    import importlib.util
    from . import config
    from .wordlists import WordLists
    print(f"Session: {os.environ.get('XDG_SESSION_TYPE', 'unknown')}")
    print(f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', 'unknown')}")
    for key in ("component", "dropin", "desktop"):
        print(f"{key}: {'present' if paths()[key].is_file() else 'missing'}")
    print(f"GTK / gi: {'present' if importlib.util.find_spec('gi') else 'missing'}")
    lists = WordLists(config.wordlist_dir())
    errors = lists.reload()
    print(f"Dictionaries: {config.wordlist_dir()}")
    for error in errors:
        print(f"  {error}")
    state = status()
    print(f"Mute input methods: {'loaded' if state.engines_ready else 'not ready'}")
    print(f"GNOME sources: {list(state.sources)}")
    print(f"Current IBus method: {state.global_engine or 'unavailable'}")
    print(f"Status ({state.code}): {status_message_en(state)}")
    print(f"Full desktop cooldown: {'ready' if is_available() else 'pending GNOME extension login'}")
    print("Check typing in each application separately; configured sources alone do not confirm filtering.")
    return 0 if state.ready else 1


def main(command):
    try:
        return {"doctor": doctor, "enable": enable, "disable": disable}[command]()
    except (OSError, RuntimeError, GLib.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
