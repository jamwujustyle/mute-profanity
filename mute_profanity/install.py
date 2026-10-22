"""Reversible, per-user GNOME installation. No sudo or device permissions."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from . import __version__

SERVICE = "org.freedesktop.IBus.session.GNOME.service"
CLI_MARKER = "# Managed by Mute: profanity command"


def paths():
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    conf = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return {
        "app": data / "mute-profanity",
        "component": data / "mute-profanity/ibus/mute-profanity.xml",
        "desktop": data / "applications/mute-profanity.desktop",
        "icon": data / "icons/hicolor/scalable/apps/mute-profanity.svg",
        "dropin": conf / f"systemd/user/{SERVICE}.d/60-mute-profanity.conf",
        "cli": Path.home() / ".local/bin/profanity",
    }


def component_xml(entry: Path, icon: Path) -> str:
    root = ET.Element("component")
    for key, value in {
        "name": "org.freedesktop.IBus.MuteProfanity",
        "description": "Тише — offline profanity input filter",
        "exec": f"/usr/bin/python3 {shlex.quote(str(entry))} engine --ibus",
        "version": __version__, "author": "Mute contributors", "license": "MIT",
        "homepage": "", "textdomain": "mute-profanity",
    }.items():
        ET.SubElement(root, key).text = value
    engines = ET.SubElement(root, "engines")
    for language, layout, title, symbol in (
        ("en", "us", "Тише EN — English", "M·en"),
        ("ru", "ru", "Тише RU — Русский", "M·ру"),
    ):
        engine = ET.SubElement(engines, "engine")
        for key, value in {
            "name": f"mute-{language}", "longname": title,
            "description": "Локальная проверка слов; Ctrl+Shift+F12 — пауза на 5 минут",
            "language": language, "license": "MIT", "author": "Mute contributors",
            "icon": str(icon), "layout": layout, "symbol": symbol,
        }.items():
            ET.SubElement(engine, key).text = value
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def desktop_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'


def dropin_text(directory: Path) -> str:
    # A component path replaces IBus defaults: retain the system engines.
    value = f"IBUS_COMPONENT_PATH={directory}:/usr/share/ibus/component"
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    # IBus's auto cache can otherwise reuse the registry from before this path
    # existed. Scan once at session startup; this introduces no runtime polling.
    return (f'[Service]\nEnvironment="{value}"\nExecStart=\n'
            "ExecStart=/bin/sh -c 'exec /usr/bin/ibus-daemon --cache none --panel disable "
            "$([ \"$XDG_SESSION_TYPE\" = \"x11\" ] && echo \"--xim\")'\n")


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def install_cli(entry: Path, target: Path):
    if target.is_symlink() or (target.exists() and CLI_MARKER not in target.read_text(encoding="utf-8")):
        raise ValueError(f"Another command already exists at {target}; it has not been replaced")
    write(target, f'''#!/bin/sh
{CLI_MARKER}
if [ "$#" -eq 0 ]; then set -- status; fi
exec /usr/bin/python3 {shlex.quote(str(entry))} "$@"
''')
    target.chmod(0o755)


def install():
    import gi
    gi.require_version("IBus", "1.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import IBus, Gtk  # noqa: F401 — dependency check before writing
    source = Path(__file__).resolve().parent.parent
    locations = paths()
    for path in locations.values():
        if any(char in str(path) for char in ("\n", "\r", "\x00", ":")):
            raise ValueError("The installation path contains unsupported characters.")
    destination = locations["app"]
    destination.mkdir(parents=True, exist_ok=True)
    if source != destination:
        shutil.copytree(source / "mute_profanity", destination / "mute_profanity",
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("run.py", "README.md", "LICENSE"):
            shutil.copy2(source / name, destination / name)
        if (source / "VERIFICATION.md").is_file():
            shutil.copy2(source / "VERIFICATION.md", destination / "VERIFICATION.md")
        shutil.copytree(source / "assets", destination / "assets", dirs_exist_ok=True)
    locations["icon"].parent.mkdir(parents=True, exist_ok=True)
    icon_source = source / "assets/mute-profanity.svg"
    if not icon_source.exists():
        icon_source = locations["icon"]
    if icon_source != locations["icon"]:
        shutil.copy2(icon_source, locations["icon"])
    write(locations["component"], component_xml(destination / "run.py", locations["icon"]))
    from .gnome_modal import prepare_extension
    prepare_extension(destination / "run.py")
    write(locations["dropin"], dropin_text(locations["component"].parent))
    write(locations["desktop"], f'''[Desktop Entry]
Type=Application
Name=Тише / Mute
Comment=Локальный фильтр мата: русский и английский
Exec=/usr/bin/python3 {desktop_quote(str(destination / "run.py"))} settings
Icon=mute-profanity
Terminal=false
Categories=Utility;Accessibility;
StartupNotify=true
''')
    from . import config
    config.wordlist_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    install_cli(destination / "run.py", locations["cli"])
    result = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print("Mute is installed for the current user. Open Mute from the applications menu for settings.")
    print("Start the background filter in Settings, or run:")
    print(f"/usr/bin/python3 {shlex.quote(str(destination / 'run.py'))} enable")
    print("Activation loads Mute and replaces ordinary US/RU sources; other layouts are preserved.")
    print("For full desktop cooldown in GNOME, sign out and back in once after activation.")
    if result.returncode:
        print("The session bus is unavailable. Enable the filter from your GNOME session.")
    return 0


def uninstall():
    # Remove input-source references first. On failure leave launchable engines
    # installed, so the user's selected keyboard cannot point at missing code.
    from .desktop import disable
    disable()
    from .gnome_modal import remove_extension
    remove_extension()
    locations = paths()
    for key in ("component", "desktop", "icon", "dropin"):
        locations[key].unlink(missing_ok=True)
    cli = locations["cli"]
    if cli.is_file() and not cli.is_symlink() and CLI_MARKER in cli.read_text(encoding="utf-8"):
        cli.unlink()
    destination = locations["app"]
    if destination.exists():
        # The app directory is wholly managed; user dictionaries/settings live elsewhere.
        shutil.rmtree(destination)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print("Mute was removed. Dictionaries and settings were preserved. Sign out and back in to finish.")
    return 0


def main(command="install"):
    try:
        return install() if command == "install" else uninstall()
    except (OSError, ImportError, ValueError, RuntimeError) as error:
        print(f"Installation could not be completed: {error}", file=sys.stderr)
        return 1
