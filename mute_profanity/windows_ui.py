"""On-demand Windows preferences and cooldown screens, using only Tk/stdlib.

The background hook process is separate. Closing this UI never stops it. Input
suppression belongs to the Windows backend, not to a Tk keyboard grab.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from . import config


def save_settings(settings):
    config.save(settings)
    try:
        from .windows import refresh

        refresh()
    except (ImportError, OSError, RuntimeError):
        # No running backend is normal before the first background start.
        pass


def word_lines(text: str) -> list[str]:
    words = list(dict.fromkeys(line.strip().casefold() for line in text.splitlines() if line.strip()))
    if len(words) > 1000:
        raise ValueError("Each list may contain at most 1,000 words.")
    if any(len(word) > 128 or any(character.isspace() for character in word) for word in words):
        raise ValueError("Use one word per line, without spaces; maximum 128 characters per word.")
    return words


def monitor_rectangles() -> list[tuple[int, int, int, int]]:
    """Return physical monitor rectangles, including negative desktop origins."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    result = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def collect(_monitor, _dc, rectangle, _data):
        area = rectangle.contents
        result.append((area.left, area.top, area.right - area.left, area.bottom - area.top))
        return True

    callback = callback_type(collect)
    user32.EnumDisplayMonitors.argtypes = (wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM)
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.EnumDisplayMonitors(None, None, callback, 0)
    if not result:
        result = [(user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
                   user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))]
    return result


def place_overlay(window, rectangle):
    left, top, width, height = rectangle
    window.geometry(f"{width}x{height}+0+0")
    window.update_idletasks()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT)
        user32.SetWindowPos.restype = wintypes.BOOL
        handle = user32.GetAncestor(window.winfo_id(), 2) or window.winfo_id()
        if not user32.SetWindowPos(handle, -1, left, top, width, height, 0x0040):
            raise OSError(ctypes.get_last_error(), "Cannot position the cooldown screen")
    else:
        window.geometry(f"{width}x{height}{left:+d}{top:+d}")


class Preferences:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.root = tk, ttk, root
        self.settings = config.load()
        self.closed = False
        self.process = None
        self.process_started = 0.0
        root.title("Тише / Mute")
        root.geometry("620x760")
        root.minsize(500, 460)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.bind("<Destroy>", self._destroyed, add=True)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 10, "bold"))
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        scroll = ttk.Frame(root)
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.columnconfigure(0, weight=1)
        scroll.rowconfigure(0, weight=1)
        canvas = tk.Canvas(scroll, highlightthickness=0)
        bar = ttk.Scrollbar(scroll, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        page = ttk.Frame(canvas, padding=24)
        page.columnconfigure(0, weight=1)
        page_id = canvas.create_window((0, 0), window=page, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(page_id, width=event.width))
        page.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        root.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        row = 0

        def label(text, *, title=False):
            nonlocal row
            widget = ttk.Label(page, text=text, wraplength=540,
                               style="Title.TLabel" if title else "TLabel")
            widget.grid(row=row, column=0, sticky="ew", pady=(0, 10))
            row += 1
            return widget

        label("Watch your language.", title=True)
        label("An offline Russian / English profanity filter. No AI, accounts or subscriptions.")
        self.status = tk.StringVar()
        ttk.Label(page, textvariable=self.status, wraplength=540).grid(row=row, column=0, sticky="ew", pady=(4, 14))
        row += 1
        actions = ttk.Frame(page)
        actions.grid(row=row, column=0, sticky="ew", pady=(0, 18))
        row += 1
        self.start_button = ttk.Button(actions, text="Start in background", command=self._start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="Stop filtering", command=self._stop)
        self.stop_button.pack(side="left")

        behavior = ttk.LabelFrame(page, text="Behavior", padding=12)
        behavior.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        behavior.columnconfigure(0, weight=1)
        row += 1
        self.flags = {}
        for index, (field, title) in enumerate((
            ("enabled", "Filter enabled"), ("instant_trigger", "React immediately"),
            ("escalate", "Increase the pause after repeated attempts"), ("beep", "Play a warning sound"),
        )):
            variable = tk.BooleanVar(value=getattr(self.settings, field))
            self.flags[field] = variable
            ttk.Checkbutton(behavior, text=title, variable=variable).grid(row=index, column=0, sticky="w", pady=4)
        ttk.Label(behavior, text="Immediate mode reacts as soon as a word matches. An ordinary word's prefix can also match; add exceptions below. With it off, checking waits for a word boundary.",
                  wraplength=500).grid(row=4, column=0, sticky="ew", pady=(8, 10))
        duration = ttk.Frame(behavior)
        duration.grid(row=5, column=0, sticky="ew")
        ttk.Label(duration, text="Pause after a match, seconds").pack(side="left")
        self.cooldown = tk.StringVar(value=str(self.settings.cooldown_seconds))
        ttk.Spinbox(duration, from_=3, to=60, increment=1, textvariable=self.cooldown, width=5).pack(side="right")

        languages = ttk.LabelFrame(page, text="Languages", padding=12)
        languages.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        row += 1
        for field, text in (("filter_ru", "Russian profanity"), ("filter_en", "English profanity")):
            variable = tk.BooleanVar(value=getattr(self.settings, field))
            self.flags[field] = variable
            ttk.Checkbutton(languages, text=text, variable=variable).pack(anchor="w", pady=3)

        self.editors = {}
        for field, title, hint in (
            ("allow_words", "Allowed words", "Full words, one per line. Immediate matching waits while input is a prefix of an exception."),
            ("extra_words", "My prohibited words", "One word per line. Pause filtering before entering a blocked word here."),
        ):
            label(title)
            label(hint)
            editor = tk.Text(page, height=3, wrap="none", relief="solid", borderwidth=1,
                             font=("Segoe UI", 10), undo=False)
            editor.insert("1.0", "\n".join(getattr(self.settings, field)))
            editor.grid(row=row, column=0, sticky="ew", pady=(0, 16))
            row += 1
            self.editors[field] = editor

        label("Add another language")
        label("Place UTF-8 .txt files in the dictionary folder: one word per line; # starts a comment. A trailing * matches a word stem of at least 3 characters. Files reload automatically.")
        ttk.Button(page, text="Open dictionary folder", command=self._open_dictionaries).grid(row=row, column=0, sticky="w", pady=(0, 16))
        row += 1
        label("Text is never logged. The background filter keeps only the current word in memory. Ctrl+Shift+F12 pauses filtering for 5 minutes.")

        footer = ttk.Frame(root, padding=(18, 12))
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.message = tk.StringVar()
        ttk.Label(footer, textvariable=self.message, wraplength=560).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        buttons = ttk.Frame(footer)
        buttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons, text="Pause 5 minutes", command=self._pause).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Resume", command=self._resume).pack(side="left")
        ttk.Button(buttons, text="Save settings", command=self._save).pack(side="right")
        self._refresh_status()

    def _read_settings(self):
        try:
            seconds = int(self.cooldown.get())
        except ValueError:
            raise ValueError("Pause must be a whole number from 3 to 60 seconds.") from None
        if not 3 <= seconds <= 60:
            raise ValueError("Pause must be from 3 to 60 seconds.")
        values = {field: variable.get() for field, variable in self.flags.items()}
        values.update({field: word_lines(editor.get("1.0", "end-1c")) for field, editor in self.editors.items()})
        return replace(config.load(), cooldown_seconds=seconds, **values)

    def _save(self, *, paused_until=None):
        try:
            settings = self._read_settings()
            if paused_until is not None:
                settings = replace(settings, paused_until=paused_until)
            save_settings(settings)
            self.settings = settings
            self.message.set("Settings saved.")
            return True
        except (OSError, ValueError) as error:
            self.message.set(f"Could not save: {error}")
            return False

    def _pause(self):
        if self._save(paused_until=time.time() + 300):
            self.message.set("Filtering paused for 5 minutes.")

    def _resume(self):
        if self._save(paused_until=0):
            self.message.set("Filtering resumed if the background process is running.")

    def _start(self):
        self.flags["enabled"].set(True)
        if self._save(paused_until=0):
            self._run_command("enable")

    def _stop(self):
        self.flags["enabled"].set(False)
        if self._save():
            self._run_command("disable")

    def _run_command(self, action):
        if self.process is not None:
            return
        from .launch import command

        try:
            self.process = subprocess.Popen(command(action), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             text=True, encoding="utf-8", errors="replace",
                                             env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            self.message.set(str(error))
            return
        self.process_started = time.monotonic()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.message.set("Updating the background filter…")
        self.root.after(100, self._poll_command)

    def _poll_command(self):
        if self.closed or self.process is None:
            return
        if self.process.poll() is None:
            if time.monotonic() - self.process_started < 20:
                self.root.after(100, self._poll_command)
                return
            self.process.kill()
            output, error = self.process.communicate()
            self.message.set("The background command timed out. Run Doctor from the launcher for details.")
        else:
            output, error = self.process.communicate()
            self.message.set((output if self.process.returncode == 0 else error or output or "Could not update the background filter.").strip())
        self.process = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="normal")

    def _refresh_status(self):
        if self.closed:
            return
        try:
            from .windows import is_ready

            running = is_ready()
        except (ImportError, OSError, AttributeError):
            running = None
        settings = config.load()
        if running is False:
            text = "The background process is not running. Click Start in background."
        elif running is None:
            text = "Background status is unavailable. Start the filter, then use Doctor to verify it."
        elif not settings.enabled:
            text = "The background process is running; filtering is disabled."
        elif settings.paused_until > time.time():
            text = "Filtering paused until " + time.strftime("%H:%M", time.localtime(settings.paused_until)) + "."
        else:
            text = "The background filter is running. You can close this settings window."
        self.status.set(text)
        self.root.after(3000, self._refresh_status)

    def _open_dictionaries(self):
        directory = config.wordlist_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))
        except (OSError, AttributeError) as error:
            self.message.set(f"Dictionary folder: {directory}. {error}")

    def _destroyed(self, event):
        if event.widget == self.root:
            self.closed = True


class Intervention:
    def __init__(self, root, seconds):
        import tkinter as tk
        from tkinter import ttk

        seconds = max(0, min(60, seconds))
        self.root = root
        self.closed = False
        self.deadline = time.monotonic() + max(0, seconds)
        self.duration = max(0.001, seconds)
        self.countdowns = []
        self.progress = []
        self.surfaces = [root]
        screens = monitor_rectangles() or [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]
        for index, rectangle in enumerate(screens):
            window = root if index == 0 else tk.Toplevel(root)
            if index:
                self.surfaces.append(window)
            window.title("Watch your language.")
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.configure(background="#18271f")
            window.protocol("WM_DELETE_WINDOW", lambda: None)
            window.bind("<KeyPress>", self._key_press)
            content = tk.Frame(window, background="#18271f", padx=32, pady=32)
            content.place(relx=0.5, rely=0.5, anchor="center")
            for text, font in (("Watch your language.", ("Segoe UI", 32, "bold")),
                               ("Profanity blocked. Take a breath and rephrase.", ("Segoe UI", 13))):
                tk.Label(content, text=text, font=font, foreground="#f2f6f2", background="#18271f",
                         wraplength=max(240, rectangle[2] - 80), justify="left").pack(anchor="w", pady=(0, 25))
            variable = tk.StringVar()
            self.countdowns.append(variable)
            tk.Label(content, textvariable=variable, font=("Segoe UI", 13), foreground="#f2f6f2",
                     background="#18271f").pack(anchor="w", pady=(0, 20))
            progress = ttk.Progressbar(content, maximum=1, length=min(500, rectangle[2] - 80))
            progress.pack(fill="x", pady=(0, 20))
            self.progress.append(progress)
            tk.Label(content, text="Emergency pause: Ctrl+Shift+F12", font=("Segoe UI", 10),
                     foreground="#b9c9be", background="#18271f").pack(anchor="w")
            place_overlay(window, rectangle)
        root.focus_force()
        if config.load().beep:
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except (ImportError, OSError, RuntimeError):
                root.bell()
        self._tick()

    def _tick(self):
        if self.closed:
            return
        remaining = max(0, self.deadline - time.monotonic())
        settings = config.load()
        if remaining <= 0 or not settings.enabled or settings.paused_until > time.time():
            self.closed = True
            self.root.destroy()
            return
        for countdown, progress in zip(self.countdowns, self.progress):
            count = math.ceil(remaining)
            countdown.set(f"Try again in {count} {'second' if count == 1 else 'seconds'}.")
            progress.configure(value=min(1, 1 - remaining / self.duration))
        self.root.after(100, self._tick)

    def _key_press(self, event):
        if event.keysym == "F12" and event.state & 0x0005 == 0x0005:
            try:
                save_settings(replace(config.load(), paused_until=time.time() + 300))
            except OSError:
                pass
            self.closed = True
            self.root.destroy()
        return "break"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Тише / Mute — offline input filter")
    parser.add_argument("--settings", action="store_true")
    parser.add_argument("--intervention", type=float, metavar="SECONDS")
    args = parser.parse_args(argv)
    if args.intervention is not None and not math.isfinite(args.intervention):
        parser.error("SECONDS must be finite")
    try:
        import tkinter as tk
    except ImportError as error:
        print(f"Mute: {error}", file=sys.stderr)
        return 1
    try:
        if os.name == "nt":
            import ctypes

            ctypes.WinDLL("user32").SetProcessDPIAware()
        root = tk.Tk()
        if args.intervention is None:
            application = Preferences(root)
        else:
            application = Intervention(root, args.intervention)
        root.mainloop()
        return 0
    except (tk.TclError, OSError, ValueError) as error:
        print(f"Mute: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
