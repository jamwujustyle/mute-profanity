#!/usr/bin/env bash
# Exercise the Shell modal only on private D-Bus/Xvfb/XDG surfaces.
set -euo pipefail

task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for dependency in dbus-run-session xvfb-run gnome-shell glib-compile-schemas timeout /usr/bin/python3; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'Missing system dependency: %s\n' "$dependency" >&2
        exit 1
    fi
done

if [[ "${MUTE_GNOME_PRIVATE_DBUS:-}" == 1 ]]; then
    test_root=${MUTE_GNOME_TEST_ROOT:?Missing private test directory}
    if [[ "$(dirname -- "$test_root")" != /tmp || "$(basename -- "$test_root")" != mute-gnome-test.* ]]; then
        printf 'Refusing an invalid private GNOME test directory.\n' >&2
        exit 1
    fi
else
    test_root=$(mktemp -d /tmp/mute-gnome-test.XXXXXXXX)
fi
cleanup() {
    if [[ "${MUTE_GNOME_PRIVATE_DBUS:-}" == 1 ]]; then
        return
    fi
    # Only this disposable root is eligible for unmount/removal. Custom bus
    # configuration below disables service activation, so these are fallback
    # cleanup for older test runs rather than normal dependencies.
    if command -v fusermount3 >/dev/null 2>&1; then
        fusermount3 -u "$test_root/runtime/doc" 2>/dev/null || true
        fusermount3 -u "$test_root/runtime/gvfs" 2>/dev/null || true
    fi
    rm -rf -- "$test_root"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

unset DISPLAY WAYLAND_DISPLAY DBUS_STARTER_ADDRESS DBUS_STARTER_BUS_TYPE IBUS_ADDRESS_FILE
export XDG_CONFIG_HOME="$test_root/config"
export XDG_CACHE_HOME="$test_root/cache"
export XDG_DATA_HOME="$test_root/data"
export XDG_RUNTIME_DIR="$test_root/runtime"
export GSETTINGS_SCHEMA_DIR="$test_root/schemas"
export GSETTINGS_BACKEND=memory GIO_USE_VFS=local NO_AT_BRIDGE=1
export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=GNOME
export GTK_IM_MODULE=gtk-im-context-simple
export IBUS_ADDRESS="unix:path=$test_root/no-ibus.sock"
export MUTE_GNOME_TEST_ROOT="$test_root" MUTE_GNOME_RUNTIME=1
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR" "$GSETTINGS_SCHEMA_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

if [[ "${MUTE_GNOME_PRIVATE_DBUS:-}" != 1 ]]; then
    # Memory backends are process-local, so stage schema defaults seen by Shell
    # instead of writing a setting in a separate process and assuming it persists.
    /usr/bin/python3 - "$task_root" "$test_root" <<'PY'
import json
from pathlib import Path
import shutil
import sys

source, temporary = map(Path, sys.argv[1:])
schemas = temporary / "schemas"
for path in Path("/usr/share/glib-2.0/schemas").iterdir():
    if path.name.endswith((".xml", ".override")):
        (schemas / path.name).symlink_to(path)
(schemas / "99-mute-runtime-test.gschema.override").write_text(
    "[org.gnome.shell]\n"
    "enabled-extensions=['mute-profanity@local']\n"
    "disabled-extensions=[]\n"
    "disable-user-extensions=false\n"
    "welcome-dialog-last-shown-version='999.0'\n"
    "[org.gnome.desktop.interface]\n"
    "enable-animations=false\n"
)
target = temporary / "data/gnome-shell/extensions/mute-profanity@local"
shutil.copytree(source / "assets/gnome-extension", target)
(target / "bridge.json").write_text(json.dumps({"entry_path": str(source / "run.py")}))
# Read-only instrumentation exists only in the disposable copy. It checks that
# the production countdown actually destroys its actor and releases its grab.
extension = target / "extension.js"
code = extension.read_text()
probe = '''<method name="Probe">
      <arg type="b" direction="out"/><arg type="b" direction="out"/>
      <arg type="u" direction="out"/><arg type="u" direction="out"/>
      <arg type="u" direction="out"/><arg type="u" direction="out"/>
    </method>'''
assert code.count('<method name="Hide"/>') == 1
assert code.count('    Hide() {') == 1
code = code.replace('<method name="Hide"/>', '<method name="Hide"/>' + probe)
code = code.replace('    Hide() {', '''    Probe() {
        const dialog = this._dialog;
        return [dialog !== null, Boolean(dialog?._hasModal),
            Math.ceil(dialog?.width ?? 0), Math.ceil(dialog?.height ?? 0),
            Math.ceil(global.stage.width), Math.ceil(global.stage.height)];
    }

    Hide() {''')
extension.write_text(code)
# No service directories: missing optional Shell services fail locally instead
# of spawning calendars, portals, volume monitors or other background daemons.
(temporary / "bus.conf").write_text(
    '<busconfig><type>session</type>'
    f'<listen>unix:path={temporary}/session-bus.sock</listen>'
    '<auth>EXTERNAL</auth><policy context="default">'
    '<allow user="*"/><allow own="*"/>'
    '<allow send_destination="*"/><allow receive_sender="*"/>'
    '</policy></busconfig>'
)
PY
    glib-compile-schemas "$GSETTINGS_SCHEMA_DIR"
    # All activated helpers inherit temporary directories and the private bus.
    dbus-run-session --config-file "$test_root/bus.conf" -- env MUTE_GNOME_PRIVATE_DBUS=1 "$0" "$@"
    exit $?
fi

# Shell needs a connectable system-bus connection during LoginManager setup.
# Route those lookups to this same private bus; never contact the host system
# bus or activate its logind/power services from the disposable compositor.
export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
cd -- "$task_root"
timeout --signal=TERM --kill-after=2s 45s xvfb-run -a -s '-screen 0 1024x768x24 -nolisten tcp' \
    /usr/bin/python3 -m unittest discover -s tests -p test_gnome_runtime.py -v
