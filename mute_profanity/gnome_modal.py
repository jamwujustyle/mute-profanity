"""Optional GNOME Shell modal client and local extension staging.

No desktop state changes happen on import. A new local extension needs a new
GNOME session before Shell can discover it; staging does not claim activation.
"""

import json
import math
import os
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET


UUID = "mute-profanity@local"
BUS_NAME = "org.gnome.Shell"
OBJECT_PATH = "/org/gnome/Shell/Extensions/MuteProfanity"
INTERFACE = "org.gnome.Shell.Extensions.MuteProfanity"
CALL_TIMEOUT_MS = 400


def extension_path() -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data / "gnome-shell" / "extensions" / UUID


def prepare_extension(entry_path: str | os.PathLike[str]) -> Path:
    """Stage only this extension and its shell-free emergency command bridge."""
    target = extension_path()
    assets = Path(__file__).resolve().parent.parent / "assets" / "gnome-extension"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("metadata.json", "extension.js"):
        shutil.copyfile(assets / name, target / name)
    bridge = target / "bridge.json"
    bridge.write_text(json.dumps({"entry_path": str(Path(entry_path).resolve())}, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    bridge.chmod(0o600)
    return target


def _call(method, parameters, result_type):
    from gi.repository import Gio, GLib

    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return connection.call_sync(BUS_NAME, OBJECT_PATH, INTERFACE, method, parameters,
                                    GLib.VariantType.new(result_type), Gio.DBusCallFlags.NO_AUTO_START,
                                    CALL_TIMEOUT_MS, None)
    except GLib.Error:
        return None


def try_show(seconds: int | float) -> bool:
    """Show a genuine Shell modal, returning false for the caller's fallback."""
    from gi.repository import GLib

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return False
    try:
        if not math.isfinite(seconds):
            return False
        duration = max(1, min(60, math.ceil(seconds)))
    except (OverflowError, ValueError):
        return False
    result = _call("Show", GLib.Variant("(u)", (duration,)), "(b)")
    return result is not None and bool(result.unpack()[0])


def try_hide() -> bool:
    """Release a current Shell modal when pausing or disabling the filter."""
    return _call("Hide", None, "()") is not None


def is_available() -> bool:
    """Read-only check that Shell has loaded the modal extension."""
    from gi.repository import Gio, GLib
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = connection.call_sync(BUS_NAME, OBJECT_PATH,
                                      "org.freedesktop.DBus.Introspectable", "Introspect", None,
                                      GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NO_AUTO_START,
                                      CALL_TIMEOUT_MS, None)
        return any(node.attrib.get("name") == INTERFACE
                   for node in ET.fromstring(result.unpack()[0]).findall("interface"))
    except (GLib.Error, ET.ParseError):
        return False


def request_enable() -> bool:
    """Enable this staged extension; new GNOME 46 UUIDs load at next login."""
    from gi.repository import Gio, GLib
    try:
        settings = Gio.Settings.new("org.gnome.shell")
        enabled = settings.get_strv("enabled-extensions")
        if UUID not in enabled:
            settings.set_strv("enabled-extensions", [*enabled, UUID])
        disabled = settings.get_strv("disabled-extensions")
        if UUID in disabled:
            settings.set_strv("disabled-extensions", [value for value in disabled if value != UUID])
        Gio.Settings.sync()
        # This works when Shell already knows the UUID; a newly staged UUID
        # stays pending until a new session. Never enable unsafe Shell Eval.
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.call_sync(BUS_NAME, "/org/gnome/Shell",
                             "org.gnome.Shell.Extensions", "EnableExtension",
                             GLib.Variant("(s)", (UUID,)), GLib.VariantType.new("(b)"),
                             Gio.DBusCallFlags.NO_AUTO_START, CALL_TIMEOUT_MS, None)
    except GLib.Error:
        return False
    return is_available()


def remove_extension():
    from gi.repository import Gio, GLib
    try_hide()
    try:
        settings = Gio.Settings.new("org.gnome.shell")
        settings.set_strv("enabled-extensions", [value for value in settings.get_strv("enabled-extensions")
                                                 if value != UUID])
        Gio.Settings.sync()
    except GLib.Error:
        pass
    shutil.rmtree(extension_path(), ignore_errors=True)
