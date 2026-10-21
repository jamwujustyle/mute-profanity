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
        raise RuntimeError("IBus не ответил вовремя; повторите подключение в сеансе GNOME.")
    success, value = completed[0]
    if not success:
        raise RuntimeError("Не удалось получить состояние IBus.") from value
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
        raise RuntimeError("Тише ещё не установлен для этого пользователя. Выполните run.py install, затем подключите фильтр.")
    try:
        result = subprocess.run(["systemctl", "--user", "restart", SERVICE],
                                capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Не удалось загрузить методы Тише: IBus не перезапустился вовремя. "
                           "Повторите подключение из сеанса GNOME или выполните run.py doctor.") from error
    if result.returncode:
        raise RuntimeError("Не удалось перезапустить службу IBus. Подключите фильтр из своего сеанса GNOME; "
                           "для диагностики выполните run.py doctor. Источники ввода не изменены.")
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
    raise RuntimeError("IBus перезапущен, но методы Тише не появились. Повторите run.py install и подключение; "
                       "run.py doctor покажет состояние. Источники ввода не изменены.")


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
                raise RuntimeError("GNOME не разрешил изменить источники ввода; настройки оставлены прежними.")
        settings.apply()
    except (GLib.Error, RuntimeError):
        settings.revert()
        raise
    Gio.Settings.sync()


def enable():
    _ready_bus()
    _update_sources(enable=True)
    print("Русский и English подключены через Тише; порядок и остальные раскладки сохранены.")
    print("Super+Space — сменить язык. Ctrl+Shift+F12 — пауза на пять минут.")
    return 0


def disable():
    _update_sources(enable=False)
    print("Источники Тише заменены обычными US/RU; порядок и остальные раскладки сохранены.")
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


def doctor():
    import importlib.util
    from . import config
    from .wordlists import WordLists
    print(f"Сеанс: {os.environ.get('XDG_SESSION_TYPE', 'неизвестно')}")
    print(f"Рабочий стол: {os.environ.get('XDG_CURRENT_DESKTOP', 'неизвестно')}")
    for key in ("component", "dropin", "desktop"):
        print(f"{key}: {'есть' if paths()[key].is_file() else 'нет'}")
    print(f"GTK / gi: {'есть' if importlib.util.find_spec('gi') else 'нет'}")
    lists = WordLists(config.wordlist_dir())
    errors = lists.reload()
    print(f"Словари: {config.wordlist_dir()}")
    for error in errors:
        print(f"  {error}")
    state = status()
    print(f"Методы Тише: {'загружены' if state.engines_ready else 'не готовы'}")
    print(f"Источники GNOME: {list(state.sources)}")
    print(f"Текущий метод IBus: {state.global_engine or 'недоступен'}")
    print(f"Состояние ({state.code}): {state.message}")
    print("Ввод конкретного приложения нужно проверить отдельно; наличие настроек не подтверждает фильтрацию.")
    return 0 if state.ready else 1


def main(command):
    try:
        return {"doctor": doctor, "enable": enable, "disable": disable}[command]()
    except (OSError, RuntimeError, GLib.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
