#!/bin/sh
# Install and activate Mute for this user with Ubuntu's GTK/IBus-enabled Python.
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
/usr/bin/python3 "$app_dir/run.py" install
exec /usr/bin/python3 "$app_dir/run.py" enable
