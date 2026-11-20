"""Free radio (2.13): internet radio from radio-browser.info, a free directory of stations, added
as Free channels adds iptv-org's lists (the welcome card's, the sources column's, the Providers
menu's and Settings → Providers' "Free radio…").

A list is an ordinary M3U provider whose address is the directory's station JSON: the M3U loader
reads it (services.m3u.stations_in, parse_stations) and makes each station a channel as an M3U
line would, marked a station (`is_station`: it plays directly in VLC, never through the
time-shift helper). Its first group, All stations (the list's "All", renamed), holds every
station, the most played first; then one country's stations are grouped by genre (`genre_of`,
from their tags), the list of all countries' (the 2,000 most played worldwide) by country.
Older 2.x builds read the entry as any M3U playlist and say its answer is no playlist.

The directory is asked with QiTV's own name (it asks apps for one), and only for its lists: no
"click" is reported and nothing of the user's is sent. Data only, no widgets.
"""

from __future__ import annotations

import re
from typing import Any, Optional
import unicodedata
from urllib.parse import urlsplit

from services import free_channels
from services.i18n import N_, translate
from services.provider_api import CLIENT_HEADERS

_API = "https://all.api.radio-browser.info"  # round-robin over the directory's servers
_HOST = "api.radio-browser.info"  # its servers are <name>.api.radio-browser.info
_PATH = "/json/stations/"
# The directory's own check of each station, honoured; the most played first.
ALL_URL = f"{_API}{_PATH}topclick/2000?hidebroken=true"
_COUNTRY_URL = (
    f"{_API}{_PATH}bycountrycodeexact/{{code}}"
    "?hidebroken=true&order=clickcount&reverse=true&limit=2000"
)
ALL_NAME = "Radio: all countries"  # saved in English, as Free channels' names are
_NAME_PREFIX = "Radio: "
RADIO_KEY = "radio"  # in a list made from the directory's stations (its groups: `retitle`)
STATION_KEY = "radio_station"  # in each of its stations (`is_station`)
# i18n: a radio list's first group: every station, most played first
ALL_STATIONS = N_("All stations")

# i18n: help under Settings → Providers → Free radio, and its menu's tooltip (radio-browser.info
# is a website's name)
HELP_TEXT = N_(
    "Internet radio from radio-browser.info: every country's most played, or one country's."
)

# iptv-org's countries (free_channels.COUNTRIES) but its "International", which is no country.
COUNTRY_CODES: tuple[str, ...] = tuple(
    code for code, _name in free_channels.COUNTRIES if code != "int"
)
_LISTED = frozenset(COUNTRY_CODES)
_ISO = {"uk": "GB"}  # iptv-org's codes that are not ISO 3166's
_IPTV_ORG = {iso.lower(): code for code, iso in _ISO.items()}

# A station's group, by the words of its tags: the first group with one of its words among them,
# in this order (a "pop rock" station is Rock's, a "news, rock" one News & talk's). Whole words
# only ("classic rock" is no Classical), letter case and accents aside ("TÜRKÜ" is "türkü").
GENRES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        N_("News & talk"),  # i18n: a group of radio stations (Free radio)
        ("news", "talk", "information", "info", "politics", "sports", "sport", "comedy")
        + ("haber", "noticias", "nachrichten"),
    ),
    (
        N_("Classical"),  # i18n: a group of radio stations: classical music
        ("classical", "classique", "clásica", "klassik", "klasik", "opera", "baroque")
        + ("symphony", "orchestra", "türk sanat", "sanat müziği"),  # Turkish art music
    ),
    (
        N_("Jazz & blues"),  # i18n: a group of radio stations
        ("jazz", "blues", "swing", "soul", "funk", "bossa nova", "big band"),
    ),
    (
        N_("Electronic & dance"),  # i18n: a group of radio stations
        ("electronic", "electronica", "electro", "dance", "house", "techno", "trance", "edm")
        + ("club", "disco", "drum and bass", "dubstep", "ambient", "chillout", "lounge", "lofi"),
    ),
    (
        N_("Rock"),  # i18n: a group of radio stations: rock music
        ("rock", "metal", "alternative", "indie", "punk", "grunge"),
    ),
    (
        N_("Pop"),  # i18n: a group of radio stations: pop music
        ("pop", "hits", "top 40", "top40", "top 100", "charts", "oldies", "schlager")
        + ("60s", "70s", "80s", "90s", "00s", "2000s", "60's", "70's", "80's", "90's")
        + ("hip hop", "hiphop", "hip-hop", "rap", "r&b", "rnb", "urban", "reggaeton", "latin"),
    ),
    (
        N_("Folk & traditional"),  # i18n: a group of radio stations
        ("folk", "traditional", "türkü", "halk", "türk halk", "ethnic", "world", "country")
        + ("celtic", "flamenco", "fado", "tango", "arabesk", "fantezi", "fantazi")
        + ("turkish music",),
    ),
    (
        N_("Religious"),  # i18n: a group of radio stations
        ("religious", "religion", "christian", "gospel", "catholic", "church", "worship")
        + ("bible", "islam", "islamic", "quran", "ilahi", "dini", "spiritual"),
    ),
    (
        N_("Kids"),  # i18n: a group of radio stations: for children
        ("kids", "children", "child", "çocuk", "kinder", "enfants", "infantil"),
    ),
)
# i18n: a group of radio stations: those of no other group (or, all countries', of no country)
OTHER = N_("Other")


def _plain(text: str) -> str:
    """`text` in lower case without accents ("TÜRKÜ" and "Türkü" are "turku")."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _words_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(_plain(word)) for word in words)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


_PATTERNS = tuple((name, _words_pattern(words)) for name, words in GENRES)


def genre_of(tags: Any) -> str:
    """A station's group (English: shown with translate()) by its tags, "rock,classic rock"
    (commas, Arabic ones too, between them): OTHER when no word of the table is among them."""
    text = _plain(str(tags or ""))
    if text.strip():
        for name, pattern in _PATTERNS:
            if pattern.search(text):
                return name
    return OTHER


def country_of(station: dict) -> str:
    """A station's country as its group in the list of all countries (English, as
    free_channels names it): OTHER for one of no country QiTV lists."""
    iso = str(station.get("countrycode") or "").strip().lower()
    code = _IPTV_ORG.get(iso, iso)
    return free_channels.country_name(code) if code in _LISTED else OTHER


def retitle(payload: Any) -> Any:
    """A list made from the directory's stations, its groups' titles in the active language
    (cached in the language of the start that loaded it), its "All" (id "*") named All
    stations; any other list as it is. In place: services.m3u.order_cached_groups orders them
    after ("*" first)."""
    if not isinstance(payload, dict) or payload.get(RADIO_KEY) is not True:
        return payload
    categories = payload.get("categories")
    if isinstance(categories, list):
        for category in categories:
            if isinstance(category, dict):
                group = str(category.get("id") or "")
                category["title"] = translate(ALL_STATIONS if group == "*" else group)
    return payload


def is_station(item: Any) -> bool:
    """Whether a channel is a station of a radio list: VLC plays it directly (it is a sound
    stream; the time-shift helper would only fail to read most of them)."""
    return isinstance(item, dict) and item.get(STATION_KEY) is True


def iso_code(code: str) -> str:
    """The directory's (ISO 3166) code of iptv-org's country `code`: "GB" for "uk"."""
    return _ISO.get(code, code.upper())


def country_url(code: str) -> str:
    return _COUNTRY_URL.format(code=iso_code(code))


def provider_entry(code: Optional[str]) -> dict:
    """The M3U provider to add: the list of all countries (None) or one country's."""
    if code is None:
        return {"type": "M3UPLAYLIST", "name": ALL_NAME, "url": ALL_URL}
    name = free_channels.country_name(code)
    return {"type": "M3UPLAYLIST", "name": f"{_NAME_PREFIX}{name}", "url": country_url(code)}


def is_directory_url(url: Any) -> bool:
    """Whether `url` asks the directory for stations (any of its servers)."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    by_directory = host == _HOST or host.endswith("." + _HOST)
    return parts.scheme in ("http", "https") and by_directory and parts.path.startswith(_PATH)


def same_list(listed: dict, entry: dict) -> bool:
    """Whether provider `listed` is the free radio list `entry` (added again, it is only
    switched to): the same address, or the directory's same list asked otherwise (another of
    its servers, another query)."""
    url, wanted = listed.get("url"), entry.get("url")
    if url == wanted:
        return True
    if not (is_directory_url(url) and is_directory_url(wanted)):
        return False
    return urlsplit(str(url)).path == urlsplit(str(wanted)).path


def headers_for(url: Any) -> dict[str, str]:
    """The headers a list's request sends: QiTV's own name to the directory, which asks apps for
    one ("QiTV v2.13.0", as OpenSubtitles and TMDB hear it); VLC's to everything else."""
    if not is_directory_url(url):
        return CLIENT_HEADERS
    from services.opensubtitles import user_agent  # (Qt and more: only when it is asked)

    return {"User-Agent": user_agent()}
