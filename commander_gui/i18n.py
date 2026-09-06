"""UI translation: active-language state and the ``tr()`` lookup helper.

Mirrors ``themes.py``'s ``THEME_INFO`` / ``set_active_theme()`` /
``active_theme()`` pattern - the same "a list of options, a module-level
active choice, get/set accessors" shape already used for themes, so this
reuses a convention proven in this codebase rather than inventing a new one.

Every user-facing string in ``commander_gui/ui/*.py`` is wrapped in
``tr("English source text")``. The English source string is itself the
dictionary key a translation is looked up by (the gettext convention: the
msgid IS the default-language string) - a string with no translation entry,
or a language with no entry for it, silently falls back to that English
text. Nothing can crash or show a blank/placeholder because of a missing
translation.
"""

from __future__ import annotations

from importlib import import_module

#: (code, native name, English name) shown on the Settings page, in order.
LANGUAGE_INFO: list[tuple[str, str, str]] = [
    ("en", "English", "English"),
    ("fr", "Français", "French"),
    ("es", "Español", "Spanish"),
    ("de", "Deutsch", "German"),
    ("ro", "Română", "Romanian"),
    ("pl", "Polski", "Polish"),
    ("ru", "Русский", "Russian"),
    ("uk", "Українська", "Ukrainian"),
    ("pt", "Português", "Portuguese"),
    ("tr", "Türkçe", "Turkish"),
]

_LANGUAGE_CODES = {code for code, _native, _english in LANGUAGE_INFO}


def _load_translations() -> dict[str, dict[str, str]]:
    """Import each locale module's TRANSLATIONS dict, keyed by language code.

    English needs no module/dict of its own: tr()'s fallback to the source
    text already IS the English text.
    """
    tables: dict[str, dict[str, str]] = {}
    for code in _LANGUAGE_CODES:
        if code == "en":
            continue
        module = import_module(f".locales.{code}", package=__package__)
        tables[code] = module.TRANSLATIONS
    return tables


_TRANSLATIONS: dict[str, dict[str, str]] = _load_translations()

_ACTIVE: str = "en"


def set_active_language(code: str) -> None:
    if code in _LANGUAGE_CODES:
        global _ACTIVE
        _ACTIVE = code


def active_language() -> str:
    return _ACTIVE


def tr(text: str, **kwargs: object) -> str:
    """Translate ``text`` via the active language's table.

    Falls back to ``text`` itself when the active language is English, has
    no table, or has no entry for this exact string. ``**kwargs`` are
    applied via ``str.format`` after translation, for strings with named
    placeholders (e.g. ``tr("Could not create {name}", name=name)``).
    """
    translated = _TRANSLATIONS.get(_ACTIVE, {}).get(text, text)
    return translated.format(**kwargs) if kwargs else translated
