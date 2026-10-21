import unittest

from mute_profanity.detector import Detector, Match, MAX_SCAN_CHARS, detect


class DetectorTests(unittest.TestCase):
    def assert_detected(self, words, language):
        for word in words:
            with self.subTest(word=word):
                result = detect(word)
                self.assertIsNotNone(result)
                self.assertEqual(language, result.language)

    def test_english_roots_and_controlled_compounds(self):
        self.assert_detected([
            "fuck", "FUCKING", "motherfucker", "clusterfuck", "fucked", "fuk", "fck",
            "shit", "bullshit", "shitty", "bitch", "sonofabitch", "cunt", "cunts",
            "ass", "asses", "asshole", "dumbass", "dick", "dickhead", "cock", "cocksucker",
            "cum", "cumming", "cumshot", "pussy", "pussies", "bastard", "bollocks",
            "wanker", "twat", "arsehole", "bugger", "douchebag", "pricks", "slut", "whore",
        ], "en")

    def test_russian_mat_and_morphology(self):
        self.assert_detected([
            "хуй", "хуя", "хуе", "хуем", "хуями", "хую", "хуёвый", "хуесос", "нахуй",
            "похуй", "похуизм", "нихуя", "дохуя", "охуеть", "ахуенный", "хули", "хуле",
            "пизда", "пиздец", "пиздеть", "пизжу", "пиздёж", "пиздить", "распиздяй",
            "пиздобол", "пиздануть", "спиздили", "пиздюли", "пездец", "ёб", "ебать",
            "ебаный", "ебучий", "ебло", "ебля", "ебусь", "ебёт", "заебал", "заебись",
            "выебать", "съебать", "съебаться", "въебать", "въебался", "подъебать", "разъебать",
            "объебал", "наебал", "ёбаный", "долбоёб", "долбаеб",
            "бля", "блять", "блядь", "блядина", "блядский", "блядство",
        ], "ru")

    def test_russian_vulgarities(self):
        self.assert_detected([
            "сука", "суки", "суку", "сукой", "сучка", "сучки", "сучара", "гондон", "гандон",
            "мудак", "мудила", "залупа", "говно", "говнюк", "дерьмо", "дерьмовый", "жопа",
            "хер", "херня", "нахер", "охереть", "срать", "насрать", "сраный", "срач", "шлюха",
        ], "ru")

    def test_transliteration(self):
        self.assert_detected([
            "huy", "hui", "huj", "xyй", "nahui", "nahuy", "pohuy", "ohuet", "huesos",
            "pizda", "pizdets", "pizdec", "pizdet", "blya", "blyat", "blyad", "bljad",
            "ebat", "ebal", "yebat", "yobnut", "zaebis", "zaebal", "dolboeb", "gavno",
            "govno", "zhopa", "mudak", "suka", "hер", "пizda",
        ], "ru")

    def test_obfuscation_and_punctuation(self):
        self.assert_detected([
            "ｆｕｃｋ", "fuuuuck", "f.u.c.k", "f_u_c_k", "f u c k", "fu-ck", "f*ck",
            "f**k", "f#ck", "$hit", "sh!t", "sh1t", "5hit", "b!tch", "b1tch", "@sshole",
            "сunt", "fuсk", "fu\u200bck", "f\u2060uck", "f\u0301uck", "(fuck!)", "aassss",
        ], "en")
        self.assert_detected([
            "бляяяять", "хууууй", "пиииздец", "х.у.й", "п-и-з-д-а", "бл*дь", "п***а",
            "xуй", "хyй", "хyи", "6лядь", "п1зда", "пизд@", "бляtь", "бля\u200bдь", "(блядь!)",
        ], "ru")

    def test_common_innocent_words_are_safe(self):
        words = [
            "страхуй", "подстрахуй", "страхуем", "страхуется", "хулиган", "хулиганы", "учеба",
            "учёба", "флаг", "флаги", "ребенок", "небо", "небылица", "лебедь", "хлеб", "зебра",
            "дебют", "себя", "себе", "себестоимость", "бляха", "блямба", "рубля", "сучок",
            "Херсон", "херсонский", "херес", "херувим", "гребу", "гребля", "потреба",
            "веб", "вебинар", "вебсайт", "Вебер", "Себастьян", "себорея", "педикюр", "педикулез",
            "ship", "shipping", "class", "classic", "analysis", "Scunthorpe", "cucumber",
            "document", "assembly", "assessment", "assess", "assets", "assassin", "assignment",
            "assistance", "pass", "grass", "cocktail", "cockpit", "cockroach", "cockatoo", "peacock",
            "Dickens", "Dickinson", "shiitake", "shitake", "hue", "hues", "human", "ebay", "ebike",
            "ebitda", "job", "jobs", "jobsite", "jobless", "title", "country", "Saturday", "hello world",
            "her", "other", "usher", "posher", "rasher", "sheer", "Hera", "Heraclitus", "herald",
            "hernia", "dermatology", "dermatitis", "dermatologist", "Fukuoka", "Fukuyama", "spake",
            "who're", "Shi'ite", "Shi'ites", "shiitakes", "niggard", "niggardly", "shtick", "shtik",
            "Huygens", "Huitzilopotchli", "Donahue's", "As's",
        ]
        for word in words:
            with self.subTest(word=word):
                self.assertIsNone(detect(word))

    def test_sentences(self):
        self.assertEqual("ru", detect("Привет, ну блядь, опять!").language)
        self.assertEqual("en", detect("This is bullshit.").language)
        self.assertIsNone(detect("Я изучаю английский, Python и работу со строками."))
        self.assertIsNone(detect("The class assignment is a document about shipping."))

    def test_language_toggles(self):
        english = Detector(filter_ru=False)
        russian = Detector(filter_en=False)
        self.assertIsNone(english.detect("пизда"))
        self.assertIsNone(english.detect("pizda"))
        self.assertIsNotNone(english.detect("fuck"))
        self.assertIsNone(russian.detect("fuck"))
        self.assertIsNotNone(russian.detect("пизда"))
        self.assertIsNone(Detector(filter_ru=False, filter_en=False).detect("fuck пизда"))

    def test_allow_and_extra_words(self):
        detector = Detector(allow_words=["ass", "сука"], extra_words=["darn", "блин"])
        self.assertIsNone(detector.detect("ass"))
        self.assertIsNone(detector.detect("сука"))
        self.assertIsNotNone(detector.detect("asshole"))
        self.assertEqual(Match("en", "custom", "custom"), detector.detect("DARN"))
        self.assertEqual(Match("ru", "custom", "custom"), detector.detect("блин"))
        self.assertIsNone(Detector(extra_words=["блин"], filter_ru=False).detect("блин"))
        self.assertIsNone(Detector(extra_words=[".*"]).detect("anything"))

    def test_no_text_in_result_or_detector_state(self):
        detector = Detector()
        result = detector.detect("private prefix fuck private suffix")
        self.assertEqual({"language", "category", "rule_id"}, set(Match.__dataclass_fields__))
        self.assertNotIn("private", repr(result))
        self.assertNotIn("private", repr(vars(detector)))

    def test_bounded_inputs_and_clipped_word(self):
        self.assertIsNone(detect("a" * 1_000_000))
        self.assertIsNone(detect("a" * 100_000 + "ass"))
        self.assertIsNotNone(detect("a" * 100_000 + " fuck"))
        self.assertIsNone(detect("fuck " + "a " * MAX_SCAN_CHARS))
        self.assertIsNotNone(detect(" " + "fuck" + " " * (MAX_SCAN_CHARS - 4)))
        self.assertIsNone(detect(("a." * 100_000)))
        self.assertIsNone(detect("f" + "\u200b" * 100_000 + "uck"))
        self.assertIsNone(detect(""))
        self.assertRaises(TypeError, detect, None)


if __name__ == "__main__":
    unittest.main()
