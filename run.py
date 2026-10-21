#!/usr/bin/python3
"""Тише entry point. GUI libraries are imported only by GUI/engine commands."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Тише / Mute — локальный фильтр ввода RU/EN")
    parser.add_argument("command", nargs="?", default="settings",
                        choices=("settings", "engine", "doctor", "enable", "disable", "install", "uninstall"))
    parser.add_argument("--ibus", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--intervention", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.intervention is not None:
        from mute_profanity.ui import main as ui_main
        return ui_main(["--intervention", str(args.intervention)])
    if args.command == "engine":
        from mute_profanity.engine import main as engine_main
        return engine_main()
    if args.command in ("install", "uninstall"):
        from mute_profanity.install import main as install_main
        return install_main(args.command)
    if args.command in ("doctor", "enable", "disable"):
        from mute_profanity.desktop import main as desktop_main
        return desktop_main(args.command)
    try:
        from mute_profanity.ui import main as ui_main
        return ui_main([])
    except (ImportError, ValueError) as error:
        print(f"Нужны системные GTK/IBus: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
