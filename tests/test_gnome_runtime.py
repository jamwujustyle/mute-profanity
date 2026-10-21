"""Actual GNOME 46 runtime checks; use only tools/test-gnome.sh.

No keyboard/mouse events are injected. The nested Shell runs on a disposable
Xvfb display, private session bus, temporary XDG directories and memory settings.
"""

import os
from pathlib import Path
import subprocess
import time
import unittest


_RUN = os.environ.get("MUTE_GNOME_RUNTIME") == "1"


@unittest.skipUnless(_RUN, "Run tools/test-gnome.sh for isolated GNOME modal checks")
class GnomeRuntimeTests(unittest.TestCase):
    def test_real_shell_show_hide_countdown_and_extension_lifecycle(self):
        from gi.repository import Gio, GLib

        root = Path(os.environ.get("MUTE_GNOME_TEST_ROOT", ""))
        self.assertEqual(root.parent, Path("/tmp"))
        self.assertTrue(root.name.startswith("mute-gnome-test."))
        self.assertEqual(os.environ.get("XDG_RUNTIME_DIR"), str(root / "runtime"))
        self.assertEqual(os.environ.get("GSETTINGS_BACKEND"), "memory")
        self.assertTrue(os.environ.get("DISPLAY", "").startswith(":"))
        self.assertEqual(os.environ.get("MUTE_GNOME_PRIVATE_DBUS"), "1")
        self.assertNotIn("/run/user/", os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""))
        self.assertIn(str(root / "session-bus.sock"), os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""))
        self.assertEqual(os.environ.get("DBUS_SYSTEM_BUS_ADDRESS"), os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
        log_path = root / "shell.log"
        with log_path.open("w+") as log:
            shell = subprocess.Popen([
                "gnome-shell", "--nested", "--wayland", "--no-x11", "--sm-disable",
                "--mode=user", "--wayland-display=mute-runtime-test",
            ], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            try:
                connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                bus_name = "org.gnome.Shell"
                path = "/org/gnome/Shell/Extensions/MuteProfanity"
                interface = "org.gnome.Shell.Extensions.MuteProfanity"

                def call(method, value=None, response="()", timeout=1000):
                    return connection.call_sync(
                        bus_name, path, interface, method, value,
                        GLib.VariantType.new(response), Gio.DBusCallFlags.NO_AUTO_START,
                        timeout, None,
                    ).unpack()

                deadline = time.monotonic() + 20
                while True:
                    try:
                        result = connection.call_sync(
                            bus_name, path, "org.freedesktop.DBus.Introspectable", "Introspect",
                            None, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NO_AUTO_START,
                            300, None,
                        ).unpack()[0]
                        if interface in result:
                            break
                    except GLib.Error:
                        pass
                    if shell.poll() is not None or time.monotonic() >= deadline:
                        log.flush()
                        self.fail("Private GNOME did not export the extension:\n" + log_path.read_text())
                    time.sleep(0.1)

                self.assertEqual((True,), call("Show", GLib.Variant("(u)", (2,)), "(b)"))
                self.assertIsNone(shell.poll(), "Nested Shell exited during its modal grab")
                allocation_deadline = time.monotonic() + 0.75
                while True:
                    exists, grabbed, width, height, stage_width, stage_height = call("Probe", response="(bbuuuu)")
                    if width and height:
                        break
                    self.assertLess(time.monotonic(), allocation_deadline, "Modal actor was not allocated")
                    time.sleep(0.05)
                self.assertTrue(exists)
                self.assertTrue(grabbed, "ModalDialog did not retain its keyboard/pointer grab")
                self.assertGreater(stage_width, 0)
                self.assertGreater(stage_height, 0)
                self.assertEqual((stage_width, stage_height), (width, height), "Modal does not cover the stage")
                time.sleep(2.5)
                self.assertIsNone(shell.poll(), "Nested Shell exited after countdown expiry")
                exists, grabbed, *_ = call("Probe", response="(bbuuuu)")
                self.assertFalse(exists, "Countdown expiry did not destroy its modal actor")
                self.assertFalse(grabbed, "Countdown expiry left a seat grab behind")
                self.assertEqual((), call("Hide"))  # Safe even after expiry.
                self.assertEqual((True,), call("Show", GLib.Variant("(u)", (1,)), "(b)"))
                self.assertEqual((), call("Hide"))
                self.assertEqual((False, False), call("Probe", response="(bbuuuu)")[:2])
                log.flush()
                output = log_path.read_text()
                for failure in ("Mute cooldown cleanup failed", "Mute cooldown was closed after a dialog error",
                                "JS ERROR: Extension mute-profanity@local", "Error: Mute"):
                    self.assertNotIn(failure, output)
                print("Private GNOME 46: Show acquired full-stage modal; expiry released/destroyed it; Hide/re-grab passed.")
            except Exception as error:
                log.flush()
                self.fail(f"{error}\nPrivate GNOME log:\n" + log_path.read_text()[-16000:])
            finally:
                shell.terminate()
                try:
                    shell.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    shell.kill()
                    shell.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
