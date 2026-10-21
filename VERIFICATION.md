# Verification — 21 October 2026

Built for Ubuntu 24.04, GNOME 46, IBus 1.5.29, Python 3.12.
No model, API key, subscription, network client, keyboard-device permission,
or administrator access is used by the app.

## Checks

- **131 ordinary checks passed**, including Russian morphology, English compounds,
  transliteration, masked spellings, innocent-word regressions, custom lists,
  config validation, bounded input, composition, cooldowns, staged installation,
  activation/source mapping, immediate detection, Windows policy/native-API
  adapters with simulated calls, packaging, and truthful readiness reports.
  Thirty display/session checks are skipped by normal discovery.
- **21 real IBus integration checks passed** using a private D-Bus/IBus session.
  They verify registry discovery from the installation XML, both layout metadata,
  safe commits, blocked words and submission, Russian keyvals, Backspace/native
  navigation, shared cooldown, emergency pause, key-release handling, password,
  PIN and private-field bypass, safe/unsafe focus loss, invisible formatting,
  automatic dictionary reload, accented Compose input, immediate RU/EN/custom
  matching, Backspace rechecking and allowlisted longer-word prefixes.
- **9 GTK/config checks passed** under isolated Xvfb, covering missing setup,
  actual active-source status, closing settings without disabling the engine,
  asynchronous activation failure, persisted immediate mode, the English
  full-screen warning, automatic expiry, ignored Escape and emergency release.
  One configuration check also runs during ordinary discovery.
- **1 actual GNOME 46 runtime check passed** in a nested Shell on private D-Bus
  and Xvfb. The production extension acquired the full-stage keyboard/pointer
  modal grab, destroyed its actor and released the grab after expiry, accepted
  Hide safely, and acquired/released another grab. A read-only state probe was
  inserted only into the temporary test copy; no input events were injected.
- Python compilation, shell syntax and whitespace checks passed.
- A local English dictionary audit checked 104,334 words for accidental matches;
  discovered innocent-word matches were added as regression cases. This is a
  false-positive check, not a representative profanity-recall measurement.

## Measured resource use

| Measurement | Observed |
| --- | ---: |
| Idle engine-runtime resident memory | 23.38 MiB |
| Idle CPU over a 5.01-second sample | 0.01% |
| Mean detector CPU per call, 126,000 checks | 15.99 µs |
| Detector-only peak memory | 14.32 MiB |
| Live installed engine resident memory | 24.32 MiB |

The isolated idle measurement includes Python, GI/IBus, loaded default rules and file
watchers in a temporary configuration. It does **not** include an active desktop
bus connection, GTK popup, settings window, additional wordlists or the existing
GNOME/IBus processes. Actual memory and CPU depend on those factors. A short idle
sample and a synthetic matcher benchmark do not establish a universal ceiling.
The separate live-engine sample includes its actual desktop bus connection;
no additional CPU ticks were observed during that five-second sample. It does
not include the separate settings process, Shell, IBus daemon or an active popup.

Reproduce from the repository:

```bash
/usr/bin/python3 -m unittest discover -s tests -v
bash tools/test-ibus.sh
bash tools/test-ui.sh
bash tools/test-gnome.sh
/usr/bin/python3 tools/benchmark.py
/usr/bin/python3 tools/measure-idle.py
```

The integration runner uses temporary sockets and temporary XDG directories.
It does not send keys to, read text from or change sources in the user's desktop.

## Windows scope

The Windows backend, settings UI, per-user installer and executable packaging
are implemented. Fifty-four Windows checks pass, using simulated Win32 API
boundaries where needed. They cover contiguous-word tracking, immediate and
completed-word rejection, modifier/shortcut behavior, focus changes, password
controls, hook readiness and cleanup, automatic startup, stop/start, settings
refresh, emergency pause/resume and safe executable/source upgrades. These are
included in ordinary discovery.

Native Windows desktop behavior has not been observed on real Windows hardware.
The actual packaged x64 GUI executable was built with Windows Python 3.14.9 and
PyInstaller 6.22.3 in an isolated Wine 11 prefix. All runtime module bytes and
the entry point matched the staged build source. The executable smoke test
passed: settings opened and closed, the English full-screen warning appeared
and expired, both background hooks became ready, and the stop event shut down
the engine cleanly. Settings and warning screenshots were visually inspected.
Wine cannot establish coverage of native elevated apps, physical monitors,
keyboard layouts or physical typing on Windows.

## Downloads

Both local release archives passed CRC verification. The Windows package
includes the real PE32+ x86-64 GUI executable and its bundled Python/Tk runtime;
no Python installation is needed by its user. The Linux source package was also
extracted and installed successfully in temporary directories, including all
IBus/desktop assets and the GNOME companion without the optional report file.

Artifacts are saved in the visible `release/` folder, which is excluded from Git.
They have not been published to a GitHub release. The manual CI workflow is
prepared; it has not been run remotely.

## Activation and limits

Installed and verified: native app, per-user desktop launcher, IBus component
registration and GNOME service configuration. Live diagnostics confirmed both
guarded sources configured, `mute-en` selected, and the separate engine process
running independently of Settings. The updated installed source reports version
0.2.0 with immediate mode enabled and no active pause. The GNOME modal companion
is staged for loading at the next login; it cannot hot-load into this GNOME 46
session. Its actual modal behavior was verified in the isolated session above.

Activation loads missing engines through the user's IBus service and replaces
ordinary US/RU sources with guarded sources. The settings window reports actual
readiness and may be closed. Other keyboard layouts keep their order and can
remain selected; the status then explains how to choose a guarded source.

Not observed: typing in the user's ordinary applications after selecting the
installed source. No messages were typed or sent in Telegram during diagnostics.

There is no measured 99% recall claim. IBus compatibility and selected source
determine coverage. Clipboard insertion, other input sources and editing text
already committed remain bypasses. See the README for setup and limitations.
