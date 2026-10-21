"""Small, dependency-free word lists for additional languages and vocabulary.

Files contain one word per line, with an optional trailing ``*`` for a stem.
Matching uses Unicode normalization and case folding while preserving accents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
import unicodedata


MAX_FILE_BYTES = 1024 * 1024
MAX_ENTRIES = 50_000
MAX_WORD_CHARACTERS = 128
MAX_DIAGNOSTICS = 100


class _Diagnostics(list[str]):
    def append(self, message: str) -> None:
        if len(self) < MAX_DIAGNOSTICS:
            super().append(message)
        elif len(self) == MAX_DIAGNOSTICS:
            super().append("Further word-list diagnostics omitted.")


def normalize(text: str) -> str:
    """Normalize both rules and input without removing language distinctions."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())


def _word_character(character: str) -> bool:
    return unicodedata.category(character)[0] in "LMN"


def _trim_punctuation(text: str) -> str:
    start, end = 0, len(text)
    while start < end and unicodedata.category(text[start])[0] in "PS":
        start += 1
    while end > start and unicodedata.category(text[end - 1])[0] in "PS":
        end -= 1
    return text[start:end]


@dataclass(frozen=True)
class WordListMatch:
    """Rule metadata, never a copy of the text the person was typing."""

    language: str
    rule: str
    stem: bool = False
    category: str = "custom"


# Match is convenient for consumers that do not need the qualified class name.
Match = WordListMatch


@dataclass(slots=True)
class _PrefixNode:
    fragment: str = ""
    children: dict[str, _PrefixNode] = field(default_factory=dict)
    match: WordListMatch | None = None


def _add_prefix(root: _PrefixNode, term: str, match: WordListMatch) -> bool:
    """Insert a compressed trie edge without allocating a dict per letter."""
    node, position = root, 0
    while position < len(term):
        first = term[position]
        child = node.children.get(first)
        if child is None:
            node.children[first] = _PrefixNode(term[position:], match=match)
            return True
        fragment = child.fragment
        common = 0
        while (common < len(fragment) and position + common < len(term)
               and fragment[common] == term[position + common]):
            common += 1
        if common == len(fragment):
            node = child
            position += common
            continue
        branch = _PrefixNode(fragment[:common])
        node.children[first] = branch
        child.fragment = fragment[common:]
        branch.children[child.fragment[0]] = child
        position += common
        if position == len(term):
            branch.match = match
        else:
            suffix = term[position:]
            branch.children[suffix[0]] = _PrefixNode(suffix, match=match)
        return True
    if node.match is not None:
        return False
    node.match = match
    return True


class WordLists:
    """Load local ``*.txt`` files and match words with bounded memory and work.

    Call :meth:`reload` after creating the object and after directory changes.
    Diagnostics contain only filenames, line numbers, and general errors.
    Missing or malformed files never make detection raise an exception.
    """

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)
        self.errors: list[str] = []
        self.entry_count = 0
        self._exact: dict[str, WordListMatch] = {}
        self._prefixes = _PrefixNode()

    def reload(self) -> list[str]:
        """Replace the current rules and return diagnostics for skipped input."""
        exact: dict[str, WordListMatch] = {}
        prefixes = _PrefixNode()
        errors: list[str] = _Diagnostics()
        entries = 0
        unique_entries = 0
        try:
            paths = sorted(self.directory.iterdir(), key=lambda path: path.name)
        except OSError:
            self._replace(exact, prefixes, 0, ["Cannot read the word-list directory."])
            return self.errors.copy()

        limit_reached = False
        for path in paths:
            if path.suffix != ".txt":
                continue
            # O_NOFOLLOW below also closes the check/open race for symlinks.
            try:
                if path.is_symlink():
                    errors.append(f"{path.name}: symbolic links are skipped.")
                    continue
                flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, flags)
                with os.fdopen(descriptor, "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        errors.append(f"{path.name}: only regular files are accepted.")
                        continue
                    if info.st_size > MAX_FILE_BYTES:
                        errors.append(f"{path.name}: file exceeds the 1 MiB limit.")
                        continue
                    content = source.read(MAX_FILE_BYTES + 1)
                if len(content) > MAX_FILE_BYTES:
                    errors.append(f"{path.name}: file exceeds the 1 MiB limit.")
                    continue
                text = content.decode("utf-8-sig")
            except UnicodeError:
                errors.append(f"{path.name}: file is not valid UTF-8.")
                continue
            except OSError:
                errors.append(f"{path.name}: cannot read file.")
                continue

            for line_number, line in enumerate(text.splitlines(), 1):
                value = line.strip()
                if not value or value.startswith("#"):
                    continue
                value = normalize(value)
                error = self._validate(value)
                if error:
                    errors.append(f"{path.name}:{line_number}: {error}")
                    continue
                if entries >= MAX_ENTRIES:
                    errors.append(f"Word-list entry limit ({MAX_ENTRIES}) reached; remaining entries skipped.")
                    limit_reached = True
                    break
                entries += 1
                stem = value.endswith("*")
                term = value[:-1] if stem else value
                # Permit copied lists with surrounding quotes or punctuation.
                term = _trim_punctuation(term)
                if not term or not any(_word_character(char) for char in term):
                    errors.append(f"{path.name}:{line_number}: entry must contain a letter or number.")
                    continue
                if stem and len(term) < 3:
                    errors.append(f"{path.name}:{line_number}: stem must contain at least 3 characters.")
                    continue
                match = WordListMatch(path.stem, term + ("*" if stem else ""), stem)
                if stem:
                    if _add_prefix(prefixes, term, match):
                        unique_entries += 1
                elif term not in exact:
                    exact[term] = match
                    unique_entries += 1
            if limit_reached:
                break

        self._replace(exact, prefixes, unique_entries, errors)
        return self.errors.copy()

    @staticmethod
    def _validate(value: str) -> str | None:
        if any(character.isspace() for character in value):
            return "use one word per line; phrases are not supported."
        if any(unicodedata.category(character)[0] == "C" for character in value):
            return "control and invisible formatting characters are not accepted."
        if len(value.rstrip("*")) > MAX_WORD_CHARACTERS:
            return f"word exceeds the {MAX_WORD_CHARACTERS}-character limit."
        if "*" in value[:-1]:
            return "a wildcard is allowed only once, at the end."
        return None

    def _replace(self, exact: dict, prefixes: _PrefixNode, count: int, errors: list[str]) -> None:
        self._exact = exact
        self._prefixes = prefixes
        self.entry_count = count
        self.errors = errors

    def _lookup(self, token: str) -> WordListMatch | None:
        match = self._exact.get(token)
        if match is not None:
            return match
        node = self._prefixes
        position = 0
        while position < len(token):
            node = node.children.get(token[position])
            if node is None or not token.startswith(node.fragment, position):
                return None
            position += len(node.fragment)
            if node.match is not None:
                return node.match
        return None

    def detect(self, text: str) -> WordListMatch | None:
        """Find a custom word, respecting Unicode word boundaries.

        Whole whitespace-delimited chunks also support entries with internal
        punctuation, such as hyphenated words. Plain words are then checked
        between punctuation boundaries. Exact words never match inside a longer
        word; the optional trailing wildcard explicitly enables that behavior.
        """
        if not self._exact and not self._prefixes.children:
            return None
        for chunk in normalize(text).split():
            token = _trim_punctuation(chunk)
            match = self._lookup(token)
            if match is not None:
                return match
            start = None
            for index, character in enumerate(token):
                if _word_character(character):
                    if start is None:
                        start = index
                elif start is not None:
                    match = self._lookup(token[start:index])
                    if match is not None:
                        return match
                    start = None
            if start is not None and start != 0:
                match = self._lookup(token[start:])
                if match is not None:
                    return match
        return None
