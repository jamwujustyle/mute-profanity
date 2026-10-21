"""Display-independent composition and cooldown logic.

Only the current word is retained. Safe compositions may be committed by IBus
on focus loss; unsafe ones must use CLEAR. No backspace injection is required.
"""

from dataclasses import dataclass
from bisect import bisect_left
import time
import unicodedata

from .config import Settings
from .detector import Detector

MAX_COMPOSITION = 128


def allow_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    start, end = 0, len(text)
    while start < end and unicodedata.category(text[start])[0] in "PS":
        start += 1
    while end > start and unicodedata.category(text[end - 1])[0] in "PS":
        end -= 1
    return text[start:end]


class Matcher:
    def __init__(self, settings: Settings, wordlists=None):
        self.allowed = {allow_key(word) for word in settings.allow_words}
        self._allowed_ordered = sorted(self.allowed)
        self.builtin = Detector(filter_en=settings.filter_en, filter_ru=settings.filter_ru,
                                allow_words=settings.allow_words, extra_words=settings.extra_words)
        self.wordlists = wordlists

    def detect(self, text: str):
        if allow_key(text) in self.allowed:
            return None
        return self.builtin.detect(text) or (self.wordlists.detect(text) if self.wordlists else None)

    def allows_completion(self, text: str) -> bool:
        """An explicit whole-word exception may be completed before judging it.

        Binary search avoids a second large prefix dictionary in memory. The
        completed-word check still rejects a prohibited standalone prefix.
        """
        key = allow_key(text)
        position = bisect_left(self._allowed_ordered, key)
        return (bool(key) and position < len(self._allowed_ordered)
                and self._allowed_ordered[position].startswith(key))


class Cooldown:
    """One shared penalty across English/Russian engines, reset after 5 minutes."""
    def __init__(self):
        self.until = 0.0
        self.last_hit = float("-inf")
        self.strikes = 0

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.until - (time.monotonic() if now is None else now))

    def hit(self, settings: Settings, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        self.strikes = self.strikes + 1 if now - self.last_hit < 300 else 1
        self.last_hit = now
        duration = min(60, settings.cooldown_seconds * (2 ** min(self.strikes - 1, 4)
                                                     if settings.escalate else 1))
        self.until = now + duration
        return duration

    def cancel(self):
        self.until = 0.0


@dataclass(frozen=True)
class Finished:
    text: str = ""
    blocked: bool = False
    seconds: int = 0


class Composition:
    def __init__(self, matcher: Matcher, settings: Settings, cooldown: Cooldown):
        self.matcher = matcher
        self.settings = settings
        self.cooldown = cooldown
        self.text = ""
        self.cursor = 0

    def clear(self):
        self.text = ""
        self.cursor = 0

    def unsafe(self) -> bool:
        return bool(self.text and self.matcher.detect(self.text))

    def insert(self, text: str) -> bool:
        if len(self.text) + len(text) > MAX_COMPOSITION:
            return False
        self.text = self.text[:self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)
        return True

    def backspace(self):
        if self.cursor:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1

    def delete(self):
        self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]

    def finish(self, now: float | None = None) -> Finished:
        text = self.text
        unsafe = self.unsafe()
        self.clear()
        if unsafe:
            return Finished(blocked=True, seconds=self.cooldown.hit(self.settings, now))
        return Finished(text=text)
