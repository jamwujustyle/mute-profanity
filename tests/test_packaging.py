import importlib.util
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("package_linux", ROOT / "tools/package-linux.py")
package_linux = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_linux)
windows_spec = importlib.util.spec_from_file_location("build_windows", ROOT / "tools/build-windows.py")
build_windows = importlib.util.module_from_spec(windows_spec)
windows_spec.loader.exec_module(build_windows)


class PackagingTests(unittest.TestCase):
    def source(self, root):
        for name in package_linux.REQUIRED_FILES:
            (root / name).write_text(name, encoding="utf-8")
        (root / "mute_profanity").mkdir()
        (root / "mute_profanity/__init__.py").write_text("", encoding="utf-8")
        (root / "mute_profanity/core.py").write_text("# core\n", encoding="utf-8")
        (root / "assets").mkdir()
        (root / "assets/icon.svg").write_text("<svg/>", encoding="utf-8")

    def test_archive_selects_only_application_files_and_executable_launcher(self):
        with tempfile.TemporaryDirectory(prefix="Mute package ") as directory:
            root = Path(directory)
            self.source(root)
            for name in (".git/config", "private/key", "tests/test_secret.py", "tools/debug.py",
                         "mute_profanity/__pycache__/secret.pyc", "mute_profanity/.local.py",
                         "assets/.private/secret.txt", "assets/cache.pyc"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("PRIVATE", encoding="utf-8")
            output = root / "release/Mute-Linux.zip"
            package_linux.package(root, output)
            with zipfile.ZipFile(output) as archive:
                expected = {*package_linux.REQUIRED_FILES, "mute_profanity/__init__.py",
                            "mute_profanity/core.py", "assets/icon.svg"}
                self.assertEqual(set(archive.namelist()), {f"Mute-Linux/{name}" for name in expected})
                mode = archive.getinfo("Mute-Linux/install-linux.sh").external_attr >> 16
                self.assertEqual(stat.S_IMODE(mode), 0o755)
                self.assertTrue(all(b"PRIVATE" not in archive.read(name) for name in archive.namelist()))
            original = output.read_bytes()
            package_linux.package(root, output)
            self.assertEqual(original, output.read_bytes())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_archive_excludes_symlinks_and_requires_regular_entry_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.source(root)
            (root / "secret.txt").write_text("PRIVATE", encoding="utf-8")
            (root / "assets/link.txt").symlink_to(root / "secret.txt")
            self.assertNotIn(root / "assets/link.txt", package_linux.release_files(root))
            (root / "run.py").unlink()
            (root / "run.py").symlink_to(root / "secret.txt")
            with self.assertRaises(ValueError):
                package_linux.package(root, root / "release.zip")

    def test_build_instructions_are_optional_and_verification_history_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.source(root)
            (root / "BUILDING.md").write_text("Build instructions", encoding="utf-8")
            (root / "VERIFICATION.md").write_text("Operational history", encoding="utf-8")
            selected = package_linux.release_files(root)
            self.assertIn(root / "BUILDING.md", selected)
            self.assertNotIn(root / "VERIFICATION.md", selected)

    def test_windows_archive_contains_only_executable_and_delivery_files(self):
        with tempfile.TemporaryDirectory(prefix="Mute Windows package ") as directory:
            root = Path(directory)
            source, output = root / "source", root / "output"
            source.mkdir()
            output.mkdir()
            for name in ("README.md", "LICENSE", "start-windows.cmd", "VERIFICATION.md",
                         "private-key.txt", "run.py"):
                (source / name).write_text(name, encoding="utf-8")
            # Only package selection is exercised here; this is not a runnable executable.
            fixture = b"MZ synthetic packaging test fixture\x00"
            (output / "Mute.exe").write_bytes(fixture)
            archive_path = build_windows.package(source, output)
            for include_building in (False, True):
                if include_building:
                    (source / "BUILDING.md").write_text("Build instructions", encoding="utf-8")
                    archive_path = build_windows.package(source, output)
                with zipfile.ZipFile(archive_path) as archive:
                    expected = {"Mute.exe", "README.md", "LICENSE", "start-windows.cmd"}
                    if include_building:
                        expected.add("BUILDING.md")
                    self.assertEqual(set(archive.namelist()),
                                     {f"Mute-Windows-x64/{name}" for name in expected})
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(archive.read("Mute-Windows-x64/Mute.exe"), fixture)
                for path in (output / "Mute.exe", archive_path):
                    self.assertEqual(path.with_name(path.name + ".sha256").read_text(),
                                     f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")

    def test_windows_packager_rejects_missing_or_non_windows_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RuntimeError):
                build_windows.package(root, root)
            (root / "Mute.exe").write_bytes(b"not a Windows executable")
            with self.assertRaises(RuntimeError):
                build_windows.package(root, root)

    @unittest.skipUnless(Path("/usr/bin/python3").is_file() and os.name == "posix", "Linux launcher")
    def test_linux_launcher_handles_spaces_and_stops_after_install_failure(self):
        with tempfile.TemporaryDirectory(prefix="Mute's app ") as directory:
            root = Path(directory)
            launcher = root / "install-linux.sh"
            launcher.write_bytes((ROOT / "install-linux.sh").read_bytes())
            (root / "run.py").write_text(
                "import os, pathlib, sys\n"
                "with pathlib.Path(os.environ['MUTE_TEST_LOG']).open('a') as f:\n"
                "    f.write(sys.argv[1] + '\\n')\n"
                "raise SystemExit(int(os.environ.get('MUTE_TEST_EXIT', '0')))\n", encoding="utf-8")
            log = root / "calls.txt"
            env = dict(os.environ, MUTE_TEST_LOG=str(log))
            result = subprocess.run(["/bin/sh", str(launcher)], cwd="/", env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(), "install\nenable\n")
            log.unlink()
            env["MUTE_TEST_EXIT"] = "7"
            result = subprocess.run(["/bin/sh", str(launcher)], cwd="/", env=env, capture_output=True)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(log.read_text(), "install\n")


if __name__ == "__main__":
    unittest.main()
