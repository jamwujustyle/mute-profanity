#!/usr/bin/env bash
# Display GTK preferences only inside a temporary virtual X server.
set -euo pipefail

task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for dependency in xvfb-run timeout /usr/bin/python3; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'Missing system dependency: %s\n' "$dependency" >&2
        exit 1
    fi
done
test_root=$(mktemp -d /tmp/mute-gtk-test.XXXXXXXX)
trap 'rm -rf -- "$test_root"' EXIT
export XDG_CONFIG_HOME="$test_root/config"
export XDG_CACHE_HOME="$test_root/cache"
export XDG_DATA_HOME="$test_root/data"
export XDG_RUNTIME_DIR="$test_root/runtime"
export IBUS_ADDRESS="unix:path=$test_root/no-ibus.sock"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$test_root/no-dbus.sock"
export GSETTINGS_BACKEND=memory GIO_USE_VFS=local NO_AT_BRIDGE=1
export GTK_IM_MODULE=gtk-im-context-simple MUTE_GTK_TEST=1
export MUTE_GTK_SCREENSHOT="${MUTE_GTK_SCREENSHOT:-/tmp/mute-preferences-activation.png}"
unset WAYLAND_DISPLAY IBUS_ADDRESS_FILE
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
cd -- "$task_root"
timeout --signal=TERM --kill-after=2s 20s xvfb-run -a /usr/bin/python3 -m unittest \
    discover -s tests -p test_ui.py -v
printf 'Preferences screenshot: %s\n' "$MUTE_GTK_SCREENSHOT"
