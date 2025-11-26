# hamchat/core/spell_highlighter.py
from __future__ import annotations
import json, re, unicodedata
from pathlib import Path
from typing import List

# Optional dependency: pyenchant
try:
    import enchant
    _HAS_ENCHANT = True
except Exception:
    enchant = None  # type: ignore
    _HAS_ENCHANT = False

from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor
from PyQt6.QtCore import Qt

# Use your project paths
try:
    from hamchat.paths import settings_dir
except Exception:
    # Fallback: project root/settings
    def settings_dir() -> Path:
        return Path("settings").resolve()

SETTINGS_PATH = settings_dir() / "app.json"

def _load_settings() -> dict:
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_settings(cfg: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

# -------------------- Dictionary factory --------------------

class DictionaryFactory:
    _dict = None
    _locale = None

    @classmethod
    def get_dict(cls):
        if not _HAS_ENCHANT:
            return None
        if cls._dict is None:
            cfg = _load_settings()
            locale = cfg.get("locale", "en_US")
            # Try requested locale, then en_GB/en_US fallbacks
            for cand in (locale, "en_GB", "en_US"):
                try:
                    if enchant.dict_exists(cand):
                        cls._dict = enchant.Dict(cand)
                        cls._locale = cand
                        break
                except Exception:
                    continue
            # As a last resort, pick the first available language
            if cls._dict is None:
                langs = [l for l in enchant.list_languages() if enchant.dict_exists(l)]
                if langs:
                    cls._dict = enchant.Dict(langs[0])
                    cls._locale = langs[0]
        return cls._dict

    @classmethod
    def set_locale(cls, locale_code: str) -> bool:
        if not _HAS_ENCHANT:
            return False
        try:
            if enchant.dict_exists(locale_code):
                cls._dict = enchant.Dict(locale_code)
                cls._locale = locale_code
                cfg = _load_settings()
                cfg["locale"] = locale_code
                _save_settings(cfg)
                return True
        except Exception:
            pass
        return False

# -------------------- Highlighter --------------------

_WORD_RE = re.compile(r"[^\W\d_][\w’'-]*", re.UNICODE)  # words; skip pure numbers/underscores
_URL_RE = re.compile(r"(https?://|file://|\w+@\w+)", re.IGNORECASE)

class SpellHighlighter(QSyntaxHighlighter):
    """
    Underlines suspected misspellings with a wavy red underline.
    Safe if pyenchant/dicts are missing (does nothing).
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.dict = DictionaryFactory.get_dict()
        self.enabled = True
        self.error_format = QTextCharFormat()
        self.error_format.setUnderlineColor(QColor("red"))
        self.error_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)

    def setEnabled(self, value: bool):
        self.enabled = value

    def isEnabled(self) -> bool:
        return self.enabled

    def highlightBlock(self, text: str):
        if not self.enabled or self.dict is None or not text:
            return

        # quick skips: ignore lines that look like urls/paths/emails
        if _URL_RE.search(text):
            return

        # normalize accents but keep indices practical
        norm = unicodedata.normalize("NFC", text)
        for m in _WORD_RE.finditer(norm):
            word = m.group(0)
            # skip all-caps (acronyms) and very short tokens
            if len(word) < 2 or word.isupper():
                continue
            try:
                if not self.dict.check(word):
                    self.setFormat(m.start(), m.end() - m.start(), self.error_format)
            except Exception:
                # If the dict flakes, don't explode
                continue

# -------------------- Utilities --------------------

def get_available_locales() -> List[str]:
    if not _HAS_ENCHANT:
        return []
    try:
        return sorted([l for l in enchant.list_languages() if enchant.dict_exists(l)])
    except Exception:
        return []
