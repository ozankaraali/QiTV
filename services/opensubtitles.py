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

What plays is searched by its title: `search_query()` cleans a provider's title ("TR: ",
"|EN| ", "4K", "1080p" …) and adds a film's year or an episode's season and number; the
search field shows it as "Title (2019)" or "Series S01E02", and `parse_query()` reads an
edited one back. Results are grouped by language (`group_languages`) and ranked within one
(`ranked`): machine translations last, hearing-impaired after the others unless the viewer
picked those before, trusted and ranked uploaders first, then the most downloaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as _dt
import logging
import os
import re
import tempfile
import threading
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from PySide6.QtCore import QLocale, QObject, Signal, Slot

from services import _build_keys
from services.config_manager import get_app_version
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

NO_KEY_TEXT = "OpenSubtitles needs an API key."
BAD_KEY_TEXT = "OpenSubtitles didn't accept the API key. Check it, or paste a new one."
BAD_LOGIN_TEXT = (
    "OpenSubtitles didn't accept your username or password. Check them in Settings → Subtitles."
)
SIGN_IN_AGAIN_TEXT = (
    "OpenSubtitles ended your sign-in. Sign in to OpenSubtitles again in Settings → Subtitles."
)
NETWORK_TEXT = "Couldn't reach OpenSubtitles. Check your internet connection and try again."
SERVER_TEXT = "OpenSubtitles is having trouble right now. Try again in a while."
UNREADABLE_TEXT = "OpenSubtitles sent an answer QiTV couldn't read. Try again in a while."
GONE_TEXT = "That subtitle file is no longer on OpenSubtitles. Pick another one."
DISK_TEXT = "Couldn't save the subtitles on this computer."


def built_in_key() -> str:
    """The key a release build carries ("" in a build from the source code)."""
    return str(getattr(_build_keys, "OPENSUBTITLES_API_KEY", "") or "").strip()


def api_key(user_key: str) -> str:
    """The key to use: the user's own when set, else the build's ("": none)."""
    return str(user_key or "").strip() or built_in_key()


def user_agent() -> str:
    return f"QiTV v{get_app_version()}"


# ---- what was found -----------------------------------------------------------------------


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


@dataclass(frozen=True)
class SearchQuery:
    text: str  # the title, cleaned
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None

    def label(self) -> str:
        """As the search field shows it: "Title (2019)", "Series S01E02"."""
        text = self.text
        if self.season is not None and self.episode is not None:
            return f"{text} S{self.season:02d}E{self.episode:02d}"
        if self.season is not None:
            return f"{text} S{self.season:02d}"
        return f"{text} ({self.year})" if self.year else text

    def params(self, languages: Sequence[str] = ()) -> list[tuple[str, str]]:
        """The search's parameters, sorted and lower-case as the API asks (else it redirects)."""
        params = {"query": self.text.lower()}
        if self.year:
            params["year"] = str(self.year)
        if self.season is not None:
            params["season_number"] = str(self.season)
        if self.episode is not None:
            params["episode_number"] = str(self.episode)
        if languages:
            params["languages"] = ",".join(sorted({code.lower() for code in languages}))
        return sorted(params.items())


@dataclass(frozen=True)
class SearchResult:
    query: SearchQuery
    files: tuple[SubtitleFile, ...]


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
                release=str(attributes.get("release") or "").strip() or file_name or "Untitled",
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
            )
        )
    return found


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


# ---- languages and ranking ----------------------------------------------------------------

_UNRANKED = frozenset({"", "anonymous", "sub leecher", "new member"})
# OpenSubtitles' own codes that are no ISO language.
_SPECIAL_LANGUAGES = {
    "ea": "Spanish (Latin America)",
    "ze": "Chinese (bilingual)",
    "zh-ca": "Chinese (Cantonese)",
    "pm": "Portuguese (Mozambique)",
    "me": "Montenegrin",
    "ma": "Manipuri",
    "sy": "Syriac",
    "und": "Unknown language",
}


def language_name(code: str) -> str:
    """A language's name in English: "English", "Portuguese (Brazil)"."""
    key = code.strip().lower()
    if key in _SPECIAL_LANGUAGES:
        return _SPECIAL_LANGUAGES[key]
    locale = QLocale(code.replace("-", "_"))
    if locale.language() in (QLocale.Language.C, QLocale.Language.AnyLanguage):
        return code.upper() or "Unknown language"
    name = QLocale.languageToString(locale.language())
    if "-" in code:
        territory = QLocale.territoryToString(locale.territory())
        if territory and locale.territory() != QLocale.Country.AnyTerritory:
            name = f"{name} ({territory})"
    return name


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
# The country and language codes providers put before a title ("TR: ", "|EN| ", "[DE] ",
# "FR| "): only these are taken for a provider's tag, and only in those forms.
_CODES = frozenset("""
    AF AL AM AR AT AU AZ BA BE BG BR BY CA CH CN CY CZ DE DK DZ EE EG EN ES EU FI FR GE GR HK HR
    HU ID IE IL IN IQ IR IS IT JO JP KR KU KW KZ LA LB LT LU LV MA MK MT MX MY NL NO NZ PH PK PL
    PT QA RO RS RU SA SE SG SI SK SQ SR SY TH TN TR TW UA UK US UZ VN ZA
    ALB ARA ARB BUL CHI CHN CZE DAN DEU DUT ENG ESP EXYU FIN FRA FRE GER GRE HEB HIN HRV HUN IND
    ITA JPN KOR KUR LAT LATAM NLD NOR PER POL POR ROM RUM RUS SPA SRP SWE TUR UKR
    """.split())
_PREFIX = re.compile(
    r"^\s*(?:"
    r"\|\s*([A-Za-z]{2,5})\s*\|"  # |EN|
    r"|\[\s*([A-Za-z]{2,5})\s*\]"  # [DE]
    r"|([A-Za-z]{2,5})\s*\|"  # FR|
    r"|([A-Za-z]{2,5}):"  # TR:
    r")\s+"
)
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
_EDGES = " \t-–—|:·,._/\\"


def _plausible_year(value: Any) -> Optional[int]:
    year = _int(str(value).strip()[:4]) if value not in (None, "") else None
    if year is None or not 1900 <= year <= _dt.date.today().year + 1:
        return None
    return year


def _strip_tags(text: str) -> str:
    """Strip a provider's tags before the title ("TR: ", "|EN| ", "[DE] ", "FR| "): only a
    known country or language code, and only while a title of two words, or of one word of
    four letters or more, remains."""
    while True:
        match = _PREFIX.match(text)
        if match is None:
            return text
        code = next(group for group in match.groups() if group)
        rest = text[match.end() :].strip()
        words = rest.split()
        if code.upper() not in _CODES or not (len(words) >= 2 or len(rest) >= 4):
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
    text = " ".join(text.split()).strip(_EDGES)
    if year is None:
        trailing = _DASH_YEAR.search(text)
        if trailing and _plausible_year(trailing.group(1)) and text[: trailing.start()].strip():
            year, text = int(trailing.group(1)), text[: trailing.start()]
    return " ".join(text.split()).strip(_EDGES) or original, year


def item_year(item: dict) -> Optional[int]:
    """A film's year as its provider gives it, if it does."""
    for key in ("year", "releaseDate", "release_date", "releasedate", "released"):
        year = _plausible_year(item.get(key))
        if year:
            return year
    return None


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
    with its season and episode numbers. `section` is "movies" or "series"; `title` what the
    player shows; `series_title` an episode's series; `item` and `context` the catalog's. A
    film's title that names an episode ("Series S01E02", a file's name) is searched as one."""
    item, context = item or {}, context or {}
    if section != "series":
        text, year = clean_title(title)
        named, season, episode = _episode_in(text)
        if season is not None and named.strip(_EDGES):
            return SearchQuery(named.strip(_EDGES), None, season, episode)
        return SearchQuery(text or str(title).strip(), year or item_year(item))
    season_dict = context.get("season") if isinstance(context.get("season"), dict) else None
    season_value, episode_value = season_and_episode(season_dict, item)
    season, episode = _int(season_value), _int(episode_value)
    series = series_title
    if not series:
        series_dict = context.get("series")
        if isinstance(series_dict, dict):
            series = str(series_dict.get("name") or series_dict.get("title") or "")
    named, found_season, found_episode = _episode_in(str(series or title))
    if found_season is None and series:
        _rest, found_season, found_episode = _episode_in(str(title))
    if season is None:
        season = found_season
    if episode is None:
        episode = found_episode
    text, _year = clean_title(named)
    by_season = _SEASON_ONLY.search(text)
    if by_season and text[: by_season.start()].strip(_EDGES):  # "Series Season 2"
        season = season if season is not None else int(by_season.group(1))
        text = text[: by_season.start()].strip(_EDGES)
    return SearchQuery(text or str(title).strip(), None, season, episode)


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


def quota_text(reset: str, *, signed_in: bool) -> str:
    """Today's downloads are used, in plain words."""
    back = f"They come back in {reset}." if reset else "They come back within a day."
    if signed_in:
        return f"You've used today's downloads on your OpenSubtitles account. {back}"
    return (
        f"You've used today's downloads: without signing in, OpenSubtitles allows "
        f"{ANONYMOUS_DAILY} a day per connection. {back} Signing in to OpenSubtitles in "
        "Settings → Subtitles raises the limit."
    )


def busy_text(seconds: Optional[int]) -> str:
    wait = f"in {seconds} seconds" if seconds and seconds > 1 else "in a moment"
    return f"OpenSubtitles is getting too many requests. Try again {wait}."


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
                raise OpenSubtitlesError(BAD_KEY, BAD_KEY_TEXT)
            raise OpenSubtitlesError(BAD_LOGIN, BAD_LOGIN_TEXT)
        data = self._checked(response, signed_in=False)
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise OpenSubtitlesError(SERVER, UNREADABLE_TEXT)
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
        if not query.text:
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
            raise OpenSubtitlesError(GONE, GONE_TEXT)
        if response.status_code in (401, 403) and token is not None:
            # Refused again with a fresh sign-in: the sign-in, not the key (anonymous
            # downloads take the key alone).
            raise OpenSubtitlesError(BAD_LOGIN, SIGN_IN_AGAIN_TEXT)
        info = self._checked(response, signed_in=token is not None)
        link = info.get("link") if isinstance(info, dict) else None
        if not isinstance(link, str) or not link.startswith("https://"):
            raise OpenSubtitlesError(SERVER, UNREADABLE_TEXT)
        return self._fetch(link), info

    def _fetch(self, link: str) -> bytes:
        """The file at a download's temporary link: each redirect https too, the body read
        in pieces and refused beyond MAX_FILE_BYTES."""
        url = link
        for _hop in range(MAX_REDIRECTS + 1):
            if not url.startswith("https://"):
                logger.info("A subtitle file's link led away from https")
                raise OpenSubtitlesError(SERVER, UNREADABLE_TEXT)
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
                raise OpenSubtitlesError(NETWORK, NETWORK_TEXT) from None
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
        raise OpenSubtitlesError(SERVER, UNREADABLE_TEXT)

    @staticmethod
    def _body(file: Any) -> bytes:
        if file.status_code in (404, 410):
            raise OpenSubtitlesError(GONE, GONE_TEXT)
        if not 200 <= file.status_code < 300:
            logger.info("The subtitle file's link answered %s", file.status_code)
            raise OpenSubtitlesError(SERVER, SERVER_TEXT)
        content = bytearray()
        try:
            for chunk in file.iter_content(chunk_size=_CHUNK):
                content += chunk
                if len(content) > MAX_FILE_BYTES:
                    logger.info("A subtitle file was larger than %s bytes", MAX_FILE_BYTES)
                    raise OpenSubtitlesError(SERVER, UNREADABLE_TEXT)
        except (requests.RequestException, OSError) as e:
            logger.info("The subtitle file stopped coming: %s", safe_exc(e))
            raise OpenSubtitlesError(NETWORK, NETWORK_TEXT) from None
        if not content:
            raise OpenSubtitlesError(SERVER, UNREADABLE_TEXT)
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
            raise OpenSubtitlesError(NO_KEY, NO_KEY_TEXT)
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
            raise OpenSubtitlesError(NETWORK, NETWORK_TEXT) from None

    def _checked(self, response: Any, *, signed_in: bool) -> Any:
        """The answer's JSON, or the problem its status says."""
        status = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status < 300:
            data = _json(response)
            if data is None:
                raise OpenSubtitlesError(SERVER, UNREADABLE_TEXT)
            return data
        logger.info("OpenSubtitles answered %s", status)
        if status in (401, 403):
            if signed_in:  # with a sign-in's token: the sign-in ended
                raise OpenSubtitlesError(BAD_LOGIN, SIGN_IN_AGAIN_TEXT)
            raise OpenSubtitlesError(BAD_KEY, BAD_KEY_TEXT)
        if status == 406:
            data = _json(response)
            reset = _reset_text(data.get("reset_time")) if isinstance(data, dict) else ""
            raise OpenSubtitlesError(QUOTA, quota_text(reset, signed_in=signed_in))
        if status == 429:
            raise OpenSubtitlesError(BUSY, busy_text(_retry_seconds(response)))
        raise OpenSubtitlesError(SERVER, SERVER_TEXT)


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
    finished = Signal(object)  # SearchResult or Problem
    error = Signal(str)

    def __init__(
        self, client: OpenSubtitlesClient, query: SearchQuery, preferred: Sequence[str] = ()
    ) -> None:
        super().__init__()
        self._client, self._query, self._preferred = client, query, tuple(preferred)

    @Slot()
    def run(self) -> None:
        try:
            files = self._client.search(self._query, preferred=self._preferred)
        except OpenSubtitlesError as e:
            self.finished.emit(e.problem)
            return
        self.finished.emit(SearchResult(self._query, tuple(files)))


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
            self.finished.emit(Problem(NO_KEY, NO_KEY_TEXT))
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
            self.finished.emit(Problem(DISK, DISK_TEXT))
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
