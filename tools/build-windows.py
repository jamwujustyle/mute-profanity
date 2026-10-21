#!/usr/bin/env python3
"""Build the Windows x64 one-file GUI executable and portable ZIP.

Run under 64-bit Windows Python 3.14 with PyInstaller 6.22.3 installed. The
result needs neither Python nor paid services on the destination computer.
"""

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zipfile

PYINSTALLER_VERSION = "6.22.3"


def package(source: Path, output: Path) -> Path:
    executable = output / "Mute.exe"
    if not executable.is_file():
        raise RuntimeError("PyInstaller did not produce a Windows executable")
    with executable.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("PyInstaller did not produce a Windows executable")
    archive = output / "Mute-Windows-x64.zip"
    files = [executable]
    for name in ("README.md", "LICENSE", "start-windows.cmd", "BUILDING.md"):
        path = source / name
        if path.is_file():
            files.append(path)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for path in files:
            target.write(path, f"Mute-Windows-x64/{path.name}")
    for path in (executable, archive):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        path.with_name(path.name + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if os.name != "nt" or struct.calcsize("P") != 8:
        parser.error("Build with 64-bit Windows Python (or the isolated tools/build-wine.py helper)")
    if sys.version_info[:2] != (3, 14):
        parser.error("Use Python 3.14 for the pinned, reproducible Windows build")
    try:
        installed = version("pyinstaller")
    except PackageNotFoundError:
        parser.error(f"Install the build tool first: python -m pip install pyinstaller=={PYINSTALLER_VERSION}")
    if installed != PYINSTALLER_VERSION:
        parser.error(f"Expected PyInstaller {PYINSTALLER_VERSION}; found {installed}")
    source = args.source.resolve()
    output = (args.output or source / "release").resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mute-pyinstaller-") as temporary:
        work = Path(temporary)
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
                   "--windowed", "--noupx", "--name", "Mute", "--distpath", str(output),
                   "--workpath", str(work / "work"), "--specpath", str(work),
                   "--paths", str(source)]
        for module in ("gi", "dbus", "mute_profanity.ui", "mute_profanity.engine",
                       "mute_profanity.desktop", "mute_profanity.install", "mute_profanity.gnome_modal"):
            command.extend(("--exclude-module", module))
        for module in ("tkinter", "tkinter.ttk", "ctypes.wintypes", "winsound", "winreg"):
            command.extend(("--hidden-import", module))
        command.append(str(source / "run.py"))
        subprocess.run(command, cwd=source, check=True, timeout=600)
    archive = package(source, output)
    print(f"Built {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
