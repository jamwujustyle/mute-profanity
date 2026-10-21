import unittest
from unittest.mock import Mock, patch

from mute_profanity.compose import Composer, is_compose_start


class ComposerTests(unittest.TestCase):
    def setUp(self):
        self.composer = Composer("C.UTF-8")
        self.addCleanup(self.composer.close)

    def test_dead_acute(self):
        self.assertEqual(("composing", ""), self.composer.feed(0xFE51))
        self.assertTrue(self.composer.composing)
        self.assertEqual(("composed", "é"), self.composer.feed(ord("e")))
        self.assertFalse(self.composer.composing)
        self.composer.reset()
        self.assertEqual(("nothing", ""), self.composer.feed(ord("a")))

    def test_multi_key_and_unicode_output(self):
        self.assertEqual(("composing", ""), self.composer.feed(0xFF20))
        self.assertEqual(("composing", ""), self.composer.feed(ord("'")))
        self.assertEqual(("composed", "é"), self.composer.feed(ord("e")))
        self.assertEqual(("nothing", ""), self.composer.feed(ord("e")))
        self.assertEqual(("composing", ""), self.composer.feed(0xFE57))
        self.assertEqual(("composed", "ü"), self.composer.feed(ord("u")))

    def test_invalid_sequence_and_restart(self):
        self.composer.feed(0xFE51)
        self.assertEqual(("cancelled", ""), self.composer.feed(0xFFBE))  # F1
        self.assertFalse(self.composer.composing)
        self.assertEqual(("nothing", ""), self.composer.feed(ord("a")))
        self.composer.feed(0xFE51)
        self.composer.reset()
        self.assertFalse(self.composer.composing)
        self.assertEqual(("nothing", ""), self.composer.feed(ord("e")))

    def test_normal_key_and_close(self):
        self.assertEqual(("nothing", ""), self.composer.feed(ord("x")))
        self.composer.close()
        self.composer.close()
        self.assertFalse(self.composer.composing)
        self.composer.reset()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.composer.feed(ord("a"))

    def test_start_key_ranges_and_input_validation(self):
        for key in (0xFE50, 0xFE51, 0xFE6F, 0xFE80, 0xFE8C, 0xFF20):
            self.assertTrue(is_compose_start(key))
        for key in (ord("a"), 0xFE4F, 0xFE70, 0xFE8D, 0xFF21):
            self.assertFalse(is_compose_start(key))
        for key in (-1, 0x100000000, "e", None):
            with self.assertRaises(ValueError):
                self.composer.feed(key)

    def test_unavailable_library_has_clear_error(self):
        with patch("mute_profanity.compose.ctypes.CDLL", side_effect=OSError("missing")):
            with self.assertRaisesRegex(RuntimeError, "libxkbcommon"):
                Composer()

    def test_partial_initialization_releases_context(self):
        library = Mock()
        library.xkb_context_new.return_value = 123
        library.xkb_compose_table_new_from_locale.return_value = None
        with patch("mute_profanity.compose._load_library", return_value=library):
            with self.assertRaisesRegex(RuntimeError, "Compose table"):
                Composer("no-such-locale")
        library.xkb_context_unref.assert_called_once_with(123)
        library.xkb_compose_table_unref.assert_not_called()
        library.xkb_compose_state_unref.assert_not_called()
        self.assertEqual(2, library.xkb_compose_table_new_from_locale.call_count)

    def test_large_compose_output_is_cancelled_safely(self):
        self.composer.feed(0xFE51)
        with patch.object(self.composer._library, "xkb_compose_state_get_utf8", return_value=256):
            self.assertEqual(("cancelled", ""), self.composer.feed(ord("e")))
        self.assertFalse(self.composer.composing)
        self.assertEqual(("nothing", ""), self.composer.feed(ord("e")))


if __name__ == "__main__":
    unittest.main()
