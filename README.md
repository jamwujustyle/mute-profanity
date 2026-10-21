# Mute / Тише

A small, offline Russian/English profanity guard for Windows and Ubuntu GNOME.
No AI, account, API key, subscription, network connection, or paid service is
needed at runtime. Settings can stay closed: the input guard runs separately.

By default, a match triggers **immediately while typing**, clears the current
word and starts an escalating **8 → 16 → 32 → 60 second** cooldown. The warning
is in **English**. The desktop cooldown covers the monitors and blocks normal
mouse/keyboard interaction until it expires. It releases automatically; it does
not suspend the operating system or its background work.

**Ctrl+Shift+F12** dismisses the cooldown and pauses filtering for five minutes.
OS emergency controls remain available. After five minutes without another
match, the next penalty returns to its base duration.

## Windows 10/11, 64-bit

Use the portable `Mute-Windows-x64.zip` build, extract it, and open **Mute.exe**.
Click **Start in background**, then close Settings. This installs the app for
your user, adds a Start-menu shortcut and enables startup at sign-in. No Python
installation or administrator rights are needed for the packaged executable.
**Stop filtering** removes automatic startup and stops the hooks.

To update, stop filtering and close the old Mute windows. Open the new download
and click **Start in background**. Your settings and dictionaries are kept.

The Windows backend uses native keyboard/mouse hooks and the active keyboard
layout. It supports Russian/English input without switching to a special input
method. The source launcher `start-windows.cmd` is also available when Python
3.14, including Tk, is installed.

Wine smoke tests do not replace testing on native Windows, elevated applications,
multiple physical monitors and different keyboard layouts. See the limitations
below and the build/verification notes.

## Ubuntu 24.04 / GNOME 46

Extract the Linux source ZIP and run **install-linux.sh**, or from this folder:

```bash
/usr/bin/python3 run.py install
/usr/bin/python3 run.py enable
```

Installation is per-user, without sudo. It replaces standard US/RU sources with
**Тише EN** and **Тише RU**, preserving source order and other layouts.
**Super+Space** still switches language. IBus starts the guard when its source
is selected, including after login. Closing Settings does not stop it.

The GNOME companion provides the **whole-desktop input grab**. GNOME 46 cannot
hot-load a newly installed local extension: **sign out and back in once** after
activation. The installer stages and enables only this app's extension. It does
not log you out, enable unsafe Shell evaluation or change the global switch for
other extensions. If user extensions are disabled globally, enable this one in
GNOME's Extensions settings before relying on whole-desktop blocking.

Before the companion is loaded, filtering and the English full-screen GTK
warning still work. On Wayland that fallback cannot guarantee a global input
grab. On X11 it attempts a keyboard/pointer seat grab and releases it on expiry.
Other Linux desktop environments and GNOME versions are not currently supported
by the automatic installer.

Open **Тише / Mute** to configure it. **Запустить фильтр в фоне** means “Start in
background.” The top status distinguishes a saved enabled preference, a selected
guarded source, and pending GNOME desktop blocking.

```bash
/usr/bin/python3 run.py settings
/usr/bin/python3 run.py doctor
/usr/bin/python3 run.py pause
/usr/bin/python3 run.py disable
/usr/bin/python3 run.py uninstall
```

`disable` restores ordinary US/RU sources and leaves other layouts in place.
Uninstall keeps your dictionaries and settings. If the system libraries are
missing, Ubuntu's packages are `python3-gi`, `gir1.2-gtk-3.0`,
`gir1.2-ibus-1.0` and `ibus`.

## Immediate feedback and exceptions

**React immediately / Мгновенная реакция** is on by default. The trigger happens
on the character that first makes the pending text match, before Space, Enter
or Send. Turning it off restores checking at word completion. Both modes block
matched input before the guard commits it; pasting and editing existing text
have separate limitations.

Immediate mode is deliberately stricter. For example, a prohibited `ass` prefix
can interrupt `assembly` before the rest of the word has been typed. Add the
**full innocent word** to Allowed words, or turn immediate mode off. While input
is a prefix of an explicit exception, the instant penalty waits; a prohibited
standalone prefix is still rejected at the word boundary. The app cannot infer
which longer word you intended to type.

Settings also control the base cooldown, escalation, warning sound, Russian and
English rules, allowed words and your additional prohibited words. On Windows,
pause filtering before entering prohibited vocabulary in the Settings editors.

## Extensible dictionaries

Open **Open dictionary folder / Открыть словари** and drop in a UTF-8 `*.txt`
file, such as `es.txt`. Use one word per line:

```text
# Invented examples, not a required vocabulary
exampleword
examplestem*
```

- A plain entry matches a whole word, ignoring case.
- A trailing `*` explicitly enables prefix matching; at least three characters
  must precede it. Avoid broad stems that also start innocent words.
- Blank lines and full-line `#` comments are ignored. Phrases and regular
  expressions are not supported.
- Unicode normalization/case folding preserves accents. A new word list does
  not automatically gain language-specific inflection or transliteration rules.
- Limits: 1 MiB/file, 50,000 entries total, 128 characters/word. Symlinks and
  unsuitable files are skipped; diagnostics identify files/lines, not typed text.
- Explicit allowed words take priority. Built-in RU/EN language toggles do not
  disable an independently added dictionary.

Linux watches dictionary changes. Windows refreshes them during typing and on
settings updates; it does no idle polling. Default folders are:

- Linux: `~/.config/mute-profanity/wordlists/`
- Windows: `%LOCALAPPDATA%\Mute\wordlists\`

`XDG_CONFIG_HOME` overrides the configuration root when explicitly set.
Dictionaries are checked independently of the filename's language label. Adding
words needs no code changes. On Linux, adding a keyboard layout other than
US/RU also needs an IBus engine descriptor; vocabulary and keyboard layout are
separate concerns.

## Coverage and privacy

There is **no measured 99% detection guarantee**. Built-in rules cover common
Russian roots/inflections, English profanity/compounds, transliteration and some
confusables, repeats and masked spellings. They cannot understand context or
intent. False positives and misses are possible, especially in immediate mode.

Linux filtering requires an IBus-compatible application and a selected guarded
source. Clipboard input, voice input, other sources, games without IBus and
editing already committed text can bypass it. Space-separated spelling is not
guaranteed to be caught. Password/PIN/private fields marked by the application
bypass detection; terminal passwords may not expose that label.

Windows suppresses the matching key and removes only a known, contiguous prefix
in the same focused control. Focus changes, mouse editing, shortcuts, uncertain
translations and IME input invalidate that tracking. Paste, application text
transformations and elevated/protected applications are not guaranteed to be
covered. Native Win32 password Edit controls are bypassed; browser/custom
password fields cannot reliably be identified through these hooks. Pause the
guard for sensitive fields whose behavior has not been verified.

Typed text is never logged, saved or transmitted. Only the current bounded word
is retained and cleared after checking/reset. Saved files contain your settings
and explicitly added vocabulary. No swearing history or statistics are stored.
Idle runtimes block in native event loops. Countdown timers exist only while a
warning is visible; settings windows have their own short-lived status refresh.

## Building and checks

See [BUILDING.md](BUILDING.md) for Windows executable and Linux ZIP packaging.
The repository's [VERIFICATION.md](VERIFICATION.md) records observed results and
limits; it is not required to run a release package.

```bash
/usr/bin/python3 -m unittest discover -s tests -v
bash tools/test-ibus.sh
bash tools/test-ui.sh
bash tools/test-gnome.sh
/usr/bin/python3 tools/benchmark.py
/usr/bin/python3 tools/measure-idle.py
```

Display/session integration checks use temporary buses and displays. They do
not type into your messages or change ordinary desktop input sources.

## Кратко по-русски

Окно настроек можно закрыть: фильтр работает отдельно в фоне. По умолчанию он
реагирует сразу при совпадении, без пробела и Enter. Предупреждение — на
английском, с отсчётом и автоматическим снятием блокировки. Ctrl+Shift+F12
закрывает предупреждение и ставит фильтр на паузу на пять минут.

В GNOME 46 один раз выйдите и войдите в сеанс после установки: это необходимо
для загрузки расширения, блокирующего ввод на всём рабочем столе. Словари можно
добавлять обычными UTF-8 файлами, по одному слову на строке.
