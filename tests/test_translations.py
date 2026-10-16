"""Every visible string is translated in every language.

``tr()`` falls back to English for a missing key, so a forgotten
translation never crashes - it just quietly shows English to everyone
using another language. These tests make that a failure instead.
"""

import ast
import importlib
import re
import unittest
from pathlib import Path

from commander_gui.i18n import LANGUAGE_INFO
from commander_gui.locales import template

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("commander_gui", "Steamdeck", "assistant")
LANGUAGES = [code for code, _native, _english in LANGUAGE_INFO if code != "en"]
PLACEHOLDER_RE = re.compile(r"\{(\w*)\}")


def _source_files():
    for folder in SOURCE_DIRS:
        for path in (REPO_ROOT / folder).rglob("*.py"):
            if "locales" not in path.parts:
                yield path


def _tr_literals() -> set[str]:
    """Every string literal passed straight to ``tr()``."""
    found: set[str] = set()
    for path in _source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.add(node.args[0].value)
    return found


def _all_string_constants() -> set[str]:
    """Every string constant in the code - also catches strings handed to
    ``tr()`` through a variable (stat names, achievement texts, ...)."""
    found: set[str] = set()
    for path in _source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


class TranslationCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.literals = _tr_literals()
        cls.tables = {
            code: importlib.import_module(f"commander_gui.locales.{code}").TRANSLATIONS
            for code in LANGUAGES
        }

    def test_template_lists_every_string_the_code_translates(self):
        missing = sorted(self.literals - set(template.TRANSLATIONS))
        self.assertEqual(missing, [], "add these to commander_gui/locales/template.py")

    def test_template_lists_every_about_page_string(self):
        """about_docs.py is plain data that about_page.py runs through tr()."""
        from commander_gui.ui.about_docs import TOPICS
        from commander_gui.ui.about_page import _strings

        found = set()
        for topic in TOPICS:
            no_code = [
                (heading, [b for b in blocks if not (isinstance(b, tuple) and b[0] == "code")])
                for heading, blocks in topic["sections"]
            ]
            found.update(_strings({**topic, "sections": no_code}))
        # Live rows contribute their captions and their keys; keys are not text.
        live_keys = {
            key for topic in TOPICS for _h, blocks in topic["sections"]
            for b in blocks if isinstance(b, tuple) and b[0] == "live" for _c, key in b[1]
        }
        missing = sorted(found - live_keys - set(template.TRANSLATIONS))
        self.assertEqual(missing, [], "add these to commander_gui/locales/template.py")

    def test_template_lists_every_assistant_label_shown_through_tr(self):
        """ASSISTANT keeps some English constants as identities and translates
        them only when shown (knowledge texts, categories, severities...)."""
        from assistant import knowledge
        from assistant.analyzers.winetricks_vcredist import _CODE_MEANINGS
        from assistant.dump import SOURCE_LABELS
        from assistant.findings import CATEGORIES, SEVERITY_LABEL
        from assistant.ui.widgets import FINDING_GROUPS

        found = {
            value
            for name, value in vars(knowledge).items()
            if name.isupper() and isinstance(value, str)
        }
        found.update(knowledge.XRAY_FATAL_GENERIC, CATEGORIES, SEVERITY_LABEL.values())
        found.update(name for name, _severities in FINDING_GROUPS)
        found.update(SOURCE_LABELS.values())
        found.update(text for text, _level in _CODE_MEANINGS.values())
        missing = sorted(found - set(template.TRANSLATIONS))
        self.assertEqual(missing, [], "add these to commander_gui/locales/template.py")

    def test_every_language_translates_every_template_string(self):
        for code, table in self.tables.items():
            with self.subTest(language=code):
                missing = sorted(
                    key for key in template.TRANSLATIONS if not table.get(key)
                )
                self.assertEqual(missing, [], f"untranslated in {code}.py")

    def test_languages_have_no_keys_the_template_lacks(self):
        for code, table in self.tables.items():
            with self.subTest(language=code):
                extra = sorted(set(table) - set(template.TRANSLATIONS))
                self.assertEqual(extra, [], f"leftover keys in {code}.py")

    def test_placeholders_survive_translation(self):
        for code, table in self.tables.items():
            for key, value in table.items():
                with self.subTest(language=code, key=key[:60]):
                    self.assertEqual(
                        sorted(set(PLACEHOLDER_RE.findall(key))),
                        sorted(set(PLACEHOLDER_RE.findall(value))),
                    )

    def test_no_leftover_strings(self):
        """A template key the code no longer contains anywhere is dead text."""
        stale = sorted(set(template.TRANSLATIONS) - _all_string_constants())
        self.assertEqual(stale, [], "remove these from the locale files")


if __name__ == "__main__":
    unittest.main()
