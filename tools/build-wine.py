#!/usr/bin/env python3
"""Build Windows artifacts in an explicitly isolated temporary Wine prefix.

This optional Linux helper downloads a checksum-pinned official Python
installer, downloads Windows build wheels, and delegates the same build as CI
to tools/build-windows.py. It never uses or changes the user's Wine prefix.
"""

import argparse
import atexit
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

PYTHON_URL = "https://www.python.org/ftp/python/3.14.9/python-3.14.9-amd64.exe"
PYTHON_SIZE = 33_453_216
PYTHON_SHA256 = "a247a0b612244a803f507b39fb7121e3602e4160d59c3b99d98e735289c53394"
CHUNK_SIZE = 1024 * 1024


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_python(directory: Path):
    installer = directory / "python-3.14.9-amd64.exe"
    if installer.is_file() and installer.stat().st_size == PYTHON_SIZE and digest(installer) == PYTHON_SHA256:
        print("Official Python installer checksum verified.", flush=True)
        return installer
    parts = directory / "python-parts"
    parts.mkdir(parents=True, exist_ok=True)
    # Reuse complete chunks of an earlier interrupted transfer. No partial
    # content is accepted until the final full-file checksum has passed.
    if installer.is_file() and installer.stat().st_size < PYTHON_SIZE:
        with installer.open("rb") as source:
            for index in range(installer.stat().st_size // CHUNK_SIZE):
                target = parts / f"{index:03}.part"
                content = source.read(CHUNK_SIZE)
                if not target.exists():
                    target.write_bytes(content)

    def chunk(index):
        start = index * CHUNK_SIZE
        end = min(PYTHON_SIZE, start + CHUNK_SIZE) - 1
        target = parts / f"{index:03}.part"
        if target.is_file() and target.stat().st_size == end - start + 1:
            return index
        request = urllib.request.Request(PYTHON_URL + f"?mute-range={start}",
                                         headers={"Range": f"bytes={start}-{end}",
                                                  "User-Agent": "Mute-reproducible-build/1"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    expected = f"bytes {start}-{end}/{PYTHON_SIZE}"
                    if response.status != 206 or response.headers.get("Content-Range") != expected:
                        raise RuntimeError(f"Unexpected HTTP range response for chunk {index}")
                    content = response.read(end - start + 2)
                if len(content) != end - start + 1:
                    raise RuntimeError(f"Incomplete installer chunk {index}")
                target.write_bytes(content)
                return index
            except (OSError, RuntimeError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)

    count = (PYTHON_SIZE + CHUNK_SIZE - 1) // CHUNK_SIZE
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(chunk, index) for index in range(count)]
        for completed, future in enumerate(as_completed(futures), 1):
            future.result()
            print(f"Python installer chunks ready: {completed}/{count}", flush=True)
    pending = installer.with_suffix(".download")
    with pending.open("wb") as target:
        for index in range(count):
            with (parts / f"{index:03}.part").open("rb") as source:
                shutil.copyfileobj(source, target)
    if pending.stat().st_size != PYTHON_SIZE or digest(pending) != PYTHON_SHA256:
        pending.unlink(missing_ok=True)
        raise RuntimeError("Official Python installer SHA-256 mismatch; no installer was executed")
    pending.replace(installer)
    print("Official Python installer checksum verified.", flush=True)
    return installer


def windows_path(path):
    # Wine's default read-only bridge to ordinary source paths is drive Z:.
    return "Z:" + str(Path(path).resolve()).replace("/", "\\")


def run_checked(command, *, environment, timeout):
    """Bound the entire virtual-display process group, not just its wrapper."""
    process = subprocess.Popen(command, env=environment, start_new_session=True)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        subprocess.run(["wineserver", "-k"], env=environment, timeout=10, check=False)
        raise
    if code:
        raise subprocess.CalledProcessError(code, command)


def stage_source(source, prefix):
    stage = prefix / "drive_c/MuteSource"
    if stage.is_symlink() or prefix.resolve() not in stage.resolve().parents:
        raise RuntimeError("The managed build source must stay inside the isolated Wine prefix")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for name in ("run.py", "README.md", "LICENSE", "BUILDING.md", "start-windows.cmd"):
        if (source / name).is_file():
            shutil.copy2(source / name, stage / name)
    for name in ("mute_profanity", "assets"):
        shutil.copytree(source / name, stage / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".*"))
    (stage / "tools").mkdir(exist_ok=True)
    shutil.copy2(source / "tools/build-windows.py", stage / "tools/build-windows.py")
    return stage


def extract_python_msi(installer, directory, prefix, environment):
    """Use official x64 payloads when a 32-bit bootstrap cannot run in Wine."""
    if not shutil.which("cabextract"):
        raise RuntimeError("This Wine installation needs optional cabextract for the x64 MSI fallback")
    wine_root = Path(shutil.which("wine")).resolve().parent.parent
    candidates = (wine_root / "lib/wine/x86_64-windows/msiexec.exe",
                  Path("/usr/lib/x86_64-linux-gnu/wine/x86_64-windows/msiexec.exe"),
                  prefix / "drive_c/windows/system32/msiexec.exe")
    msiexec = next((path for path in candidates if path.is_file()), None)
    if msiexec is None:
        raise RuntimeError("Cannot find Wine's 64-bit msiexec for the official payload extraction")
    extracted = directory / "extracted"
    subprocess.run(["cabextract", "-q", "-d", str(extracted), str(installer)], check=True, timeout=30)
    manifest = ET.fromstring((extracted / "0").read_text(encoding="utf-8-sig"))
    payloads = {item.attrib.get("Id"): item.attrib.get("SourcePath") for item in manifest.iter()
                if item.tag.rsplit("}", 1)[-1] == "Payload"}
    for name in ("core", "exe", "lib", "tcltk", "pip"):
        payload = payloads.get(name + "_JustForMe")
        if not payload or Path(payload).name != payload or not (extracted / payload).is_file():
            raise RuntimeError(f"Missing official Python {name} payload")
        print(f"Extracting official x64 Python {name} payload…", flush=True)
        marker = {"core": "python314.dll", "exe": "python.exe", "lib": "Lib/encodings/__init__.py",
                  "tcltk": "DLLs/_tkinter.pyd", "pip": "Lib/ensurepip/__init__.py"}[name]
        if (prefix / "drive_c/Python" / marker).is_file():
            continue
        run_checked(["wine", str(msiexec), "/a", windows_path(extracted / payload),
                     "/qn", "TARGETDIR=C:\\Python"], environment=environment, timeout=60)
    python = prefix / "drive_c/Python/python.exe"
    if not python.is_file():
        raise RuntimeError("Official payload extraction did not create python.exe")
    run_checked(["wine", str(python), "-m", "ensurepip", "--default-pip"],
                 environment=environment, timeout=90)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/mute-windows-build"))
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--winearch", choices=("wow64", "win64"), default="wow64",
                        help="Wine 11 wow64 mode avoids requiring 32-bit host libraries")
    parser.add_argument("--in-display", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    directory = args.workdir.resolve()
    normal_prefix = (Path.home() / ".wine").resolve()
    if directory == Path.home() or directory == normal_prefix or normal_prefix in directory.parents:
        parser.error("Use a separate temporary build directory, never the normal Wine prefix")
    directory.mkdir(parents=True, exist_ok=True)
    installer = download_python(directory)
    if args.download_only:
        return 0
    for tool in ("wine", "xvfb-run"):
        if not shutil.which(tool):
            parser.error(f"Missing optional Linux build dependency: {tool}")
    source = Path(__file__).resolve().parent.parent
    prefix = directory / "wine"
    if prefix.is_symlink() or prefix.resolve() != prefix:
        parser.error("The isolated Wine prefix must not be a symlink")
    environment = {**os.environ, "WINEPREFIX": str(prefix), "WINEARCH": args.winearch, "WINEDEBUG": "-all",
                   "WINEDLLOVERRIDES": "mscoree,mshtml=",
                   "PYTHONIOENCODING": "utf-8", "GSETTINGS_BACKEND": "memory", "GIO_USE_VFS": "local",
                   "DBUS_SESSION_BUS_ADDRESS": f"unix:path={directory / 'no-dbus.sock'}",
                   "IBUS_ADDRESS": f"unix:path={directory / 'no-ibus.sock'}",
                   "XDG_CACHE_HOME": str(directory / "cache"),
                   "XDG_CONFIG_HOME": str(directory / "config"),
                   "XDG_DATA_HOME": str(directory / "data")}
    environment.pop("WAYLAND_DISPLAY", None)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    if not args.in_display:
        run_checked(["xvfb-run", "-a", sys.executable, str(Path(__file__).resolve()),
                     "--workdir", str(directory), "--winearch", args.winearch, "--in-display"],
                    environment=environment, timeout=1200)
        return 0
    atexit.register(lambda: subprocess.run(["wineserver", "-k"], env=environment, timeout=10, check=False))
    python = prefix / "drive_c/Python/python.exe"
    if not python.is_file():
        print("Installing Python inside the isolated Wine prefix…", flush=True)
        try:
            run_checked(["wine", str(installer), "/quiet", "InstallAllUsers=0",
                        "TargetDir=C:\\Python", "Include_launcher=0", "Include_test=0", "Include_doc=0",
                        "Include_pip=1", "Include_tcltk=1"], environment=environment, timeout=180)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print("Using official x64 MSI payloads instead of the 32-bit installer bootstrap…", flush=True)
            extract_python_msi(installer, directory, prefix, environment)
    if not python.is_file():
        extract_python_msi(installer, directory, prefix, environment)
    if prefix not in python.resolve().parents:
        raise RuntimeError("The Windows Python interpreter must stay inside the isolated Wine prefix")
    if not (prefix / "drive_c/Python/Lib/site-packages/pip/__init__.py").is_file():
        run_checked(["wine", str(python), "-m", "ensurepip", "--default-pip"],
                    environment=environment, timeout=90)
    wheels = directory / "wheels"
    wheels.mkdir(exist_ok=True)
    print("Downloading pinned Windows build-tool wheels…", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--no-cache-dir",
                    "--dest", str(wheels), "--platform", "win_amd64", "--python-version", "314",
                    "--implementation", "cp", "--only-binary=:all:", "pyinstaller==6.22.3",
                    "pefile>=2022.5.30", "pywin32-ctypes>=0.2.1"],
                   check=True, timeout=240)
    print("Installing build tools inside the isolated Wine prefix…", flush=True)
    run_checked(["wine", str(python), "-m", "pip", "install",
                    "--no-index", "--no-cache-dir", "--find-links", windows_path(wheels), "pyinstaller==6.22.3"],
                   environment=environment, timeout=180)
    stage = stage_source(source, prefix)
    print("Building the Windows one-file executable…", flush=True)
    run_checked(["wine", str(python),
                  "C:\\MuteSource\\tools\\build-windows.py"], environment=environment, timeout=660)
    output = source / "release"
    output.mkdir(exist_ok=True)
    for name in ("Mute.exe", "Mute-Windows-x64.zip", "Mute.exe.sha256", "Mute-Windows-x64.zip.sha256"):
        shutil.copy2(stage / "release" / name, output / name)
    print(f"Windows artifacts copied to {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
