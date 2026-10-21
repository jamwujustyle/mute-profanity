"""One argument builder for source checkouts and packaged executables."""

from pathlib import Path
import sys


def command(*arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "run.py"), *arguments]
