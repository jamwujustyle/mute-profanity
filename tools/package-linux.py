#!/usr/bin/env python3
"""Build the portable Linux source archive without repository or build files."""

import argparse
from pathlib import Path
import stat
import zipfile


ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_ROOT = "Mute-Linux"
REQUIRED_FILES = ("run.py", "README.md", "LICENSE", "install-linux.sh")
OPTIONAL_FILES = ("BUILDING.md",)


def release_files(source: Path) -> list[Path]:
    """Select application files explicitly; never follow links into private data."""
    source = Path(source)
    result = []
    for name in REQUIRED_FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing regular application file: {name}")
        result.append(path)
    for name in OPTIONAL_FILES:
        path = source / name
        if path.is_file() and not path.is_symlink():
            result.append(path)
    for directory, pattern in (("mute_profanity", "*.py"), ("assets", "*")):
        base = source / directory
        if base.is_symlink() or not base.is_dir():
            raise ValueError(f"Missing regular application directory: {directory}")
        for path in sorted(base.rglob(pattern)):
            relative = path.relative_to(source)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if path.is_symlink() or not path.is_file() or path.suffix in (".pyc", ".pyo"):
                continue
            # A linked parent must not make an otherwise ordinary file distributable.
            if any(parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents):
                continue
            result.append(path)
    if source / "mute_profanity/__init__.py" not in result:
        raise ValueError("Missing application package: mute_profanity/__init__.py")
    return result


def package(source: Path, output: Path) -> Path:
    source, output = Path(source), Path(output)
    files = release_files(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                relative = path.relative_to(source)
                info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{relative.as_posix()}", date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                mode = 0o755 if relative.as_posix() == "install-linux.sh" else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release/Mute-Linux.zip")
    args = parser.parse_args(argv)
    output = package(ROOT, args.output)
    print(f"Created {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
