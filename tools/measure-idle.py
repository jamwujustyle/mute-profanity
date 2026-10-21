#!/usr/bin/python3
"""Measure the idle engine runtime in a temporary config, without desktop input."""
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
with tempfile.TemporaryDirectory(prefix="mute-idle-") as directory:
    os.environ["XDG_CONFIG_HOME"] = directory
    from gi.repository import GLib
    from mute_profanity.engine import Runtime
    runtime = Runtime(popup=False)
    loop = GLib.MainLoop()
    GLib.timeout_add(5000, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    start_cpu = time.process_time()
    start_wall = time.monotonic()
    loop.run()
    wall = time.monotonic() - start_wall
    cpu = time.process_time() - start_cpu
    with open("/proc/self/status", encoding="ascii") as stream:
        rss_kib = next(int(line.split()[1]) for line in stream if line.startswith("VmRSS:"))
    print(json.dumps({"idle_wall_seconds": round(wall, 2), "idle_cpu_seconds": round(cpu, 6),
                      "idle_cpu_percent": round(cpu / wall * 100, 4),
                      "rss_mib": round(rss_kib / 1024, 2),
                      "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)}, indent=2))
    for monitor in runtime.monitors:
        monitor.cancel()
