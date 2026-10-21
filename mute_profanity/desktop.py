"""Source management and diagnostics. Never replaces the user's existing layouts."""

import os
import sys

import gi
gi.require_version("IBus", "1.0")
from gi.repository import Gio, GLib, IBus

from .install import SERVICE, paths

SOURCES = (("ibus", "mute-en"), ("ibus", "mute-ru"))


def source_settings():
    return Gio.Settings.new("org.gnome.desktop.input-sources")


def enable():
    IBus.init()
    bus = IBus.Bus.new()
    if not bus.is_connected():
        raise RuntimeError("IBus недоступен. Выполните команду в терминале своего сеанса GNOME.")
    names = {engine.get_name() for engine in bus.list_engines()}
    if not all(name in names for _, name in SOURCES):
        raise RuntimeError("Источники ещё не обнаружены IBus. После установки выйдите из сеанса и войдите снова.")
    settings = source_settings()
    sources = [tuple(pair) for pair in settings.get_value("sources").unpack()]
    for pair in SOURCES:
        if pair not in sources:
            sources.append(pair)
    if not settings.set_value("sources", GLib.Variant("a(ss)", sources)):
        raise RuntimeError("GNOME не разрешил изменить источники ввода.")
    Gio.Settings.sync()
    print("Добавлены Тише EN и Тише RU. Выберите Тише через Super+Space в верхней панели.")
    print("Обычные источники EN/RU обходят фильтр. Ctrl+Shift+F12 — пауза на пять минут.")
    return 0


def disable():
    settings = source_settings()
    sources = [tuple(pair) for pair in settings.get_value("sources").unpack()]
    retained = [pair for pair in sources if pair not in SOURCES]
    if not retained:
        retained = [("xkb", "us"), ("xkb", "ru")]
    if not settings.set_value("sources", GLib.Variant("a(ss)", retained)):
        raise RuntimeError("Не удалось убрать источники Тише; приложение пока оставлено установленным.")
    Gio.Settings.sync()
    print("Источники Тише убраны; обычные раскладки доступны.")
    return 0


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
    IBus.init()
    bus = IBus.Bus.new()
    if not bus.is_connected():
        print("IBus: недоступен из этого процесса; проверка живого ввода невозможна.")
        return 1
    names = {engine.get_name() for engine in bus.list_engines()}
    for _, name in SOURCES:
        print(f"{name}: {'зарегистрирован' if name in names else 'не зарегистрирован'}")
    try:
        print(f"Источники GNOME: {source_settings().get_value('sources').unpack()}")
    except GLib.Error:
        pass
    print("Фильтрация работает только при выбранном Тише в приложении, которое использует IBus.")
    return 0 if all(name in names for _, name in SOURCES) else 1


def main(command):
    try:
        return {"doctor": doctor, "enable": enable, "disable": disable}[command]()
    except (OSError, RuntimeError, GLib.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
