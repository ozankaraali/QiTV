"""Every key in ui.keymap.SHORTCUTS has a control on screen (2.0.1).

ON_SCREEN gives, per SHORTCUTS row, the controls that do the same as (screen, control), and this
test finds each one on its screen: laid out (a button, the title row, a header), listed (a row,
a context-menu entry) or a gesture on the picture itself (a click, a double-click, the wheel).
EXEMPT gives the rows that need none, and why. A key added to SHORTCUTS without either fails
here, and so does a control that is gone. The menu bar never counts: Windows and Linux hide it
in fullscreen.

Screens and their controls:
- "transport": the player controls' buttons by name ("grid" is the multiview's), and on live
  TV the title row's "title" (the channel's name and logo), "favorite" (the star after it) and
  "record" (the red dot after that, 2.3);
- "picture": the video's own "click", "double-click" and "wheel";
- "mixer": each row's "speaker" and "slider", and the "row" itself;
- "sound": the Sound panel ♫ opens: its track "rows", its sync's "earlier" (−), "later" (+)
  and "reset";
- "subtitles": the Subtitles panel its button opens, alike, and its "Load a file…";
- "picture_panel": the Picture panel its button opens (2.3): its setting rows' ‹ and › by
  their keys ("aspect", "deinterlace", "sleep") and its "stats" row;
- "subtitle_search": the OpenSubtitles search its "Search OpenSubtitles…" opens: the key page's
  "field", "clear" (✕), "get_key", "paste" and "save", the search's "field", "clear", "paste" and
  "search", its "languages" and "files" rows, and "close" (×);
- "pip_label": the small picture's "swap", "speaker" and "close";
- "drawer": the top row's "back", "names", "search", "refresh" and "settings", the ⌕ "find" box,
  its "rows", and the rows of its sources and sections columns by title ("Settings", "Movies" …);
- "context": the channel list's right-click menu, by entry (a channel of a genre's, and one of
  ⟲ Recent's);
- "guide": its header's buttons (2.9.3: the "day" and "now" too) and its "rows";
- "library": the top bar's items ("live", "search", "fullscreen" …), the grid's "posters", the
  highlighted title's strip buttons ("favorite", "play") and the A–Z "rail";
- "series" (2.9.3): the series view's "back" (‹), its season "chips", its episode "rows" and a
  begun episode's "start_over" pill;
- "folders" (Plan 4): the Library's Folders tab: its "tiles", a tile's "remove" (×), the "add"
  tile (Add folder: Open Folder…) and "clear".

The audio screen (a song: Plan 4) is checked on its own: AUDIO_ON_SCREEN gives, per "player"
and "menu" row, what does the same there, on these screens, and AUDIO_EXEMPT the rows that need
nothing there, and why:
- "dock": the player controls in audio mode by name (the scrub is "seek", the volume's bar
  "level", its speaker "volume");
- "more": the dock's "⋯" menu, by item ("stop", "audio", "subtitles", "picture", "fullscreen",
  "mini", "library", "search");
- "audio_screen": the screen's own controls in its three views ("back", "words", "up_next",
  "view_cover" …).
"""

import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from services.navigation import RECENT_GROUP, NavModel
from services.opensubtitles import SubtitleFile
from tests.test_navigation import FakeCatalog
from tests.test_poster_grid import FakePosters, details, titles
from ui.context_actions import OPEN_PIP_TEXT, REMOVE_RECENT_TEXT, ContextActions
from ui.keymap import SHORTCUTS
from ui.library.folders_view import FoldersView, Tile
from ui.library.poster_grid import PosterGrid
from ui.library.series_view import SeriesView
from ui.library.top_nav import ALL_TABS, TopNav
from ui.overlay_host import OverlayHost
from ui.overlays.audio_screen import VIEWS as AUDIO_VIEWS, AudioScreen
from ui.overlays.drawer import Drawer
from ui.overlays.guide import Guide
from ui.overlays.mixer import Mixer, MixerRow
from ui.overlays.more_menu import MoreMenu
from ui.overlays.pip_label import PipLabel
from ui.overlays.subtitle_search import LanguageRow, SubtitleSearchPage
from ui.overlays.track_panel import PanelModel, PanelRow, PanelSync, TrackPanel
from ui.overlays.transport import Transport
from ui.picture_controls import ASPECT, DEINTERLACE, SLEEP, STATS
from ui.pip import corner_rect
from ui.video_surface import VideoSurface

ON_SCREEN: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    # ---- Watching TV ---------------------------------------------------------------------
    # A channel in the channel list (a click on the picture opens it), or the wheel; during a
    # movie ↑/↓ are the volume: the speaker opens the mixer.
    ("player", "Up, Down"): (
        ("drawer", "rows"),
        ("picture", "wheel"),
        ("transport", "volume"),
        ("mixer", "slider"),
    ),
    ("player", "Backspace"): (("transport", "previous"),),
    ("player", "Return"): (("picture", "click"),),
    # Movies and a channel behind live: ◀10 10▶; at a time-shifted channel's live edge: ◀10.
    ("player", "Left, Right"): (("transport", "back10"), ("transport", "fwd10")),
    ("player", "Space"): (("transport", "play"),),
    ("player", "X"): (("transport", "stop"),),  # 2.6: ■ beside ▶
    ("player", "L"): (("transport", "golive"),),
    # The record button after the channel's star (2.2): lit while it records, dimmed on a
    # channel played directly (a press says why).
    ("player", "R"): (("transport", "record"),),
    ("player", "G"): (("transport", "guide"),),
    ("player", "I"): (("transport", "title"),),
    ("player", "+, -"): (("transport", "volume"), ("mixer", "slider")),
    ("player", "M"): (("transport", "volume"), ("mixer", "speaker"), ("pip_label", "speaker")),
    ("player", "Tab"): (("mixer", "row"),),
    # ♫ opens the Sound panel: its − and + are J and K.
    ("player", "J, K"): (("transport", "audio"), ("sound", "earlier"), ("sound", "later")),
    ("player", "Shift+J, Shift+K"): (
        ("transport", "subtitles"),
        ("subtitles", "earlier"),
        ("subtitles", "later"),
    ),
    # The Picture button opens the Picture panel; its aspect row's ‹ and › step the ratio.
    ("player", "A"): (("transport", "picture"), ("picture_panel", "aspect")),
    ("player", "Shift+I"): (("picture_panel", "stats"),),  # its Statistics row
    ("player", "F, F11, Ctrl+Meta+F"): (("transport", "fullscreen"), ("picture", "double-click")),
    ("player", "Ctrl+F"): (("transport", "search"),),
    ("player", "Ctrl+D"): (("transport", "favorite"),),
    ("player", "P, Ctrl+P"): (("transport", "pip"), ("pip_label", "close")),
    ("player", "S"): (("pip_label", "swap"), ("picture", "click")),
    # The multiview's button on the player controls (one picture, the way back, while its
    # grid shows).
    ("player", "V"): (("transport", "grid"),),
    ("player", "Alt+P"): (("transport", "mini"),),
    # Close what is open: a click on the picture closes the channel list, × the guide (the
    # banner, the zap list and the mixer fade by themselves); ⛶ leaves fullscreen; after a
    # movie or an episode from the Library, ‹ Library goes back to where it came from.
    ("player", "Esc"): (
        ("picture", "click"),
        ("guide", "close"),
        ("transport", "fullscreen"),
        ("transport", "library"),
    ),
    # ---- Channel list ----------------------------------------------------------------------
    ("drawer", "Up, Down"): (("drawer", "rows"),),  # a click on the row; the wheel scrolls
    ("drawer", "PgUp, PgDown"): (("drawer", "rows"),),
    ("drawer", "Left, Right"): (("drawer", "back"), ("drawer", "names"), ("drawer", "rows")),
    ("drawer", "Return"): (("drawer", "rows"),),
    ("drawer", "A…Z"): (("drawer", "find"),),  # a click on it, then the rows of what it finds
    ("drawer", "Ctrl+D"): (("context", "Add to favorites"),),
    ("drawer", "Ctrl+P"): (("context", OPEN_PIP_TEXT),),
    ("drawer", "Delete"): (("context", REMOVE_RECENT_TEXT),),  # a channel of ⟲ Recent
    ("drawer", "Ctrl+F"): (("drawer", "search"),),
    # Beside the list: its first click closes the list, the double-click is fullscreen.
    ("drawer", "F11, Ctrl+Meta+F"): (("picture", "double-click"),),
    ("drawer", "Esc"): (("picture", "click"),),
    # ---- Guide -----------------------------------------------------------------------------
    ("guide", "Up, Down"): (("guide", "rows"),),
    ("guide", "Left, Right"): (("guide", "earlier"), ("guide", "later")),
    ("guide", "Home"): (("guide", "now"),),  # 2.9.3
    ("guide", "Return"): (("guide", "rows"), ("guide", "live")),  # a double-click, ▶ Watch
    # 2.8: the footer's ● Record (Cancel recording for a scheduled programme)
    ("guide", "R"): (("guide", "record"),),
    ("guide", "Ctrl+F"): (("guide", "search"),),
    ("guide", "F, F11, Ctrl+Meta+F"): (("guide", "fullscreen"),),
    ("guide", "G, Esc"): (("guide", "close"),),
    # ---- Movies and Series -----------------------------------------------------------------
    ("library", "Up, Down, Left, Right"): (
        ("library", "posters"),
        ("series", "chips"),
        ("series", "rows"),
    ),
    ("library", "Return"): (
        ("library", "posters"),
        ("library", "play"),
        ("series", "rows"),
        ("series", "start_over"),
    ),
    ("library", "Home, End"): (("library", "rail"),),  # # and Z; the wheel scrolls
    # 2.9.3: typing searches (the top bar's field); the A–Z rail jumps to a letter.
    ("library", "A…Z"): (("library", "search"), ("library", "rail")),
    ("library", "Ctrl+F"): (("library", "search"),),
    ("library", "/"): (("library", "search"),),
    ("library", "Ctrl+D"): (("library", "favorite"),),
    ("library", "Delete"): (("library", "delete"),),  # 2.9.7: a recording's strip
    ("library", "F11, Ctrl+Meta+F"): (("library", "fullscreen"),),
    # Back to the grid (from a series or the search): its tab, or the series' ‹ (2.9.3); to
    # live TV: Live TV.
    ("library", "Esc"): (
        ("library", "live"),
        ("library", "movies"),
        ("library", "series"),
        ("series", "back"),
    ),
    # ---- Anywhere ----------------------------------------------------------------------------
    # Live TV: the Library's tab, a channel row; during a movie, its TV button (2.9.3).
    ("menu", "Ctrl+1"): (("library", "live"), ("drawer", "rows"), ("transport", "live_tv")),
    ("menu", "Ctrl+2"): (("drawer", "Movies"), ("library", "movies")),
    ("menu", "Ctrl+3"): (("drawer", "Series"), ("library", "series")),
    ("menu", "Ctrl+4"): (("transport", "grid"),),
    ("menu", "Ctrl+5"): (("library", "recordings"), ("drawer", "Recordings")),  # 2.9.7
    ("menu", "Ctrl+O"): (("drawer", "Open file"),),
    # Plan 4: Open Folder…, the Folders tab's Add folder tile (the tab shows once a folder was
    # opened: dropped, handed to QiTV or chosen in the File menu)
    ("menu", "Ctrl+Shift+O"): (("folders", "add"),),
    ("menu", "Ctrl+,"): (("drawer", "settings"), ("drawer", "Settings")),
    ("menu", "Ctrl+R"): (("drawer", "refresh"),),
    ("menu", "Ctrl+Shift+R"): (("library", "refresh"),),  # 2.8: films and series
    # ---- Finding subtitles (the OpenSubtitles search) ----------------------------------------
    # A click on a row; the wheel scrolls a list; a click on the field puts the keys there.
    ("subtitles", "Up, Down"): (
        ("subtitle_search", "languages"),
        ("subtitle_search", "files"),
        ("subtitle_search", "field"),
    ),
    ("subtitles", "PgUp, PgDown"): (("subtitle_search", "languages"), ("subtitle_search", "files")),
    ("subtitles", "Left, Right"): (("subtitle_search", "languages"), ("subtitle_search", "files")),
    ("subtitles", "Return"): (
        ("subtitle_search", "files"),
        ("subtitle_search", "search"),
        ("subtitle_search", "save"),
    ),
    ("subtitles", "A…Z"): (("subtitle_search", "field"),),
    ("subtitles", "Ctrl+V"): (("subtitle_search", "paste"),),
    ("subtitles", "Backspace"): (("subtitle_search", "clear"),),  # ✕ clears the field
    ("subtitles", "Esc"): (("subtitle_search", "close"),),
}

EXEMPT: dict[tuple[str, str], str] = {
    ("player", "0…9"): "a channel number: the channel list lists every channel (the brief)",
    ("drawer", "Backspace"): "deletes a letter typed into the find (its ✕ clears it all)",
    ("guide", "0…9"): "a channel number: the rows and the wheel reach every channel",
    ("menu", "Ctrl+Q"): "the window's own close button (leave fullscreen with ⛶ first)",
}

# On the audio screen (a song plays: on demand, as a film), every player and menu key's action.
# The Library rows go through it: the screen's ‹ and ⋯'s Library open it, with its tabs and ↻,
# and its Live TV, where a click opens the channel list (Open file, Settings, ↻), as the film
# audit's chain. ‹ and ⋯'s Library open the Library as it was left for any song, a folder's
# included (Ruling P4-8 a: TVPages.library_from_sound), pinned by
# test_back_and_more_library_open_the_library_for_a_folder_song
# (tests/test_tv_controller_audio.py).
_LIBRARY = (("audio_screen", "back"), ("more", "library"))
AUDIO_ON_SCREEN: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    # On demand ↑/↓, + and − are the volume: the dock's bar sets it (a slider). M mutes: the
    # speaker opens the mixer (its speaker; Tab's other row is a click on it).
    ("player", "Up, Down"): (("dock", "level"),),
    ("player", "+, -"): (("dock", "level"),),
    ("player", "M"): (("dock", "volume"),),
    ("player", "Tab"): (("dock", "volume"),),
    ("player", "Left, Right"): (("dock", "seek"),),  # no ◀10 10▶: the scrub reaches any moment
    ("player", "Space"): (("dock", "play"),),
    ("player", "X"): (("more", "stop"),),
    ("player", "J, K"): (("more", "audio"),),  # the Sound panel: its − and +
    ("player", "Shift+J, Shift+K"): (("more", "subtitles"),),  # the Subtitles panel's
    ("player", "A"): (("more", "picture"),),
    ("player", "Shift+I"): (("more", "picture"),),  # the Picture panel's Statistics row
    ("player", "F, F11, Ctrl+Meta+F"): (("more", "fullscreen"),),
    ("player", "Ctrl+F"): (("more", "search"),),
    ("player", "Alt+P"): (("more", "mini"),),
    # Back to the Library (⋯'s Library, the screen's ‹); out of fullscreen with ⋯'s Fullscreen.
    ("player", "Esc"): (*_LIBRARY, ("more", "fullscreen")),
    ("menu", "Ctrl+1"): _LIBRARY,  # its Live TV tab
    ("menu", "Ctrl+2"): _LIBRARY,  # Movies
    ("menu", "Ctrl+3"): _LIBRARY,  # Series
    ("menu", "Ctrl+5"): _LIBRARY,  # Recordings
    ("menu", "Ctrl+Shift+R"): _LIBRARY,  # its ↻
    # The channel list (Return) and what it holds: Live TV, then a click on the picture.
    ("player", "Return"): _LIBRARY,
    ("menu", "Ctrl+O"): _LIBRARY,  # its Open file
    ("menu", "Ctrl+Shift+O"): _LIBRARY,  # its Folders tab's Add folder
    ("menu", "Ctrl+,"): _LIBRARY,  # its Settings
    ("menu", "Ctrl+R"): _LIBRARY,  # its ↻
}

# On demand (a song, as a film) these do nothing, or say they are live TV's (P, S, V).
_LIVE = "live TV's (a channel's): on a song it does nothing or says so"
AUDIO_EXEMPT: dict[tuple[str, str], str] = {
    ("player", "0…9"): _LIVE + " (a channel number)",
    ("player", "Backspace"): _LIVE + " (the previous channel)",
    ("player", "L"): _LIVE + " (Go live)",
    ("player", "R"): _LIVE + " (record the channel)",
    ("player", "G"): _LIVE + " (the guide)",
    # On demand I shows the player's controls: the dock always shows (the screen, the song).
    ("player", "I"): "on a song it shows the controls, and the dock always shows",
    ("player", "Ctrl+D"): "a favorite channel: nothing while a song plays, as on a film",
    ("player", "P, Ctrl+P"): _LIVE + " (picture in picture with the previous channel)",
    ("player", "S"): _LIVE + " (swaps the second channel's small picture)",
    ("player", "V"): _LIVE + " (the multiview)",
    ("menu", "Ctrl+4"): _LIVE + " (the multiview)",
    ("menu", "Ctrl+Q"): EXEMPT[("menu", "Ctrl+Q")],
}

TIMELINE = {
    "start": 0.0,
    "end": 600.0,
    "position": 599.0,
    "live_edge": 600.0,
    "lag": 1.0,
    "mode": "live",
    "rate": 1.0,
    "paused": False,
}


def _host() -> QWidget:
    host = QWidget()
    host.resize(1280, 720)
    host.show()
    return host


def _transport(host: QWidget) -> set[str]:
    """Every control the player controls lay out: live, at the live edge, behind it, a movie
    from the Library."""
    t = Transport(host)
    t.set_pip_state(True, False)
    t.set_grid_state(True, False)
    found: set[str] = set()

    def take() -> None:
        found.update(n for n, r in t._layout().items() if not r.isEmpty())
        found.update(n for n, r in t.title_rects().items() if not r.isEmpty())

    t.show_live("One", "News")
    take()
    t.show_timeshift("One", "News")
    t.update_timeline(TIMELINE, [])
    take()
    t.update_timeline(dict(TIMELINE, position=300.0, lag=300.0, mode="shifted"), [])
    take()
    t.set_library_available(True)
    t.show_vod("Film", "")
    t.update_position(0, 100_000)
    take()
    return found


def _audio_dock(host: QWidget) -> Transport:
    t = Transport(host)
    t.show_audio("Ferries at Night", "Mira Sol · Tidewick Isle")
    t.set_audio_state(False, "off", True)  # a song of an album
    t.update_position(84_000, 221_000)
    return t


def _dock(host: QWidget) -> set[str]:
    """The player controls in audio mode: what the dock lays out (its times only say)."""
    t = _audio_dock(host)
    return {n for n, r in t._layout().items() if not r.isEmpty() and n != "times"}


def _more(host: QWidget) -> set[str]:
    """The dock's "⋯" menu, opened from its button."""
    t = _audio_dock(host)
    menu = MoreMenu(t, host)
    menu.open_at(t.mapToGlobal(t.button_rect("more").topLeft().toPoint()))
    return {n for n, r in menu.controls().items() if not r.isEmpty()}


def _audio_screen(host: QWidget) -> set[str]:
    """The audio screen's own controls in each view, a song of a two-track album."""
    screen = AudioScreen(host)
    screen.show_song("Ferries at Night", "Mira Sol", "Tidewick Isle")
    screen.set_tracks([{"title": "Low Tide"}, {"title": "Ferries at Night"}], 1)
    found: set[str] = set()
    for view in AUDIO_VIEWS:
        screen.set_view(view)
        found.update(n for n, r in screen.controls().items() if not r.isEmpty())
    return found


def _picture() -> set[str]:
    signals = {"click": "clicked", "double-click": "doubleClicked", "wheel": "wheelSteps"}
    return {
        gesture
        for gesture, signal in signals.items()
        if hasattr(VideoSurface, signal) and hasattr(OverlayHost, signal)
    }


def _mixer(host: QWidget) -> set[str]:
    mixer = Mixer(host)
    mixer.show_rows([MixerRow("Main", "One", 50, False), MixerRow("PiP", "Two", 40, True)], 0)
    found = set()
    for row in (0, 1):
        if not mixer.speaker_rect(row).isEmpty():
            found.add("speaker")
        if not mixer.slider_rect(row).isEmpty():
            found.add("slider")
        if not mixer._rects()[row].row.isEmpty():
            found.add("row")
    return found


def _sound(host: QWidget) -> set[str]:
    panel = TrackPanel(host)
    rows = (PanelRow(1, "English", True), PanelRow(2, "Deutsch"))
    sync = PanelSync("Sound 150 ms later", "Sound 5,000 ms earlier", reset=True)
    panel.show_panel(PanelModel("Sound", rows, sync))
    found = {"rows"} if not panel.row_rect(0).isEmpty() else set()
    found.update(n for n in ("earlier", "later", "reset") if not panel.control_rect(n).isEmpty())
    return found


def _subtitles(host: QWidget) -> set[str]:
    panel = TrackPanel(host)
    rows = (
        PanelRow(-1, "Off"),
        PanelRow(3, "English", True),
        PanelRow("load_file", "Load a file…", action=True),
    )
    sync = PanelSync("In sync", "Subtitles 5,000 ms earlier")
    panel.show_panel(PanelModel("Subtitles", rows, sync))
    found = {"rows"} if not panel.row_rect(0).isEmpty() else set()
    if any(row.action for row in panel.model().rows) and not panel.row_rect(2).isEmpty():
        found.add("Load a file…")
    found.update(n for n in ("earlier", "later") if not panel.control_rect(n).isEmpty())
    return found


def _picture_panel(host: QWidget) -> set[str]:
    panel = TrackPanel(host)
    rows = (
        PanelRow(ASPECT, "Aspect ratio", value="Auto", widest="Fill the window"),
        PanelRow(DEINTERLACE, "Deinterlacing", value="Auto"),
        PanelRow(SLEEP, "Sleep timer", value="Off", widest="1:30:00 left"),
        PanelRow(STATS, "Statistics"),
    )
    panel.show_panel(PanelModel("Picture", rows))
    found = set()
    for index, row in enumerate(rows):
        steps = not (panel.step_rect(index, -1).isEmpty() or panel.step_rect(index, 1).isEmpty())
        if not panel.row_rect(index).isEmpty() and (steps or not row.value):
            found.add(str(row.key))
    return found


def _subtitle_search(host: QWidget) -> set[str]:
    """The key page, a key typed into it, then the search with what it found."""
    page = SubtitleSearchPage(host)
    found: set[str] = set()

    def take(*names: str) -> None:
        found.update(n for n in names if not page.control_rect(n).isEmpty())

    page.show_key_page()
    page.type_text("k")
    take("field", "clear", "get_key", "paste", "save", "close")
    page.show_search("Lighthouse (2019)")
    files = [SubtitleFile(1, "en", "Lighthouse.2019"), SubtitleFile(2, "de", "Lighthouse.2019")]
    rows = [LanguageRow("en", "English", 1), LanguageRow("de", "German", 1)]
    page.set_results(rows, {"en": files[:1], "de": files[1:]}, "2 subtitles in 2 languages")
    take("field", "clear", "paste", "search", "close")
    for name in ("languages", "files"):
        if not page.row_rect(name, 0).isEmpty():
            found.add(name)
    return found


def _pip_label(host: QWidget) -> set[str]:
    label = PipLabel(host)
    label.follow(corner_rect(QSize(1280, 720), "br"))
    label.set_info("Two", "LIVE")
    return {n for n in ("swap", "speaker", "close") if not label.button_rect(n).isEmpty()}


def _drawer_and_context(host: QWidget) -> tuple[set[str], set[str]]:
    catalog = FakeCatalog()
    watched = {
        "provider_name": "Home",
        "item_type": "channel",
        "item_data": {"id": 1, "name": "One"},
    }
    config = SimpleNamespace(favorites=[], watch_history=[watched])
    nav = NavModel(catalog, config)
    drawer = Drawer(nav, None, lambda item: None, host)
    nav.reset_to("live", "10")
    drawer.open_drawer()
    found = {"rows"} if drawer._columns["right"].nodes else set()
    for name, rect in (
        ("back", drawer.back_rect()),
        ("search", drawer.search_rect()),
        ("refresh", drawer.refresh_rect()),
        ("settings", drawer.settings_gear_rect()),
        ("find", drawer.find_rect()),
    ):
        if not rect.isEmpty():
            found.add(name)
    if drawer.breadcrumb_parts():
        found.add("names")
    actions = ContextActions(
        nav=nav,
        config=config,
        lanes=None,
        external=None,
        toggle_favorite=lambda item: None,
        play_external=lambda *args: None,
        notify=lambda text, kind: None,
        open_pip=lambda node: None,
        forget_recent=lambda item: None,
        clear_recents=lambda: None,
    )
    entries: set[str] = set()
    for group in ("10", RECENT_GROUP):  # a right-click on a channel of a genre, of ⟲ Recent
        nav.reset_to("live", group)
        drawer.open_drawer()
        menu = actions.menu(drawer._columns["right"].nodes[0])
        if menu is not None:
            entries.update(a.text() for a in menu.actions() if not a.isSeparator())
    nav.focus_source("Home")  # the sources on the left, the provider's sections on the right
    drawer.open_drawer(focus_side="left")
    for side in ("left", "right"):
        found.update(node.title for node in drawer._columns[side].nodes)
    return found, entries


def _guide(host: QWidget) -> set[str]:
    from datetime import datetime, timedelta, timezone

    from services.epg_view import Programme

    now = datetime.now(timezone.utc)
    on_now = [Programme("On now", now - timedelta(minutes=10), now + timedelta(minutes=20))]
    guide = Guide(
        None,
        lambda channel, start, limit: on_now,
        lambda: now,
        host,
        scheduled=lambda channel, programme: False,  # 2.8: QiTV records (its footer shows)
    )
    guide.open_guide([{"id": "1", "name": "One"}, {"id": "2", "name": "Two"}], 0)
    found = {n for n, r in guide.header_buttons().items() if not r.isEmpty()}
    found.update(n for n, r in guide.footer_buttons().items() if not r.isEmpty())
    if guide.current_channel() is not None:
        found.add("rows")
    return found


def _library(host: QWidget) -> set[str]:
    nav = TopNav(host)
    nav.set_tabs(ALL_TABS)  # 2.9.7: Recordings shows once the Library has its page
    nav.setGeometry(0, 0, 1280, nav.height())
    found = {n for n, r in nav._item_rects().items() if not r.isEmpty()}
    grid = PosterGrid(FakePosters(), host)
    grid.setGeometry(0, nav.height(), 1280, 720 - nav.height())
    items = titles(80)
    grid.set_sections([("", items)])
    grid.set_details_provider(lambda item: details())
    grid.set_letter_index({"t": 0})
    if grid.current_index() >= 0:
        found.add("posters")
    found.update(name for name, _label in grid.button_labels())
    if grid._rail_visible():
        found.add("rail")
    # 2.9.7: the Recordings tab's tiles: a recording's strip (Delete…, Show in Folder)
    recordings = PosterGrid(FakePosters(), host)
    recordings.setGeometry(0, nav.height(), 1280, 720 - nav.height())
    recordings.set_sections([("", titles(3))])
    recordings.set_details_provider(lambda item: details("recording", None))
    found.update(name for name, _label in recordings.button_labels())
    return found


def _series(host: QWidget) -> set[str]:
    """The series view (2.9.3): ‹, the season chips, the episodes, a begun one's Start over."""
    from services.library import Details

    view = SeriesView(None, host)
    view.setGeometry(0, 0, 1280, 720)
    view.show()
    view.set_series({"id": "s"}, Details("Show", "", "", "", "", "", None, None, False, "series"))
    seasons = [{"id": "1", "name": "Season 1"}, {"id": "2", "name": "Season 2"}]
    view.set_seasons(seasons)
    view.set_progress_provider(lambda episode: 0.4)  # begun: its Start over pill
    view.set_episodes(seasons[0], [{"id": "e1", "ename": "One"}, {"id": "e2", "ename": "Two"}])
    found = set()
    if not view.back_rect().isEmpty():
        found.add("back")
    if len(view.chip_rects()) == 2:
        found.add("chips")
    if view.current_episode() is not None:
        found.add("rows")
    if not view._view.start_over_rect(0).isEmpty():
        found.add("start_over")
    return found


def _folders(host: QWidget) -> set[str]:
    """The Folders tab (Plan 4): a folder's tile and its ×, the Add folder tile, Clear."""
    view = FoldersView(host)
    view.setGeometry(0, 0, 1280, 720)
    view.show()
    view.set_tiles([Tile("/media/Tidewick Isle", "Tidewick Isle", "8 videos", read=True)])
    return {name for name, rect in view.controls().items() if not rect.isEmpty()}


class MouseOnlyAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        host = _host()
        cls.addClassCleanup(host.deleteLater)
        cls.addClassCleanup(host.close)
        drawer, context = _drawer_and_context(_child(host))
        cls.screens = {
            "transport": _transport(_child(host)),
            "picture": _picture(),
            "mixer": _mixer(_child(host)),
            "sound": _sound(_child(host)),
            "subtitles": _subtitles(_child(host)),
            "picture_panel": _picture_panel(_child(host)),
            "subtitle_search": _subtitle_search(_child(host)),
            "pip_label": _pip_label(_child(host)),
            "drawer": drawer,
            "context": context,
            "guide": _guide(_child(host)),
            "library": _library(_child(host)),
            "series": _series(_child(host)),
            "folders": _folders(_child(host)),
        }

    def test_every_key_has_a_control_on_screen_or_a_reason(self):
        for context, rows in SHORTCUTS.items():
            for keys, what in rows:
                row = (context, keys)
                with self.subTest(context=context, keys=keys, what=what):
                    listed = row in ON_SCREEN, row in EXEMPT
                    self.assertEqual(sum(listed), 1, "list it in ON_SCREEN or in EXEMPT")
                    if row in EXEMPT:
                        self.assertTrue(EXEMPT[row])
                        continue
                    self.assertTrue(ON_SCREEN[row])
                    for screen, control in ON_SCREEN[row]:
                        self.assertIn(control, self.screens[screen], f"{screen}: {control}")

    def test_the_tables_name_only_keys_that_are_listed(self):
        listed = {(context, keys) for context, rows in SHORTCUTS.items() for keys, _ in rows}
        self.assertEqual(set(ON_SCREEN) - listed, set())
        self.assertEqual(set(EXEMPT) - listed, set())

    def test_open_folder_has_an_on_screen_control(self):
        """Ctrl/⌘+Shift+O (File → Open Folder…): the Folders tab's Add folder tile asks for
        a folder as the menu does; each tile has its × and the shelf its Clear."""
        self.assertIn(("Ctrl+Shift+O", "Open a folder"), SHORTCUTS["menu"])
        self.assertEqual(ON_SCREEN[("menu", "Ctrl+Shift+O")], (("folders", "add"),))
        self.assertEqual(self.screens["folders"], {"tiles", "remove", "add", "clear"})
        view = FoldersView()
        self.addCleanup(view.deleteLater)
        self.addCleanup(view.close)
        view.resize(1280, 720)
        view.show()
        asked = []
        view.addRequested.connect(lambda: asked.append(True))
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.add_rect().center().toPoint())
        self.assertEqual(asked, [True])  # an empty shelf has it too


class AudioScreenAuditTests(unittest.TestCase):
    """A song's screen (Plan 4): every player and menu key's action has a control on the dock,
    in its "⋯" menu or on the screen itself, or a reason it needs none there."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        host = _host()
        cls.addClassCleanup(host.deleteLater)
        cls.addClassCleanup(host.close)
        cls.screens = {
            "dock": _dock(_child(host)),
            "more": _more(_child(host)),
            "audio_screen": _audio_screen(_child(host)),
        }

    def test_every_player_action_is_reachable_on_the_audio_screen(self):
        for context in ("player", "menu"):
            for keys, what in SHORTCUTS[context]:
                row = (context, keys)
                with self.subTest(context=context, keys=keys, what=what):
                    listed = row in AUDIO_ON_SCREEN, row in AUDIO_EXEMPT
                    self.assertEqual(sum(listed), 1, "list it in AUDIO_ON_SCREEN or AUDIO_EXEMPT")
                    if row in AUDIO_EXEMPT:
                        self.assertTrue(AUDIO_EXEMPT[row])
                        continue
                    self.assertTrue(AUDIO_ON_SCREEN[row])
                    for screen, control in AUDIO_ON_SCREEN[row]:
                        self.assertIn(control, self.screens[screen], f"{screen}: {control}")

    def test_the_audio_tables_name_only_player_and_menu_keys_that_are_listed(self):
        listed = {(c, keys) for c in ("player", "menu") for keys, _ in SHORTCUTS[c]}
        self.assertEqual(set(AUDIO_ON_SCREEN) - listed, set())
        self.assertEqual(set(AUDIO_EXEMPT) - listed, set())

    def test_the_dock_and_its_menu_show_what_they_hold(self):
        dock = {"seek", "shuffle", "prev_track", "play", "next_track", "repeat", "volume"}
        self.assertEqual(self.screens["dock"], dock | {"level", "more"})
        menu = {"stop", "audio", "subtitles", "picture", "fullscreen", "mini", "library"}
        self.assertEqual(self.screens["more"], menu | {"search"})
        # Words (the AI captions) is the screen's own pill, once (Ruling P4-7): no key of
        # SHORTCUTS turns the captions on, so no row names it; it is here.
        self.assertLessEqual({"back", "words", "up_next"}, self.screens["audio_screen"])
        self.assertNotIn("words", self.screens["dock"])


def _child(host: QWidget) -> QWidget:
    """A host of its own for each screen, the default window's size."""
    child = QWidget(host)
    child.setGeometry(0, 0, 1280, 720)
    child.show()
    return child


if __name__ == "__main__":
    unittest.main()
