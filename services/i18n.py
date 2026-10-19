"""The app's texts in the chosen language (no widgets).

Every text QiTV shows is written in English in the code and looked up in a catalog,
`i18n/<code>.json` (UTF-8, `{"English source": "translation"}`):

    tr("Settings")
    tr("Sound {ms} ms later", ms=150)            # named fields, in every language
    tr_n("{n} channel", "{n} channels", count)    # counts: the forms the language needs
    tr("Live", ctx="the badge")                   # one English text, two meanings

A missing or empty translation shows the English text. Texts are looked up when they are
shown: the language is chosen once, while QiTV starts (main.set_language), before any window
exists, and a text kept at module level is only marked (`N_`) and translated where it is used
(`translate`). scripts/i18n_extract.py collects every marked text into i18n/template.json,
the starting point of a new language.

A plural entry holds one form per CLDR category the language uses ("one", "few", "many",
"other"); Turkish writes one form after a number, so its entries hold "other" alone.

QITV_PSEUDO_LANG=1 shows every text accented and 40% longer, to find texts that a longer
language would cut off.
"""

from __future__ import annotations

import json
import locale
import logging
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

SOURCE_LANGUAGE = "en"
SOURCE_NAME = "English"
PSEUDO_ENV = "QITV_PSEUDO_LANG"
# The template every language starts from (not a language).
TEMPLATE = "template"
# Keys starting with this hold the catalog's own data ("@meta": its language's name).
META_PREFIX = "@"
PLURAL_FORMS = ("zero", "one", "two", "few", "many", "other")

_catalog: dict[str, Any] = {}
_language = SOURCE_LANGUAGE
_pseudo = os.environ.get(PSEUDO_ENV) == "1"
_qt_translators: list[Any] = []  # kept alive while installed
_reported: set[str] = set()
_upper_locale: Optional[tuple[str, Any]] = None
_pseudo_keys: Optional[frozenset[str]] = None


def catalog_dir() -> Path:
    """Where the catalogs are: beside the packaged app's files, or the source tree's i18n/."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "i18n"
    return Path(__file__).resolve().parent.parent / "i18n"


def N_(text: str) -> str:
    """Marks a text kept in a table or constant for the catalogs; `translate` shows it."""
    return text


def tr(text: str, /, *, ctx: Optional[str] = None, **fields: Any) -> str:
    """`text` (an English literal) in the active language, its {fields} filled in.

    `ctx` tells apart one English text with two meanings ("Live" the badge, "Live" the
    section); it is part of the catalog's key ("the badge|Live") and never shown.
    """
    found = _catalog.get(_key(text, ctx)) if _catalog else None
    return _finish(text, found if isinstance(found, str) and found else None, fields)


def tr_n(singular: str, plural: str, n: int, /, *, ctx: Optional[str] = None, **fields: Any) -> str:
    """The form for `n` of a counted text: tr_n("{n} channel", "{n} channels", 3).

    `n` is a field too ({n}); other fields are named as in tr().
    """
    fields.setdefault("n", n)
    english = singular if abs(int(n)) == 1 else plural
    entry = _catalog.get(_key(singular, ctx)) if _catalog else None
    if isinstance(entry, dict):
        entry = entry.get(plural_category(_language, n)) or entry.get("other")
    return _finish(english, entry if isinstance(entry, str) and entry else None, fields)


def translate(text: str, **fields: Any) -> str:
    """A text marked with N_ where it was written (a table's, a constant's), shown now.

    A text that is no catalog's key (a provider's group name) comes back as it is.
    """
    found = _catalog.get(text) if _catalog else None
    if _pseudo and text not in _pseudo_known():
        return text
    return _finish(text, found if isinstance(found, str) and found else None, fields)


def upper(text: str) -> str:
    """`text` in capitals as the active language writes them (Turkish i → İ)."""
    global _upper_locale
    if _upper_locale is None or _upper_locale[0] != _language:
        from PySide6.QtCore import QLocale

        _upper_locale = (_language, QLocale(_language))
    return str(_upper_locale[1].toUpper(text))


def language() -> str:
    """The active language's code ("en" until set_language chose another)."""
    return _language


def set_language(code: Optional[str]) -> str:
    """Make `code`'s catalog the active one; English when it has none. Returns the code used."""
    global _catalog, _language
    code = (code or SOURCE_LANGUAGE).strip()
    if code == SOURCE_LANGUAGE:
        _catalog, _language = {}, SOURCE_LANGUAGE
        return _language
    catalog = load_catalog(code)
    if catalog is None:
        logger.warning("No texts for language %r; showing English", code)
        _catalog, _language = {}, SOURCE_LANGUAGE
    else:
        _catalog, _language = catalog, code
    _reported.clear()
    return _language


def load_catalog(code: str) -> Optional[dict[str, Any]]:
    """`code`'s catalog, or None when there is none or it can't be read."""
    if not _is_code(code) or code == TEMPLATE:
        return None
    path = catalog_dir() / f"{code}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Couldn't read %s: %s", path.name, e)
        return None
    return data if isinstance(data, dict) else None


def available_languages() -> list[tuple[str, str]]:
    """(code, the language's own name) of English and of every catalog, English first."""
    found: list[tuple[str, str]] = []
    try:
        paths = sorted(catalog_dir().glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        code = path.stem
        if code in (TEMPLATE, SOURCE_LANGUAGE) or not _is_code(code):
            continue
        catalog = load_catalog(code)
        if catalog is not None:
            found.append((code, native_name(code, catalog)))
    found.sort(key=lambda entry: entry[1].casefold())
    return [(SOURCE_LANGUAGE, SOURCE_NAME)] + found


def native_name(code: str, catalog: Optional[Mapping[str, Any]] = None) -> str:
    """The language's name in itself: the catalog's "@meta" name, else Qt's."""
    meta = (catalog or {}).get("@meta")
    name = meta.get("name") if isinstance(meta, dict) else None
    if isinstance(name, str) and name:
        return name
    if code == SOURCE_LANGUAGE:
        return SOURCE_NAME
    from PySide6.QtCore import QLocale

    own = str(QLocale(code).nativeLanguageName() or code)
    return str(QLocale(code).toUpper(own[:1])) + own[1:]


def system_language() -> str:
    """The first of the system's preferred languages QiTV has texts for, else English."""
    codes = {code for code, _name in available_languages()}
    for wanted in _system_preferences():
        if wanted in codes:
            return wanted
    return SOURCE_LANGUAGE


def resolve(choice: Optional[str]) -> str:
    """The language a saved choice means: none (the default) follows the system."""
    if not choice:
        return system_language()
    codes = {code for code, _name in available_languages()}
    return choice if choice in codes else SOURCE_LANGUAGE


def app_locale() -> Any:
    """The QLocale for dates and day names: the system's own when it speaks the active
    language (its region's formats), else the active language's."""
    from PySide6.QtCore import QLocale

    system = QLocale.system()
    if _base(system.name()) == _language:
        return system
    return QLocale(_language)


def install_qt_translations(app: Any) -> bool:
    """Load Qt's own texts (standard buttons, file dialogs) in the active language.

    Qt's catalogs ship with PySide6 (qtbase_<code>.qm); the packaged app carries them where
    PyInstaller's Qt hook collects them. Returns whether one was loaded.
    """
    from PySide6.QtCore import QTranslator

    for translator in _qt_translators:
        app.removeTranslator(translator)
    _qt_translators.clear()
    if _language == SOURCE_LANGUAGE:
        return False
    for directory in _qt_translation_dirs():
        translator = QTranslator(app)
        if translator.load(f"qtbase_{_language}", str(directory)):
            app.installTranslator(translator)
            _qt_translators.append(translator)
            return True
    logger.info("No Qt translations for %r", _language)
    return False


def plural_category(code: str, n: int) -> str:
    """The CLDR plural category of the whole number `n` in language `code`."""
    n = abs(int(n))
    base = _base(code)
    if base in _NO_PLURALS:
        return "other"
    if base in _ONE_FOR_0_1:
        return "one" if n in (0, 1) else "other"
    if base in _SLAVIC:
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    if base == "pl":
        if n == 1:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    if base in ("cs", "sk"):
        return "one" if n == 1 else "few" if 2 <= n <= 4 else "other"
    return "one" if n == 1 else "other"


def placeholders(text: str) -> set[str]:
    """The {field} names a text uses."""
    import string

    names = set()
    for _literal, name, _spec, _conversion in string.Formatter().parse(text):
        if name is not None:
            names.add(name)
    return names


def pseudo(text: str) -> str:
    """The pseudo-language: accented letters, 40% longer, fields kept."""
    out, depth = [], 0
    for char in text:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        out.append(char if depth else _ACCENTS.get(char, char))
    # "[", " ", "]" and the tildes add 40% of the text's length.
    return "[" + "".join(out) + " " + "~" * max(1, round(len(text) * 0.4) - 3) + "]"


# --- inside --------------------------------------------------------------------------------

_NO_PLURALS = {"ja", "ko", "zh", "vi", "th", "id", "ms"}
_ONE_FOR_0_1 = {"fr", "pt"}
_SLAVIC = {"ru", "uk", "be"}
_ACCENTS = dict(zip("aceinouyAEINOUCSZsz", "àçéîñöüýÅÉÎÑÖÜÇŠŽšž"))


def _key(text: str, ctx: Optional[str]) -> str:
    return f"{ctx}|{text}" if ctx else text


def _pseudo_known() -> frozenset[str]:
    """The template's texts: translate() makes only those pseudo, never a provider's names."""
    global _pseudo_keys
    if _pseudo_keys is None:
        try:
            data = json.loads((catalog_dir() / f"{TEMPLATE}.json").read_text(encoding="utf-8"))
            _pseudo_keys = frozenset(data if isinstance(data, dict) else ())
        except OSError, ValueError:
            _pseudo_keys = frozenset()
    return _pseudo_keys


def _finish(english: str, found: Optional[str], fields: Mapping[str, Any]) -> str:
    text = found or english
    if _pseudo:
        text = pseudo(text)
    try:
        return text.format_map(fields)
    except (KeyError, IndexError, ValueError) as e:
        if found is not None or _pseudo:
            if english not in _reported:
                _reported.add(english)
                logger.warning("The %s text of %r doesn't format: %s", _language, english, e)
            try:
                return english.format_map(fields)
            except KeyError, IndexError, ValueError:
                pass
        logger.error("The text %r doesn't format: %s", english, e)
        return english


def _is_code(code: str) -> bool:
    return bool(code) and len(code) <= 16 and all(c.isalnum() or c in "-_" for c in code)


def _base(name: str) -> str:
    return name.replace("-", "_").split("_")[0].lower()


def _system_preferences() -> list[str]:
    """The system's languages, most preferred first, as base codes ("tr")."""
    wanted: list[str] = []
    try:
        from PySide6.QtCore import QLocale

        wanted += [_base(name) for name in QLocale.system().uiLanguages()]
        wanted.append(_base(QLocale.system().name()))
    except Exception as e:  # Qt missing in a helper: the C library's locale still answers
        logger.debug("No system languages from Qt: %s", e)
    try:
        name = locale.getlocale()[0]
        if name:
            wanted.append(_base(name))
    except ValueError:
        pass
    return [code for code in dict.fromkeys(wanted) if code]


def _qt_translation_dirs() -> list[Path]:
    from PySide6.QtCore import QLibraryInfo

    dirs = [Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        dirs.append(Path(bundle) / "PySide6" / "Qt" / "translations")
    try:
        import PySide6

        dirs.append(Path(PySide6.__file__).resolve().parent / "Qt" / "translations")
    except Exception:
        pass
    return [d for d in dict.fromkeys(dirs) if d.is_dir()]
