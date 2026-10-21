#!/usr/bin/python3
"""Reproducible local matcher timing. No input capture or network."""
import json
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mute_profanity.config import Settings
from mute_profanity.core import Matcher

matcher = Matcher(Settings())
samples = ("hello", "привет", "assembly", "cucumber", "страхуй", "хулиган", "fuck", "пиздец", "блядь", "f.u.c.k")
# Exercise the actual per-keystroke path, including intermediate prefixes.
prefixes = [word[:length] for word in samples for length in range(1, len(word) + 1)]
iterations = 2000
start = time.process_time()
for _ in range(iterations):
    for sample in prefixes:
        matcher.detect(sample)
seconds = time.process_time() - start
calls = iterations * len(prefixes)
print(json.dumps({"calls": calls, "cpu_seconds": round(seconds, 3),
                  "microseconds_per_call": round(seconds / calls * 1e6, 2),
                  "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)}, indent=2))
