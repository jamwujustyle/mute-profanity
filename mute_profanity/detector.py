"""Small, offline Russian/English profanity filter.

This is a deliberately authored lexicon and morphology matcher, not a language
model.  It cannot determine intent, distinguish a quotation from swearing, or
promise a measured recall percentage.  ``Detector`` is reusable: its instance
contains rules and configured words, never previously checked text.

Work is bounded to the final 1,024 input characters, 128 characters per token,
and six adjacent pieces for punctuation disguises.  If the input was clipped,
an incomplete first token is ignored.  The typing service should pass its
current word; the larger limit also makes short sentence previews useful.

Matching uses NFKC/casefold, removes invisible format/combining characters,
recognizes common visual substitutions and leetspeak, and checks a second
phonetic Russian transliteration.  Repeated letters are collapsed for roots
without doubled letters.  English words with innocent continuations use
controlled endings instead of unrestricted substring searches.  Russian mat
uses explicit prefixes and roots; a small exception set protects real words.

``allow_words`` are whole-word exceptions and ``extra_words`` are whole-word
additions (plain text, never regular expressions).  Add a false-positive word
to the allowlist instead of disabling an entire language.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from collections.abc import Iterable


MAX_SCAN_CHARS = 1024
MAX_TOKEN_CHARS = 128
MAX_JOIN_PIECES = 6


@dataclass(frozen=True, slots=True)
class Match:
    """A decision, with no copy of the user's typed text."""

    language: str
    category: str
    rule_id: str


_EN_STRONG = Match("en", "profanity", "en-strong")
_EN_SLUR = Match("en", "slur", "en-slur")
_RU_MAT = Match("ru", "profanity", "ru-mat")
_RU_VULGAR = Match("ru", "profanity", "ru-vulgar")
_RU_SLUR = Match("ru", "slur", "ru-slur")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_REPEATS = re.compile(r"(.)\1+")

# Visual similarity and phonetic transliteration are separate: Cyrillic р
# looks like p, but sounds like r.  Combining the two indiscriminately creates
# many false positives in ordinary Russian.
_EN_VISUAL = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y",
    "х": "x", "к": "k", "м": "m", "т": "t", "в": "b", "н": "h",
    "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ӏ": "l", "ԁ": "d",
    "α": "a", "ο": "o", "ρ": "p", "κ": "k", "τ": "t", "χ": "x",
    "ι": "i", "ϲ": "c", "0": "o", "1": "i", "3": "e", "4": "a",
    "5": "s", "6": "b", "7": "t", "8": "b", "9": "g",
})
_RU_VISUAL = str.maketrans({
    "a": "а", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у", "b": "в",
    "i": "и", "j": "и", "s": "с", "ё": "е", "й": "и", "і": "и",
    "ї": "и", "0": "о", "1": "и", "3": "з", "4": "ч", "6": "б",
    "α": "а", "ο": "о", "ρ": "р", "κ": "к", "τ": "т", "χ": "х",
})
_RU_PHONETIC = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya", "0": "o", "1": "i", "3": "e",
    "4": "a", "5": "s", "6": "b", "7": "t", "8": "b", "9": "g",
})
_LEET_SYMBOLS = {"$": "s", "@": "a", "!": "i", "|": "i"}


def _prepare(text: str) -> str:
    # Slice before normalization: even hostile input cannot make this call
    # normalize an unbounded document.  Character expansion is bounded by Unicode.
    offset = max(0, len(text) - MAX_SCAN_CHARS)
    source = text[offset:]
    if offset and text[offset - 1].isalnum() and source[:1].isalnum():
        first_boundary = next((i for i, c in enumerate(source) if not c.isalnum()), len(source))
        source = source[first_boundary:]
    source = unicodedata.normalize("NFKC", source).casefold()
    source = "".join(c for c in source if unicodedata.category(c) not in {"Cf", "Mn", "Me"})
    result: list[str] = []
    for index, char in enumerate(source):
        if char in _LEET_SYMBOLS:
            following = index + 1 < len(source) and source[index + 1].isalnum()
            preceding = index > 0 and source[index - 1].isalnum()
            if following and (preceding or char in {"$", "@"}):
                char = _LEET_SYMBOLS[char]
        result.append(char)
    return "".join(result)


def _squash(word: str) -> str:
    return _REPEATS.sub(r"\1", word)


def _keys(word: str) -> tuple[str, str, str]:
    return (
        word.translate(_EN_VISUAL),
        word.translate(_RU_VISUAL),
        word.translate(_RU_PHONETIC),
    )


# The strong roots can take ordinary English compound endings.  Roots that
# also begin innocent words (ass, cock, dick, cum) have separate exact endings.
_EN_ROOTS = re.compile(
    r"(?:(?:mother|mutha|cluster|mind|brain|skull|butt|dumb|horse|holy|mega|super|un|re|out)?"
    r"(?:fuck[a-z]*|fuk(?:s|er[a-z]*|ing|in|ed|wit|off|up|head|face|you|ya)?|"
    r"fuq(?:s|er|ing|ed|you)?|fck[a-z]*)|"
    r"(?:bull|horse|dog|dip|jack|bat|ape|chicken|holy|dumb|no)?(?:shit[a-z]*|sht(?:s|ty|ting|ted|head|hole)?)|"
    r"(?:sonof|sonofa)?bitch[a-z]*|cunt[a-z]*|"
    r"bastard[a-z]*|bollocks[a-z]*|wank(?:er|ers|ing|ed)?|twat[a-z]*|"
    r"arse(?:s|hole[a-z]*)?|bugger[a-z]*|douche(?:s|bag[a-z]*)?|"
    r"whore(?:s|ish|dom|house|houses|monger|mongers)?|slut(?:s|ty|tish|shaming)?|"
    r"prick(?:s|head|heads)?)\Z"
)
_EN_CONTROLLED = re.compile(
    r"(?:a+s{2,}(?:e+s|hole[a-z]*|hat[a-z]*|wipe[a-z]*|face[a-z]*)?|"
    r"(?:bad|dumb|jack|smart|kiss|hard|fat|lame|wise)a+s{2,}(?:es)?|"
    r"dick(?:s|ed|ing|head[a-z]*|face[a-z]*|wad[a-z]*|bag[a-z]*|less|ish|suck[a-z]*)?|"
    r"cock(?:s|head[a-z]*|face[a-z]*|suck[a-z]*|block[a-z]*|womble[a-z]*)?|"
    r"cum(?:s|ming|shot|shots|dumpster|dumpsters)?|"
    r"pussy(?:ass)?|pussies)\Z"
)
_EN_SLURS = re.compile(r"(?:nigg(?:er[a-z]*|a(?:s|z)?)|fag(?:s|got[a-z]*|ot[a-z]*)?|retard(?:s|ed)?)\Z")
_EN_EXCEPTIONS = {"shiitake", "shitake", "shiitaki", "shitaki", "shiitakes", "shitakes", "shiite", "shiites"}

_RU_PREFIX = r"(?:(?:недо|пере|подъ|разъ|безъ|изъ|объ|съ|въ|долбо|долба|раз|рас|без|бес|при|про|под|воз|вз|вы|от|до|за|на|по|об|не|ни|ах|ох|о|а|у|в|с){0,3})"
_RU_MAT_ROOTS = re.compile(
    _RU_PREFIX + r"(?:ху[ияею][а-я]*|хул[иея]|п[ие]з[дж][а-я]*|еб[а-я]*)\Z"
)
_RU_BLYA = re.compile(r"(?:бля|бля[дт][а-я]*)\Z")
_RU_VULGAR_ROOTS = re.compile(
    _RU_PREFIX + r"(?:хер[а-я]*|говн[а-я]*|говен[а-я]*|дерьм[а-я]*|жоп[а-я]*|"
    r"сра(?:ть|л[а-я]*|н[а-я]*|к[а-я]*|ч[а-я]*)|ссат[а-я]*|ссу[а-я]*|"
    r"залуп[а-я]*|г[ао]ндон[а-я]*|муда[кч][а-я]*|мудил[а-я]*)\Z"
)
_RU_INSULTS = re.compile(
    r"(?:сук[аиуе](?:м[аи]?|х|и|й|ю|ми)?|суко[ию]|сучк[аиуе](?:м[аи]?|х|и|й|ю|ми)?|"
    r"сучко[ию]|суч(?:ар|ен|он|ь)[а-я]*|сучии|шлюх[а-я]*|шалав[а-я]*)\Z"
)
_RU_SLURS = re.compile(r"(?:п[ие]д[ао]р[а-я]*|пид[ао]рас[а-я]*|педик[а-я]*|педераст[а-я]*)\Z")
_RU_EXCEPTIONS = {"себя", "себе", "себею", "ебеи", "ебаи"}
_RU_EXCEPTION_PREFIXES = ("херсон", "херес", "херувим", "себестоим", "веб", "себор", "себастьян", "себастиан", "педикюр", "педикул")

_LAT_PREFIX = r"(?:(?:nedo|pere|pod|raz|ras|bez|bes|pri|pro|dolbo|dolba|vy|ot|do|za|na|po|ob|ne|ni|ah|oh|vo|o|a|u|v|s){0,3})"
_LAT_MAT = re.compile(
    _LAT_PREFIX + r"(?:[hx]u(?:[yij][a-z]*|e(?:m|v|s|t)[a-z]*|ya[a-z]*|yu[a-z]*)|"
    r"p[ie]z(?:d|j|zh)[a-z]*|"
    r"(?:yeb|yob|eb|jeb|job)(?:at|al|an|nu|ni|uch|ut|et|em|is|os|la|lo|ly)[a-z]*|"
    r"(?:yeb|yob|eb|jeb|job)i|(?:dolbo|dolba)(?:eb|yeb|yob)[a-z]*)\Z"
)
_LAT_BLYA = re.compile(r"(?:bl(?:ya|ja|ia)(?:d[a-z]*|t[a-z]*)?)\Z")
_LAT_VULGAR = re.compile(
    _LAT_PREFIX + r"(?:[hx]er(?:nya[a-z]*|ni|nyu|nyami|ov[a-z]*)|"
    r"g[ao]vn[a-z]*|dermo(?:v[a-z]*|ed[a-z]*)?|zhop[a-z]*|zalup[a-z]*|"
    r"g[ao]ndon[a-z]*|mudak[a-z]*|mudil[a-z]*|suka|suki|cyk[auie]|suchka|shlyu[hx][a-z]*)\Z"
)
_LAT_SLURS = re.compile(r"(?:p[ie]d[ao]r[a-z]*|pider[a-z]*|pederast[a-z]*)\Z")
_LAT_EXCEPTIONS = {"hue", "hues"}
_LAT_EXCEPTION_PREFIXES = ("herson", "kherson", "heres", "heruvim", "veb", "sebor", "sebastyan", "sebastian", "huygen", "huitzilo")
_LAT_HER = re.compile(r"(?:(?:na|ni|po|do|za|o|a)her(?:a|u|om)?|[xk]her|xer)\Z")

# Short, unmistakably masked spellings.  Separators alone are joined separately.
_MASK_EN = re.compile(
    r"(?:f[*#•]{1,2}k|f[*#•]ck|fu[*#•]k|s[h*#•][i*#•]t|"
    r"b[i*#•]tch|c[u*#•]nt|a[*#•]{2}hole)\Z"
)
_MASK_RU = re.compile(r"(?:бл[я*#•][дт*#•]?ь?|б[*#•]{3}ь|п[и*#•][з*#•][д*#•][а*#•]|х[у*#•][ияй*#•])\Z")
_JOIN_SEPARATORS = frozenset(" ._-*·•/\\~\t")


class Detector:
    """Offline filter with editable whole-word additions and exceptions."""

    def __init__(
        self,
        *,
        filter_en: bool = True,
        filter_ru: bool = True,
        allow_words: Iterable[str] = (),
        extra_words: Iterable[str] = (),
    ) -> None:
        self.filter_en = bool(filter_en)
        self.filter_ru = bool(filter_ru)
        self._allow: set[str] = set()
        self._extra: dict[str, Match] = {}
        for word in allow_words:
            prepared = _prepare(word)
            if prepared and len(prepared) <= MAX_TOKEN_CHARS:
                self._allow.update(_keys(prepared))
        for word in extra_words:
            prepared = _prepare(word)
            if prepared and len(prepared) <= MAX_TOKEN_CHARS:
                language = "ru" if re.search(r"[а-яё]", prepared) else "en"
                match = Match(language, "custom", "custom")
                for key in _keys(prepared):
                    self._extra[key] = match

    def _word(self, word: str) -> Match | None:
        if not word or len(word) > MAX_TOKEN_CHARS:
            return None
        en, ru, phonetic = _keys(word)
        if self._allow.intersection((en, ru, phonetic)):
            return None
        for key in (en, ru, phonetic):
            custom = self._extra.get(key)
            if custom and ((custom.language == "en" and self.filter_en) or (custom.language == "ru" and self.filter_ru)):
                return custom
        en_short = _squash(en)
        if self.filter_en and en not in _EN_EXCEPTIONS and en_short not in _EN_EXCEPTIONS:
            if _EN_ROOTS.fullmatch(en) or _EN_ROOTS.fullmatch(en_short) or _EN_CONTROLLED.fullmatch(en):
                return _EN_STRONG
            if _EN_SLURS.fullmatch(en) or _EN_SLURS.fullmatch(en_short):
                return _EN_SLUR
        if self.filter_ru:
            ru = _squash(ru)
            if ru in _RU_EXCEPTIONS or ru.startswith(_RU_EXCEPTION_PREFIXES):
                return None
            # Pure Latin text takes the phonetic path.  Applying the visual
            # Cyrillic alphabet to every English word turns "spake" into a
            # Russian vulgarity and produces surprising false positives.
            if re.search(r"[а-яё]", word):
                if _RU_MAT_ROOTS.fullmatch(ru) or _RU_BLYA.fullmatch(ru):
                    return _RU_MAT
                if _RU_VULGAR_ROOTS.fullmatch(ru) or _RU_INSULTS.fullmatch(ru):
                    return _RU_VULGAR
                if _RU_SLURS.fullmatch(ru):
                    return _RU_SLUR
            phonetic = _squash(phonetic)
            if phonetic in _LAT_EXCEPTIONS or phonetic.startswith(_LAT_EXCEPTION_PREFIXES):
                return None
            if _LAT_MAT.fullmatch(phonetic) or _LAT_BLYA.fullmatch(phonetic):
                return _RU_MAT
            if _LAT_VULGAR.fullmatch(phonetic) or _LAT_HER.fullmatch(phonetic) or (phonetic == "her" and re.search(r"[а-яё]", word)):
                return _RU_VULGAR
            if _LAT_SLURS.fullmatch(phonetic):
                return _RU_SLUR
        return None

    def detect(self, text: str) -> Match | None:
        """Return the first detected category, or None; retain no input text."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text or not (self.filter_en or self.filter_ru):
            return None
        prepared = _prepare(text)
        # Check each printable token first, so a sentence never needs an
        # unbounded number of overlapping punctuation combinations.
        pieces = list(_TOKEN.finditer(prepared))
        for piece in pieces:
            result = self._word(piece.group())
            if result:
                return result
        for start, first in enumerate(pieces):
            joined = first.group()
            previous = first
            all_single = len(joined) == 1
            for following in pieces[start + 1 : start + MAX_JOIN_PIECES]:
                separator = prepared[previous.end() : following.start()]
                chunk = following.group()
                all_single = all_single and len(chunk) == 1
                if (
                    not separator or len(separator) > 3 or not set(separator) <= _JOIN_SEPARATORS
                    or (any(c.isspace() for c in separator) and not all_single)
                    or len(joined) + len(chunk) > MAX_TOKEN_CHARS
                ):
                    break
                joined += chunk
                result = self._word(joined)
                if result:
                    return result
                previous = following
        # Preserve asterisks for deliberately masked spellings.  Bounded,
        # whitespace-separated chunks also tolerate surrounding punctuation.
        for chunk in prepared.split():
            chunk = chunk.strip(".,!?;:()[]{}<>\"'`~_-\\/")
            if chunk in self._allow or len(chunk) > MAX_TOKEN_CHARS:
                continue
            if self.filter_en and _MASK_EN.fullmatch(chunk):
                return _EN_STRONG
            if self.filter_ru and _MASK_RU.fullmatch(chunk):
                return _RU_MAT
        return None


_DEFAULT = Detector()


def detect(text: str) -> Match | None:
    """Check using the built-in English and Russian rules."""
    return _DEFAULT.detect(text)
