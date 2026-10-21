# Verification — 21 October 2026

Built for Ubuntu 24.04, GNOME 46, IBus 1.5.29, Python 3.12.
No model, API key, subscription, network client, keyboard-device permission,
or administrator access is used by the app.

## Checks

- **43 unit checks passed**, including Russian morphology, English compounds,
  transliteration, masked spellings, innocent-word regressions, custom lists,
  config validation, bounded input, composition, cooldowns and staged installation.
  Fourteen integration checks are intentionally skipped by normal discovery.
- **14 real IBus integration checks passed** using a private D-Bus/IBus session.
  They verify registry discovery from the installation XML, both layout metadata,
  safe commits, blocked words and submission, Russian keyvals, Backspace/native
  navigation, shared cooldown, emergency pause, key-release handling, password,
  PIN and private-field bypass, safe/unsafe focus loss, invisible formatting,
  automatic dictionary reload and accented Compose input.
- Native GTK preferences and intervention windows were constructed and exercised
  under an isolated Xvfb display. Screenshots were visually inspected. Settings,
  validation, pause/resume, local word checks and countdown completion worked.
- Python compilation, shell syntax and whitespace checks passed.
- A local English dictionary audit checked 104,334 words for accidental matches;
  discovered innocent-word matches were added as regression cases. This is a
  false-positive check, not a representative profanity-recall measurement.

## Measured resource use

| Measurement | Observed |
| --- | ---: |
| Idle engine-runtime resident memory | 23.09 MiB |
| Idle CPU over a 5.01-second sample | 0.01% |
| Mean detector CPU per call, 126,000 checks | 15.38 µs |
| Detector-only peak memory | 14.45 MiB |

The idle measurement includes Python, GI/IBus, loaded default rules and file
watchers in a temporary configuration. It does **not** include an active desktop
bus connection, GTK popup, settings window, additional wordlists or the existing
GNOME/IBus processes. Actual memory and CPU depend on those factors. A short idle
sample and a synthetic matcher benchmark do not establish a universal ceiling.

Reproduce from the repository:

```bash
/usr/bin/python3 -m unittest discover -s tests -v
bash tools/test-ibus.sh
/usr/bin/python3 tools/benchmark.py
/usr/bin/python3 tools/measure-idle.py
```

The integration runner uses temporary sockets and temporary XDG directories.
It does not send keys to, read text from or change sources in the user's desktop.

## Activation and limits

Prepared and verified: installable native app, per-user desktop launcher,
IBus component registration and GNOME service configuration.

Not observed: typing in the user's ordinary applications after selecting the
installed source. A fresh login is needed for GNOME's input-source discovery;
the installer does not log the user out or interrupt the current IBus process.

There is no measured 99% recall claim. IBus compatibility and selected source
determine coverage. Clipboard insertion, other input sources and editing text
already committed remain bypasses. See the README for setup and limitations.
