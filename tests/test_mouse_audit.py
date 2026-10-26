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
- "guide": its header's buttons and its "rows";
- "library": the top bar's items ("live", "search", "fullscreen" …), the grid's "posters", the
  highlighted title's strip buttons ("favorite", "play") and the A–Z "rail".
"""

import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication, QWidget

from services.navigation import RECENT_GROUP, NavModel
from services.opensubtitles import SubtitleFile
from tests.test_navigation import FakeCatalog
from tests.test_poster_grid import FakePosters, details, titles
from ui.context_actions import OPEN_PIP_TEXT, REMOVE_RECENT_TEXT, ContextActions
from ui.keymap import SHORTCUTS
from ui.library.poster_grid import PosterGrid
from ui.library.top_nav import TopNav
from ui.overlay_host import OverlayHost
from ui.overlays.drawer import Drawer
from ui.overlays.guide import Guide
from ui.overlays.mixer import Mixer, MixerRow
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
    ("guide", "Return"): (("guide", "rows"),),  # a double-click
    # 2.8: the footer's ● Record (Cancel recording for a scheduled programme)
    ("guide", "R"): (("guide", "record"),),
    ("guide", "Ctrl+F"): (("guide", "search"),),
    ("guide", "F, F11, Ctrl+Meta+F"): (("guide", "fullscreen"),),
    ("guide", "G, Esc"): (("guide", "close"),),
    # ---- Movies and Series -----------------------------------------------------------------
    ("library", "Up, Down, Left, Right"): (("library", "posters"),),
    ("library", "Return"): (("library", "posters"), ("library", "play")),
    ("library", "Home, End"): (("library", "rail"),),  # # and Z; the wheel scrolls
    ("library", "A…Z"): (("library", "rail"),),
    ("library", "Ctrl+F"): (("library", "search"),),
    ("library", "/"): (("library", "search"),),
    ("library", "Ctrl+D"): (("library", "favorite"),),
    ("library", "F11, Ctrl+Meta+F"): (("library", "fullscreen"),),
    # Back to the grid (from a series or the search): its tab; to live TV: Live TV.
    ("library", "Esc"): (("library", "live"), ("library", "movies"), ("library", "series")),
    # ---- Anywhere ----------------------------------------------------------------------------
    ("menu", "Ctrl+1"): (("library", "live"), ("drawer", "rows")),
    ("menu", "Ctrl+2"): (("drawer", "Movies"), ("library", "movies")),
    ("menu", "Ctrl+3"): (("drawer", "Series"), ("library", "series")),
    ("menu", "Ctrl+4"): (("transport", "grid"),),
    ("menu", "Ctrl+O"): (("drawer", "Open file"),),
    ("menu", "Ctrl+,"): (("drawer", "settings"), ("drawer", "Settings")),
    ("menu", "Ctrl+R"): (("drawer", "refresh"),),
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
    from datetime import datetime, timezone

    from datetime import timedelta

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
    return found


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


def _child(host: QWidget) -> QWidget:
    """A host of its own for each screen, the default window's size."""
    child = QWidget(host)
    child.setGeometry(0, 0, 1280, 720)
    child.show()
    return child


if __name__ == "__main__":
    unittest.main()
