"""On-demand native preferences and cooldown windows.

The input method runs separately. No GTK window or timer is needed while the
filter is quietly waiting for keyboard input.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import math
from pathlib import Path
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


_CSS = b"""
.page { padding: 22px; }
.page-title { font-size: 25px; font-weight: 700; }
.section-title { font-weight: 700; }
.muted { opacity: 0.72; }
.card { border: 1px solid alpha(@theme_fg_color, 0.13); border-radius: 10px;
        background-color: alpha(@theme_base_color, 0.65); }
.setting-row { padding: 10px 13px; }
.word-editor { padding: 8px; }
.status { padding: 12px; border-radius: 8px;
          background-color: alpha(@theme_selected_bg_color, 0.10); }
.intervention-title { font-size: 24px; font-weight: 700; }
"""


def _label(text: str, *, muted: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.set_line_wrap(True)
    if muted:
        label.get_style_context().add_class("muted")
    return label


def _button(label: str, callback, *, primary: bool = False) -> Gtk.Button:
    button = Gtk.Button.new_with_label(label)
    button.connect("clicked", callback)
    if primary:
        button.get_style_context().add_class("suggested-action")
    return button


def _words(view: Gtk.TextView) -> list[str]:
    buffer = view.get_buffer()
    text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    return list(dict.fromkeys(line.strip().casefold() for line in text.splitlines() if line.strip()))


def _card() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.get_style_context().add_class("card")
    return box


def _row(parent: Gtk.Box, title: str, widget: Gtk.Widget, description: str | None = None) -> None:
    if parent.get_children():
        parent.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=15)
    row.get_style_context().add_class("setting-row")
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    text.pack_start(_label(title), False, False, 0)
    if description:
        subtitle = _label(description, muted=True)
        subtitle.set_max_width_chars(42)
        text.pack_start(subtitle, False, False, 0)
    row.pack_start(text, True, True, 0)
    widget.set_valign(Gtk.Align.CENTER)
    row.pack_end(widget, False, False, 0)
    parent.pack_start(row, False, False, 0)


class Preferences(Gtk.Window):
    def __init__(self, *, playground: bool = False):
        super().__init__(title="Тише / Mute")
        from . import config

        self.config = config
        self.settings = config.load()
        self.set_default_size(600, 700)
        self.set_size_request(470, 420)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("destroy", self._destroy)
        header = Gtk.HeaderBar(title="Тише / Mute")
        header.set_subtitle("Меньше брани. Больше смысла.")
        header.set_show_close_button(True)
        self.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.pack_start(scroller, True, True, 0)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.get_style_context().add_class("page")
        scroller.add(page)

        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        title = _label("Привычку можно изменить")
        title.get_style_context().add_class("page-title")
        intro.pack_start(title, False, False, 0)
        intro.pack_start(_label("Фильтр останавливает ругательства во время набора и даёт короткую паузу.", muted=True), False, False, 0)
        page.pack_start(intro, False, False, 0)

        self.status = _label("")
        self.status.get_style_context().add_class("status")
        page.pack_start(self.status, False, False, 0)

        behavior = _card()
        self.enabled = Gtk.Switch(active=self.settings.enabled)
        _row(behavior, "Фильтр включён", self.enabled)
        self.cooldown = Gtk.SpinButton.new_with_range(3, 60, 1)
        self.cooldown.set_value(self.settings.cooldown_seconds)
        self.cooldown.set_numeric(True)
        self.cooldown.set_width_chars(3)
        _row(behavior, "Пауза после ругательства, с", self.cooldown)
        self.escalate = Gtk.Switch(active=self.settings.escalate)
        _row(behavior, "Увеличивать паузу при повторах", self.escalate, "Быстрые повторные попытки приводят к более долгой паузе.")
        self.beep = Gtk.Switch(active=self.settings.beep)
        _row(behavior, "Звуковой сигнал", self.beep)
        page.pack_start(behavior, False, False, 0)

        languages = _card()
        self.filter_ru = Gtk.Switch(active=self.settings.filter_ru)
        self.filter_en = Gtk.Switch(active=self.settings.filter_en)
        _row(languages, "Русская брань", self.filter_ru)
        _row(languages, "English profanity", self.filter_en)
        page.pack_start(languages, False, False, 0)

        self.allow_words = self._word_editor(page, "Разрешённые слова", "Исключения из фильтра; по одному слову на строке.", self.settings.allow_words)
        self.extra_words = self._word_editor(page, "Мои запрещённые слова", "Дополнительные слова; по одному слову на строке.", self.settings.extra_words)

        dictionaries = Gtk.Expander(label="Добавить язык или свой словарь")
        dictionary_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        dictionary_content.set_margin_top(10)
        dictionary_content.pack_start(_label("Поместите текстовый файл .txt в папку словарей. Любой язык: UTF-8, одно слово на строке. Строки с # — комментарии. Для корня слова добавьте * в конце; корень должен содержать минимум 3 буквы."), False, False, 0)
        dictionary_content.pack_start(_label("Новые файлы и изменения подхватываются автоматически. Проверить результат можно ниже.", muted=True), False, False, 0)
        dictionary_button = _button("Открыть словари", self._open_dictionaries)
        dictionary_button.set_halign(Gtk.Align.START)
        dictionary_content.pack_start(dictionary_button, False, False, 0)
        dictionaries.add(dictionary_content)
        page.pack_start(dictionaries, False, False, 0)

        setup = Gtk.Expander(label="Где работает фильтр и как переключать язык")
        instructions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        instructions.set_margin_top(10)
        instructions.pack_start(_label("Работает только с источниками ввода «Тише EN» / «Тише RU» в приложениях с IBus. Super+Space — сменить язык. Ctrl+Shift+F12 — пауза на 5 минут."), False, False, 0)
        instructions.pack_start(_label("Добавьте оба источника в настройках клавиатуры и выбирайте их при наборе. Сохранение этих настроек само по себе не меняет источник ввода.", muted=True), False, False, 0)
        add_sources = _button("Добавить Тише EN/RU", self._enable_sources)
        add_sources.set_halign(Gtk.Align.START)
        instructions.pack_start(add_sources, False, False, 0)
        keyboard = _button("Настройки клавиатуры", self._open_keyboard)
        keyboard.set_halign(Gtk.Align.START)
        instructions.pack_start(keyboard, False, False, 0)
        setup.add(instructions)
        page.pack_start(setup, False, False, 0)

        test = Gtk.Expander(label="Проверить слово")
        test.set_expanded(playground)
        test_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        test_content.set_margin_top(10)
        self.test_entry = Gtk.Entry()
        self.test_entry.set_property("im-module", "gtk-im-context-simple")
        self.test_entry.set_placeholder_text("Введите слово для локальной проверки")
        self.test_entry.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        self.test_entry.connect("activate", self._test_word)
        test_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        test_row.pack_start(self.test_entry, True, True, 0)
        test_row.pack_end(_button("Проверить", self._test_word), False, False, 0)
        test_content.pack_start(test_row, False, False, 0)
        self.test_result = _label("Проверка использует текущие настройки и ничего не сохраняет.", muted=True)
        test_content.pack_start(self.test_result, False, False, 0)
        test.add(test_content)
        page.pack_start(test, False, False, 0)

        privacy = _label("Полностью офлайн · Без ИИ и подписок\nТекст не записывается в журнал. В памяти хранится только текущее набираемое слово.", muted=True)
        privacy.set_justify(Gtk.Justification.LEFT)
        page.pack_start(privacy, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        footer.set_margin_start(18)
        footer.set_margin_end(18)
        footer.set_margin_top(12)
        footer.set_margin_bottom(12)
        self.message = _label("")
        footer.pack_start(self.message, False, False, 0)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.pause_button = _button("Пауза 5 минут", self._pause)
        self.resume_button = _button("Продолжить", self._resume)
        actions.pack_start(self.pause_button, False, False, 0)
        actions.pack_start(self.resume_button, False, False, 0)
        actions.pack_end(_button("Сохранить", self._save, primary=True), False, False, 0)
        footer.pack_start(actions, False, False, 0)
        outer.pack_start(footer, False, False, 0)
        self._refresh_status()
        self.status_timer = GLib.timeout_add_seconds(3, self._update_status)

    @staticmethod
    def _word_editor(parent: Gtk.Box, title: str, hint: str, words: list[str]) -> Gtk.TextView:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        heading = _label(title)
        heading.get_style_context().add_class("section-title")
        section.pack_start(heading, False, False, 0)
        section.pack_start(_label(hint, muted=True), False, False, 0)
        view = Gtk.TextView()
        view.set_property("im-module", "gtk-im-context-simple")
        view.get_buffer().set_text("\n".join(words))
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_accepts_tab(False)
        view.get_style_context().add_class("word-editor")
        scroll = Gtk.ScrolledWindow()
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.set_min_content_height(66)
        scroll.set_max_content_height(100)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)
        section.pack_start(scroll, False, False, 0)
        parent.pack_start(section, False, False, 0)
        return view

    def _read_settings(self):
        allow_words = _words(self.allow_words)
        extra_words = _words(self.extra_words)
        if len(allow_words) > 1000 or len(extra_words) > 1000:
            raise ValueError("В каждом списке может быть не больше 1000 слов.")
        if any(len(word) > 128 for word in allow_words + extra_words):
            raise ValueError("Слово не должно быть длиннее 128 символов.")
        if any(any(char.isspace() for char in word) for word in allow_words + extra_words):
            raise ValueError("Укажите по одному слову на строке, без пробелов внутри.")
        # Preserve fields changed by the background process since this window opened.
        current = self.config.load()
        return replace(current, enabled=self.enabled.get_active(), cooldown_seconds=self.cooldown.get_value_as_int(),
                       escalate=self.escalate.get_active(), filter_ru=self.filter_ru.get_active(),
                       filter_en=self.filter_en.get_active(), beep=self.beep.get_active(),
                       allow_words=allow_words, extra_words=extra_words)

    def _persist(self, *, paused_until: float | None = None) -> bool:
        try:
            settings = self._read_settings()
            if paused_until is not None:
                settings = replace(settings, paused_until=paused_until)
            self.config.save(settings)
        except (OSError, ValueError) as error:
            self.message.set_text(f"Не удалось сохранить: {error}")
            return False
        self.settings = settings
        self._refresh_status()
        return True

    def _save(self, _button=None):
        if self._persist():
            self.message.set_text("Настройки сохранены.")

    def _pause(self, _button=None):
        if self._persist(paused_until=time.time() + 300):
            self.message.set_text("Пауза включена на 5 минут.")

    def _resume(self, _button=None):
        if self._persist(paused_until=0):
            self.message.set_text("Пауза снята.")

    def _refresh_status(self):
        if not self.settings.enabled:
            self.status.set_text("Фильтр выключен. Включите его и сохраните настройки.")
        elif self.settings.paused_until > time.time():
            until = time.strftime("%H:%M", time.localtime(self.settings.paused_until))
            self.status.set_text(f"Пауза до {until}. Затем фильтр продолжит работу.")
        else:
            self.status.set_text("Фильтр включён для источников ввода «Тише EN» и «Тише RU».")
        self.resume_button.set_sensitive(self.settings.paused_until > time.time())

    def _update_status(self):
        self.settings = self.config.load()
        self._refresh_status()
        return GLib.SOURCE_CONTINUE

    def _destroy(self, _window):
        GLib.source_remove(self.status_timer)
        Gtk.main_quit()

    def _open_keyboard(self, _button=None):
        try:
            subprocess.Popen(["gnome-control-center", "keyboard"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.message.set_text("Откройте настройки системы → Клавиатура → Источники ввода.")

    def _enable_sources(self, _button=None):
        try:
            from .desktop import enable
            enable()
            self.message.set_text("Источники добавлены. Выберите Тише через Super+Space.")
        except (OSError, RuntimeError, GLib.Error) as error:
            self.message.set_text(str(error))

    def _open_dictionaries(self, _button=None):
        directory = self.config.wordlist_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.message.set_text(f"Папка словарей: {directory}")

    def _test_word(self, _button=None):
        value = self.test_entry.get_text().strip()
        if not value:
            self.test_result.set_text("Введите слово для проверки.")
            return
        try:
            from .core import Matcher
            from .wordlists import WordLists

            settings = self._read_settings()
            lists = WordLists(self.config.wordlist_dir())
            lists.reload()
            result = Matcher(settings, lists).detect(value)
            if result:
                language = getattr(result, "language", None)
                language_text = {"ru": "русский", "en": "английский", "custom": "мой словарь"}.get(language, language)
                suffix = f" · {language_text}" if language_text else ""
                self.test_result.set_text(f"Будет заблокировано{suffix}.")
            else:
                self.test_result.set_text("Разрешено текущими правилами.")
        except (OSError, ValueError) as error:
            self.test_result.set_text(f"Не удалось проверить: {error}")


class Intervention(Gtk.Window):
    def __init__(self, seconds: float):
        super().__init__(title="Тише — короткая пауза")
        from . import config

        display = Gdk.Display.get_default()
        if config.load().beep and display is not None:
            display.beep()
        self.set_default_size(460, 260)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.set_urgency_hint(True)
        self.connect("destroy", self._destroy)
        self.connect("key-press-event", self._key_press)
        self.deadline = time.monotonic() + max(0, seconds)
        self.timer = None
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
        content.set_border_width(28)
        self.add(content)
        title = _label("Стоп. Переформулируйте.")
        title.get_style_context().add_class("intervention-title")
        content.pack_start(title, False, False, 0)
        content.pack_start(_label("Ругательство не отправлено. Сделайте вдох и подберите другие слова.", muted=True), False, False, 0)
        self.countdown = _label("")
        content.pack_start(self.countdown, False, False, 0)
        self.progress = Gtk.ProgressBar()
        content.pack_start(self.progress, False, False, 0)
        self.close_button = _button("Продолжить", lambda _button: self.destroy(), primary=True)
        self.close_button.set_halign(Gtk.Align.END)
        content.pack_end(self.close_button, False, False, 0)
        self.duration = max(0.001, seconds)
        self._tick()
        if self.deadline > time.monotonic():
            self.timer = GLib.timeout_add(200, self._tick)

    def _tick(self):
        remaining = max(0, self.deadline - time.monotonic())
        self.progress.set_fraction(min(1, 1 - remaining / self.duration))
        self.close_button.set_sensitive(remaining <= 0)
        if remaining > 0:
            self.countdown.set_text(f"Можно продолжить через {math.ceil(remaining)} с")
            return GLib.SOURCE_CONTINUE
        self.countdown.set_text("Попробуйте выразить мысль иначе.")
        self.timer = None
        return GLib.SOURCE_REMOVE

    def _key_press(self, _window, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _destroy(self, _window):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        Gtk.main_quit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Тише / Mute — настройки фильтра")
    parser.add_argument("--settings", action="store_true", help="Открыть настройки")
    parser.add_argument("--intervention", type=float, metavar="SECONDS", help="Показать короткую паузу")
    parser.add_argument("--playground", action="store_true", help="Открыть проверку слов в настройках")
    args = parser.parse_args(argv)
    if args.intervention is not None and not math.isfinite(args.intervention):
        parser.error("SECONDS должно быть конечным числом")
    available, _ = Gtk.init_check(None)
    if not available:
        print("Тише: графическая сессия недоступна.", file=sys.stderr)
        return 1
    GLib.set_prgname("mute-profanity")
    GLib.set_application_name("Тише / Mute")
    css = Gtk.CssProvider()
    css.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    icon = Path(__file__).resolve().parent.parent / "assets" / "mute-profanity.svg"
    if icon.exists():
        Gtk.Window.set_default_icon_from_file(str(icon))
    try:
        window = Intervention(args.intervention) if args.intervention is not None else Preferences(playground=args.playground)
    except (OSError, ValueError) as error:
        print(f"Тише: {error}", file=sys.stderr)
        return 1
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
