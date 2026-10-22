#!/usr/bin/python3
"""Тише entry point. GUI libraries are imported only by GUI/engine commands."""

import argparse
import sys
from dataclasses import replace
import time


def ui_main(arguments):
    if sys.platform == "win32":
        from mute_profanity.windows_ui import main
    else:
        from mute_profanity.ui import main
    return main(arguments)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="Mute.exe" if sys.platform == "win32" else "profanity",
                                     description="Mute — offline Russian/English profanity filter")
    parser.add_argument("command", nargs="?", default="settings",
                        choices=("settings", "engine", "doctor", "enable", "disable", "install", "uninstall", "pause",
                                 "on", "off", "status"))
    parser.add_argument("--ibus", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--intervention", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command in ("on", "enable", "off", "disable"):
        from mute_profanity.cli import switch
        return switch(args.command in ("on", "enable"))
    if args.command == "status":
        from mute_profanity.cli import status
        return status()
    if args.command == "pause":
        from mute_profanity import config
        config.save(replace(config.load(), paused_until=time.time() + 300))
        if sys.platform == "win32":
            from mute_profanity.windows import refresh
            refresh()
        else:
            from mute_profanity.gnome_modal import try_hide
            try_hide()
        print("Profanity filter: paused for 5 minutes")
        return 0
    if args.intervention is not None:
        return ui_main(["--intervention", str(args.intervention)])
    if args.command == "engine":
        if sys.platform == "win32":
            from mute_profanity.windows import main as engine_main
        else:
            from mute_profanity.engine import main as engine_main
        return engine_main()
    if sys.platform == "win32" and args.command in ("install", "uninstall", "doctor", "enable", "disable"):
        from mute_profanity.windows_app import main as windows_main
        return windows_main(args.command)
    if args.command in ("install", "uninstall"):
        from mute_profanity.install import main as install_main
        return install_main(args.command)
    if args.command in ("doctor", "enable", "disable"):
        from mute_profanity.desktop import main as desktop_main
        return desktop_main(args.command)
    try:
        return ui_main([])
    except (ImportError, ValueError) as error:
        print(f"Mute could not open Settings: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
