import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mute_profanity.wordlists import MAX_FILE_BYTES, MAX_WORD_CHARACTERS, WordLists


class WordListsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.lists = WordLists(self.directory)

    def write(self, name, content):
        (self.directory / name).write_text(content, encoding="utf-8")

    def test_exact_words_casefold_and_punctuation(self):
        self.write("en.txt", "# a comment\n\nfoobar\n")
        self.assertEqual(self.lists.reload(), [])
        match = self.lists.detect("A sentence: (FOOBAR!), done.")
        self.assertIsNotNone(match)
        self.assertEqual((match.language, match.category, match.rule, match.stem),
                         ("en", "custom", "foobar", False))
        self.assertIsNone(self.lists.detect("xfoobar foobarx"))
        self.assertIsNotNone(self.lists.detect("fine/foobar/fine"))

    def test_russian_and_normalized_unicode_preserve_accents(self):
        self.write("ru.txt", "пример\n")
        self.write("es.txt", "café\n")
        self.write("wide.txt", "ＦＯＯ\n")
        self.lists.reload()
        self.assertEqual(self.lists.detect("ПРИМЕР!").language, "ru")
        self.assertEqual(self.lists.detect("cafe\u0301").language, "es")
        self.assertIsNone(self.lists.detect("cafe"))
        self.assertEqual(self.lists.detect("foo").language, "wide")

    def test_combining_marks_are_word_characters(self):
        self.write("hi.txt", "नमस्ते\n")
        self.lists.reload()
        self.assertIsNotNone(self.lists.detect("(नमस्ते!)"))
        self.assertIsNone(self.lists.detect("नमस्तेवाला"))

    def test_stems_are_opt_in_and_require_three_characters(self):
        self.write("stems.txt", "foobar*\nxy*\na*b\n*\n")
        errors = self.lists.reload()
        self.assertEqual(len(errors), 3)
        self.assertEqual(self.lists.entry_count, 1)
        self.assertTrue(self.lists.detect("foobaring").stem)
        self.assertIsNotNone(self.lists.detect("foobar"))
        self.assertIsNone(self.lists.detect("xfoobar"))

    def test_compressed_stems_share_edges_and_accept_shorter_later_rule(self):
        self.write("stems.txt", "foobarbaz*\nfoobarqux*\nfoo*\nfoo*\nfizz*\n")
        self.assertEqual(self.lists.reload(), [])
        self.assertEqual(self.lists.entry_count, 4)
        for value in ("foobarbazing", "foobarquxing", "food", "fizzing"):
            self.assertIsNotNone(self.lists.detect(value), value)
        self.assertIsNone(self.lists.detect("fi fob"))

    def test_very_long_rules_and_normalization_generated_phrases_are_rejected(self):
        self.write("custom.txt", "x" * (MAX_WORD_CHARACTERS + 1) + "*\n\ufdfa\n")
        self.assertEqual(len(self.lists.reload()), 2)
        self.assertEqual(self.lists.entry_count, 0)

    def test_malformed_file_diagnostics_are_bounded(self):
        self.write("invalid.txt", "invalid phrase\n" * 200)
        errors = self.lists.reload()
        self.assertEqual(len(errors), 101)
        self.assertIn("omitted", errors[-1])

    def test_phrases_and_control_characters_are_rejected_without_echo(self):
        self.write("custom.txt", "private phrase\nsecret\u200bword\nvalid\n")
        errors = self.lists.reload()
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(message.startswith("custom.txt:") for message in errors))
        self.assertFalse(any("private phrase" in message or "secret" in message for message in errors))
        self.assertIsNotNone(self.lists.detect("valid"))

    def test_internal_punctuation_entry_and_punctuation_boundaries(self):
        self.write("custom.txt", "foo-bar\nbaz\n")
        self.lists.reload()
        self.assertIsNotNone(self.lists.detect("ordinary (foo-bar!), text"))
        self.assertIsNotNone(self.lists.detect("foo/baz!"))

    def test_reload_removes_old_rules_and_ignores_non_text_files(self):
        self.write("en.txt", "oldword\n")
        self.write("ignored.json", "ignoredword\n")
        self.lists.reload()
        self.assertIsNotNone(self.lists.detect("oldword"))
        self.write("en.txt", "newword\n")
        self.lists.reload()
        self.assertIsNone(self.lists.detect("oldword ignoredword"))
        self.assertIsNotNone(self.lists.detect("newword"))

    def test_invalid_utf8_oversized_symlink_and_directory_are_skipped(self):
        (self.directory / "invalid.txt").write_bytes(b"\xff")
        (self.directory / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        self.write("real.txt", "realword\n")
        (self.directory / "link.txt").symlink_to(self.directory / "real.txt")
        (self.directory / "folder.txt").mkdir()
        errors = self.lists.reload()
        self.assertEqual(len(errors), 4)
        self.assertEqual(self.lists.detect("realword").language, "real")

    def test_fifo_does_not_block_reload(self):
        os.mkfifo(self.directory / "pipe.txt")
        self.assertIn("only regular files", self.lists.reload()[0])

    def test_entry_limit_deterministic_duplicate_precedence_and_diagnostics_copy(self):
        self.write("aa.txt", "first\nshared\n")
        self.write("zz.txt", "shared\nlast\n")
        with patch("mute_profanity.wordlists.MAX_ENTRIES", 3):
            errors = self.lists.reload()
        self.assertEqual(self.lists.entry_count, 2)
        self.assertEqual(self.lists.detect("shared").language, "aa")
        self.assertIsNone(self.lists.detect("last"))
        self.assertEqual(len(errors), 1)
        errors.clear()
        self.assertEqual(len(self.lists.errors), 1)

    def test_missing_directory_reports_error_and_detection_is_safe(self):
        lists = WordLists(self.directory / "missing")
        self.assertTrue(lists.reload())
        self.assertIsNone(lists.detect("anything"))

    def test_utf8_bom_is_accepted(self):
        (self.directory / "bom.txt").write_bytes(b"\xef\xbb\xbffoobar\n")
        self.assertEqual(self.lists.reload(), [])
        self.assertIsNotNone(self.lists.detect("foobar"))


if __name__ == "__main__":
    unittest.main()
