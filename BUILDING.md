# Building Mute

The application has no network or paid-service requirement at runtime. Build
dependencies are separate and are not installed by the application.

## Windows executable

Use Windows x64 with Python 3.14 and Tk. Install the pinned build tool:

```powershell
python -m pip install PyInstaller==6.22.3
python tools/build-windows.py
```

The builder creates a windowed, one-file executable and portable ZIP under
`release/`. It excludes Linux-only GI/IBus modules. The packaged executable
contains Python and Tk; its users do not need to install Python.

The manual **Build downloadable applications** GitHub Actions workflow can build
Windows and Linux artifacts. It has read-only repository permissions and does
not publish a release. Running that workflow or publishing its outputs is a
separate repository action; creating the workflow does not run or publish it.

The optional Wine helper builds in an isolated temporary prefix/display. It
uses official Python and verifies the release checksum before executing the
installer. Wine smoke tests can catch import/ABI/startup issues, but native
Windows behavior still needs verification on real Windows hardware.

## Linux source package

```bash
python3 tools/package-linux.py
```

This creates `release/Mute-Linux.zip`. The allowlist-based packager includes the
application modules, assets, README, license and installation launcher. It skips
Git metadata, credentials, caches, local settings, tests and build directories.
The source package uses the distro's Python/GTK/IBus libraries rather than
shipping a large independent desktop runtime.

Supported automatic setup currently targets Ubuntu 24.04 GNOME 46. GNOME's
whole-desktop cooldown companion requires one new login after initial staging.
No administrator rights are required for the per-user application installation.

## Verification

Normal unittest discovery runs portable policy, configuration, dictionary,
installer and packaging checks. Platform integration checks opt in separately:

```bash
python3 -m unittest discover -s tests -v
bash tools/test-ibus.sh
bash tools/test-ui.sh
bash tools/test-gnome.sh
```

The optional native Chrome autocomplete regression uses a fresh profile and
private Xvfb/D-Bus/IBus session:

```bash
/usr/bin/python3 tools/test-chrome.py
```

It needs Chrome, XTest and the system Python `gi`/`websocket` modules, and it
never uses the normal browser profile or desktop input.

The display/session runners create private temporary environments; they must not
be pointed at a normal desktop bus. Windows native-hook smoke tests must likewise
run only in a disposable Windows session or the isolated Wine build environment.
Test results and generated artifacts must be described as prepared, built,
verified or published according to what was actually observed.

References: [PyInstaller usage](https://pyinstaller.org/en/stable/usage.html),
[PyInstaller platform-specific builds](https://pyinstaller.org/en/stable/operating-mode.html),
[Python Windows releases](https://www.python.org/downloads/windows/).
