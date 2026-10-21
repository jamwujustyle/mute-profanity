"""Per-user Windows installation/startup; no administrator access required."""

from dataclasses import replace
import filecmp
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from . import config

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "MuteProfanity"


def application_dir() -> Path:
    return config.config_dir() / "application"


def installed_command(*arguments: str) -> list[str]:
    root = application_dir()
    if getattr(sys, "frozen", False):
        return [str(root / "Mute.exe"), *arguments]
    executable = Path(sys.executable)
    windowed = executable.with_name("pythonw.exe")
    return [str(windowed if windowed.is_file() else executable), str(root / "run.py"), *arguments]


def startup(enable: bool):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enable:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ,
                             subprocess.list2cmdline(installed_command("engine")))
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def _shortcut():
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    path = appdata / "Microsoft/Windows/Start Menu/Programs/Mute.lnk"
    path.parent.mkdir(parents=True, exist_ok=True)
    command = installed_command("settings")
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + quote(path) + ");"
              "$s.TargetPath=" + quote(command[0]) + ";"
              "$s.Arguments=" + quote(subprocess.list2cmdline(command[1:])) + ";"
              "$s.WorkingDirectory=" + quote(application_dir()) + ";$s.Save()")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        # The portable executable remains usable if PowerShell is restricted.
        pass


def _installation_files() -> list[tuple[Path, Path]]:
    target = application_dir()
    if getattr(sys, "frozen", False):
        return [(Path(sys.executable).resolve(), target / "Mute.exe")]
    source = Path(__file__).resolve().parent.parent
    files = [(source / "run.py", target / "run.py")]
    for path in sorted((source / "mute_profanity").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files.append((path, target / path.relative_to(source)))
    files.extend((source / name, target / name) for name in ("README.md", "VERIFICATION.md", "LICENSE")
                 if (source / name).is_file())
    return files


def _installation_needs_update() -> bool:
    for source, destination in _installation_files():
        if source.resolve() == destination.resolve():
            continue
        if not destination.is_file() or not filecmp.cmp(source, destination, shallow=False):
            return True
    return False


def install():
    from . import windows
    target = application_dir()
    if _installation_needs_update():
        if windows.is_running():
            raise RuntimeError("A different version of Mute is running. Stop filtering first, "
                               "then click Start in this new copy to install the update.")
        target.mkdir(parents=True, exist_ok=True)
        for source, destination in _installation_files():
            if source.resolve() == destination.resolve():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if getattr(sys, "frozen", False):
                # A locked installed settings window must not leave a partial executable.
                staged = destination.with_suffix(".exe.update")
                try:
                    shutil.copy2(source, staged)
                    os.replace(staged, destination)
                except PermissionError as error:
                    raise RuntimeError("Close any other installed Mute windows, then click Start "
                                       "in this new copy again to finish the update.") from error
                finally:
                    staged.unlink(missing_ok=True)
            else:
                shutil.copy2(source, destination)
    config.wordlist_dir().mkdir(parents=True, exist_ok=True)
    _shortcut()
    return 0


def enable():
    from . import windows
    if _installation_needs_update():
        install()
    config.save(replace(config.load(), enabled=True, paused_until=0))
    windows.refresh()
    startup(True)
    if not windows.is_running():
        subprocess.Popen(installed_command("engine"), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                                       getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    deadline = time.monotonic() + 3
    while not windows.is_ready() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not windows.is_ready():
        raise RuntimeError("The keyboard and mouse guard did not become ready. "
                           "Stop filtering and try Start again; run Mute.exe doctor for details.")
    print("Mute is running in the background and will start when you sign in. You can close Settings.")
    return 0


def disable():
    from . import windows
    config.save(replace(config.load(), enabled=False))
    windows.refresh()
    startup(False)
    if windows.is_running():
        if not windows.stop():
            raise RuntimeError("Filtering is disabled in settings, but the background process could not be asked to stop. "
                               "Run Mute.exe doctor before restarting it.")
        deadline = time.monotonic() + 3
        while windows.is_running() and time.monotonic() < deadline:
            time.sleep(0.05)
        if windows.is_running():
            raise RuntimeError("Filtering is disabled in settings, but the background process has not stopped yet. "
                               "Wait before starting it again; run Mute.exe doctor for details.")
    print("Mute is disabled; automatic startup has been removed.")
    return 0


def uninstall():
    from . import windows
    disable()
    deadline = time.monotonic() + 3
    while windows.is_running() and time.monotonic() < deadline:
        time.sleep(0.05)
    target = application_dir()
    # Windows cannot remove the currently running packaged settings executable.
    # Remove everything removable; report the exact remaining app directory.
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        pass
    except OSError:
        print(f"The filter is stopped. Close this window, then delete {target} to finish removing the app.")
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    (appdata / "Microsoft/Windows/Start Menu/Programs/Mute.lnk").unlink(missing_ok=True)
    print("Your settings and dictionaries have been kept.")
    return 0


def doctor():
    from . import windows
    running, ready = windows.is_running(), windows.is_ready()
    report = "\n".join((
        f"Background engine: {'running' if running else 'not running'}",
        f"Keyboard and mouse hooks: {'ready' if ready else 'not ready'}",
        f"Checking enabled: {config.load().enabled}",
        f"Dictionaries: {config.wordlist_dir()}",
        "Native Windows hook behavior must be verified on the target Windows desktop.",
    ))
    if sys.stdout is None:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        try:
            root.withdraw()
            messagebox.showinfo("Mute diagnostics", report, parent=root)
        finally:
            root.destroy()
    else:
        print(report)
    return 0 if ready else 1


def main(command):
    try:
        return {"install": install, "enable": enable, "disable": disable,
                "uninstall": uninstall, "doctor": doctor}[command]()
    except (OSError, RuntimeError) as error:
        if sys.stderr is not None:
            print(str(error), file=sys.stderr)
        return 1
