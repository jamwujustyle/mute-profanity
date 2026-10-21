from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from mute_profanity.launch import command


class LaunchTests(unittest.TestCase):
    def test_source_launches_entrypoint_and_preserves_separate_arguments(self):
        with patch.object(sys, "frozen", False, create=True):
            result = command("--intervention", "8")
        self.assertEqual(result[0], sys.executable)
        self.assertEqual(Path(result[1]).name, "run.py")
        self.assertEqual(result[2:], ["--intervention", "8"])

    def test_packaged_launch_does_not_pass_run_py_to_the_executable(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", r"C:\Apps With Spaces\Mute.exe"):
            self.assertEqual(command("engine"), [r"C:\Apps With Spaces\Mute.exe", "engine"])


if __name__ == "__main__":
    unittest.main()
