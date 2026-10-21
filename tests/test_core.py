from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mute_profanity import config
from mute_profanity.config import Settings
from mute_profanity.core import Composition, Cooldown, Matcher, MAX_COMPOSITION
from mute_profanity.wordlists import WordLists


class TypingTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.cooldown = Cooldown()
        self.composition = Composition(Matcher(self.settings), self.settings, self.cooldown)

    def test_clean_word_is_committed_and_bad_word_is_never_committed(self):
        self.composition.insert("привет")
        result = self.composition.finish(now=0)
        self.assertEqual(result.text, "привет")
        self.assertFalse(result.blocked)
        self.composition.insert("блядь")
        self.assertTrue(self.composition.unsafe())
        result = self.composition.finish(now=1)
        self.assertEqual(result.text, "")
        self.assertTrue(result.blocked)
        self.assertEqual(result.seconds, 8)
        self.assertEqual(self.composition.text, "")

    def test_potentially_bad_prefix_can_be_completed_to_ordinary_word(self):
        self.composition.insert("ass")
        self.assertTrue(self.composition.unsafe())
        self.assertEqual(self.cooldown.remaining(now=0), 0)
        self.composition.insert("embly")
        self.assertFalse(self.composition.unsafe())
        self.assertEqual(self.composition.finish(now=0).text, "assembly")

    def test_editing_preedit_changes_the_checked_text(self):
        self.composition.insert("fucc")
        self.composition.backspace()
        self.composition.insert("k")
        self.assertTrue(self.composition.unsafe())
        self.composition.cursor = 2
        self.composition.delete()
        self.composition.insert("n")
        self.assertEqual(self.composition.text, "funk")
        self.assertFalse(self.composition.unsafe())

    def test_capacity_is_bounded_without_silent_text_loss(self):
        self.assertTrue(self.composition.insert("x" * MAX_COMPOSITION))
        self.assertFalse(self.composition.insert("y"))
        self.assertEqual(len(self.composition.text), MAX_COMPOSITION)

    def test_escalation_is_shared_capped_and_resets_after_quiet_interval(self):
        self.assertEqual([self.cooldown.hit(self.settings, now=n) for n in range(5)],
                         [8, 16, 32, 60, 60])
        self.assertEqual(self.cooldown.hit(self.settings, now=400), 8)
        self.assertEqual(self.cooldown.remaining(now=403), 5)
        self.cooldown.cancel()
        self.assertEqual(self.cooldown.remaining(now=403), 0)
        plain = replace(self.settings, escalate=False, cooldown_seconds=5)
        self.assertEqual(self.cooldown.hit(plain, now=404), 5)

    def test_new_language_wordlist_and_allowlist_override(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "es.txt").write_text("palabrota\n", encoding="utf-8")
            lists = WordLists(directory)
            lists.reload()
            self.assertEqual(Matcher(self.settings, lists).detect("palabrota").language, "es")
            settings = replace(self.settings, allow_words=["palabrota"])
            self.assertIsNone(Matcher(settings, lists).detect("palabrota!"))
            self.assertIsNone(Matcher(settings, lists).detect("«palabrota»"))

    def test_instant_mode_can_finish_an_explicit_exception_prefix(self):
        matcher = Matcher(replace(self.settings, allow_words=["assembly", "assist"]))
        self.assertIsNotNone(matcher.detect("ass"))
        self.assertTrue(matcher.allows_completion("ass"))
        self.assertFalse(matcher.allows_completion("asshole"))
        self.assertIsNone(matcher.detect("assembly"))
        self.assertIsNotNone(matcher.detect("ass!"))


class SettingsTests(unittest.TestCase):
    def test_settings_roundtrip_and_bad_json_defaults(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            settings = Settings(cooldown_seconds=11, allow_words=["пример"], paused_until=123.5)
            config.save(settings)
            self.assertEqual(config.load(), settings)
            self.assertEqual(config.config_path().stat().st_mode & 0o777, 0o600)
            config.config_path().write_text("{invalid", encoding="utf-8")
            self.assertEqual(config.load(), Settings())
            config.config_path().write_text(json.dumps({"enabled": "no", "cooldown_seconds": 900,
                                                       "paused_until": float("nan"), "allow_words": "bad"}))
            self.assertEqual(config.load().cooldown_seconds, 60)
            self.assertTrue(config.load().enabled)
            self.assertEqual(config.load().paused_until, 0)
            self.assertEqual(config.load().allow_words, [])
            self.assertEqual(config.validated({"paused_until": 10 ** 1000}).paused_until, 0)


if __name__ == "__main__":
    unittest.main()
