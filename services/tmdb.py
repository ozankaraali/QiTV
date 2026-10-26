"""A film's or a series' details from TMDB (its API, v3), where the provider leaves gaps; no
widgets.

The Library shows what the provider lists. When TMDB is on (Settings), the title in focus and
the posterless ones on screen are looked up here, on the app's job runner: `LookupWorker` emits
a `Looked`, and `fill_gaps` puts its story, year, running time, genres, rating, cast and
director where the provider's item has none (the provider's own always stays). The poster
fills in only where the provider has none, or its image can't be loaded.

Matching (`look_up`): by the TMDB id the provider gives (an Xtream panel's "tmdb", a Jellyfin
or Emby item's), else its IMDb id, else by the title: `clean_title` drops a provider's known
tags and quality marks, and an original title in Latin letters is preferred. The whole title
is searched first; without another tag before it ("SO-IN - ") only when TMDB calls nothing so
("SAS: …" stays whole). A result is the title only when it has the same words (`same_words`:
"Harbour" is not "Harbour: Part Two") and, when the provider gives a year, a year within one
of it; several of one name without a year are none of them. A title in another language than
TMDB's answer is checked against the result's translated and alternative titles. A wrong
poster is worse than none: nothing fuzzier matches.

Never sent: a title whose name has a word parental controls call adult (XXX, adult, 18+, …),
a dated event without an id, and (ui/tmdb_fill.py) anything in a group named so or hidden by
parental controls. Requests go in the app's language and carry the token (the user's own from
Settings, else the one a release build carries: services/_build_keys.py, written by CI), never
the user's name. Titles, the token and URLs are never logged. Answers are cached on disk
(`TmdbCache`, saved at most every SAVE_EVERY_S) for 30 days, and "TMDB has no such title" for
3: TMDB's terms allow six months at most (its images too: services/image_manager.py).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Iterable, Optional, Sequence

from PySide6.QtCore import QObject, Signal, Slot

from services import _build_keys
from services.atomic_write import atomic_write
from services.jobs import cancel_requested
from services.lazy_import import lazy_module
from services.opensubtitles import (
    clean_title,
    item_year,
    original_title,
    provider_ids,
    same_words,
    title_words,
    user_agent,
)
from services.parental import looks_adult
from services.redact import safe_exc

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

API_URL = "https://api.themoviedb.org/3"
IMAGE_URL = "https://image.tmdb.org/t/p/"
POSTER_SIZE = "w342"
TIMEOUT = 10
FALLBACK_LANGUAGE = "en-US"  # a story TMDB lacks in the app's language is taken in English
MOVIE, TV = "movie", "tv"
DAY_S = 24 * 60 * 60
FOUND_S = 30 * DAY_S  # TMDB's terms: nothing cached longer than six months
MISSING_S = 3 * DAY_S  # "no such title": a new one may be added
MAX_ENTRIES = 20_000
SAVE_EVERY_S = 30  # a lookup saves the cache at most this often (it is rewritten whole)
CAST_COUNT = 5
MAKERS_COUNT = 2  # a film's directors, a series' creators
GENRES_SHOWN = 3
MIN_VOTES = 10  # a rating of fewer votes says little
CANDIDATES = 3  # a translated title: the search's results checked by their other titles
BUSY_S = 10.0  # TMDB asked to slow down without saying for how long
# The app's languages as TMDB names them (a language alone works too, as its default country).
LANGUAGES = {"en": "en-US", "de": "de-DE", "tr": "tr-TR", "es": "es-ES", "fr": "fr-FR"}

# A TmdbError's kinds.
BAD_TOKEN, BUSY, NETWORK, SERVER = "token", "busy", "network", "server"


def built_in_token() -> str:
    """The token a release build carries ("" in a build from the source code)."""
    return str(getattr(_build_keys, "TMDB_API_TOKEN", "") or "").strip()


def api_token(user_token: str) -> str:
    """The token to use: the user's own when set, else the build's ("": none)."""
    return str(user_token or "").strip() or built_in_token()


def tmdb_language(code: Optional[str]) -> str:
    """The app's language ("tr") as TMDB names it ("tr-TR"); English without one."""
    code = str(code or "").strip()
    return LANGUAGES.get(code, code) if code else FALLBACK_LANGUAGE


class TmdbError(Exception):
    """A request TMDB refused or could not answer: `kind` says why (BAD_TOKEN, BUSY, NETWORK,
    SERVER); `retry_after` how many seconds TMDB asked to wait (BUSY)."""

    def __init__(self, kind: str, retry_after: float = 0.0) -> None:
        super().__init__(kind)
        self.kind = kind
        self.retry_after = retry_after


@dataclass(frozen=True)
class Info:
    """What TMDB says of a title (empty or None where it says nothing)."""

    tmdb_id: int
    kind: str  # MOVIE | TV
    title: str  # in the language asked for
    year: Optional[int] = None
    plot: str = ""
    minutes: Optional[int] = None
    genres: tuple[str, ...] = ()
    rating: Optional[float] = None  # TMDB's users' average, out of 10
    cast: tuple[str, ...] = ()
    director: str = ""  # a film's directors, a series' creators
    poster: str = ""  # TMDB's image path ("/abc.jpg")

    def poster_url(self, size: str = POSTER_SIZE) -> str:
        return f"{IMAGE_URL}{size}{self.poster}" if self.poster.startswith("/") else ""

    def to_json(self) -> dict:
        data = asdict(self)
        data["genres"], data["cast"] = list(self.genres), list(self.cast)
        return data

    @classmethod
    def from_json(cls, data: Any) -> Optional[Info]:
        """The Info a cache entry holds; None when it isn't one."""
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                tmdb_id=int(data["tmdb_id"]),
                kind=str(data["kind"]),
                title=str(data.get("title") or ""),
                year=_int_or_none(data.get("year")),
                plot=str(data.get("plot") or ""),
                minutes=_int_or_none(data.get("minutes")),
                genres=tuple(str(g) for g in data.get("genres") or ()),
                rating=float(data["rating"]) if data.get("rating") is not None else None,
                cast=tuple(str(c) for c in data.get("cast") or ()),
                director=str(data.get("director") or ""),
                poster=str(data.get("poster") or ""),
            )
        except KeyError, TypeError, ValueError:
            return None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except TypeError, ValueError:
        return None


# ---- what to look up ----------------------------------------------------------------------


@dataclass(frozen=True)
class Wanted:
    """A title to look up: its kind (MOVIE or TV), its cleaned title and year, the ids its
    provider gives, and `alt`: the title without a provider's tag before it ("SO-IN - "),
    searched only when TMDB knows nothing by the whole one ("" when there is no tag)."""

    kind: str
    text: str
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    imdb_id: Optional[int] = None
    alt: str = ""

    def key(self) -> str:
        """The cache's key (without the language): its id, else its title's words and year."""
        if self.tmdb_id:
            return f"{self.kind}:id:{self.tmdb_id}"
        if self.imdb_id:
            return f"{self.kind}:imdb:{self.imdb_id}"
        return f"{self.kind}:q:{' '.join(sorted(title_words(self.text)))}:{self.year or ''}"


# A provider's tag before a title, in capitals and before a dash or a bar ("SO-IN - ", "ZONE+
# | "), beyond the country and language codes clean_title knows. Not before a colon: that is a
# title's own ("SAS: …", "TRON: …"). The title without it is searched second (Wanted.alt).
_TAG = re.compile(r"^([A-Z0-9+]{2,6}(?:-[A-Z0-9+]{1,6})*)\s*[-–—|]\s+")
# A dated event ("… 23.09.2024"): a match or a show's day, not a film; asked only by an id.
_DATE = re.compile(r"(?<![\d.])\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2}(?![\d.])")


def _without_tag(text: str) -> str:
    match = _TAG.match(text)
    rest = text[match.end() :].strip() if match else ""
    return rest if rest and (len(rest.split()) >= 2 or len(rest) >= 4) else text


def wanted_for(item: Any, kind: str) -> Optional[Wanted]:
    """What to look up for a catalog item of `kind` (MOVIE or TV); None for anything else
    (an episode, a recording, a title whose name has an adult word, a dated event without an
    id, a title without words or ids). Titles in an adult group are left out by the Library's
    side (ui/tmdb_fill.py), which knows the groups."""
    if kind not in (MOVIE, TV) or not isinstance(item, dict):
        return None
    if item.get("_recording") or item.get("_scheduled"):
        return None  # a file of the disk, named by its channel and time
    name = str(item.get("name") or item.get("title") or "")
    if looks_adult(name):
        return None  # parental controls' words (XXX, adult, 18+, …): never sent
    tmdb_id, imdb_id = provider_ids(item)
    text, year = clean_title(original_title(item) or name)
    alt = _without_tag(text)
    if not title_words(text) or _DATE.search(name):
        text = alt = ""
    if not (text or tmdb_id or imdb_id):
        return None
    alt = alt if alt != text and title_words(alt) else ""
    return Wanted(kind, text, year or item_year(item), tmdb_id, imdb_id, alt)


# ---- matching -----------------------------------------------------------------------------


def _names(result: dict) -> list[str]:
    names = (result.get(k) for k in ("title", "name", "original_title", "original_name"))
    return [n for n in names if isinstance(n, str) and n]


def _year_of(result: dict) -> Optional[int]:
    date = str(result.get("release_date") or result.get("first_air_date") or "")
    return int(date[:4]) if date[:4].isdigit() else None


def _fits(result: dict, year: int) -> bool:
    theirs = _year_of(result)
    return theirs is not None and abs(theirs - year) <= 1


def pick(results: Iterable[Any], wanted: Wanted) -> tuple[Optional[dict], list[dict]]:
    """(The search result that is the title wanted, or None; the results left to check by
    their translated and alternative titles, when none is called so in TMDB's answer).

    A result of the same words is it: with a year, the closest within one; without, only
    when it is the one of that name. Called so in another year, it is not there. Called so
    nowhere, the title may be a translation: the results of its year (the first, without
    one) are left to check."""
    found = [r for r in results if isinstance(r, dict) and isinstance(r.get("id"), int)]
    named = [r for r in found if any(same_words(wanted.text, n) for n in _names(r))]
    year = wanted.year
    if named:
        if year is None:
            return (named[0], []) if len(named) == 1 else (None, [])
        near = sorted(
            (r for r in named if _fits(r, year)), key=lambda r: abs((_year_of(r) or 0) - year)
        )
        return (near[0] if near else None), []
    if year is None:
        return None, found[:1]
    return None, [r for r in found if _fits(r, year)][:CANDIDATES]


def _known_as(data: dict, text: str) -> bool:
    """Whether TMDB knows a title by `text`: its own, translated or alternative titles."""
    names = _names(data)
    translations = data.get("translations")
    if isinstance(translations, dict):
        for entry in translations.get("translations") or []:
            inner = entry.get("data") if isinstance(entry, dict) else None
            if isinstance(inner, dict):
                names += _names(inner)
    alternatives = data.get("alternative_titles")
    if isinstance(alternatives, dict):
        for entry in alternatives.get("titles") or alternatives.get("results") or []:
            if isinstance(entry, dict) and isinstance(entry.get("title"), str):
                names.append(entry["title"])
    return any(same_words(text, name) for name in names)


def look_up(client: TmdbClient, wanted: Wanted) -> Optional[Info]:
    """The title `wanted` as TMDB knows it, else None. Raises TmdbError."""
    if wanted.tmdb_id:
        info = _by_id(client, wanted.kind, wanted.tmdb_id)
        if info is not None:
            return info
    if wanted.imdb_id:
        tmdb_id = client.find_imdb(wanted.kind, wanted.imdb_id)
        if tmdb_id:
            info = _by_id(client, wanted.kind, tmdb_id)
            if info is not None:
                return info
    # The whole title first; without a provider's tag only when TMDB calls nothing so.
    left: list[tuple[str, dict]] = []  # (the text, a result to check by its other titles)
    for text in (t for t in (wanted.text, wanted.alt) if t):
        results = client.search(wanted.kind, text)
        chosen, unsure = pick(results, replace(wanted, text=text))
        if chosen is not None:
            return _by_id(client, wanted.kind, chosen["id"])
        left += [(text, result) for result in unsure]
        if _called(results, text):
            break  # TMDB knows that whole title (in another year): it is not cut down
    for text, result in left[:CANDIDATES]:
        data = client.details(wanted.kind, result["id"], titles=True)
        if data is not None and _known_as(data, text):
            return _info_of(client, wanted.kind, data)
    return None


def _called(results: Iterable[Any], text: str) -> bool:
    """Whether a search's results hold a title called `text` (any year)."""
    return any(same_words(text, name) for r in results if isinstance(r, dict) for name in _names(r))


def _billing(person: dict) -> int:
    """A cast member's place in the credits (the unordered last)."""
    order = person.get("order")
    return order if isinstance(order, int) else 999


def _by_id(client: TmdbClient, kind: str, tmdb_id: int) -> Optional[Info]:
    data = client.details(kind, tmdb_id)
    return None if data is None else _info_of(client, kind, data)


def _info_of(client: TmdbClient, kind: str, data: dict) -> Info:
    tmdb_id = int(data.get("id") or 0)
    plot = str(data.get("overview") or "").strip()
    if not plot and client.language != FALLBACK_LANGUAGE and tmdb_id:
        plot = client.overview(kind, tmdb_id)
    runtime = data.get("runtime")
    if not isinstance(runtime, int):
        runs = data.get("episode_run_time")
        runtime = runs[0] if isinstance(runs, list) and runs and isinstance(runs[0], int) else None
    genres = tuple(
        str(g["name"]) for g in data.get("genres") or [] if isinstance(g, dict) and g.get("name")
    )
    rating = None
    average, votes = data.get("vote_average"), data.get("vote_count")
    if isinstance(average, (int, float)) and isinstance(votes, int):
        if votes >= MIN_VOTES and average > 0:
            rating = round(float(average), 1)
    credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
    people = [c for c in credits.get("cast") or [] if isinstance(c, dict) and c.get("name")]
    people.sort(key=_billing)
    if kind == MOVIE:
        makers = [
            c
            for c in credits.get("crew") or []
            if isinstance(c, dict) and c.get("job") == "Director"
        ]
    else:
        makers = [c for c in data.get("created_by") or [] if isinstance(c, dict)]
    names = [str(c["name"]) for c in makers if c.get("name")]
    return Info(
        tmdb_id=tmdb_id,
        kind=kind,
        title=(_names(data) or [""])[0],
        year=_year_of(data),
        plot=plot,
        minutes=runtime if runtime and runtime > 0 else None,
        genres=genres,
        rating=rating,
        cast=tuple(str(c["name"]) for c in people[:CAST_COUNT]),
        director=", ".join(dict.fromkeys(names[:MAKERS_COUNT])),
        poster=str(data.get("poster_path") or ""),
    )


# ---- filling a provider's gaps --------------------------------------------------------------


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except TypeError, ValueError:
        return False


def fill_gaps(item: dict, info: Optional[Info]) -> dict:
    """`item` with what TMDB knows where its provider says nothing (a copy; the item itself
    as it is when TMDB knows nothing)."""
    if info is None:
        return item
    filled = dict(item)
    if info.plot and not (item.get("plot") or item.get("description")):
        filled["plot"] = info.plot
    name = str(item.get("name") or item.get("title") or "")
    if info.year and not (item_year(item) or clean_title(name)[1]):
        filled["year"] = str(info.year)
    if info.minutes and not any(item.get(k) for k in ("duration_secs", "duration", "time")):
        filled["duration_secs"] = info.minutes * 60
    if info.genres and not (item.get("genre") or item.get("genres_str")):
        filled["genre"] = ", ".join(info.genres[:GENRES_SHOWN])
    if info.rating and not any(
        _positive(item.get(k)) for k in ("rating", "rating_imdb", "rating_5based")
    ):
        filled["rating"] = f"{info.rating:.1f}"
    if info.cast and not (item.get("cast") or item.get("actors")):
        filled["cast"] = ", ".join(info.cast)
    if info.director and not item.get("director"):
        filled["director"] = info.director
    return filled


# ---- the API --------------------------------------------------------------------------------


class TmdbClient:
    """TMDB's API with `token` (a Read Access Token, sent as a Bearer), in `language`.
    `http` stands in for `requests` (tests). Safe to use from one worker thread at a time."""

    def __init__(self, token: str, language: str = FALLBACK_LANGUAGE, *, http: Any = None):
        self.token = str(token or "").strip()
        self.language = language or FALLBACK_LANGUAGE
        self._http = http

    def details(self, kind: str, tmdb_id: int, *, titles: bool = False) -> Optional[dict]:
        """A title's details with its credits (and its other titles, with `titles`); None
        when TMDB has no such title."""
        extra = "credits,translations,alternative_titles" if titles else "credits"
        return self._get(
            f"/{kind}/{int(tmdb_id)}", {"language": self.language, "append_to_response": extra}
        )

    def overview(self, kind: str, tmdb_id: int) -> str:
        """A title's story in English ("" when none)."""
        data = self._get(f"/{kind}/{int(tmdb_id)}", {"language": FALLBACK_LANGUAGE})
        return str((data or {}).get("overview") or "").strip()

    def find_imdb(self, kind: str, imdb_id: int) -> Optional[int]:
        """The TMDB id of a title of `kind` with that IMDb id, if TMDB knows one."""
        data = self._get(
            f"/find/tt{int(imdb_id):07d}",
            {"external_source": "imdb_id", "language": self.language},
        )
        results = (data or {}).get("movie_results" if kind == MOVIE else "tv_results") or []
        first = results[0] if isinstance(results, list) and results else None
        return first["id"] if isinstance(first, dict) and isinstance(first.get("id"), int) else None

    def search(self, kind: str, text: str) -> list[dict]:
        """The first page of a search for `text` (its year is matched here: a film's year
        can differ by a country's release)."""
        data = self._get(f"/search/{kind}", {"query": text, "language": self.language})
        results = (data or {}).get("results")
        return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []

    def _get(self, path: str, params: dict) -> Optional[dict]:
        if not self.token:
            raise TmdbError(BAD_TOKEN)
        if cancel_requested():  # QiTV is closing: no new request keeps it waiting
            raise TmdbError(NETWORK)
        http = self._http if self._http is not None else requests
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": user_agent(),
            "Accept": "application/json",
        }
        try:
            response = http.request(
                "GET", API_URL + path, params=params, headers=headers, timeout=TIMEOUT
            )
        except (requests.RequestException, OSError) as e:  # the type only: the URL may tell
            logger.info("TMDB could not be reached: %s", safe_exc(e))
            raise TmdbError(NETWORK) from None
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 404:
            return None
        if status in (401, 403):
            logger.info("TMDB refused the token (%s)", status)
            raise TmdbError(BAD_TOKEN)
        if status == 429:
            raise TmdbError(BUSY, _retry_after(response))
        if not 200 <= status < 300:
            logger.info("TMDB answered %s", status)
            raise TmdbError(SERVER)
        try:
            data = response.json()
        except ValueError:
            raise TmdbError(SERVER) from None
        if not isinstance(data, dict):
            raise TmdbError(SERVER)
        return data


def _retry_after(response: Any) -> float:
    headers = getattr(response, "headers", None) or {}
    try:
        seconds = float(headers.get("Retry-After") or BUSY_S)
    except TypeError, ValueError:
        seconds = BUSY_S
    return min(300.0, max(1.0, seconds))


# ---- the cache ------------------------------------------------------------------------------


class TmdbCache:
    """What TMDB answered, by "<language>|<Wanted.key()>", in a JSON file at `path`:
    {"version": 1, "titles": {key: {"at": unix time, "info": Info's JSON or null}}}. An entry
    older than FOUND_S (an Info) or MISSING_S (None: no such title) counts as unknown and is
    dropped; beyond MAX_ENTRIES the oldest go. Safe from several threads."""

    VERSION = 1

    def __init__(self, path: str, *, now: Callable[[], float] = time.time) -> None:
        self.path = path
        self._now = now
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, Optional[Info]]] = {}
        self._loaded = False
        self._dirty = False
        self._saved_at: Optional[float] = None

    def load(self) -> None:
        """Read the file once (what's in memory wins); an unreadable one is an empty cache."""
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.info("The TMDB cache was unreadable; starting afresh: %s", safe_exc(e))
            return
        titles = data.get("titles") if isinstance(data, dict) else None
        if not isinstance(titles, dict) or data.get("version") != self.VERSION:
            return
        read: dict[str, tuple[float, Optional[Info]]] = {}
        for key, entry in titles.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("at"), (int, float)):
                continue
            info = Info.from_json(entry.get("info"))
            if entry.get("info") is not None and info is None:
                continue
            read[str(key)] = (float(entry["at"]), info)
        with self._lock:
            for key, value in read.items():
                if not self._fresh(value):
                    continue
                self._entries.setdefault(key, value)

    def _fresh(self, value: tuple[float, Optional[Info]]) -> bool:
        at, info = value
        return self._now() - at <= (FOUND_S if info is not None else MISSING_S)

    def get(self, key: str) -> tuple[bool, Optional[Info]]:
        """(Whether TMDB's answer is known, the Info it gave: None for "no such title")."""
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                return False, None
            if not self._fresh(value):
                del self._entries[key]
                return False, None
            return True, value[1]

    def put(self, key: str, info: Optional[Info]) -> None:
        with self._lock:
            self._entries[key] = (self._now(), info)
            self._dirty = True

    def save(self, *, throttle: bool = False) -> bool:
        """Write what changed (merged with the file first); False when nothing was written.
        `throttle`: not within SAVE_EVERY_S of the last write (what changed waits for the next;
        a quit before it loses only those answers, asked again)."""
        self.load()
        with self._lock:
            if not self._dirty:
                return False
            last = self._saved_at
            if throttle and last is not None and self._now() - last < SAVE_EVERY_S:
                return False
            entries = sorted(
                ((k, v) for k, v in self._entries.items() if self._fresh(v)),
                key=lambda kv: kv[1][0],
            )[-MAX_ENTRIES:]
            self._entries = dict(entries)
            self._dirty = False
            payload = {
                "version": self.VERSION,
                "titles": {
                    key: {"at": at, "info": info.to_json() if info is not None else None}
                    for key, (at, info) in entries
                },
            }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with atomic_write(self.path) as f:
                f.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except OSError as e:
            logger.warning("Couldn't save the TMDB cache: %s", safe_exc(e))
            with self._lock:
                self._dirty = True
            return False
        with self._lock:
            self._saved_at = self._now()
        return True


# ---- the worker -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Looked:
    """A LookupWorker's answer: each title's Info by its Wanted.key() (None: TMDB has none),
    the seconds TMDB asked to wait (the batch stopped there), and whether it refused the
    token. A title missing from `found` could not be asked (no connection): ask again later."""

    found: dict[str, Optional[Info]] = field(default_factory=dict)
    busy_s: float = 0.0
    bad_token: bool = False


class LookupWorker(QObject):
    """Looks up `wanted` in turn, from `cache` when it knows them, and saves the cache."""

    finished = Signal(object)  # Looked
    error = Signal(str)

    def __init__(self, client: TmdbClient, cache: TmdbCache, wanted: Sequence[Wanted]) -> None:
        super().__init__()
        self._client, self._cache, self._wanted = client, cache, tuple(wanted)

    @Slot()
    def run(self) -> None:
        self._cache.load()
        found: dict[str, Optional[Info]] = {}
        busy, bad_token, failed = 0.0, False, 0
        for wanted in self._wanted:
            if cancel_requested():
                break
            key = f"{self._client.language}|{wanted.key()}"
            known, info = self._cache.get(key)
            if not known:
                try:
                    info = look_up(self._client, wanted)
                except TmdbError as e:
                    if e.kind == BUSY:
                        busy = e.retry_after or BUSY_S
                        break
                    if e.kind == BAD_TOKEN:
                        bad_token = True
                        break
                    failed += 1
                    continue  # no connection or a bad answer: asked again later
                self._cache.put(key, info)
            found[wanted.key()] = info
        if failed:
            logger.info("TMDB: %d of %d titles could not be looked up", failed, len(self._wanted))
        self._cache.save(throttle=True)
        self.finished.emit(Looked(found, busy, bad_token))
