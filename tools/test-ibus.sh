#!/usr/bin/env bash
# Run the real IBus adapter against private D-Bus/IBus instances only.
set -euo pipefail

task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for dependency in dbus-run-session ibus-daemon timeout /usr/bin/python3; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'Missing system dependency: %s\n' "$dependency" >&2
        exit 1
    fi
done

if [[ "${MUTE_IBUS_PRIVATE_DBUS:-}" == 1 ]]; then
    test_root=${MUTE_IBUS_TEST_ROOT:?Missing private test directory}
    if [[ "$(dirname -- "$test_root")" != /tmp || "$(basename -- "$test_root")" != mute-ibus-test.* ]]; then
        printf 'Refusing an invalid private test directory.\n' >&2
        exit 1
    fi
else
    test_root=$(mktemp -d /tmp/mute-ibus-test.XXXXXXXX)
fi
daemon_pid=
cleanup() {
    if [[ -n "$daemon_pid" ]]; then
        kill "$daemon_pid" 2>/dev/null || true
        for ((attempt = 0; attempt < 20; attempt++)); do
            if ! kill -0 "$daemon_pid" 2>/dev/null; then
                break
            fi
            sleep 0.05
        done
        kill -KILL "$daemon_pid" 2>/dev/null || true
        wait "$daemon_pid" 2>/dev/null || true
    fi
    rm -rf -- "$test_root"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Neither inherited desktop sockets nor the normal IBus address/config are used.
unset DISPLAY WAYLAND_DISPLAY IBUS_ADDRESS_FILE
export XDG_CONFIG_HOME="$test_root/config"
export XDG_CACHE_HOME="$test_root/cache"
export XDG_DATA_HOME="$test_root/data"
export XDG_RUNTIME_DIR="$test_root/runtime"
export IBUS_ADDRESS="unix:path=$test_root/ibus.sock"
export MUTE_IBUS_TEST_ROOT="$test_root"
export MUTE_IBUS_INTEGRATION=1
export IBUS_COMPONENT_PATH="$test_root/components"
export GIO_USE_VFS=local
export GTK_IM_MODULE=ibus QT_IM_MODULE=ibus XMODIFIERS=@im=ibus
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR" "$IBUS_COMPONENT_PATH"
chmod 700 "$XDG_RUNTIME_DIR"

# Start D-Bus only after the temporary environment is in place, so any D-Bus
# activated helper also inherits the temporary directories.
if [[ "${MUTE_IBUS_PRIVATE_DBUS:-}" != 1 ]]; then
    dbus-run-session -- env MUTE_IBUS_PRIVATE_DBUS=1 "$0" "$@"
    exit $?
fi

# Exercise the same XML generator as the user installation. The private daemon
# scans only this component directory; it never sees system or user engines.
PYTHONPATH="$task_root" /usr/bin/python3 - "$task_root" "$IBUS_COMPONENT_PATH/mute-profanity.xml" <<'PY'
from pathlib import Path
import sys
from mute_profanity.install import component_xml

source = Path(sys.argv[1])
Path(sys.argv[2]).write_text(component_xml(source / "run.py", source / "assets/mute-profanity.svg"), encoding="utf-8")
PY

ibus-daemon --single --panel disable --emoji-extension disable --config disable \
    --cache none --timeout 3000 --address "$IBUS_ADDRESS" \
    >"$test_root/ibus.log" 2>&1 &
daemon_pid=$!
for ((attempt = 0; attempt < 100; attempt++)); do
    if [[ -S "$test_root/ibus.sock" ]]; then
        break
    fi
    if ! kill -0 "$daemon_pid" 2>/dev/null; then
        cat "$test_root/ibus.log" >&2
        exit 1
    fi
    sleep 0.05
done
if [[ ! -S "$test_root/ibus.sock" ]]; then
    printf 'The isolated IBus daemon did not start within 5 seconds.\n' >&2
    cat "$test_root/ibus.log" >&2
    exit 1
fi

cd -- "$task_root"
if ! timeout --signal=TERM --kill-after=2s 45s /usr/bin/python3 -m unittest \
    discover -s tests -p test_ibus_integration.py -v; then
    cat "$test_root/ibus.log" >&2
    exit 1
fi
