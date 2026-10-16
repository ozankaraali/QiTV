"""The owner's mouse-only rule (2.0.1): every key in ui.keymap.SHORTCUTS has a control on screen.

ON_SCREEN gives, per SHORTCUTS row, the controls that do the same as (screen, control), and this
test finds each one on its screen: laid out (a button, the title row, a header), listed (a row,
a context-menu entry) or a gesture on the picture itself (a click, a double-click, the wheel).
EXEMPT gives the rows that need none, and why. A key added to SHORTCUTS without either fails
here, and so does a control that is gone. The menu bar never counts: Windows and Linux hide it
in fullscreen.

Screens and their controls:
- "transport": the player controls' buttons by name, and on live TV the title row's "title"
  (the channel's name and logo) and "favorite" (the star after it);
- "picture": the video's own "click", "double-click" and "wheel";
- "mixer": each row's "speaker" and "slider", and the "row" itself;
- "pip_label": the small picture's "swap", "speaker" and "close";
- "drawer": the top row's "back", "names", "search", "refresh" and "settings", its "rows", and
  the rows of its sources and sections columns by title ("Settings", "Open file", "Movies" …);
- "context": the channel list's right-click menu, by entry;
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

from services.navigation import NavModel
from tests.test_navigation import FakeCatalog
from tests.test_poster_grid import FakePosters, details, titles
from ui.context_actions import OPEN_PIP_TEXT, ContextActions
from ui.keymap import SHORTCUTS
from ui.library.poster_grid import PosterGrid
from ui.library.top_nav import TopNav
from ui.overlay_host import OverlayHost
from ui.overlays.drawer import Drawer
from ui.overlays.guide import Guide
from ui.overlays.mixer import Mixer, MixerRow
from ui.overlays.pip_label import PipLabel
from ui.overlays.transport import Transport
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
    ("player", "L"): (("transport", "golive"),),
    ("player", "G"): (("transport", "guide"),),
    ("player", "I"): (("transport", "title"),),
    ("player", "+, -"): (("transport", "volume"), ("mixer", "slider")),
    ("player", "M"): (("transport", "volume"), ("mixer", "speaker"), ("pip_label", "speaker")),
    ("player", "Tab"): (("mixer", "row"),),
    ("player", "F, F11, Ctrl+Meta+F"): (("transport", "fullscreen"), ("picture", "double-click")),
    ("player", "Ctrl+F"): (("transport", "search"),),
    ("player", "Ctrl+D"): (("transport", "favorite"),),
    ("player", "P, Ctrl+P"): (("transport", "pip"), ("pip_label", "close")),
    ("player", "S"): (("pip_label", "swap"), ("picture", "click")),
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
    ("drawer", "Ctrl+D"): (("context", "Add to favorites"),),
    ("drawer", "Ctrl+P"): (("context", OPEN_PIP_TEXT),),
    ("drawer", "Ctrl+F"): (("drawer", "search"),),
    # Beside the list: its first click closes the list, the double-click is fullscreen.
    ("drawer", "F11, Ctrl+Meta+F"): (("picture", "double-click"),),
    ("drawer", "Esc"): (("picture", "click"),),
    # ---- Guide -----------------------------------------------------------------------------
    ("guide", "Up, Down"): (("guide", "rows"),),
    ("guide", "Left, Right"): (("guide", "earlier"), ("guide", "later")),
    ("guide", "Return"): (("guide", "rows"),),  # a double-click
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
    ("menu", "Ctrl+O"): (("drawer", "Open file"),),
    ("menu", "Ctrl+,"): (("drawer", "settings"), ("drawer", "Settings")),
    ("menu", "Ctrl+R"): (("drawer", "refresh"),),
}

EXEMPT: dict[tuple[str, str], str] = {
    ("player", "0…9"): "a channel number: the channel list lists every channel (the brief)",
    ("drawer", "A…Z"): "typing to filter needs a keyboard; the rows are there to click",
    ("drawer", "Backspace"): "deletes a letter typed into the filter",
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


def _pip_label(host: QWidget) -> set[str]:
    label = PipLabel(host)
    label.follow(corner_rect(QSize(1280, 720), "br"))
    label.set_info("Two", "LIVE")
    return {n for n in ("swap", "speaker", "close") if not label.button_rect(n).isEmpty()}


def _drawer_and_context(host: QWidget) -> tuple[set[str], set[str]]:
    catalog = FakeCatalog()
    config = SimpleNamespace(favorites=[], watch_history=[])
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
    )
    menu = actions.menu(drawer._columns["right"].nodes[0])  # a right-click on a channel
    entries = {a.text() for a in menu.actions() if not a.isSeparator()} if menu else set()
    nav.focus_source("Home")  # the sources on the left, the provider's sections on the right
    drawer.open_drawer(focus_side="left")
    for side in ("left", "right"):
        found.update(node.title for node in drawer._columns[side].nodes)
    return found, entries


def _guide(host: QWidget) -> set[str]:
    from datetime import datetime, timezone

    guide = Guide(None, lambda channel, start, limit: [], lambda: datetime.now(timezone.utc), host)
    guide.open_guide([{"id": "1", "name": "One"}, {"id": "2", "name": "Two"}], 0)
    found = {n for n, r in guide.header_buttons().items() if not r.isEmpty()}
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
