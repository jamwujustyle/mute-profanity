import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from mute_profanity import install


class InstallTests(unittest.TestCase):
    def test_component_has_two_layouts_and_quoted_paths(self):
        xml = install.component_xml(Path("/tmp/Mute's App/run.py"), Path("/tmp/icon.svg"))
        root = ET.fromstring(xml)
        engines = root.find("engines").findall("engine")
        self.assertEqual([e.findtext("name") for e in engines], ["mute-en", "mute-ru"])
        self.assertEqual([e.findtext("layout") for e in engines], ["us", "ru"])
        import shlex
        self.assertEqual(shlex.split(root.findtext("exec")),
                         ["/usr/bin/python3", "/tmp/Mute's App/run.py", "engine", "--ibus"])

    def test_install_stages_complete_app_without_affecting_host(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "XDG_DATA_HOME": str(Path(directory, "data")),
            "XDG_CONFIG_HOME": str(Path(directory, "config")),
        }), patch.object(install.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(install.install(), 0)
            locations = install.paths()
            self.assertTrue((locations["app"] / "run.py").is_file())
            self.assertTrue((locations["app"] / "mute_profanity/engine.py").is_file())
            self.assertIn("/usr/share/ibus/component", locations["dropin"].read_text())
            self.assertIn("--cache none", locations["dropin"].read_text())
            self.assertIn("Тише", locations["desktop"].read_text())
            self.assertTrue(locations["icon"].is_file())
            import gi
            gi.require_version("IBus", "1.0")
            from gi.repository import IBus
            component = IBus.Component.new_from_file(str(locations["component"]))
            self.assertEqual([e.get_name() for e in component.get_engines()], ["mute-en", "mute-ru"])


if __name__ == "__main__":
    unittest.main()
