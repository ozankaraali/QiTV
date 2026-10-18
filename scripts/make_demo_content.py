"""Fictional demo content for QiTV: a playlist, a guide, logos, a library server, screenshots.

    uv run python scripts/make_demo_content.py --out build/demo
    uv run python scripts/make_demo_content.py --out build/demo --serve
    uv run python scripts/make_demo_content.py --out build/demo --screenshots docs/screenshots

`--out` gets everything made here:
- `demo.m3u`: 24 fictional channels in 4 groups, each a live stream from the demo server;
- `logos/`: the channels' logos (initials on coloured squares), drawn with QPainter;
- `guide.xml`: a programme guide (XMLTV) for those channels, from 6 hours ago to 30 hours on;
- `posters/`: gradient posters with their titles for the library;
- `config/`: a QiTV settings folder with two providers, "Demo TV" (the playlist) and
  "Demo Library" (the demo server's Xtream-shaped player API: 3 genres of 12 films each and
  one series with two seasons), the guide as a local file, three favourites, a film half
  watched and two episodes of the series started.

`--serve` then serves it on 127.0.0.1 until Ctrl+C, for a manual run in another terminal:
`QITV_CONFIG_DIR=build/demo/config uv run python main.py`. Every live channel plays
tests/fixtures/live.ts as an endless real-time stream; every film and episode plays
tests/fixtures/smoke.mp4. Both are generated test patterns (see tests/fixtures/live_source.py).

`--screenshots DIR` takes the README's screenshots: it serves the content on a free port and
runs the real TV window at 1600×900 on Qt's offscreen platform (no window shows) in a fresh
settings folder, with libVLC muted; it takes about a minute. Each screenshot is the window's
grab(), the video the players decoded (libVLC's memory output) drawn where their surfaces are,
and the overlay host's grab() over them, as scripts/smoke_tv.py composes its screenshots. Only
the demo video is touched, with libVLC's own filters: the test pattern's timecode rows are cut
off (they would sit under the window's text), its colours are toned down, and the second
player's are turned so that the small picture stands out from the large one.

Nothing here touches the network: the server binds 127.0.0.1 only, and every URL in the
content points there. There are no real channels, posters, footage or credentials (the demo
library's "demo"/"demo" login is answered only by this script).
"""

from __future__ import annotations

import argparse
import colorsys
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler
import json
import logging
import os
from pathlib import Path
import random
import re
import select
import shutil
import socket
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Generator, Iterable, Optional
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.live.server import ThreadingLoopbackHTTPServer  # noqa: E402
from tests.fixtures.live_source import LIVE_TS, LoopedStream  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
HOST = "127.0.0.1"
PLAYLIST = "demo.m3u"
GUIDE = "guide.xml"
LOGOS = "logos"
POSTERS = "posters"
CONFIG = "config"
SMOKE_MP4 = ROOT / "tests" / "fixtures" / "smoke.mp4"
TV_PROVIDER = "Demo TV"
LIBRARY_PROVIDER = "Demo Library"
USERNAME = PASSWORD = "demo"  # the demo server's own login: answered only here
GUIDE_FROM_H, GUIDE_TO_H = 6, 30  # hours before and after now
SCREENSHOT_SIZE = (1600, 900)

# ---- the content --------------------------------------------------------------------------------

# Four groups of six channels, numbered 1-24 in this order. Every name is made up.
CHANNEL_GROUPS: dict[str, list[str]] = {
    "News": [
        "Northwind News",
        "Harbor News 24",
        "Copperline World",
        "Morning Quay",
        "Skylark Weather",
        "Ledger Business",
    ],
    "Sports": [
        "Harbor Sports 1",
        "Harbor Sports 2",
        "Ridgeback Sports",
        "Fieldhouse Live",
        "Pitlane Motor",
        "Tidewater Golf",
    ],
    "Entertainment": [
        "Bluefin Drama",
        "Larkspur Comedy",
        "Juniper One",
        "Orchard Classics",
        "Moonrise Cinema",
        "Velvet Stage",
    ],
    "Kids": [
        "Paperkite Kids",
        "Pebble Junior",
        "Marble Toons",
        "Puddle Jump",
        "Little Lighthouse",
        "Puzzlebox Science",
    ],
}
GROUP_HUES = {"News": 0.58, "Sports": 0.38, "Entertainment": 0.78, "Kids": 0.08}
FAVORITES = ["Northwind News", "Harbor Sports 1", "Bluefin Drama"]

# (title, description) per group. Programmes last 30, 60 or 90 minutes and start on the hour
# or the half hour, as the guide's columns do: a title is never squeezed into a sliver.
PROGRAMMES: dict[str, list[tuple[str, str]]] = {
    "News": [
        ("Morning Briefing", "The night's stories and the day ahead, in half an hour."),
        ("World Desk", "Correspondents on three continents report on the day."),
        ("Market Watch", "Prices, harbours and the people behind the numbers."),
        ("The Weather Hour", "A slow look at the week's fronts, winds and tides."),
        ("Evening Report", "The day's main stories, with interviews from the studio."),
        ("In Focus", "One story, told in depth by the people it touches."),
        ("Late Edition", "Tomorrow's front pages and a calm look back at today."),
        ("Headlines", "The latest stories in brief."),
    ],
    "Sports": [
        ("Matchday Live", "Both halves from the coastal league, with the build-up."),
        ("Coastal Regatta", "Sailing from the bay, buoy by buoy."),
        ("Training Ground", "A week with a second-division club before a cup tie."),
        ("Classic Rally", "Old cars, narrow roads and a long mountain stage."),
        ("The Final Whistle", "Results, goals and the talking points of the weekend."),
        ("Highlights", "The best moments of the week's matches."),
        ("Sports Desk", "News and interviews from every sport."),
    ],
    "Entertainment": [
        ("The Lighthouse Keeper", "A new keeper arrives, and the island has questions."),
        ("Kitchen Table", "Four cooks, one table and a forgotten family recipe."),
        ("Saltwater Stories", "Short films about towns that live from the sea."),
        ("Quiz Harbour", "Teams from three villages answer for the trophy."),
        ("Night Owls", "Comedy and music for the late hours."),
        ("The Long Weekend", "Old friends rent a house by the dunes for three days."),
        ("Comedy Hour", "Stand-up from the pier theatre."),
    ],
    "Kids": [
        ("Paper Boats", "Mira and Tom race paper boats down the rain gutter."),
        ("Puzzle Time", "Shapes, colours and a riddle to take home."),
        ("The Busy Garden", "What the snails, bees and worms do all day."),
        ("Story Hour", "A bedtime story, read by the lighthouse cat."),
        ("Science Splash", "Floating, sinking and why ice cubes crack."),
        ("Cartoon Corner", "Short cartoons about a very small dragon."),
    ],
}
PROGRAMME_MINUTES = (30, 60, 90)

# Three genres of twelve films (title, year, rating, plot).
MOVIE_GENRES: dict[str, list[tuple[str, int, float, str]]] = {
    "Drama": [
        ("Northwind Crossing", 2024, 7.6, "A ferry captain's last winter crossing."),
        ("Quiet Pier", 2021, 6.9, "Two strangers share a bench every morning."),
        ("Saltwater Letters", 2023, 7.8, "Letters found in a sea chest change a family."),
        ("The Glass Lighthouse", 2019, 7.1, "An architect returns to the town she left."),
        ("Copper Tide", 2022, 6.7, "A fishing village bets everything on one season."),
        ("Winter Orchard Road", 2020, 7.3, "Three sisters keep their father's orchard alive."),
        ("The Night Cartographer", 2025, 8.0, "A mapmaker charts a city that keeps changing."),
        ("A House on Gull Dune", 2018, 6.5, "A summer house, a storm and a secret."),
        ("Embers of Marrow Bay", 2023, 7.0, "A fire brigade of four guards a quiet bay."),
        ("The Tidekeeper", 2024, 7.4, "The man who opens the sluice gates every night."),
        ("Slow River Summer", 2017, 6.8, "A boy and his grandmother sail a canal boat home."),
        ("Paper Lantern Hill", 2022, 7.2, "A town lights its hill for one last festival."),
    ],
    "Comedy": [
        ("The Foghorn Affair", 2023, 6.9, "A stolen foghorn and a very loud wedding."),
        ("Gull Patrol", 2022, 6.4, "Two wardens versus one clever seagull."),
        ("Tidewater Picnic", 2021, 6.6, "A family picnic, a rising tide and no plan B."),
        ("Two Left Anchors", 2024, 7.1, "The worst sailing team in the regatta."),
        ("The Accidental Harbourmaster", 2020, 7.0, "A tourist is mistaken for the new boss."),
        ("Pudding Island", 2019, 6.2, "A bakery war on an island of 300 people."),
        ("Dockside Dog Days", 2023, 6.8, "A dog walker loses eleven dogs in one afternoon."),
        ("The Great Kite Contest", 2025, 7.3, "A retired pilot builds a kite to win it all."),
        ("Too Many Uncles", 2018, 6.1, "Seven uncles, one christening, no seating plan."),
        ("Bicycle for Four", 2022, 6.7, "Four friends, one tandem, a long road south."),
        ("Soup Club", 2021, 7.2, "A Tuesday soup club becomes the town's newsroom."),
        ("Mayor of Nowhere Pier", 2024, 6.9, "An election with exactly two voters."),
    ],
    "Documentary": [
        ("Salt Roads", 2024, 7.8, "Caravans, coastlines and the people who move salt."),
        ("Kingdom of Kelp", 2023, 8.1, "A year in an underwater forest."),
        ("The Clockmenders", 2021, 7.4, "The last workshop that repairs tower clocks."),
        ("Night Shift at the Port", 2022, 7.0, "Cranes, ships and the crews who never sleep."),
        ("Bees on the Rooftops", 2020, 7.5, "City beekeepers and their million neighbours."),
        ("The Map Room", 2019, 7.2, "How old maps were drawn, copied and argued over."),
        ("Stone and Sea", 2025, 7.9, "Dry-stone walls that hold back the Atlantic."),
        ("Keepers of the Light", 2018, 7.3, "The families who lived in lighthouses."),
        ("Small Islands, Big Weather", 2023, 7.6, "Life on islands where storms set the clock."),
        ("The Bread Ovens of Vell", 2022, 6.9, "A village that still bakes in one shared oven."),
        ("Wind Machines", 2021, 7.1, "From windmills to turbines along one coast."),
        ("Lichen: A Slow Life", 2024, 7.7, "The quietest living thing on the rocks."),
    ],
}
MOVIE_HUES = {"Drama": 0.60, "Comedy": 0.10, "Documentary": 0.33}
HALF_WATCHED = "Salt Roads"  # the seeded resume point: Continue and Resume show it
HALF_WATCHED_MS = (37 * 60 + 12) * 1000
HALF_WATCHED_LENGTH_MS = 98 * 60 * 1000
# Season 1's first episode watched, its second half watched: the series view shows both.
EPISODES_WATCHED = {(1, 1): 0.97, (1, 2): 0.4}

SERIES_TITLE = "Lantern Street"
SERIES_GENRE = "Mystery"
SERIES_PLOT = (
    "On a street lit only by lanterns, a retired postwoman and a night-shift baker solve the "
    "small mysteries nobody else notices."
)
SEASONS: dict[int, list[tuple[str, int, str]]] = {
    1: [
        ("Arrival", 44, "A lantern goes dark on the night a stranger moves in."),
        ("Low Tide", 42, "A bicycle is found at the bottom of the harbour."),
        ("The Baker's Alibi", 46, "Someone has been delivering bread before dawn."),
        ("Letters Without Stamps", 43, "Envelopes appear in every letterbox but one."),
        ("Fog Week", 45, "The street's clocks all stop at ten past four."),
        ("The Last Lantern", 51, "The stranger's secret, and a party on the pier."),
    ],
    2: [
        ("Return Ticket", 44, "A train ticket from 1987 turns up in a coat pocket."),
        ("The Quiet Shop", 43, "A shop that is never open sells out overnight."),
        ("Gulls at Midnight", 45, "Every gull in town gathers on one roof."),
        ("A Map in the Flour", 42, "The bakery's flour sacks hide a hand-drawn map."),
        ("Two Keys", 47, "Two neighbours own a key to the same door."),
        ("Lights Out", 52, "The whole street goes dark for the winter fair."),
    ],
}


def channels() -> list[tuple[int, str, str]]:
    """(number, name, group) for the 24 channels, in playlist order."""
    numbered: list[tuple[int, str, str]] = []
    for group, names in CHANNEL_GROUPS.items():
        for name in names:
            numbered.append((len(numbered) + 1, name, group))
    return numbered


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def initials(name: str) -> str:
    """ "Northwind News" → "NN", "Harbor Sports 1" → "HS1"."""
    words = name.split()
    letters = "".join(w[0] for w in words if w[0].isalpha())[:2]
    digits = "".join(w for w in words if w.isdigit())
    return (letters + digits).upper()


def movies() -> list[dict[str, Any]]:
    """The films as the demo server lists them (base URL left out: see Catalog)."""
    found: list[dict[str, Any]] = []
    for genre_index, (genre, titles) in enumerate(MOVIE_GENRES.items()):
        for title, year, rating, plot in titles:
            stream_id = 1001 + len(found)
            found.append(
                {
                    "stream_id": stream_id,
                    "name": title,
                    "category_id": str(genre_index + 1),
                    "genre": genre,
                    "year": year,
                    "rating": rating,
                    "plot": plot,
                }
            )
    return found


# ---- images (QImage/QPainter: a QGuiApplication must exist) -------------------------------------


def _app() -> Any:
    """The running Qt application, else a new offscreen one (no window is ever shown)."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication([sys.argv[0]])
        if sys.platform == "darwin":
            from PySide6.QtGui import QFont

            app.setFont(QFont(".AppleSystemUIFont", 13))  # what the app gets on a Mac's screen
    return app


def _hsv(hue: float, saturation: float, value: float) -> Any:
    from PySide6.QtGui import QColor

    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, saturation, value)
    return QColor(round(red * 255), round(green * 255), round(blue * 255))


def draw_logo(name: str, hue: float, size: int = 256) -> Any:
    """A channel logo: its initials on a coloured square, with a small wave under them."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen

    _app()
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, _hsv(hue, 0.55, 0.92))
    gradient.setColorAt(1.0, _hsv(hue + 0.04, 0.75, 0.62))
    painter.fillRect(0, 0, size, size, gradient)
    font = QFont()
    font.setBold(True)
    text = initials(name)
    font.setPixelSize(round(size * (0.42 if len(text) <= 2 else 0.34)))
    painter.setFont(font)
    painter.setPen(_hsv(0, 0, 1.0))
    painter.drawText(QRectF(0, size * 0.12, size, size * 0.6), Qt.AlignmentFlag.AlignCenter, text)
    wave = QPainterPath()
    y = size * 0.78
    wave.moveTo(size * 0.22, y)
    for step in range(4):
        x = size * (0.22 + 0.14 * step)
        wave.quadTo(x + size * 0.035, y - size * 0.05, x + size * 0.07, y)
        wave.quadTo(x + size * 0.105, y + size * 0.05, x + size * 0.14, y)
    pen = QPen(_hsv(0, 0, 1.0))
    pen.setWidthF(size * 0.035)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawPath(wave)
    painter.end()
    return image


def draw_poster(title: str, hue: float, seed: int, size=(400, 600)) -> Any:
    """A 2:3 poster: a two-colour gradient, a sun or moon over a horizon, and the title.

    The bottom tenth stays empty: the Library draws its progress bar there.
    """
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QFont, QFontMetricsF, QImage, QLinearGradient, QPainter

    _app()
    rng = random.Random(seed)
    width, height = size
    image = QImage(width, height, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    shift = rng.uniform(-0.12, 0.12)
    sky = QLinearGradient(0, 0, 0, height)
    sky.setColorAt(0.0, _hsv(hue + shift, 0.55, 0.55))
    sky.setColorAt(0.65, _hsv(hue + shift + 0.06, 0.65, 0.30))
    sky.setColorAt(1.0, _hsv(hue + shift + 0.1, 0.7, 0.12))
    painter.fillRect(0, 0, width, height, sky)
    horizon = height * rng.uniform(0.48, 0.6)
    radius = width * rng.uniform(0.12, 0.2)
    centre = QPointF(width * rng.uniform(0.3, 0.7), horizon - radius * rng.uniform(0.2, 1.1))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_hsv(hue + 0.5 * rng.random(), 0.25, 0.95))
    painter.drawEllipse(centre, radius, radius)
    for band in range(4):  # the hills or waves below the horizon
        painter.setBrush(_hsv(hue + shift, 0.6, 0.30 - band * 0.06))
        top = horizon + band * height * 0.07
        painter.drawRect(QRectF(0, top, width, height - top))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(round(width * 0.105))
    painter.setFont(font)
    metrics = QFontMetricsF(font)
    lines: list[str] = []
    for word in title.split():  # greedy wrap to the poster's width
        candidate = f"{lines[-1]} {word}" if lines else word
        if lines and metrics.horizontalAdvance(candidate) <= width * 0.84:
            lines[-1] = candidate
        else:
            lines.append(word)
    line_height = metrics.height() * 1.02
    top = height * 0.86 - line_height * len(lines)
    painter.setPen(_hsv(0, 0, 0.97))
    for row, line in enumerate(lines):
        painter.drawText(
            QRectF(width * 0.08, top + row * line_height, width * 0.84, line_height),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            line,
        )
    painter.end()
    return image


def _save(image: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise OSError(f"Could not write {path}")


# ---- the files ----------------------------------------------------------------------------------


def logo_name(number: int) -> str:
    return f"channel-{number:02d}.png"


def poster_name(kind: str, key: int) -> str:
    return f"{kind}-{key}.png"


def write_images(out: Path) -> None:
    for number, name, group in channels():
        hue = GROUP_HUES[group] + (number % 6) * 0.025
        _save(draw_logo(name, hue), out / LOGOS / logo_name(number))
    for movie in movies():
        poster = draw_poster(movie["name"], MOVIE_HUES[movie["genre"]], movie["stream_id"])
        _save(poster, out / POSTERS / poster_name("movie", movie["stream_id"]))
    _save(draw_poster(SERIES_TITLE, 0.12, 201), out / POSTERS / poster_name("series", 201))


def write_playlist(out: Path, base_url: str) -> Path:
    lines = ["#EXTM3U"]
    for number, name, group in channels():
        lines.append(
            f'#EXTINF:-1 tvg-id="{slug(name)}.demo" tvg-logo="{base_url}/{LOGOS}/'
            f'{logo_name(number)}" group-title="{group}",{name}'
        )
        lines.append(f"{base_url}/live/{number}.ts")
    path = out / PLAYLIST
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _xmltv_time(moment: datetime) -> str:
    """XMLTV's time in this computer's offset, as most guides give it."""
    return moment.astimezone().strftime("%Y%m%d%H%M%S %z")


def guide_programmes(now: datetime) -> dict[str, list[tuple[datetime, datetime, str, str]]]:
    """Per channel id, (start, stop, title, description) from GUIDE_FROM_H before `now` to
    GUIDE_TO_H after it, on half-hour boundaries. The same channel always gets the same run."""
    start_of = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_of -= timedelta(hours=GUIDE_FROM_H)
    end = now + timedelta(hours=GUIDE_TO_H)
    found = {}
    for number, name, group in channels():
        rng = random.Random(number)
        shows = PROGRAMMES[group]
        offset = rng.randrange(len(shows))
        at = start_of + timedelta(minutes=30 * rng.randrange(2))
        listing = (
            [(start_of, at, shows[offset - 1][0], shows[offset - 1][1])] if at > start_of else []
        )
        index = offset
        while at < end:
            title, description = shows[index % len(shows)]
            length = timedelta(minutes=rng.choice(PROGRAMME_MINUTES))
            listing.append((at, at + length, title, description))
            at += length
            index += 1 + rng.randrange(2)
        found[f"{slug(name)}.demo"] = listing
    return found


def write_guide(out: Path, now: datetime) -> Path:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="QiTV demo">']
    names = {f"{slug(name)}.demo": name for _number, name, _group in channels()}
    for channel_id, name in names.items():
        lines.append(f'  <channel id="{channel_id}"><display-name>{escape(name)}</display-name>')
        lines.append("  </channel>")
    for channel_id, listing in guide_programmes(now).items():
        for start, stop, title, description in listing:
            lines.append(
                f'  <programme start="{_xmltv_time(start)}" stop="{_xmltv_time(stop)}" '
                f'channel="{channel_id}"><title lang="en">{escape(title)}</title>'
                f'<desc lang="en">{escape(description)}</desc></programme>'
            )
    lines.append("</tv>")
    path = out / GUIDE
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class Catalog:
    """The demo library as the Xtream player API answers it, at `base_url`."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.vod_categories = [
            {"category_id": str(index + 1), "category_name": genre, "parent_id": 0}
            for index, genre in enumerate(MOVIE_GENRES)
        ]
        self.vod_streams = [
            {
                "num": index + 1,
                "name": movie["name"],
                "stream_type": "movie",
                "stream_id": movie["stream_id"],
                "stream_icon": self.poster("movie", movie["stream_id"]),
                "rating": f"{movie['rating']:.1f}",
                "rating_5based": round(movie["rating"] / 2, 1),
                "added": str(1_760_000_000 + movie["stream_id"] * 3600),
                "category_id": movie["category_id"],
                "container_extension": "mp4",
                "plot": movie["plot"],
                "releasedate": f"{movie['year']}-05-01",
            }
            for index, movie in enumerate(movies())
        ]
        self.series_categories = [{"category_id": "21", "category_name": SERIES_GENRE}]
        cover = self.poster("series", 201)
        self.series = [
            {
                "num": 1,
                "name": SERIES_TITLE,
                "series_id": 201,
                "cover": cover,
                "plot": SERIES_PLOT,
                "genre": SERIES_GENRE,
                "year": "2025",
                "rating": "8.2",
                "category_id": "21",
            }
        ]
        episodes: dict[str, list[dict[str, Any]]] = {}
        for season, listing in SEASONS.items():
            episodes[str(season)] = [
                {
                    "id": str(3000 + season * 100 + number),
                    "episode_num": number,
                    "title": title,
                    "season": season,
                    "container_extension": "mp4",
                    "info": {
                        "duration_secs": minutes * 60,
                        "duration": f"00:{minutes:02d}:00",
                        "plot": plot,
                    },
                }
                for number, (title, minutes, plot) in enumerate(listing, start=1)
            ]
        self.series_info = {
            "seasons": [
                {"season_number": season, "name": f"Season {season}", "cover": cover}
                for season in SEASONS
            ],
            "info": {"name": SERIES_TITLE, "plot": SERIES_PLOT, "genre": SERIES_GENRE},
            "episodes": episodes,
        }

    def poster(self, kind: str, key: int) -> str:
        return f"{self.base_url}/{POSTERS}/{poster_name(kind, key)}"

    def answer(self, query: dict[str, list[str]]) -> Any:
        """The player API's answer, or None for a wrong login or an unknown action."""
        if query.get("username") != [USERNAME] or query.get("password") != [PASSWORD]:
            return None
        action = (query.get("action") or [""])[0]
        if not action:
            return {
                "user_info": {
                    "auth": 1,
                    "status": "Active",
                    "max_connections": "2",
                    "allowed_output_formats": ["ts"],
                },
                "server_info": {},
            }
        if action == "get_series_info":
            wanted = (query.get("series_id") or [""])[0]
            return self.series_info if wanted == "201" else {}
        return {
            "get_live_categories": [],
            "get_live_streams": [],
            "get_vod_categories": self.vod_categories,
            "get_vod_streams": self.vod_streams,
            "get_series_categories": self.series_categories,
            "get_series": self.series,
        }.get(action)

    def movie_item(self, title: str) -> tuple[dict[str, Any], str]:
        """A film as the Library stores it (services/workers.py), and its stream URL."""
        stream = next(s for s in self.vod_streams if s["name"] == title)
        cmd = f"{self.base_url}/movie/{USERNAME}/{PASSWORD}/{stream['stream_id']}.mp4"
        item = {
            "id": stream["stream_id"],
            "number": str(stream["num"]),
            "name": title,
            "logo": stream["stream_icon"],
            "tv_genre_id": stream["category_id"],
            "cmd": cmd,
            "director": "",
            "description": stream["plot"],
            "rating": stream["rating"],
            "year": stream["releasedate"],
            "container_extension": "mp4",
        }
        return item, cmd


def providers(out: Path, base_url: str) -> list[dict[str, Any]]:
    return [
        {"type": "M3UPLAYLIST", "name": TV_PROVIDER, "url": str((out / PLAYLIST).resolve())},
        {
            "type": "XTREAM",
            "name": LIBRARY_PROVIDER,
            "url": base_url,
            "username": USERNAME,
            "password": PASSWORD,
        },
    ]


def write_config(config_dir: Path, out: Path, base_url: str, now: datetime) -> None:
    """A QiTV settings folder (QITV_CONFIG_DIR) for the demo: providers, guide, favourites."""
    index = config_dir / "cache" / "provider" / "index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps(providers(out, base_url), indent=2), encoding="utf-8")
    item, cmd = Catalog(base_url).movie_item(HALF_WATCHED)
    content_id = f"{LIBRARY_PROVIDER}:{item['id']}"
    stamp = (now - timedelta(days=1)).isoformat()
    positions = {
        content_id: {
            "position": HALF_WATCHED_MS,
            "duration": HALF_WATCHED_LENGTH_MS,
            "timestamp": stamp,
        }
    }
    for (season, number), share in EPISODES_WATCHED.items():
        minutes = SEASONS[season][number - 1][1]
        episode_id = 3000 + season * 100 + number  # services.watch_state's episode key
        positions[f"{LIBRARY_PROVIDER}:{episode_id}:s{season}:e{number}"] = {
            "position": round(minutes * 60_000 * share),
            "duration": minutes * 60_000,
            "timestamp": stamp,
        }
    width, height = SCREENSHOT_SIZE
    config = {
        "selected_provider_name": TV_PROVIDER,
        "check_updates": False,
        "epg_source": "Local File",
        "epg_file": str((out / GUIDE).resolve()),
        "favorites": FAVORITES,
        "start_with_last_channel": False,
        "window_positions": {"tv_window": {"x": 40, "y": 40, "width": width, "height": height}},
        "watch_history": [
            {
                "content_id": content_id,
                "item_data": item,
                "item_type": "movie",
                "link": cmd,
                "provider_name": LIBRARY_PROVIDER,
                "timestamp": stamp,
                "name": HALF_WATCHED,
            }
        ],
        "playback_positions": positions,
    }
    (config_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")


def generate(out: Path, base_url: str, now: Optional[datetime] = None) -> None:
    """Make everything under `out` (module docstring) for a server at `base_url`."""
    now = now or datetime.now(timezone.utc)
    out.mkdir(parents=True, exist_ok=True)
    write_images(out)
    write_playlist(out, base_url)
    write_guide(out, now)
    config_dir = out / CONFIG
    if config_dir.exists():
        shutil.rmtree(config_dir)  # a stale image or provider cache would outlive the content
    write_config(config_dir, out, base_url, now)
    logger.info("Demo content for %s written to %s", base_url, out)


# ---- the server ---------------------------------------------------------------------------------

_LIVE = re.compile(r"^/live/(\d+)\.ts$")
_IMAGE = re.compile(rf"^/({LOGOS}|{POSTERS})/([a-z0-9-]+\.png)$")
_MEDIA = re.compile(rf"^/(movie|series)/{USERNAME}/{PASSWORD}/\d+\.mp4$")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


class DemoServer:
    """Serves `out` on 127.0.0.1: live channels, images, the player API and its media."""

    def __init__(self, out: Path, port: int = 0) -> None:
        self.out = out
        self._stop = threading.Event()
        live = LoopedStream(LIVE_TS.read_bytes())
        media = SMOKE_MP4.read_bytes()
        numbers = {number for number, _name, _group in channels()}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                path = parts.path
                live_match, image = _LIVE.match(path), _IMAGE.match(path)
                if live_match and int(live_match.group(1)) in numbers:
                    self._live()
                elif image:
                    file = owner.out / image.group(1) / image.group(2)
                    if file.is_file():
                        self._send(200, "image/png", file.read_bytes())
                    else:
                        self._send(404, "text/plain", b"")
                elif path == "/player_api.php":
                    answer = owner.catalog.answer(parse_qs(parts.query))
                    if answer is None:
                        self._send(404, "application/json", b"{}")
                    else:
                        self._send(200, "application/json", json.dumps(answer).encode())
                elif path == "/xmltv.php":
                    guide = owner.out / GUIDE
                    self._send(200, "application/xml", guide.read_bytes())
                elif _MEDIA.match(path):
                    self._media(media)
                else:
                    self._send(404, "text/plain", b"")

            def _live(self) -> None:
                """The clip as an endless real-time stream (tests/fixtures/live_source.py)."""
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Connection", "close")
                self.end_headers()
                started = time.monotonic()
                for due, chunk in live.chunks():
                    if not self._wait(started + due):
                        return
                    try:
                        self.wfile.write(chunk)
                    except OSError:
                        return

            def _wait(self, until: float) -> bool:
                while not owner._stop.is_set():
                    left = until - time.monotonic()
                    if left <= 0:
                        return True
                    readable, _, _ = select.select([self.connection], [], [], min(left, 0.05))
                    if not readable:
                        continue
                    try:
                        if not self.connection.recv(4096):
                            return False
                    except OSError:
                        return False
                return False

            def _media(self, body: bytes) -> None:
                match = _RANGE.fullmatch(self.headers.get("Range", ""))
                if match is None:
                    self._send(200, "video/mp4", body)
                    return
                first = int(match.group(1) or 0)
                last = min(int(match.group(2)) if match.group(2) else len(body) - 1, len(body) - 1)
                headers = {"Content-Range": f"bytes {first}-{last}/{len(body)}"}
                self._send(206, "video/mp4", body[first : last + 1], headers)

            def _send(
                self, status: int, kind: str, body: bytes, headers: Optional[dict] = None
            ) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Accept-Ranges", "bytes")
                    for name, value in (headers or {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the client went away (a seek, a stop)

        self._server = ThreadingLoopbackHTTPServer((HOST, port), Handler)
        self.url = f"http://{HOST}:{self._server.server_port}"
        self.catalog = Catalog(self.url)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(3)

    def __enter__(self) -> "DemoServer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ---- no network ---------------------------------------------------------------------------------


def _is_loopback(host: Any) -> bool:
    if host is None:
        return True
    name = (host.decode() if isinstance(host, bytes) else str(host)).strip("[]").lower()
    return name in ("", "localhost", "::1") or name.startswith("127.")


def refuse_network(attempts: list[str]) -> Callable[[], None]:
    """Refuse (and record) Python-level lookups and connections beyond this machine.

    Returns the function that restores the socket module.
    """
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def refuse(target: str) -> None:
        attempts.append(target)
        raise OSError(f"the demo has no network access ({target})")

    def connect(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0]):
            refuse(f"connect {address[0]}:{address[1]}")
        real_connect(sock, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(host):
            refuse(f"lookup {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    setattr(socket.socket, "connect", connect)
    setattr(socket, "getaddrinfo", getaddrinfo)

    def restore() -> None:
        setattr(socket.socket, "connect", real_connect)
        setattr(socket, "getaddrinfo", real_getaddrinfo)

    return restore


# ---- screenshots --------------------------------------------------------------------------------

# Named for the README, numbered in the brief's order (they are taken in another).
SHOT_BANNER = "01-live-banner.png"
SHOT_COLUMN = "02-fast-zap.png"
SHOT_DRAWER = "03-drawer.png"
SHOT_TIMESHIFT = "04-timeshift.png"
SHOT_LIBRARY = "05-library.png"
SHOT_SERIES = "06-series.png"
SHOT_GUIDE = "07-guide.png"
SHOT_PIP = "08-pip.png"
SHOTS = (
    SHOT_BANNER,
    SHOT_COLUMN,
    SHOT_DRAWER,
    SHOT_TIMESHIFT,
    SHOT_LIBRARY,
    SHOT_SERIES,
    SHOT_GUIDE,
    SHOT_PIP,
)
FRAME_SIZE = (1280, 720)  # what libVLC scales the decoded video to for the screenshots
# No sound and no video window (the pictures go to FrameTap), and the test pattern's top
# rows cut off: its timecode would sit under the window's own text.
VLC_ARGS = [
    "--aout=dummy",
    "--vout=dummy",
    "--video-filter=croppadd",
    "--croppadd-croptop=40",
]
# libVLC's adjust filter per player: the test pattern's colours toned down, and the second
# player's turned, so that the small picture stands out from the large one.
TONES = (
    {"Saturation": 0.55, "Contrast": 0.95, "Brightness": 0.9},
    {"Saturation": 0.6, "Contrast": 0.95, "Brightness": 0.95, "Hue": 150.0},
)
POLL_MS = 50
OPEN_TIMEOUT_MS = 30_000
LOAD_TIMEOUT_MS = 20_000
BUFFER_MS = 14_000  # history behind the live edge before the pause
PAUSE_MS = 9_000  # how far behind live the time-shift screenshot is
SETTLE_MS = 600  # overlays fade in and posters swap in
OFFSCREEN_NOISE = "This plugin does not support"  # what the offscreen platform cannot do
_SCREENS = {
    "screens": [
        {
            "name": "Demo",
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
            "logicalDpi": 96,
            "logicalBaseDpi": 96,
            "dpr": 1,
        }
    ]
}


class FrameTap:
    """Keeps the newest picture a libVLC player decoded (its memory output: no window)."""

    def __init__(self, player: Any, vlc: Any, size: tuple[int, int] = FRAME_SIZE) -> None:
        import ctypes

        self.width, self.height = size
        self._buffer = ctypes.create_string_buffer(self.width * self.height * 4)
        self._address = ctypes.addressof(self._buffer)
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None
        decorators = vlc.CallbackDecorators

        @decorators.VideoLockCb
        def lock(_opaque: Any, planes: Any) -> None:
            planes[0] = self._address
            return None

        @decorators.VideoUnlockCb
        def unlock(_opaque: Any, _picture: Any, _planes: Any) -> None:
            return None

        @decorators.VideoDisplayCb
        def display(_opaque: Any, _picture: Any) -> None:
            with self._lock:
                self._frame = self._buffer.raw

        self._callbacks = (lock, unlock, display)  # libVLC calls them: keep them alive
        player.video_set_callbacks(lock, unlock, display, None)
        player.video_set_format("RV32", self.width, self.height, self.width * 4)

    def image(self) -> Any:
        """The newest picture as a QImage, else None."""
        from PySide6.QtGui import QImage

        with self._lock:
            frame = self._frame
        if frame is None:
            return None
        return QImage(
            frame, self.width, self.height, self.width * 4, QImage.Format.Format_RGB32
        ).copy()

    def has_frame(self) -> bool:
        with self._lock:
            return self._frame is not None


class Shooter:
    """Drives the TV window through the screenshots; each yield is a delay in ms."""

    def __init__(self, window: Any, server: DemoServer, shots: Path, taps: dict) -> None:
        self.window = window
        self.server = server
        self.shots = shots
        self.taps = taps  # PlaybackSession -> FrameTap
        self.saved: list[str] = []
        self.problems: list[str] = []
        self._steps = self._scenario()

    # ---- stepping

    def start(self) -> None:
        from PySide6.QtCore import QTimer

        def run() -> None:
            try:
                delay = next(self._steps)
            except StopIteration:
                return
            except Exception as e:
                logger.exception("The screenshots stopped")
                self.problems.append(f"stopped: {e!r}")
                self.window.close()
                return
            QTimer.singleShot(delay, run)

        QTimer.singleShot(0, run)

    def _wait(
        self, predicate: Callable[[], bool], timeout_ms: int, what: str
    ) -> Generator[int, None, bool]:
        deadline = time.monotonic() + timeout_ms / 1000
        while not predicate():
            if time.monotonic() >= deadline:
                self.problems.append(f"timed out waiting for {what}")
                logger.warning("Timed out waiting for %s", what)
                return False
            yield POLL_MS
        return True

    def _key(self, key: Any, modifier: Any = None) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        QTest.keyClick(self.window, key, modifier or Qt.KeyboardModifier.NoModifier)

    def _move_mouse(self) -> None:
        """A mouse move over the middle of the video (the pointer itself stays put)."""
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication

        surface = self.window.surface
        local = QPointF(surface.width() / 2, surface.height() / 2)
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            local,
            QPointF(surface.mapToGlobal(local)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(surface, event)

    # ---- what the window shows

    def _playing(self, session: Any) -> bool:
        from services.playback import PlaybackState

        tap = self.taps.get(session)
        return session.state() is PlaybackState.PLAYING and tap is not None and tap.has_frame()

    def _logos_loaded(self, names: Iterable[str]) -> bool:
        base = self.server.url
        wanted = {name for name in names}
        urls = [
            f"{base}/{LOGOS}/{logo_name(number)}"
            for number, name, _group in channels()
            if name in wanted
        ]
        return all(self.window.logos.pixmap(url) is not None for url in urls)

    def _grid_items(self, limit: int = 14) -> list[dict]:
        """The Library grid's first titles (as many as show on the first lines)."""
        grid = self.window.library_page.grid
        items = (grid.item(row) for row in range(limit))
        return [item for item in items if item is not None]

    def _posters_loaded(self) -> bool:
        from ui.library.poster_grid import poster_url

        items, posters = self._grid_items(), self.window.posters
        return bool(items) and all(posters.pixmap(poster_url(item)) is not None for item in items)

    def _video_layers(self) -> list[tuple[Any, Any]]:
        """(the picture, where it shows in the window) for each player, the large one first."""
        from PySide6.QtCore import QPoint, QRect

        from services.playback import PlaybackState

        w = self.window
        page, sessions = w.player_page, w.sessions
        origin = page.mapTo(w, QPoint(0, 0))
        slots = [(sessions.large(), QRect(origin, page.size()))]
        small = sessions.small()
        if small is not None:
            rect = w.pip_frame.small_rect()
            slots.append((small, QRect(origin + rect.topLeft(), rect.size())))
        layers = []
        for slot, target in slots:
            tap = self.taps.get(slot.playback)
            # libVLC keeps showing the last picture while a zap opens the next channel.
            shown = slot.playback.state() not in (
                PlaybackState.IDLE,
                PlaybackState.ENDED,
                PlaybackState.ERROR,
            )
            frame = tap.image() if tap is not None and shown else None
            if frame is not None and not target.isEmpty():
                layers.append((frame, target))
        return layers

    def capture(self, name: str) -> None:
        """The window's grab, each player's decoded picture where it shows, and the overlay
        host's grab over them (as scripts/smoke_tv.py composes its screenshots). The player
        page shows full size, or in the Library's corner while what plays goes on there."""
        from PySide6.QtCore import QPoint, QRectF
        from PySide6.QtGui import QImage, QPainter

        w = self.window
        pixmap = w.grab()
        image = QImage(pixmap.size(), QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(pixmap.devicePixelRatio())
        image.fill(0xFF000000)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(0, 0, pixmap)
        if w.player_page.isVisible():
            for frame, target in self._video_layers():
                painter.drawImage(QRectF(target), frame)
            host = w.host
            if host.isVisible():
                painter.drawPixmap(w.mapFromGlobal(host.mapToGlobal(QPoint(0, 0))), host.grab())
        painter.end()
        if image.size().toTuple() != SCREENSHOT_SIZE:
            self.problems.append(f"{name} is {image.size().toTuple()}, not {SCREENSHOT_SIZE}")
        path = self.shots / name
        if not image.save(str(path), "PNG"):
            self.problems.append(f"could not write {path}")
            return
        self.saved.append(name)
        logger.info("Saved %s", path)

    def _tone(self) -> None:
        """Apply TONES to each player that has a picture (the filter needs its video output).

        Never while a worker ends a player's input: that holds the lock these calls take.
        """
        from services import playback

        vlc = playback.vlc
        w = self.window
        for session, tone in zip((w.session, w.pip_session), TONES):
            player = getattr(session, "_player", None)
            if player is None or getattr(session, "_job", None) is not None:
                continue
            if not player.has_vout():
                continue
            for option, value in tone.items():
                player.video_set_adjust_float(getattr(vlc.VideoAdjustOption, option), value)
            player.video_set_adjust_int(vlc.VideoAdjustOption.Enable, 1)

    def _shoot(self, name: str) -> Generator[int, None, None]:
        """Tone the pictures, wait for the next frames, capture."""
        self._tone()
        yield 400
        self.capture(name)

    # ---- moves

    def _highlighted(self, attribute: str) -> Any:
        return getattr(self.window.overlays.drawer.highlighted(), attribute, None)

    def _pick_genre(self, genre_id: str) -> Generator[int, None, None]:
        """From the Library's grid: ← to the genres, down to `genre_id`, Enter."""
        from PySide6.QtCore import Qt

        genres = self.window.library_page.genres
        self._key(Qt.Key.Key_Left)
        yield 200
        for _ in range(16):
            if genres.current_id() == genre_id:
                break
            self._key(Qt.Key.Key_Down)
            yield 150
        self._key(Qt.Key.Key_Return)
        yield from self._wait(
            lambda: str((self._grid_items(1) or [{}])[0].get("tv_genre_id")) == genre_id,
            LOAD_TIMEOUT_MS,
            f"genre {genre_id}",
        )

    def _pick_title(self, title: str) -> Generator[int, None, None]:
        """Move the grid's highlight to `title` with the arrow keys."""
        from PySide6.QtCore import Qt

        grid = self.window.library_page.grid
        names = [str(item.get("name")) for item in self._grid_items(64)]
        if title not in names:
            self.problems.append(f"{title} is not in the grid")
            return
        target, columns = names.index(title), max(1, grid.columns())
        current = max(0, grid.current_index())
        rows = target // columns - current // columns
        for _ in range(abs(rows)):
            self._key(Qt.Key.Key_Down if rows > 0 else Qt.Key.Key_Up)
            yield 150
        current = max(0, grid.current_index())
        steps = target - current
        for _ in range(abs(steps)):
            self._key(Qt.Key.Key_Right if steps > 0 else Qt.Key.Key_Left)
            yield 150

    # ---- the scenario

    def _scenario(self) -> Generator[int, None, None]:
        from PySide6.QtCore import Qt

        from services.playback import PlaybackState

        w = self.window
        K = Qt.Key
        drawer, overlays, sessions = w.overlays.drawer, w.overlays, w.sessions
        w.menuBar().hide()  # on macOS the menu bar is the screen's, not the window's
        w.setGeometry(40, 40, *SCREENSHOT_SIZE)  # after the start fitted it to the screen
        yield from self._wait(drawer.isVisible, LOAD_TIMEOUT_MS, "the drawer at startup")
        yield from self._wait(w.catalog.epg_ready, LOAD_TIMEOUT_MS, "the guide")
        yield from self._wait(
            lambda: self._highlighted("layer") == "group", LOAD_TIMEOUT_MS, "the groups"
        )

        # Into News (the groups are sorted, after Favorites and All) and its first channel.
        for _ in range(8):
            if self._highlighted("title") == "News":
                break
            self._key(K.Key_Down)
            yield 250
        self._key(K.Key_Return)
        yield from self._wait(lambda: self._highlighted("layer") == "item", 3000, "the channels")
        self._key(K.Key_Return)
        yield from self._wait(lambda: self._playing(w.session), OPEN_TIMEOUT_MS, "a channel")

        # 1. Down zaps to the next channel: the banner (shown again once the video plays).
        self._key(K.Key_Down)
        yield 500
        yield from self._wait(lambda: self._playing(w.session), OPEN_TIMEOUT_MS, "the zap")
        yield 1500
        self._key(K.Key_I)
        yield SETTLE_MS
        yield from self._shoot(SHOT_BANNER)

        # 2. Two quick downs: the fast-zap column (caught before it tunes, 400 ms on).
        self._tone()
        self._key(K.Key_Escape)
        yield 300
        self._key(K.Key_Down)
        yield 120
        self._key(K.Key_Down)
        yield 200
        yield from self._wait(overlays.column.isVisible, 2000, "the fast-zap column")
        self.capture(SHOT_COLUMN)
        yield from self._wait(lambda: not overlays.column.isVisible(), 8000, "the column to close")
        yield from self._wait(lambda: self._playing(w.session), OPEN_TIMEOUT_MS, "its channel")

        # 3. The drawer over the channel: the groups, and the group's channels.
        yield from self._wait(lambda: not overlays.banner.isVisible(), 8000, "the banner")
        self._key(K.Key_Return)
        yield from self._wait(drawer.isVisible, 3000, "the drawer")
        yield from self._wait(
            lambda: self._logos_loaded(CHANNEL_GROUPS["News"]), LOAD_TIMEOUT_MS, "the logos"
        )
        yield SETTLE_MS
        yield from self._shoot(SHOT_DRAWER)
        self._key(K.Key_Escape)
        yield from self._wait(lambda: not drawer.isVisible(), 3000, "the drawer to close")

        # 4. Time-shift: some history, a pause, then on behind live with the transport up.
        yield BUFFER_MS
        self._key(K.Key_Space)
        yield from self._wait(lambda: w.session.state() is PlaybackState.PAUSED, 3000, "the pause")
        yield PAUSE_MS
        self._key(K.Key_Space)
        yield from self._wait(
            lambda: w.session.state() is PlaybackState.PLAYING, 3000, "playing again"
        )
        yield 1500  # a fresh frame and the timeline's next reports
        self._move_mouse()  # the transport shows (and stays a while)
        yield from self._wait(overlays.transport.isVisible, 2000, "the transport")
        yield SETTLE_MS
        yield from self._shoot(SHOT_TIMESHIFT)
        self._key(K.Key_L)  # back to live
        yield 2500

        # 7. The guide.
        overlays.transport.dismiss()
        self._key(K.Key_G)
        yield from self._wait(overlays.guide.isVisible, 3000, "the guide")
        yield SETTLE_MS
        yield from self._shoot(SHOT_GUIDE)
        self._key(K.Key_Escape)
        yield 500

        # 8. Picture-in-picture with the channel before, and the mixer.
        self._key(K.Key_P)
        yield from self._wait(
            lambda: sessions.small() is not None and self._playing(sessions.small().playback),
            OPEN_TIMEOUT_MS,
            "the small picture",
        )
        yield 1500
        self._key(K.Key_Minus)  # the mixer shows both pictures' levels
        yield SETTLE_MS
        yield from self._shoot(SHOT_PIP)
        self._key(K.Key_P)  # closes the small picture
        yield 1000

        # 5. The Library on the demo library: a genre, the half-watched film's details, and the
        # channel playing on in the corner.
        w.controller.select_provider(LIBRARY_PROVIDER)
        yield from self._wait(
            lambda: w.catalog.current_provider_name() == LIBRARY_PROVIDER
            and not w.catalog.is_loading(),
            LOAD_TIMEOUT_MS,
            "the demo library",
        )
        w.screens.open_library("movies")
        yield from self._wait(lambda: bool(self._grid_items()), LOAD_TIMEOUT_MS, "the films")
        genre = str(1 + list(MOVIE_GENRES).index("Documentary"))
        yield from self._pick_genre(genre)
        yield from self._pick_title(HALF_WATCHED)
        yield from self._wait(self._posters_loaded, LOAD_TIMEOUT_MS, "the posters")
        yield SETTLE_MS
        yield from self._shoot(SHOT_LIBRARY)

        # 6. The series, on its half-watched episode, with the corner player stopped (×): it
        # would cover the episodes' ✓ and Start over.
        w.screens.stop_corner()
        yield 500
        w.screens.open_library("series")
        yield from self._wait(lambda: bool(self._grid_items()), LOAD_TIMEOUT_MS, "the series")
        yield from self._pick_genre("21")
        self._key(K.Key_Return)
        series = w.library_page.series
        yield from self._wait(series.isVisible, LOAD_TIMEOUT_MS, "the series view")
        yield 1500
        self._key(K.Key_Down)
        yield 1000
        yield from self._shoot(SHOT_SERIES)

        self.window.close()


def take_screenshots(out: Path, shots: Path) -> int:
    """Serve the demo, run the TV window offscreen through the scenario, save the PNGs."""
    scratch = Path(tempfile.mkdtemp(prefix="qitv-demo-"))
    screens = scratch / "screens.json"
    screens.write_text(json.dumps(_SCREENS), encoding="utf-8")
    # A screen big enough for the window (Qt's offscreen one is 800×800). The option's ":"
    # separator rules out a Windows path: there the window is sized after the start anyway.
    configured = f"offscreen:configfile={screens}" if os.name != "nt" else "offscreen"
    os.environ["QT_QPA_PLATFORM"] = configured
    os.environ["QITV_CONFIG_DIR"] = str(scratch / "config")
    attempts: list[str] = []
    restore = refuse_network(attempts)
    server = DemoServer(out)
    try:
        generate(out, server.url)
        write_config(scratch / "config", out, server.url, datetime.now(timezone.utc))
        shots.mkdir(parents=True, exist_ok=True)
        for name in SHOTS:
            (shots / name).unlink(missing_ok=True)
        code, saved, problems = _run_window(server, shots)
    finally:
        server.close()
        restore()
        shutil.rmtree(scratch, ignore_errors=True)
    missing = [name for name in SHOTS if name not in saved]
    problems += [f"missing {name}" for name in missing]
    problems += [f"network attempt: {a}" for a in attempts]
    for problem in problems:
        logger.error("%s", problem)
    logger.info("Screenshots in %s: %d of %d", shots, len(saved), len(SHOTS))
    return 0 if code == 0 and not problems else 1


def _run_window(server: DemoServer, shots: Path) -> tuple[int, list[str], list[str]]:
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtGui import QIcon
    import qdarktheme

    from services import playback
    from services.config_manager import ConfigManager
    from services.epg_manager import EpgManager
    from services.image_manager import ImageManager
    from services.provider_manager import ProviderManager
    from ui import tv_window
    from ui.tv_window import TVWindow

    app = _app()

    def quiet(_mode: Any, _context: Any, message: str) -> None:
        if not message.startswith(OFFSCREEN_NOISE):
            sys.stderr.write(f"{message}\n")

    qInstallMessageHandler(quiet)
    app.setWindowIcon(QIcon(str(ROOT / "assets" / "qitv.png")))
    if playback.vlc is None:
        return 1, [], ["libVLC is not available: install VLC"]

    def muted(extra_args: Optional[list[str]] = None) -> Any:
        return playback.create_instance([*(extra_args or playback.default_args()), *VLC_ARGS])

    tv_window.create_instance = muted  # both players stay silent
    config = ConfigManager()
    images = ImageManager(config, config.max_cache_image_size * 1024 * 1024)
    provider_manager = ProviderManager(config)
    epg = EpgManager(config, provider_manager)
    qdarktheme.setup_theme("dark", custom_colors={"primary": "#C96B43"})  # as main.py does
    window = TVWindow(app, config, provider_manager, images, epg)
    taps = {}
    for session in (window.session, window.pip_session):
        player = getattr(session, "_player", None)
        if player is not None:
            taps[session] = FrameTap(player, playback.vlc)
    shooter = Shooter(window, server, shots, taps)
    window.show()
    shooter.start()
    code = app.exec()
    return code, shooter.saved, shooter.problems


# ---- entry point --------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--out", required=True, help="where the demo content goes")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="the demo server's port on 127.0.0.1 (default: %(default)s)",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--serve", action="store_true", help="serve the content until Ctrl+C")
    action.add_argument("--screenshots", metavar="DIR", help="take the README's screenshots")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    out = Path(args.out).resolve()
    if args.screenshots:
        return take_screenshots(out, Path(args.screenshots).resolve())
    if not args.serve:
        generate(out, f"http://{HOST}:{args.port}")
        return 0
    with DemoServer(out, args.port) as server:
        generate(out, server.url)
        logger.info(
            "Serving at %s. In another terminal: QITV_CONFIG_DIR=%s uv run python main.py",
            server.url,
            out / CONFIG,
        )
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
