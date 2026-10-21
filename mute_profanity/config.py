"""Small, validated user configuration. Never stores typed text."""

from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import tempfile
import sys


def config_dir() -> Path:
    if sys.platform == "win32" and not os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Mute"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mute-profanity"


def config_path() -> Path:
    return config_dir() / "config.json"


def wordlist_dir() -> Path:
    return config_dir() / "wordlists"


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    cooldown_seconds: int = 8
    escalate: bool = True
    filter_en: bool = True
    filter_ru: bool = True
    beep: bool = True
    allow_words: list[str] = field(default_factory=list)
    extra_words: list[str] = field(default_factory=list)
    paused_until: float = 0.0
    instant_trigger: bool = True


def _words(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(x.strip() for x in value[:1000]
                             if isinstance(x, str) and 0 < len(x.strip()) <= 128))


def validated(data: dict) -> Settings:
    defaults = Settings()
    flags = {key: data[key] if isinstance(data.get(key), bool) else getattr(defaults, key)
             for key in ("enabled", "instant_trigger", "escalate", "filter_en", "filter_ru", "beep")}
    seconds = data.get("cooldown_seconds", 8)
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        seconds = 8
    paused = data.get("paused_until", 0.0)
    try:
        finite_pause = not isinstance(paused, bool) and isinstance(paused, (int, float)) and math.isfinite(paused)
    except OverflowError:
        finite_pause = False
    if not finite_pause:
        paused = 0.0
    return Settings(**flags, cooldown_seconds=max(3, min(60, seconds)),
                    allow_words=_words(data.get("allow_words")),
                    extra_words=_words(data.get("extra_words")),
                    paused_until=max(0.0, paused))


def load() -> Settings:
    try:
        path = config_path()
        if path.stat().st_size > 1024 * 1024:
            return Settings()
        data = json.loads(path.read_text(encoding="utf-8"))
        return validated(data) if isinstance(data, dict) else Settings()
    except (OSError, ValueError, TypeError):
        return Settings()


def save(settings: Settings) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Atomic replace lets file watchers and readers see only complete settings.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".config-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(validated(asdict(settings))), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
