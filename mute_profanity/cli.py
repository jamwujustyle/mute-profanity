"""English command-line controls, independent of the settings window."""

from dataclasses import replace
import sys
import time

from . import config


def switch(enabled: bool) -> int:
    config.save(replace(config.load(), enabled=enabled, paused_until=0))
    if sys.platform == "win32":
        from .windows_app import main
    else:
        from .gnome_modal import try_hide
        from .desktop import main
        if not enabled:
            try_hide()
    result = main("enable" if enabled else "disable")
    if result == 0:
        print(f"Profanity filter: {'on' if enabled else 'off'}")
    return result


def status() -> int:
    settings = config.load()
    if not settings.enabled:
        print("Profanity filter: off")
        return 0
    if settings.paused_until > time.time():
        until = time.strftime("%H:%M", time.localtime(settings.paused_until))
        print(f"Profanity filter: paused until {until}")
        return 0
    if sys.platform == "win32":
        from .windows import is_ready
        ready = is_ready()
    else:
        from .desktop import status as desktop_status
        state = desktop_status()
        if state.code == "unavailable":
            print("Profanity filter: unavailable (desktop input service is not ready)")
            return 1
        ready = state.ready
    print("Profanity filter: on" if ready else
          "Profanity filter: off (background input guard is not active)")
    return 0
