"""The texts in another language: services/i18n (look-up, fields, plural forms, fallback,
Qt's own texts), scripts/i18n_extract.py (the template and the checks), the shipped catalogs,
and Settings' Language choice."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from scripts import i18n_extract as extract
from services import i18n
from services.i18n import N_, tr, tr_n, translate

ROOT = Path(__file__).resolve().parents[1]


def write_catalog(directory: str, code: str, entries: dict) -> None:
    path = Path(directory) / f"{code}.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


class CatalogCase(unittest.TestCase):
    """A temporary catalog folder; English again afterwards."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        patch = mock.patch.object(i18n, "catalog_dir", return_value=Path(self.dir))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(i18n.set_language, "en")


class LookupTests(CatalogCase):
    def setUp(self):
        super().setUp()
        write_catalog(
            self.dir,
            "tr",
            {
                "@meta": {"name": "Türkçe"},
                "Settings": "Ayarlar",
                "Sound {ms} ms later": "Ses {ms} ms sonra",
                "Untranslated": "",
                "the badge|Live": "CANLI",
                "Live": "Canlı",
                "{n} channel": {"other": "{n} kanal"},
                "Broken {name}": "Bozuk {nme}",
                "The provider refused the stream.": "Sağlayıcı yayını reddetti.",
            },
        )

    def test_english_is_the_source_text_with_its_fields(self):
        self.assertEqual(tr("Settings"), "Settings")
        self.assertEqual(tr("Sound {ms} ms later", ms=150), "Sound 150 ms later")
        self.assertEqual(tr("{{literal}} {x}", x=1), "{literal} 1")

    def test_a_language_shows_its_translation_with_the_same_fields(self):
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.assertEqual(i18n.language(), "tr")
        self.assertEqual(tr("Settings"), "Ayarlar")
        self.assertEqual(tr("Sound {ms} ms later", ms=150), "Ses 150 ms sonra")

    def test_a_missing_or_empty_translation_shows_english(self):
        i18n.set_language("tr")
        self.assertEqual(tr("Not in the catalog {x}", x=2), "Not in the catalog 2")
        self.assertEqual(tr("Untranslated"), "Untranslated")

    def test_a_context_tells_one_english_text_apart(self):
        i18n.set_language("tr")
        self.assertEqual(tr("Live", ctx="the badge"), "CANLI")
        self.assertEqual(tr("Live"), "Canlı")
        self.assertEqual(tr("Live", ctx="unknown"), "Live")

    def test_a_translation_with_the_wrong_fields_falls_back_to_english(self):
        i18n.set_language("tr")
        with self.assertLogs("services.i18n", "WARNING"):
            self.assertEqual(tr("Broken {name}", name="x"), "Broken x")

    def test_a_language_without_a_catalog_is_english(self):
        with self.assertLogs("services.i18n", "WARNING"):
            self.assertEqual(i18n.set_language("xx"), "en")
            self.assertEqual(i18n.set_language("../tr"), "en")
        self.assertEqual(tr("Settings"), "Settings")

    def test_a_marked_constant_is_translated_where_it_is_shown(self):
        refused = N_("The provider refused the stream.")
        self.assertEqual(refused, "The provider refused the stream.")
        i18n.set_language("tr")
        self.assertEqual(translate(refused), "Sağlayıcı yayını reddetti.")
        # A provider's own name, or a text translated already, comes back as it is.
        self.assertEqual(translate("Sports HD"), "Sports HD")
        self.assertEqual(translate("Ayarlar"), "Ayarlar")
        with self.assertNoLogs("services.i18n"):
            self.assertEqual(translate("Quiz {night} }"), "Quiz {night} }")  # never formatted

    def test_turkish_capitals_keep_the_dotted_i(self):
        self.assertEqual(i18n.upper("live tv"), "LIVE TV")
        i18n.set_language("tr")
        self.assertEqual(i18n.upper("dizi listesi"), "DİZİ LİSTESİ")

    def test_dates_use_the_language_s_day_names(self):
        i18n.set_language("tr")
        self.assertEqual(i18n.app_locale().language(), QLocale.Language.Turkish)
        self.assertEqual(i18n.app_locale().dayName(1), "Pazartesi")


class PluralTests(CatalogCase):
    def test_english_has_one_form_for_one_and_another_for_the_rest(self):
        for n, text in ((0, "0 channels"), (1, "1 channel"), (2, "2 channels")):
            self.assertEqual(tr_n("{n} channel", "{n} channels", n), text)
        self.assertEqual(
            tr_n("{n} stream on {name}", "{n} streams on {name}", 3, name="A"), "3 streams on A"
        )

    def test_turkish_writes_one_form_after_a_number(self):
        write_catalog(self.dir, "tr", {"{n} channel": {"other": "{n} kanal"}})
        i18n.set_language("tr")
        for n in (0, 1, 5):
            self.assertEqual(tr_n("{n} channel", "{n} channels", n), f"{n} kanal")

    def test_each_language_picks_its_own_form(self):
        write_catalog(
            self.dir, "fr", {"{n} channel": {"one": "{n} chaîne", "other": "{n} chaînes"}}
        )
        i18n.set_language("fr")
        self.assertEqual(tr_n("{n} channel", "{n} channels", 0), "0 chaîne")  # French: 0 is one
        self.assertEqual(tr_n("{n} channel", "{n} channels", 2), "2 chaînes")
        write_catalog(
            self.dir,
            "ru",
            {"{n} channel": {"one": "{n} канал", "few": "{n} канала", "many": "{n} каналов"}},
        )
        i18n.set_language("ru")
        self.assertEqual(tr_n("{n} channel", "{n} channels", 21), "21 канал")
        self.assertEqual(tr_n("{n} channel", "{n} channels", 3), "3 канала")
        self.assertEqual(tr_n("{n} channel", "{n} channels", 11), "11 каналов")
        # A form the catalog lacks falls back to "other", then to English.
        write_catalog(self.dir, "de", {"{n} channel": {"other": "{n} Sender"}})
        i18n.set_language("de")
        self.assertEqual(tr_n("{n} channel", "{n} channels", 1), "1 Sender")
        write_catalog(self.dir, "es", {"{n} channel": {"one": "", "other": ""}})
        i18n.set_language("es")
        self.assertEqual(tr_n("{n} channel", "{n} channels", 1), "1 channel")

    def test_plural_rules(self):
        self.assertEqual(i18n.plural_category("tr", 1), "one")
        self.assertEqual(i18n.plural_category("tr", 2), "other")
        self.assertEqual(i18n.plural_category("ja", 1), "other")
        self.assertEqual(i18n.plural_category("pl", 22), "few")
        self.assertEqual(i18n.plural_category("pl", 25), "many")
        self.assertEqual(i18n.plural_category("cs", 3), "few")


class LanguageChoiceTests(CatalogCase):
    def test_english_comes_first_then_each_catalog_by_its_own_name(self):
        write_catalog(self.dir, "tr", {"@meta": {"name": "Türkçe"}})
        write_catalog(self.dir, "de", {})
        write_catalog(self.dir, "template", {"Settings": ""})
        (Path(self.dir) / "broken.json").write_text("{", encoding="utf-8")
        with self.assertLogs("services.i18n", "WARNING"):
            languages = i18n.available_languages()
        self.assertEqual(languages, [("en", "English"), ("de", "Deutsch"), ("tr", "Türkçe")])

    def test_the_system_language_counts_when_qitv_has_it(self):
        write_catalog(self.dir, "tr", {"@meta": {"name": "Türkçe"}})
        with mock.patch.object(i18n, "_system_preferences", return_value=["de", "tr", "en"]):
            self.assertEqual(i18n.system_language(), "tr")
            self.assertEqual(i18n.resolve(None), "tr")
            self.assertEqual(i18n.resolve(""), "tr")
            self.assertEqual(i18n.resolve("en"), "en")
            self.assertEqual(i18n.resolve("tr"), "tr")
            self.assertEqual(i18n.resolve("de"), "en")  # chosen once, its catalog since gone
        with mock.patch.object(i18n, "_system_preferences", return_value=["de", "fr"]):
            self.assertEqual(i18n.system_language(), "en")

    def test_qt_s_own_buttons_follow_the_language(self):
        app = QApplication.instance() or QApplication([])
        write_catalog(self.dir, "tr", {"@meta": {"name": "Türkçe"}})
        i18n.set_language("tr")
        self.addCleanup(i18n.install_qt_translations, app)  # removes it (English again)
        self.assertTrue(i18n.install_qt_translations(app))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.assertEqual(buttons.buttons()[0].text(), "İptal")

    def test_the_pseudo_language_is_longer_and_keeps_the_fields(self):
        text = "Sound {ms} ms later"
        made = i18n.pseudo(text)
        self.assertGreaterEqual(len(made), len(text) * 1.4 - 1)
        self.assertIn("{ms}", made)
        self.assertEqual(i18n.placeholders(made), {"ms"})
        with mock.patch.object(i18n, "_pseudo", True):
            shown = tr("Sound {ms} ms later", ms=150)
            self.assertIn("150", shown)
            self.assertNotIn("Sound", shown)
            self.assertEqual(translate("Sports HD"), "Sports HD")  # not QiTV's text


class ExtractorTests(unittest.TestCase):
    def collect(self, source: str):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "module.py"
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return extract.collect([path], root=Path(directory.name))

    def test_it_collects_texts_fields_plurals_contexts_and_comments(self):
        entries, problems = self.collect('''
            from services.i18n import N_, tr, tr_n

            LABELS = {"a": N_("Movies")}

            def show(n, ms):
                tr("Sound {ms} ms later", ms=ms)  # i18n: the sound panel's delay
                # i18n: a badge; keep it short
                tr("Live", ctx="badge")
                tr_n("{n} channel", "{n} channels", n)
                return lambda: tr("Later")
        ''')
        self.assertEqual(problems, [])
        self.assertEqual(
            sorted(entries), ["Later", "Movies", "Sound {ms} ms later", "badge|Live", "{n} channel"]
        )
        self.assertEqual(entries["Sound {ms} ms later"].comments, ["the sound panel's delay"])
        self.assertEqual(entries["badge|Live"].comments, ["a badge; keep it short"])
        data = extract.template(entries)
        self.assertEqual(data["{n} channel"], {"one": "", "other": ""})
        self.assertEqual(data["@plurals"], {"{n} channel": "{n} channels"})
        self.assertEqual(data["Movies"], "")
        self.assertIn("badge|Live", data["@context"])

    def test_it_reports_texts_it_cannot_read_and_texts_read_at_import(self):
        _entries, problems = self.collect('''
            from services.i18n import tr

            TITLE = tr("At import")

            class Panel:
                LABEL = tr("At class level")

                def label(self, text, other=tr("A default value")):
                    tr(text)
                    tr(f"Hello {text}")
                    tr("Fine")
        ''')
        self.assertEqual(len(problems), 5, problems)
        self.assertEqual(sum("runs at import" in p for p in problems), 3)
        self.assertEqual(sum("needs a string literal" in p for p in problems), 2)

    def test_catalog_checks(self):
        entries, _ = self.collect('''
            from services.i18n import tr, tr_n

            def f(n, ms):
                tr("Sound {ms} ms later", ms=ms)
                tr_n("{n} channel", "{n} channels", n)
                tr("Settings")
        ''')
        catalog = {
            "@meta": {"name": "Test"},
            "Sound {ms} ms later": "Ses {msec} ms sonra",
            "{n} channel": {"other": "{n} kanal"},
            "Retired": "Eski",
        }
        problems = extract.placeholder_problems(catalog, entries)
        self.assertEqual(len(problems), 1)
        self.assertIn("Sound {ms} ms later", problems[0])
        self.assertEqual(extract.missing(catalog, entries), ["Settings"])
        self.assertEqual(extract.obsolete(catalog, entries), ["Retired"])
        updated = extract.updated_catalog(catalog, entries)
        self.assertEqual(updated["@meta"], {"name": "Test"})
        self.assertEqual(updated["Settings"], "")
        self.assertEqual(updated["{n} channel"], {"other": "{n} kanal"})
        self.assertNotIn("Retired", updated)
        self.assertEqual(
            extract.placeholder_problems({"{n} channel": {"other": "{count} kanal"}}, entries),
            ["'{n} channel' (other): fields ['count']"],
        )


class ShippedCatalogTests(unittest.TestCase):
    """The repository's own texts and catalogs."""

    @classmethod
    def setUpClass(cls):
        cls.entries, cls.problems = extract.collect()

    def test_every_tr_call_uses_a_literal_and_runs_when_shown(self):
        self.assertEqual(self.problems, [])

    def test_nothing_is_translated_while_the_modules_import(self):
        # The language is chosen after the imports (main.py): a text read at import, even
        # through a helper such as key_hint(), would stay English.
        probe = textwrap.dedent("""
            import importlib, pkgutil, sys
            from services import i18n
            calls = []
            for name in ("tr", "tr_n"):
                real = getattr(i18n, name)
                def spy(*args, _real=real, **kwargs):
                    caller = sys._getframe(1)
                    calls.append(f"{caller.f_code.co_filename}:{caller.f_lineno} {args[0]!r}")
                    return _real(*args, **kwargs)
                setattr(i18n, name, spy)
            import services, ui
            for package in (services, ui):
                for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
                    importlib.import_module(module.name)
            importlib.import_module("main")
            print("\\n".join(calls))
            """)
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(ROOT))
        done = subprocess.run(
            [sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "")

    def test_the_template_is_up_to_date(self):
        # Regenerate with `uv run python scripts/i18n_extract.py`.
        self.assertEqual(extract.read_json(extract.TEMPLATE_PATH), extract.template(self.entries))

    def test_every_catalog_holds_template_texts_with_their_fields(self):
        paths = extract.catalog_paths()
        self.assertIn("tr", [path.stem for path in paths])
        for path in paths:
            with self.subTest(path.name):
                catalog = extract.read_json(path)
                self.assertEqual(extract.obsolete(catalog, self.entries), [])
                self.assertEqual(extract.placeholder_problems(catalog, self.entries), [])
                # UTF-8, sorted, as the extractor writes it (a diff shows one text per line).
                self.assertEqual(path.read_text(encoding="utf-8"), extract.dumps(catalog))

    def test_turkish_translates_every_text(self):
        catalog = extract.read_json(extract.CATALOGS / "tr.json")
        self.assertEqual(extract.missing(catalog, self.entries), [])
        self.assertEqual(i18n.native_name("tr", catalog), "Türkçe")

    def test_the_packaged_apps_carry_the_catalogs(self):
        for spec in ("qitv-macos.spec", "qitv-windows.spec", "qitv-linux.spec"):
            with self.subTest(spec):
                self.assertIn("('i18n/*.json', 'i18n')", (ROOT / spec).read_text())


if __name__ == "__main__":
    unittest.main()
