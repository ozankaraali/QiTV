"""Finding and downloading subtitles on OpenSubtitles.com (its REST API, v1); no widgets.

The Subtitles panel's "Search OpenSubtitles…" (ui/subtitle_search.py) runs these on the
app's job runner, never on the GUI thread: `SearchWorker` searches, `DownloadWorker` fetches a
file into the subtitles folder (or finds it there already) and `LogoutWorker` ends a sign-in.
Each emits plain data: a `SearchResult`, a `Downloaded`, or a `Problem` that says in plain
words what went wrong (a key OpenSubtitles refused, today's downloads used, no connection…).

The API wants an API key with every request and an app's user agent ("QiTV v2.2.0"). The key
is the user's own (Settings → Subtitles) or, without one, the key a release build carries
(services/_build_keys.py, written by CI). Signing in (a username and password, optional)
raises the daily downloads: `OpenSubtitlesClient` logs in before its first download, keeps the
token in memory only, and logs in again once when OpenSubtitles no longer takes it. Neither
the key, the password nor the token is ever logged, and neither is a download's link.

What plays is searched by its id when its provider gives one (a TMDB or IMDb id: a film's,
an episode's, or its series' with the season and episode numbers), else by its title:
`search_query()` cleans a provider's title ("TR: ", "TR - ", "|EN| ", "4K", "1080p" …),
prefers a film's original title when the provider names one in Latin letters, and adds a
film's year or an episode's season and number; the search field shows it as "Title (2019)"
or "Series S01E02", and `parse_query()` reads an edited one back. An id that finds nothing
is searched again by the title.

Each subtitle names the film or episode it was made for (`Feature`). `split_matches` keeps
the ones whose film is the one asked for (its id, else its title's words and the year within
one): a title in another language can find other films' subtitles, which are never shown as
this one's. Results are grouped by language (`group_languages`) and ranked within one
(`ranked`): machine translations last, hearing-impaired after the others unless the viewer
picked those before, trusted and ranked uploaders first, then the most downloaded.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import datetime as _dt
import logging
import os
import re
import tempfile
import threading
from typing import Any, Iterable, Optional, Sequence
import unicodedata
from urllib.parse import urljoin, urlsplit

from PySide6.QtCore import QLocale, QObject, Signal, Slot

from services import _build_keys, i18n
from services.config_manager import get_app_version
from services.i18n import N_, tr, tr_n, translate
from services.jobs import cancel_requested
from services.lazy_import import lazy_module
from services.next_episode import season_and_episode
from services.redact import safe_exc, safe_message

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

API_URL = "https://api.opensubtitles.com/api/v1"
CONSUMERS_URL = "https://www.opensubtitles.com/en/consumers"  # sign up, then make a key
TIMEOUT = (4, 10)  # connect, read (seconds): short
MAX_PAGES = 3  # of a search's results (OpenSubtitles sends up to 50 a page)
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5  # of a download's link, each one https
_CHUNK = 64 * 1024
_REDIRECTS = (301, 302, 303, 307, 308)
ANONYMOUS_DAILY = 5  # downloads a day without signing in, per connection
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")
# A subtitle's release when OpenSubtitles names neither it nor its file: kept in English (it
# may name the file), translated where the page shows it.
UNTITLED = N_("Untitled")  # i18n: a subtitle on the OpenSubtitles page whose release has no name

# What went wrong (`Problem.kind`).
NO_KEY = "no_key"
BAD_KEY = "bad_key"
BAD_LOGIN = "bad_login"
QUOTA = "quota"
BUSY = "busy"
NETWORK = "network"
SERVER = "server"
GONE = "gone"
DISK = "disk"

# What to tell the viewer (`Problem.text`: translated where the problem is made).
NO_KEY_TEXT = N_("OpenSubtitles needs an API key.")
BAD_KEY_TEXT = N_("OpenSubtitles didn't accept the API key. Check it, or paste a new one.")
BAD_LOGIN_TEXT = N_(
    "OpenSubtitles didn't accept your username or password. Check them in Settings → Subtitles."
)
SIGN_IN_AGAIN_TEXT = N_(
    "OpenSubtitles ended your sign-in. Sign in to OpenSubtitles again in Settings → Subtitles."
)
NETWORK_TEXT = N_("Couldn't reach OpenSubtitles. Check your internet connection and try again.")
SERVER_TEXT = N_("OpenSubtitles is having trouble right now. Try again in a while.")
UNREADABLE_TEXT = N_("OpenSubtitles sent an answer QiTV couldn't read. Try again in a while.")
GONE_TEXT = N_("That subtitle file is no longer on OpenSubtitles. Pick another one.")
DISK_TEXT = N_("Couldn't save the subtitles on this computer.")


def built_in_key() -> str:
    """The key a release build carries ("" in a build from the source code)."""
    return str(getattr(_build_keys, "OPENSUBTITLES_API_KEY", "") or "").strip()


def api_key(user_key: str) -> str:
    """The key to use: the user's own when set, else the build's ("": none)."""
    return str(user_key or "").strip() or built_in_key()


def user_agent() -> str:
    return f"QiTV v{get_app_version()}"


def shown_release(release: str) -> str:
    """A subtitle's release as the page shows it ("Untitled" translated)."""
    return translate(UNTITLED) if release == UNTITLED else release


# ---- what was found -----------------------------------------------------------------------


@dataclass(frozen=True)
class Feature:
    """The film or episode a subtitle was made for, as OpenSubtitles names it."""

    kind: str = ""  # "movie", "episode", "tvshow" (lower case)
    title: str = ""  # a film's title; an episode's own ("The Bridge")
    movie_name: str = ""  # OpenSubtitles' full name: "2019 - Harbour Lights"
    parent_title: str = ""  # an episode's series
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    imdb_id: Optional[int] = None
    parent_tmdb_id: Optional[int] = None
    parent_imdb_id: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None

    def names(self) -> list[str]:
        return [name for name in (self.title, self.parent_title, self.movie_name) if name]

    def label(self) -> str:
        """ "Harbour Lights (2019)", "Salt Road S02E05"."""
        if self.parent_title and self.season is not None and self.episode is not None:
            return f"{self.parent_title} S{self.season:02d}E{self.episode:02d}"
        if self.title:
            return f"{self.title} ({self.year})" if self.year else self.title
        return self.movie_name or self.parent_title


@dataclass(frozen=True)
class SubtitleFile:
    file_id: int
    language: str  # OpenSubtitles' code: "en", "pt-BR"
    release: str  # the release it was made for, else its file name
    file_name: str = ""
    downloads: int = 0
    hearing_impaired: bool = False
    machine_translated: bool = False  # by a machine (or an AI)
    trusted: bool = False  # from a trusted uploader
    uploader_rank: str = ""  # "Sub leecher", "Gold member", "Administrator" …
    order: int = 0  # where the API listed it
    feature: Optional[Feature] = None  # what it was made for, when OpenSubtitles said


@dataclass(frozen=True)
class SearchQuery:
    text: str  # the title, cleaned
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    # Ids from the provider (an IMDb id without its "tt"): a film's or an episode's own, or an
    # episode's series' ("parent"). With one, it is searched for instead of the text.
    tmdb_id: Optional[int] = None
    imdb_id: Optional[int] = None
    parent_tmdb_id: Optional[int] = None
    parent_imdb_id: Optional[int] = None

    @property
    def by_id(self) -> bool:
        return bool(self.tmdb_id or self.imdb_id or self.by_parent)

    @property
    def by_parent(self) -> bool:
        """An episode's, by its series' id."""
        return bool(self.parent_tmdb_id or self.parent_imdb_id)

    def without_ids(self) -> SearchQuery:
        """The same search by its text alone."""
        return replace(self, tmdb_id=None, imdb_id=None, parent_tmdb_id=None, parent_imdb_id=None)

    def label(self) -> str:
        """As the search field shows it: "Title (2019)", "Series S01E02"."""
        text = self.text
        if self.season is not None and self.episode is not None:
            return f"{text} S{self.season:02d}E{self.episode:02d}"
        if self.season is not None:
            return f"{text} S{self.season:02d}"
        return f"{text} ({self.year})" if self.year else text

    def params(self, languages: Sequence[str] = ()) -> list[tuple[str, str]]:
        """The search's parameters, sorted and lower-case as the API asks (else it redirects).
        By an id when there is one (an IMDb id first: OpenSubtitles' own): an episode's series'
        with its season and episode numbers, else its own (or a film's); else by the text and
        the year."""
        params: dict[str, str] = {}
        numbered = True  # the season and episode numbers go with the search
        own = self.imdb_id or self.tmdb_id
        numbers = self.season is not None and self.episode is not None
        if self.by_parent and (numbers or not own):
            if self.parent_imdb_id:
                params["parent_imdb_id"] = str(self.parent_imdb_id)
            else:
                params["parent_tmdb_id"] = str(self.parent_tmdb_id)
            params["type"] = "episode"
        elif own:
            if self.imdb_id:
                params["imdb_id"] = str(self.imdb_id)
            else:
                params["tmdb_id"] = str(self.tmdb_id)
            episode = self.season is not None or self.episode is not None or self.by_parent
            params["type"] = "episode" if episode else "movie"
            numbered = False  # an episode's own id names it whole
        else:
            params["query"] = _without_year(self.text, self.year).lower()
            if self.year:
                params["year"] = str(self.year)
        if numbered and self.season is not None:
            params["season_number"] = str(self.season)
        if numbered and self.episode is not None:
            params["episode_number"] = str(self.episode)
        if languages:
            params["languages"] = ",".join(sorted({code.lower() for code in languages}))
        return sorted(params.items())


def _without_year(text: str, year: Optional[int]) -> str:
    """The text without the year at its end ("Harbour Lights 2019"): it goes as `year`."""
    words = text.split()
    if year and len(words) > 1 and words[-1] == str(year):
        return " ".join(words[:-1])
    return text


@dataclass(frozen=True)
class SearchResult:
    query: SearchQuery  # what was searched for (by its text when its id found nothing)
    files: tuple[SubtitleFile, ...]  # for the title asked for
    others: tuple[SubtitleFile, ...] = ()  # what else the search found: other titles'


@dataclass(frozen=True)
class Downloaded:
    path: str  # where it is on this computer
    name: str  # its file name: the Subtitles panel names it so
    file_id: int
    language: str
    remaining: Optional[int] = None  # downloads left today, when OpenSubtitles said
    reset: str = ""  # when they come back: "23 hours and 52 minutes"
    reused: bool = False  # it was on this computer already: nothing was downloaded


@dataclass(frozen=True)
class Problem:
    kind: str
    text: str  # what to tell the viewer


class OpenSubtitlesError(Exception):
    def __init__(self, kind: str, text: str) -> None:
        super().__init__(kind)
        self.problem = Problem(kind, text)


def parse_search(data: Any, start: int = 0) -> list[SubtitleFile]:
    """The subtitles of a search's answer (each one's first file), in the API's order."""
    entries = data.get("data") if isinstance(data, dict) else None
    found: list[SubtitleFile] = []
    for entry in entries if isinstance(entries, list) else []:
        attributes = entry.get("attributes") if isinstance(entry, dict) else None
        if not isinstance(attributes, dict):
            continue
        files = [f for f in attributes.get("files") or [] if isinstance(f, dict)]
        first = next((f for f in files if _int(f.get("file_id")) is not None), None)
        if first is None:
            continue
        uploader = attributes.get("uploader")
        rank = uploader.get("rank") if isinstance(uploader, dict) else ""
        file_name = str(first.get("file_name") or "").strip()
        found.append(
            SubtitleFile(
                file_id=int(first["file_id"]),
                language=str(attributes.get("language") or "").strip() or "und",
                release=str(attributes.get("release") or "").strip() or file_name or UNTITLED,
                file_name=file_name,
                downloads=_int(attributes.get("download_count")) or 0,
                hearing_impaired=attributes.get("hearing_impaired") is True,
                machine_translated=(
                    attributes.get("machine_translated") is True
                    or attributes.get("ai_translated") is True
                ),
                trusted=attributes.get("from_trusted") is True,
                uploader_rank=str(rank or ""),
                order=start + len(found),
                feature=_feature(attributes.get("feature_details")),
            )
        )
    return found


def _feature(details: Any) -> Optional[Feature]:
    """A subtitle's `feature_details`: the film or episode it was made for (None: unnamed)."""
    if not isinstance(details, dict):
        return None

    def text(key: str) -> str:
        return " ".join(str(details.get(key) or "").split())

    title, movie_name, parent = text("title"), text("movie_name"), text("parent_title")
    if not (title or movie_name or parent):
        return None
    return Feature(
        kind=text("feature_type").lower(),
        title=title,
        movie_name=movie_name,
        parent_title=parent,
        year=_plausible_year(details.get("year")),
        tmdb_id=_positive(details.get("tmdb_id")),
        imdb_id=_positive(details.get("imdb_id")),
        parent_tmdb_id=_positive(details.get("parent_tmdb_id")),
        parent_imdb_id=_positive(details.get("parent_imdb_id")),
        season=_int(details.get("season_number")),
        episode=_int(details.get("episode_number")),
    )


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _positive(value: Any) -> Optional[int]:
    number = _int(value)
    return number if number is not None and number > 0 else None


# ---- whether a subtitle is for the title asked for ----------------------------------------

# Words that tell no title from another ("The Film" is not "The Other").
_LITTLE_WORDS = frozenset("""
    the a an of and in on at to for with la le les el los las lo il gli un une una uno de del
    des du der die das den dem ein eine und et en ve bir ile
    """.split())


def _words(text: str) -> list[str]:
    """A title's words, without case or accents ("Süper" → "super", "İstanbul" → "istanbul")."""
    folded = unicodedata.normalize("NFKD", str(text).replace("ı", "i").replace("İ", "I"))
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    return [w for w in re.findall(r"[^\W_]+", folded) if len(w) > 1 or w.isdigit()]


def _key_words(text: str) -> set[str]:
    words = set(_words(text))
    return (words - _LITTLE_WORDS) or words


def same_title(asked: str, name: str) -> bool:
    """Whether `name` is the title `asked` for: at least half of the words asked for are in
    it, and they make a third of its own ("Harbour Lights" is "2019 - Harbour Lights", not
    "Harbour Lights of the Northern Coast and the Islands")."""
    wanted, theirs = _key_words(asked), _key_words(name)
    common = len(wanted & theirs)
    return common > 0 and 2 * common >= len(wanted) and 3 * common >= len(theirs)


def _same_ids(query: SearchQuery, feature: Feature) -> Optional[bool]:
    """Whether the ids asked for are the feature's (None: none to compare)."""
    pairs = (
        (query.tmdb_id, feature.tmdb_id),
        (query.imdb_id, feature.imdb_id),
        (query.parent_tmdb_id, feature.parent_tmdb_id),
        (query.parent_imdb_id, feature.parent_imdb_id),
    )
    compared = [ours == theirs for ours, theirs in pairs if ours and theirs]
    return any(compared) if compared else None


def feature_matches(feature: Feature, query: SearchQuery) -> bool:
    """Whether a subtitle made for `feature` is for what `query` asks: its ids (the ones
    asked for), else its title's words; the year within one when both are known, the season
    and episode when both are."""
    if query.year and feature.year and abs(feature.year - query.year) > 1:
        return False
    for asked, theirs in ((query.season, feature.season), (query.episode, feature.episode)):
        if asked is not None and theirs is not None and asked != theirs:
            return False
    ids = _same_ids(query, feature)
    if ids is not None:
        return ids
    return any(same_title(query.text, name) for name in feature.names())


def split_matches(
    files: Iterable[SubtitleFile], query: SearchQuery
) -> tuple[list[SubtitleFile], list[SubtitleFile]]:
    """(the subtitles for what `query` asks, the others), each in its order. When none names
    what it was made for, there is nothing to tell them by: all of them are taken."""
    files = list(files)
    if not any(file.feature is not None for file in files):
        return files, []
    fits = [file.feature is not None and feature_matches(file.feature, query) for file in files]
    # A film one subtitle's own name matched is that film wherever else it comes: a translated
    # title finds a few subtitles named so and many more under the film's original name, all
    # with its ids (OpenSubtitles' film).
    films = {_film_of(f.feature) for f, ok in zip(files, fits) if ok and f.feature is not None}
    films.discard(None)
    matching: list[SubtitleFile] = []
    others: list[SubtitleFile] = []
    for file, ok in zip(files, fits):
        ok = ok or (file.feature is not None and _film_of(file.feature) in films)
        (matching if ok else others).append(file)
    return matching, others


def _film_of(feature: Feature) -> Optional[tuple]:
    """What tells a film (or an episode) from another by OpenSubtitles' ids; None without them."""
    if feature.tmdb_id is None and feature.imdb_id is None:
        return None
    return (feature.tmdb_id, feature.imdb_id, feature.season, feature.episode)


def found_for(files: Iterable[SubtitleFile]) -> str:
    """What most of `files` were made for ("Harbour Lights (2019)"; "" when none says)."""
    labels = Counter(file.feature.label() for file in files if file.feature is not None)
    labels.pop("", None)
    return labels.most_common(1)[0][0] if labels else ""


# ---- languages and ranking ----------------------------------------------------------------

_UNRANKED = frozenset({"", "anonymous", "sub leecher", "new member"})
# OpenSubtitles' own codes that are no ISO language: their names on the OpenSubtitles page
# (where, in a language other than English, the other languages show in their own names:
# "Deutsch", "Español").
_SPECIAL_LANGUAGES = {
    "ea": N_("Spanish (Latin America)"),  # i18n: a subtitle language
    "ze": N_("Chinese (bilingual)"),  # i18n: a subtitle language
    "zh-ca": N_("Chinese (Cantonese)"),  # i18n: a subtitle language
    "pm": N_("Portuguese (Mozambique)"),  # i18n: a subtitle language
    "me": N_("Montenegrin"),  # i18n: a subtitle language
    "ma": N_("Manipuri"),  # i18n: a subtitle language
    "sy": N_("Syriac"),  # i18n: a subtitle language
    "und": N_("Unknown language"),  # i18n: a subtitle's language, when it has none
}


def language_name(code: str) -> str:
    """A language's name: in English "English", "Portuguese (Brazil)"; in another of the
    app's languages the language's own name, "Deutsch", "Português (Brasil)" (Qt names a
    language in English or in itself only)."""
    key = code.strip().lower()
    if key in _SPECIAL_LANGUAGES:
        return translate(_SPECIAL_LANGUAGES[key])
    locale = QLocale(code.replace("-", "_"))
    if locale.language() in (QLocale.Language.C, QLocale.Language.AnyLanguage):
        return code.upper() or translate(_SPECIAL_LANGUAGES["und"])
    if i18n.language() != i18n.SOURCE_LANGUAGE:
        own = _own_name(locale, with_territory="-" in code)
        if own:
            return own
    name = QLocale.languageToString(locale.language())
    if "-" in code:
        territory = QLocale.territoryToString(locale.territory())
        if territory and locale.territory() != QLocale.Country.AnyTerritory:
            name = f"{name} ({territory})"
    return name


def _own_name(locale: QLocale, *, with_territory: bool) -> str:
    """The language's name in itself ("Português (Brasil)"); "" when Qt has none.

    Qt names some languages after a region ("American English" for en_US): without a
    region, the shorter of its names and its name for the whole world ("English"); with one,
    the region's name follows, unless the language's own name says it already ("português
    europeu", "繁體中文")."""
    own = str(locale.nativeLanguageName() or "")
    if not own:
        return ""
    plain = str(QLocale(locale.language()).nativeLanguageName() or "")  # its first region's
    if not with_territory:
        world = QLocale(locale.language(), QLocale.Country.World)
        if world.territory() == QLocale.Country.World and world.nativeLanguageName():
            own = min((own, str(world.nativeLanguageName())), key=len)
    elif own == plain and locale.territory() != QLocale.Country.AnyTerritory:
        territory = str(locale.nativeTerritoryName() or "")
        if territory:
            own = f"{own} ({territory})"
    return str(locale.toUpper(own[:1])) + own[1:]


def system_language(locale: Optional[QLocale] = None) -> str:
    """This computer's language as OpenSubtitles writes it: "en", "pt-BR", "zh-TW"."""
    name = (locale or QLocale.system()).name()  # "en_US", "pt_BR", "zh_CN"
    language, _sep, region = name.partition("_")
    language = language.lower()
    if language == "pt":
        return "pt-BR" if region == "BR" else "pt-PT"
    if language == "zh":
        return "zh-TW" if region in ("TW", "HK", "MO") else "zh-CN"
    return language if language and language != "c" else "en"


def group_languages(
    files: Iterable[SubtitleFile], preferred: Sequence[str] = ()
) -> list[tuple[str, int]]:
    """(language, how many) of `files`: the `preferred` ones first (in that order, when found),
    then the most subtitles first."""
    counts: dict[str, int] = {}
    for file in files:
        counts[file.language] = counts.get(file.language, 0) + 1
    first = [code for code in dict.fromkeys(preferred) if code in counts]
    rest = sorted((c for c in counts if c not in first), key=lambda c: (-counts[c], c))
    return [(code, counts[code]) for code in first + rest]


def uploader_trust(file: SubtitleFile) -> int:
    """2: a trusted uploader; 1: a ranked one; 0: anonymous or new."""
    if file.trusted:
        return 2
    return 0 if file.uploader_rank.strip().lower() in _UNRANKED else 1


def ranked(files: Iterable[SubtitleFile], *, prefer_hi: bool = False) -> list[SubtitleFile]:
    """Best first: machine translations last; hearing-impaired after the others (before them
    with `prefer_hi`); then trusted and ranked uploaders; then the most downloaded; else the
    API's order."""

    def key(file: SubtitleFile) -> tuple:
        hi_last = file.hearing_impaired != prefer_hi
        return (
            file.machine_translated,
            hi_last,
            -uploader_trust(file),
            -file.downloads,
            file.order,
        )

    return sorted(files, key=key)


# ---- what to search for -------------------------------------------------------------------

_VIDEO_EXTENSION = re.compile(
    r"\.(mkv|mp4|m4v|avi|mov|ts|m2ts|webm|wmv|flv|mpe?g|ogv)$", re.IGNORECASE
)
# The country and language codes providers put before or after a title ("TR: ", "TR - ",
# "|EN| ", "[DE] ", "FR| ", " - TR"): only these are taken for a provider's tag, and only in
# those forms.
_CODES = frozenset("""
    AE AF AL AM AR AT AU AZ BA BD BE BG BR BY CA CH CL CN CO CS CY CZ DA DE DK DZ EE EG EL EN ES
    EU FA FI FR GE GR HE HI HK HR HU ID IE IL IN IQ IR IS IT JA JO JP KO KR KU KW KZ LA LB LT LU
    LV MA MK MT MX MY NB NG NL NO NZ PE PH PK PL PT QA RO RS RU SA SE SG SI SK SL SQ SR SV SY TH
    TN TR TW UA UK UR US UZ VE VI VN ZA ZH
    AFG ALB ARA ARB AZE BIH BOS BUL CHI CHN CRO CZE DAN DEU DUT ENG ESP EXYU FIN FRA FRE GER GRE
    HEB HIN HRV HUN IND ITA JPN KOR KUR LAT LATAM MKD NLD NOR PAK PER POL POR ROM RUM RUS SLO SPA
    SRB SRP SWE THA TUR UKR VIE
    """.split())
# A provider's tag before a title: a code in brackets ("|EN| ", "[DE] ", "(TR) "), or in
# capitals followed by a separator ("TR: ", "FR| ", "TR ▸ ", "TR - "; a dash with a space
# beside it, never a word's hyphen: "DE-FROST" stays). A title's own words are rarely all in
# capitals: "It - Harbour Two" stays.
_TAG_SEPARATORS = "|:▸►▶»•·~"
_PREFIX = re.compile(
    r"^\s*(?:"
    r"\|\s*([A-Za-z]{2,5})\s*\|"  # |EN|
    r"|\[\s*([A-Za-z]{2,5})\s*\]"  # [DE]
    r"|\(\s*([A-Z]{2,5})\s*\)"  # (TR)
    r"|\{\s*([A-Z]{2,5})\s*\}"  # {TR}
    rf"|([A-Z]{{2,5}})\s*[{_TAG_SEPARATORS}]"  # FR|  TR:  TR ▸
    r"|([A-Z]{2,5})(?:\s+[-–—]|[-–—]\s)"  # TR -  TR –  TR—
    rf")(?:\s*[-–—{_TAG_SEPARATORS}](?=\s))?\s*"  # "[TR] - "
)
# A provider's tag after a title: " - TR", " | TR", " ▸ TR".
_SUFFIX = re.compile(rf"(?:\s+[-–—]|\s*[{_TAG_SEPARATORS}])\s*([A-Z]{{2,5}})\s*$")
_YEAR_IN_BRACKETS = re.compile(r"[(\[]\s*((?:19|20)\d{2})\s*[)\]]")
_BRACKETED = re.compile(r"\[([^\]]*)\]|\{([^}]*)\}|\|([^|]*)\||\(([^)]*)\)")
_QUALITY = re.compile(
    r"(?<![\w+])(?:4K|8K|UHD|FHD|QHD|HD|SD|HDR10\+?|HDR|DV|HEVC|H\.?26[45]|[xX]\.?26[45]"
    r"|(?:2160|1080|720|576|480)[pPi]|[56]0\s?FPS|MULTI[\- ]?SUBS?|MULTI|DUAL|ATMOS|WEB[\- ]?DL"
    r"|WEB[\- ]?Rip|WEBRIP|Blu[\- ]?Ray|BLURAY|BDRip|BRRip|DVDRip|HDRip|HDTV|AAC|AC3|DTS"
    r"|DDP?5\.1|10bit|REMUX|PROPER|REPACK|EXTENDED|VOSTFR|SUBBED|DUBBED)(?![\w+])"
)
_EPISODE = re.compile(r"\bS(\d{1,2})\s*[\.\-]?\s*E(\d{1,3})\b", re.IGNORECASE)
_EPISODE_X = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")
_SEASON_ONLY = re.compile(r"\b(?:S|Season\s*)(\d{1,2})\s*$", re.IGNORECASE)
_DASH_YEAR = re.compile(r"\s[-–—]\s*((?:19|20)\d{2})\s*$")
_FILE_YEAR = re.compile(r"(?<=\s)((?:19|20)\d{2})(?=\s|$)")
_EDGES = " \t-–—|:·,._/\\▸►▶»•~"


def _plausible_year(value: Any) -> Optional[int]:
    year = _int(str(value).strip()[:4]) if value not in (None, "") else None
    if year is None or not 1900 <= year <= _dt.date.today().year + 1:
        return None
    return year


def _a_title(text: str) -> bool:
    """Whether `text` is enough of a title to be left: two words, or one of four letters."""
    return len(text.split()) >= 2 or len(text) >= 4


def _strip_tags(text: str) -> str:
    """Strip a provider's tags before the title ("TR: ", "TR - ", "|EN| ", "[DE] ", "FR| "):
    only a known country or language code, and only while a title of two words, or of one
    word of four letters or more, remains."""
    while True:
        match = _PREFIX.match(text)
        if match is None:
            return text
        code = next(group for group in match.groups() if group)
        rest = text[match.end() :].strip()
        if code.upper() not in _CODES or not _a_title(rest):
            return text
        text = rest


def _strip_tag_after(text: str) -> str:
    """Strip a provider's tags after the title (" - TR", " | EN"), alike."""
    while True:
        match = _SUFFIX.search(text)
        if match is None or match.group(1) not in _CODES:
            return text
        rest = text[: match.start()].strip(_EDGES)
        if not _a_title(rest):
            return text
        text = rest


def _drop_bracketed(match: re.Match[str]) -> str:
    """A bracketed tag goes when it is a code or a quality mark ("[MULTI-SUB]", "(US)",
    "|4K|"); anything else in brackets is the title's own ("[REC]", "(Director's Cut)")."""
    inner = next((group for group in match.groups() if group is not None), "").strip()
    if inner.upper() in _CODES or (inner and not _QUALITY.sub("", inner).strip(_EDGES)):
        return " "
    return match.group(0)


def clean_title(name: str) -> tuple[str, Optional[int]]:
    """A provider's or a file's title without its tags and quality marks, and the year it
    names, if any: "TR: The Film (2019) 4K" → ("The Film", 2019), and
    "The.Film.2019.1080p.WEB-DL.x264.mkv" → ("The Film", 2019). Conservative: what may be the
    title's own stays (the search field can be edited)."""
    original = " ".join(str(name or "").split())
    text = original
    file_like = bool(_VIDEO_EXTENSION.search(text))
    text = _VIDEO_EXTENSION.sub("", text)
    if file_like or (" " not in text and re.search(r"[._]", text)):
        text = re.sub(r"[._]+", " ", text)  # Some.Film.2019.1080p → Some Film 2019 1080p
        file_like = True
    text = _strip_tags(text)
    year = None
    match = _YEAR_IN_BRACKETS.search(text)
    if match and text[: match.start()].strip(_EDGES):
        year = int(match.group(1))
        # In a file's name what follows the year is the release's ("1080p WEB-DL x264-GROUP").
        text = text[: match.start()] + ("" if file_like else " " + text[match.end() :])
    if file_like:
        if year is None:
            for found in _FILE_YEAR.finditer(text):
                if _plausible_year(found.group(1)) and text[: found.start()].strip(_EDGES):
                    year, text = int(found.group(1)), text[: found.start()]
                    break
        quality = _QUALITY.search(text)
        if quality and text[: quality.start()].strip(_EDGES):
            text = text[: quality.start()]
    text = _BRACKETED.sub(_drop_bracketed, text)
    cleaned = " ".join(_QUALITY.sub(" ", text).split()).strip(_EDGES)
    if cleaned:
        text = _strip_tags(cleaned)
    text = _strip_tag_after(" ".join(text.split()).strip(_EDGES))
    if year is None:
        trailing = _DASH_YEAR.search(text)
        if trailing and _plausible_year(trailing.group(1)) and text[: trailing.start()].strip():
            year, text = int(trailing.group(1)), text[: trailing.start()]
            text = _strip_tag_after(text.strip(_EDGES))  # "Title - TR - 2019"
    return " ".join(text.split()).strip(_EDGES) or original, year


def item_year(item: dict) -> Optional[int]:
    """A film's year as its provider gives it, if it does."""
    for key in ("year", "releaseDate", "release_date", "releasedate", "released"):
        year = _plausible_year(item.get(key))
        if year:
            return year
    return None


# Where a provider names a title's ids and original title: on its item (an Xtream panel's
# "tmdb"), or in the item's info (an Xtream episode's "tmdb_id", kept as its "description").
_TMDB_KEYS = ("tmdb_id", "tmdb", "tmdbid")
_IMDB_KEYS = ("imdb_id", "imdb", "imdbid")
_ORIGINAL_KEYS = ("o_name", "original_title", "original_name")
_IMDB_ID = re.compile(r"\btt0*(\d{1,10})\b|^0*(\d{5,10})$", re.IGNORECASE)  # 5+ digits: no rating


def _sources(item: Any) -> list[dict]:
    if not isinstance(item, dict):
        return []
    return [item] + [
        item[key] for key in ("info", "description") if isinstance(item.get(key), dict)
    ]


def _tmdb_id(value: Any) -> Optional[int]:
    text = str(value).strip() if value is not None and not isinstance(value, bool) else ""
    return _positive(text) if re.fullmatch(r"\d{1,10}", text) else None


def _imdb_id(value: Any) -> Optional[int]:
    text = str(value).strip() if value is not None and not isinstance(value, bool) else ""
    match = _IMDB_ID.search(text)
    return _positive(match.group(1) or match.group(2)) if match else None


def provider_ids(item: Any) -> tuple[Optional[int], Optional[int]]:
    """(its TMDB id, its IMDb id without "tt") as a title's provider gives them; None each
    when it gives none."""
    tmdb = imdb = None
    for source in _sources(item):
        for key in _TMDB_KEYS:
            tmdb = tmdb or _tmdb_id(source.get(key))
        for key in _IMDB_KEYS:
            imdb = imdb or _imdb_id(source.get(key))
    return tmdb, imdb


def original_title(item: Any) -> str:
    """A title's original name as its provider gives it, when in Latin letters ("" else):
    OpenSubtitles knows a film by it, or by its English one, not by a translation's."""
    for source in _sources(item):
        for key in _ORIGINAL_KEYS:
            name = " ".join(str(source.get(key) or "").split())
            letters = [c for c in name if c.isalpha()]
            if letters and all(ord(c) < 0x250 for c in letters):
                return name
    return ""


def _episode_in(text: str) -> tuple[str, Optional[int], Optional[int]]:
    """ "Series S01E02 Title" → ("Series", 1, 2); the text whole when it names no episode."""
    match = _EPISODE.search(text) or _EPISODE_X.search(text)
    if not match:
        return text, None, None
    return text[: match.start()], int(match.group(1)), int(match.group(2))


def search_query(
    section: str,
    title: str,
    *,
    series_title: str = "",
    item: Optional[dict] = None,
    context: Optional[dict] = None,
) -> SearchQuery:
    """What to search for what plays: a film's title (and year), or an episode's series title
    with its season and episode numbers, and the ids the provider gives (a film's or an
    episode's own, an episode's series'). `section` is "movies" or "series"; `title` what the
    player shows; `series_title` an episode's series; `item` and `context` the catalog's. An
    original title in Latin letters is searched for rather than a translated one. A film's
    title that names an episode ("Series S01E02", a file's name) is searched as one."""
    item, context = item or {}, context or {}
    if section != "series":
        text, year = clean_title(title)
        named, season, episode = _episode_in(text)
        if season is not None and named.strip(_EDGES):
            return SearchQuery(named.strip(_EDGES), None, season, episode)
        original, original_year = clean_title(original_title(item))
        if original:
            text, year = original, year or original_year
        tmdb, imdb = provider_ids(item)
        return SearchQuery(
            text or str(title).strip(), year or item_year(item), tmdb_id=tmdb, imdb_id=imdb
        )
    season_dict = context.get("season") if isinstance(context.get("season"), dict) else None
    series_dict = context.get("series") if isinstance(context.get("series"), dict) else None
    season_value, episode_value = season_and_episode(season_dict, item)
    season, episode = _int(season_value), _int(episode_value)
    series = series_title
    if not series and series_dict is not None:
        series = str(series_dict.get("name") or series_dict.get("title") or "")
    named, found_season, found_episode = _episode_in(str(series or title))
    if found_season is None and series:
        _rest, found_season, found_episode = _episode_in(str(title))
    if season is None:
        season = found_season
    if episode is None:
        episode = found_episode
    text, _year = clean_title(original_title(series_dict) or named)
    by_season = _SEASON_ONLY.search(text)
    if by_season and text[: by_season.start()].strip(_EDGES):  # "Series Season 2"
        season = season if season is not None else int(by_season.group(1))
        text = text[: by_season.start()].strip(_EDGES)
    tmdb, imdb = provider_ids(item)
    parent_tmdb, parent_imdb = provider_ids(series_dict)
    if season_dict is not None:  # the series' id its info gave (services.workers)
        parent_tmdb = parent_tmdb or _tmdb_id(season_dict.get("series_tmdb"))
    # A provider that gives an episode its series' id: it is the series'. An episode's own id
    # without its series or numbers could be taken for a film's: its text is searched for.
    known = season is not None or episode is not None or parent_tmdb or parent_imdb
    tmdb = None if tmdb == parent_tmdb or not known else tmdb
    imdb = None if imdb == parent_imdb or not known else imdb
    return SearchQuery(
        text or str(title).strip(),
        None,
        season,
        episode,
        tmdb_id=tmdb,
        imdb_id=imdb,
        parent_tmdb_id=parent_tmdb,
        parent_imdb_id=parent_imdb,
    )


def parse_query(text: str) -> SearchQuery:
    """The search field read back: "Series S01E02", "Title (2019)" or just a title."""
    words = " ".join(str(text or "").split())
    year = None
    match = _YEAR_IN_BRACKETS.search(words)
    if match:
        year = int(match.group(1))
        words = words[: match.start()] + words[match.end() :]
    named, season, episode = _episode_in(words)
    if season is None:
        found = _SEASON_ONLY.search(words)
        if found:
            named, season = words[: found.start()], int(found.group(1))
    return SearchQuery(" ".join(named.split()).strip(_EDGES), year, season, episode)


# ---- the client ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    username: str = ""
    password: str = field(default="", repr=False)

    @property
    def signs_in(self) -> bool:
        return bool(self.username.strip() and self.password)


def _reset_text(value: Any) -> str:
    """ "08 hours and 38 minutes" → "8 hours and 38 minutes"."""
    text = " ".join(str(value or "").split())
    return re.sub(r"\b0(\d)\b", r"\1", text)


_RESET = re.compile(r"(\d+) hours? and (\d+) minutes?|(\d+) hours?|(\d+) minutes?")


def shown_reset(reset: str) -> str:
    """When today's downloads come back, as OpenSubtitles says it ("8 hours and 38 minutes"),
    in the app's language; as it is in English, or when it says it some other way."""
    match = _RESET.fullmatch(reset)
    if i18n.language() == i18n.SOURCE_LANGUAGE or match is None:
        return reset
    hours, minutes, hours_only, minutes_only = match.groups()
    if hours is not None:
        return tr(
            "{hours} and {minutes}",  # i18n: a time span: "8 hours and 38 minutes"
            hours=tr_n("{n} hour", "{n} hours", int(hours)),
            minutes=tr_n("{n} minute", "{n} minutes", int(minutes)),
        )
    if hours_only is not None:
        return tr_n("{n} hour", "{n} hours", int(hours_only))
    return tr_n("{n} minute", "{n} minutes", int(minutes_only))


def quota_text(reset: str, *, signed_in: bool) -> str:
    """Today's downloads are used, in plain words."""
    time = shown_reset(reset)
    if signed_in:
        if reset:
            return tr(
                "You've used today's downloads on your OpenSubtitles account. "
                "They come back in {time}.",  # i18n: {time}: a time span, "8 hours and 38 minutes"
                time=time,
            )
        return tr(
            "You've used today's downloads on your OpenSubtitles account. "
            "They come back within a day."
        )
    if reset:
        return tr_n(
            "You've used today's downloads: without signing in, OpenSubtitles allows {n} a day "
            "per connection. They come back in {time}. Signing in to OpenSubtitles in "
            "Settings → Subtitles raises the limit.",
            "You've used today's downloads: without signing in, OpenSubtitles allows {n} a day "
            "per connection. They come back in {time}. Signing in to OpenSubtitles in "
            "Settings → Subtitles raises the limit.",
            ANONYMOUS_DAILY,
            time=time,
        )  # i18n: {n}: downloads; {time}: a time span, "8 hours and 38 minutes"
    return tr_n(
        "You've used today's downloads: without signing in, OpenSubtitles allows {n} a day "
        "per connection. They come back within a day. Signing in to OpenSubtitles in "
        "Settings → Subtitles raises the limit.",
        "You've used today's downloads: without signing in, OpenSubtitles allows {n} a day "
        "per connection. They come back within a day. Signing in to OpenSubtitles in "
        "Settings → Subtitles raises the limit.",
        ANONYMOUS_DAILY,
    )  # i18n: {n}: downloads


def busy_text(seconds: Optional[int]) -> str:
    if seconds and seconds > 1:
        return tr_n(
            "OpenSubtitles is getting too many requests. Try again in {n} second.",
            "OpenSubtitles is getting too many requests. Try again in {n} seconds.",
            seconds,
        )
    return tr("OpenSubtitles is getting too many requests. Try again in a moment.")


def _retry_seconds(response: Any) -> Optional[int]:
    headers = getattr(response, "headers", None) or {}
    for name in ("Retry-After", "retry-after", "RateLimit-Reset", "ratelimit-reset"):
        value = _int(headers.get(name)) if hasattr(headers, "get") else None
        if value is not None and value >= 0:
            return value
    return None


def _close(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _refuses_the_key(response: Any) -> bool:
    """Whether a refused sign-in is about the API key rather than the username and password
    (OpenSubtitles says which in its message)."""
    data = _json(response)
    message = str(data.get("message") or "") if isinstance(data, dict) else ""
    words = message.lower()
    return "api key" in words or "api-key" in words or "apikey" in words or "consume" in words


def _json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


class OpenSubtitlesClient:
    """OpenSubtitles' REST API for one key (and sign-in). Safe to share between jobs: the
    sign-in's token is kept under a lock. `http` stands in for `requests` (tests)."""

    def __init__(self, credentials: Credentials, *, http: Any = None) -> None:
        self.credentials = credentials
        self._http = http
        self._lock = threading.Lock()  # the token and its host
        self._login_lock = threading.Lock()  # one sign-in at a time
        self._token: Optional[str] = None
        self._base = API_URL

    # -- the sign-in -----------------------------------------------------------------------

    def signed_in(self) -> bool:
        with self._lock:
            return self._token is not None

    def login(self) -> Optional[str]:
        """Sign in with the credentials' username and password (None without them)."""
        credentials = self.credentials
        if not credentials.signs_in:
            return None
        response = self._call(
            "POST",
            f"{API_URL}/login",
            json={"username": credentials.username.strip(), "password": credentials.password},
        )
        if response.status_code in (400, 401, 403):
            logger.info("OpenSubtitles refused the sign-in (%s)", response.status_code)
            if _refuses_the_key(response):
                raise OpenSubtitlesError(BAD_KEY, translate(BAD_KEY_TEXT))
            raise OpenSubtitlesError(BAD_LOGIN, translate(BAD_LOGIN_TEXT))
        data = self._checked(response, signed_in=False)
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise OpenSubtitlesError(SERVER, translate(UNREADABLE_TEXT))
        base = self._base_from(data.get("base_url"))
        with self._lock:
            self._token, self._base = token, base
        return token

    def logout(self) -> None:
        """End the sign-in (OpenSubtitles forgets the token; so does this client)."""
        with self._lock:
            token, base = self._token, self._base
            self._token, self._base = None, API_URL
        if token is None:
            return
        try:
            self._call("DELETE", f"{base}/logout", token=token)
        except OpenSubtitlesError:
            logger.info("Couldn't sign out of OpenSubtitles; the sign-in ends by itself")

    @staticmethod
    def _base_from(value: Any) -> str:
        """The host a signed-in user's requests go to (OpenSubtitles' own only)."""
        host = str(value or "").strip()
        if "//" in host:
            host = urlsplit(host).hostname or ""
        host = host.strip("/").lower()
        if host == "opensubtitles.com" or host.endswith(".opensubtitles.com"):
            return f"https://{host}/api/v1"
        return API_URL

    def _token_now(self) -> Optional[str]:
        """The sign-in's token, signing in first when there is none (one sign-in at a time:
        a job that waited for another's uses its token)."""
        with self._lock:
            token = self._token
        if token is not None or not self.credentials.signs_in:
            return token
        with self._login_lock:
            with self._lock:
                token = self._token
            return token if token is not None else self.login()

    # -- searching and downloading -----------------------------------------------------------

    def search(
        self, query: SearchQuery, *, preferred: Sequence[str] = (), pages: int = MAX_PAGES
    ) -> list[SubtitleFile]:
        """Every language's subtitles for `query` (up to `pages` pages); when there are more,
        the `preferred` languages' first page too."""
        if not query.text and not query.by_id:
            return []
        found: list[SubtitleFile] = []
        seen: set[int] = set()

        def take(data: Any) -> None:
            for file in parse_search(data, start=len(found)):
                if file.file_id not in seen:
                    seen.add(file.file_id)
                    found.append(file)

        total_pages = 1
        for page in range(1, pages + 1):
            if page > 1 and cancel_requested():
                return found  # QiTV is closing
            params = query.params() + ([("page", str(page))] if page > 1 else [])
            try:
                data = self._get("/subtitles", sorted(params))
            except OpenSubtitlesError:
                if page == 1:
                    raise
                break  # what the first pages found is shown
            take(data)
            total_pages = _int(data.get("total_pages") if isinstance(data, dict) else 1) or 1
            if page >= total_pages:
                break
        if total_pages > pages and preferred and not cancel_requested():
            try:
                take(self._get("/subtitles", query.params(preferred)))
            except OpenSubtitlesError:
                pass
        return found

    def download(self, file_id: int) -> tuple[bytes, dict]:
        """The file's bytes and OpenSubtitles' answer (its name, the downloads left today)."""
        token = self._token_now()
        response = self._call("POST", "/download", json={"file_id": int(file_id)}, token=token)
        if response.status_code == 401 and token is not None:
            with self._lock:
                if self._token == token:
                    self._token = None  # expired: sign in again, once
            token = self._token_now()
            response = self._call("POST", "/download", json={"file_id": int(file_id)}, token=token)
        if response.status_code == 404:
            raise OpenSubtitlesError(GONE, translate(GONE_TEXT))
        if response.status_code in (401, 403) and token is not None:
            # Refused again with a fresh sign-in: the sign-in, not the key (anonymous
            # downloads take the key alone).
            raise OpenSubtitlesError(BAD_LOGIN, translate(SIGN_IN_AGAIN_TEXT))
        info = self._checked(response, signed_in=token is not None)
        link = info.get("link") if isinstance(info, dict) else None
        if not isinstance(link, str) or not link.startswith("https://"):
            raise OpenSubtitlesError(SERVER, translate(UNREADABLE_TEXT))
        return self._fetch(link), info

    def _fetch(self, link: str) -> bytes:
        """The file at a download's temporary link: each redirect https too, the body read
        in pieces and refused beyond MAX_FILE_BYTES."""
        url = link
        for _hop in range(MAX_REDIRECTS + 1):
            if not url.startswith("https://"):
                logger.info("A subtitle file's link led away from https")
                raise OpenSubtitlesError(SERVER, translate(UNREADABLE_TEXT))
            try:
                file = self._http_module().request(
                    "GET",
                    url,
                    headers={"User-Agent": user_agent()},
                    timeout=TIMEOUT,
                    allow_redirects=False,
                    stream=True,
                )
            except (requests.RequestException, OSError) as e:  # never the link: it is a key
                logger.info("Couldn't fetch the subtitle file: %s", safe_exc(e))
                raise OpenSubtitlesError(NETWORK, translate(NETWORK_TEXT)) from None
            if file.status_code in _REDIRECTS:
                location = str((getattr(file, "headers", None) or {}).get("Location") or "")
                _close(file)
                url = urljoin(url, location) if location else ""
                continue
            try:
                return self._body(file)
            finally:
                _close(file)
        logger.info("A subtitle file's link redirected too often")
        raise OpenSubtitlesError(SERVER, translate(UNREADABLE_TEXT))

    @staticmethod
    def _body(file: Any) -> bytes:
        if file.status_code in (404, 410):
            raise OpenSubtitlesError(GONE, translate(GONE_TEXT))
        if not 200 <= file.status_code < 300:
            logger.info("The subtitle file's link answered %s", file.status_code)
            raise OpenSubtitlesError(SERVER, translate(SERVER_TEXT))
        content = bytearray()
        try:
            for chunk in file.iter_content(chunk_size=_CHUNK):
                content += chunk
                if len(content) > MAX_FILE_BYTES:
                    logger.info("A subtitle file was larger than %s bytes", MAX_FILE_BYTES)
                    raise OpenSubtitlesError(SERVER, translate(UNREADABLE_TEXT))
        except (requests.RequestException, OSError) as e:
            logger.info("The subtitle file stopped coming: %s", safe_exc(e))
            raise OpenSubtitlesError(NETWORK, translate(NETWORK_TEXT)) from None
        if not content:
            raise OpenSubtitlesError(SERVER, translate(UNREADABLE_TEXT))
        return bytes(content)

    # -- HTTP --------------------------------------------------------------------------------

    def _http_module(self) -> Any:
        return self._http if self._http is not None else requests

    def _get(self, path: str, params: list[tuple[str, str]]) -> Any:
        return self._checked(self._call("GET", path, params=params), signed_in=False)

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Optional[list[tuple[str, str]]] = None,
        json: Optional[dict] = None,
        token: Optional[str] = None,
    ) -> Any:
        if not self.credentials.api_key:
            raise OpenSubtitlesError(NO_KEY, translate(NO_KEY_TEXT))
        if path.startswith("/"):
            with self._lock:
                base = self._base if token is not None else API_URL
            url = base + path
        else:
            url = path
        headers = {
            "Api-Key": self.credentials.api_key,
            "User-Agent": user_agent(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return self._http_module().request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=TIMEOUT,
                allow_redirects=True,
            )
        except (requests.RequestException, OSError) as e:  # the type and host only: the
            logger.info("OpenSubtitles could not be reached: %s", safe_exc(e))  # URL may tell
            raise OpenSubtitlesError(NETWORK, translate(NETWORK_TEXT)) from None

    def _checked(self, response: Any, *, signed_in: bool) -> Any:
        """The answer's JSON, or the problem its status says."""
        status = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status < 300:
            data = _json(response)
            if data is None:
                raise OpenSubtitlesError(SERVER, translate(UNREADABLE_TEXT))
            return data
        logger.info("OpenSubtitles answered %s", status)
        if status in (401, 403):
            if signed_in:  # with a sign-in's token: the sign-in ended
                raise OpenSubtitlesError(BAD_LOGIN, translate(SIGN_IN_AGAIN_TEXT))
            raise OpenSubtitlesError(BAD_KEY, translate(BAD_KEY_TEXT))
        if status == 406:
            data = _json(response)
            reset = _reset_text(data.get("reset_time")) if isinstance(data, dict) else ""
            raise OpenSubtitlesError(QUOTA, quota_text(reset, signed_in=signed_in))
        if status == 429:
            raise OpenSubtitlesError(BUSY, busy_text(_retry_seconds(response)))
        raise OpenSubtitlesError(SERVER, translate(SERVER_TEXT))


# ---- saving -------------------------------------------------------------------------------

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"{name}{n}" for name in ("COM", "LPT") for n in range(1, 10)]
)


def safe_file_name(name: str, fallback: str) -> str:
    """A file name safe on every system, with a subtitle extension (.srt unless it has one)."""
    clean = _UNSAFE.sub(" ", os.path.basename(str(name or "").replace("\\", "/")))
    clean = " ".join(clean.split()).strip(" .")[:120].strip(" .")
    if not clean:
        clean = fallback
    if clean.split(".")[0].strip().upper() in _RESERVED:  # Windows' device names
        clean = "_" + clean
    if os.path.splitext(clean)[1].lower() not in SUBTITLE_EXTENSIONS:
        clean += ".srt"
    return clean


def subtitle_path(folder: str, file_id: int, name: str) -> str:
    """Where a downloaded file is kept: a folder of its own per OpenSubtitles file, so the
    file keeps its own name (the Subtitles panel shows it)."""
    return os.path.join(folder, str(int(file_id)), safe_file_name(name, f"{int(file_id)}.srt"))


def save_subtitle(path: str, content: bytes) -> None:
    """Write `content` at `path` whole (a temporary file swapped in)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".download-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


# ---- the jobs -----------------------------------------------------------------------------


class SearchWorker(QObject):
    """Searches for `query`: by its id, then by its text when the id finds nothing for it.
    Emits the subtitles for the title asked for and, apart, the others found."""

    finished = Signal(object)  # SearchResult or Problem
    error = Signal(str)

    def __init__(
        self, client: OpenSubtitlesClient, query: SearchQuery, preferred: Sequence[str] = ()
    ) -> None:
        super().__init__()
        self._client, self._query, self._preferred = client, query, tuple(preferred)

    @Slot()
    def run(self) -> None:
        query = self._query
        try:
            files = self._client.search(query, preferred=self._preferred)
            matching, others = split_matches(files, query)
            if not matching and query.by_id and query.text and not cancel_requested():
                logger.info("OpenSubtitles found nothing by the id; searching by the title")
                query = query.without_ids()
                files = self._client.search(query, preferred=self._preferred)
                matching, others = split_matches(files, query)
        except OpenSubtitlesError as e:
            self.finished.emit(e.problem)
            return
        self.finished.emit(SearchResult(query, tuple(matching), tuple(others)))


class DownloadWorker(QObject):
    """Fetches `file` into `folder`; a file already saved there (`reuse`) is not fetched."""

    finished = Signal(object)  # Downloaded or Problem
    error = Signal(str)

    def __init__(
        self,
        client: Optional[OpenSubtitlesClient],
        file: SubtitleFile,
        folder: str,
        *,
        saved: str = "",
    ) -> None:
        super().__init__()
        self._client, self._file, self._folder, self._saved = client, file, folder, saved

    @Slot()
    def run(self) -> None:
        file = self._file
        if self._saved and os.path.isfile(self._saved):
            name = os.path.basename(self._saved)
            self.finished.emit(
                Downloaded(self._saved, name, file.file_id, file.language, reused=True)
            )
            return
        if self._client is None:
            self.finished.emit(Problem(NO_KEY, translate(NO_KEY_TEXT)))
            return
        try:
            content, info = self._client.download(file.file_id)
        except OpenSubtitlesError as e:
            self.finished.emit(e.problem)
            return
        name = str(info.get("file_name") or file.file_name or file.release)
        path = subtitle_path(self._folder, file.file_id, name)
        try:
            save_subtitle(path, content)
        except OSError as e:
            logger.warning("Couldn't save the subtitles: %s", safe_message(e))
            self.finished.emit(Problem(DISK, translate(DISK_TEXT)))
            return
        remaining = _int(info.get("remaining"))
        self.finished.emit(
            Downloaded(
                path,
                os.path.basename(path),
                file.file_id,
                file.language,
                remaining=remaining if remaining is not None and remaining >= 0 else None,
                reset=_reset_text(info.get("reset_time")),
            )
        )


class LogoutWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, client: OpenSubtitlesClient) -> None:
        super().__init__()
        self._client = client

    @Slot()
    def run(self) -> None:
        self._client.logout()
        self.finished.emit(None)
