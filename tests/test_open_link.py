"""File → Open Link… and the channel list's Open a link (plan 4): a card over the picture with
one field and Paste. Under it, what the link is (services.link_kind.classify) and a muted note;
Play hands a stream over with its kind, and a whole provider goes to Add provider. A link that
only its first bytes tell (an .m3u8, any other web address) is read on a worker when Play is
pressed: a list of channels becomes the provider offer. Fictional hosts; the bytes come from a
local server, never the network."""

import os
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import wait_until

from services.jobs import JobRunner
from services.link_kind import FILM, LIVE, PROBE_BYTES
from services.navigation import OPEN_FILE_ROW, OPEN_FOLDER_ROW, OPEN_LINK_ROW
from services.provider_link import M3U, STB, XTREAM, ProviderLink
from services.vlc_profile import DEFAULT_USER_AGENT
from tests.fixtures.local_provider import PROVIDER_NAME
from tests.http_fixtures import LocalSource, leaf
from tests.job_fixtures import ImmediateRunner
from tests.test_tv_window import WindowCase
from ui import open_link
from ui.keymap import Action
from ui.open_link import LinkCard, OpenLink
from ui.pointer import SecondClickGuard

KEPT = "Once it plays, it's kept in Network streams."
WHOLE = "This is a whole provider, not one stream."
NOT_A_LINK = "This isn't a link QiTV can open"
ACCOUNT = "http://panel.example:8080/live/demo/secret/12.ts"
PLAYLIST = "http://panel.example/get.php?username=demo&password=secret&type=m3u_plus"
MAC = "00:1A:79:00:00:01"
CHANNELS = (
    "#EXTM3U\n"
    '#EXTINF:-1 group-title="News",Harbour News\nhttp://a.example/1.ts\n'
    '#EXTINF:-1 group-title="News",Coast News\nhttp://a.example/2.ts\n'
)


class HeldRunner:
    """Keeps each job until the test lets it run (then on the test's thread, for its timing
    only: the thread is the real runner's test's)."""

    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, on_error, context))
        return worker

    def finish(self, index):
        ImmediateRunner().run_worker(*self.jobs[index])


class CardCase(unittest.TestCase):
    """The card on a host of the TV's size, with what it hands over recorded."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.addCleanup(self.disarm)
        self.played, self.added = [], []
        self.verify = True
        self.runner = HeldRunner()
        self.card = LinkCard(self.host)
        self.flow = self.make_flow(self.runner)

    def make_flow(self, runner):
        return OpenLink(
            self.card,
            runner,
            play=lambda url, kind: self.played.append((url, kind)),
            add_provider=self.added.append,
            verify_ssl=lambda url: self.verify,
            may_probe=lambda url: "limited.example" not in url,
            parent=self.host,
        )

    def disarm(self):
        for guard in self.host.findChildren(QObject):
            if isinstance(guard, SecondClickGuard):
                guard.disarm()

    def typed(self, text):
        """The card shown afresh, `text` typed into its field."""
        self.flow.open()
        if text:
            self.card.handle(Action.NAV_TEXT, text)
        self.assertEqual(self.card.values(), {"link": text})

    def offer(self):
        """(the found line, the note, the button row's texts, the accent's key, the other
        ways' texts) as the card shows them."""
        accent = next((b.key for b in self.card._buttons if b.accent), "")
        return (
            self.card.found()[0],
            self.card.note(),
            [b.text for b in self.card._buttons],
            accent,
            [b.text for b in self.card._alternates],
        )


class CardTests(CardCase):
    def test_found_line_per_kind(self):
        play, add = ["Cancel", "Play"], ["Cancel", "Add as a provider…"]
        cases = [
            ("https://cdn.example/films/Harbour.mkv", "✓ A film", KEPT, play, []),
            ("rtsp://cam.example/stream", "✓ A live stream", KEPT, play, []),
            # An Xtream stream link is one stream, with the whole account as the other way.
            (ACCOUNT, "✓ A live stream", KEPT, play, ["Add the whole account (panel.example)"]),
            (
                "http://panel.example:8080/movie/demo/secret/40.mp4",
                "✓ A film",
                KEPT,
                play,
                ["Add the whole account (panel.example)"],
            ),
            # Only its first bytes tell: no line until Play reads them (a song or a .ts may be
            # a file or a stream that never ends: final review I2).
            ("https://cdn.example/live/index.m3u8?token=abc", "", KEPT, play, []),
            ("https://cdn.example/watch?v=7", "", KEPT, play, []),
            ("http://cdn.example/live/channel.ts", "", KEPT, play, []),
            ("https://cdn.example/radio/coast.mp3", "", KEPT, play, []),
            # A whole provider: what it is, at its host only (never the login).
            (PLAYLIST, "✓ Xtream at panel.example, with its login", WHOLE, add, []),
            ("http://lists.example/tv.m3u", "✓ M3U playlist at lists.example", WHOLE, add, []),
            ("http://portal.example/c/", "✓ STB portal at portal.example", WHOLE, add, []),
            ("", "", KEPT, play, []),
            ("not a link", "", KEPT, play, []),  # said only when Play is pressed
        ]
        for text, found, note, buttons, alternates in cases:
            with self.subTest(text):
                self.typed(text)
                shown, said, row, accent, others = self.offer()
                self.assertEqual((shown, said, row, others), (found, note, buttons, alternates))
                self.assertEqual(accent, "provider" if buttons is add else "play")
                self.assertNotIn("demo", shown + said + "".join(others))
                self.assertTrue(self.card.found()[1])  # green: nothing went wrong
                self.assertEqual(self.card.title(), "Open a link")
        # The note sits under the found line, the buttons under the note.
        self.typed("https://cdn.example/films/Harbour.mkv")
        found, note = self.card.control_rect("found"), self.card.control_rect("note")
        play_button = self.card.control_rect("button", "play")
        self.assertFalse(note.isEmpty())
        self.assertGreaterEqual(note.top(), found.bottom())
        self.assertGreaterEqual(play_button.top(), note.bottom())
        self.assertEqual(note.left(), found.left())
        # Paste: the line follows the clipboard's link (in place of the text typed before).
        QGuiApplication.clipboard().setText(PLAYLIST + "\n")
        paste = self.card.control_rect("paste", "link").center().toPoint()
        QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=paste)
        self.assertEqual(self.card.values(), {"link": PLAYLIST})
        self.assertEqual(self.offer()[3], "provider")
        self.assertEqual((self.played, self.added), ([], []))

    def test_play_hands_the_stream_over_with_its_kind(self):
        streams = (
            ("http://limited.example/live/channel.ts", LIVE),  # never read there: live
            ("https://cdn.example/films/Harbour.mkv", FILM),
            (ACCOUNT, LIVE),
            ("'http://cdn.example/films/Coast.mp4'", FILM),  # quoted as copied: the link only
        )
        for text, kind in streams:
            with self.subTest(text):
                self.typed(text)
                self.card.handle(Action.NAV_ACTIVATE)  # Enter in the field: the accent, Play
                self.assertTrue(self.card.isHidden())
                self.assertEqual(self.played[-1], (text.strip("'"), kind))
        self.assertEqual(self.played, [(text.strip("'"), kind) for text, kind in streams])
        self.assertEqual(self.runner.jobs, [])  # its address said it all: nothing was read
        # Not a link: Play says so (in the warning colour) and the card stays.
        for text in ("not a link", ""):
            with self.subTest(text):
                self.typed(text)
                self.card.handle(Action.NAV_ACTIVATE)
                self.assertFalse(self.card.isHidden())
                self.assertEqual(self.card.found(), (NOT_A_LINK, False))
        self.card.handle(Action.NAV_TEXT, "x")  # typing on: the line goes
        self.assertEqual(self.card.found(), ("", True))
        self.assertEqual(len(self.played), 4)
        self.assertEqual((self.added, self.runner.jobs), ([], []))

    def test_a_provider_link_hands_over_to_add_provider(self):
        self.typed(PLAYLIST)
        self.card.handle(Action.NAV_ACTIVATE)  # Enter: Add as a provider…
        self.assertTrue(self.card.isHidden())
        self.assertEqual(
            self.added, [ProviderLink(XTREAM, "http://panel.example", "demo", "secret")]
        )
        # A portal's, by the mouse.
        self.added.clear()
        self.typed("http://portal.example/c/?mac=00:1A:79:00:00:01")
        QTest.mouseClick(
            self.card,
            Qt.MouseButton.LeftButton,
            pos=self.card.control_rect("button", "provider").center().toPoint(),
        )
        self.assertTrue(self.card.isHidden())
        self.assertEqual(
            self.added, [ProviderLink(STB, "http://portal.example/c/", mac="00:1A:79:00:00:01")]
        )
        # An Xtream stream link's other way: its whole account (↓ to the buttons, ↓ to it).
        self.added.clear()
        self.disarm()
        self.typed(ACCOUNT)
        self.card.handle(Action.NAV_DOWN)
        self.card.handle(Action.NAV_DOWN)
        self.assertEqual(self.card.focus(), ("alt", "account"))
        self.card.handle(Action.OK)  # the phone's OK
        self.assertTrue(self.card.isHidden())
        self.assertEqual(
            self.added, [ProviderLink(XTREAM, "http://panel.example:8080", "demo", "secret")]
        )
        self.assertEqual((self.played, self.runner.jobs), ([], []))

    def test_cancel_while_probing_drops_the_answer(self):
        url = "https://cdn.example/live/index.m3u8?token=abc"
        live = leaf("a.ts", "b.ts", ended=False).decode()
        with mock.patch.object(open_link, "read_start", side_effect=[live, CHANNELS]) as read:
            self.typed(url)
            self.card.handle(Action.NAV_ACTIVATE)  # Play: the first bytes are asked for
            self.assertTrue(self.card.busy())
            self.assertEqual(len(self.runner.jobs), 1)
            read.assert_not_called()  # on the runner's thread, not here
            self.card.handle(Action.ESCAPE)  # Cancel works while it waits
            self.assertTrue(self.card.isHidden())
            # Opened again and played: a second request; the first one's late answer is
            # dropped (it would have played), the second's is the one shown.
            self.typed(url)
            self.card.handle(Action.NAV_ACTIVATE)
            self.assertEqual(len(self.runner.jobs), 2)
            self.runner.finish(0)
            self.assertEqual(self.played, [])
            self.assertTrue(self.card.busy())
            self.assertFalse(self.card.isHidden())
            self.runner.finish(1)
        self.assertFalse(self.card.busy())
        self.assertEqual(self.offer()[3], "provider")
        self.assertEqual(self.played, [])
        # Cancelled with nothing reopened: the answer reaches nothing.
        with mock.patch.object(open_link, "read_start", return_value=live):
            self.typed(url)
            self.card.handle(Action.NAV_ACTIVATE)
            QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
            self.assertTrue(self.card.isHidden())  # the dimmed picture: Cancel
            self.runner.finish(2)
        self.assertTrue(self.card.isHidden())
        self.assertEqual((self.played, self.added), ([], []))

    def test_a_link_at_a_provider_with_a_stream_limit_plays_unread(self):
        """Reading it would be one stream more than the provider allows (and a closed one
        may stay counted there for minutes): it plays as live at once."""
        limited = "http://limited.example:8080/demo/secret/12"  # Xtream's short live form
        self.typed(limited)
        self.card.handle(Action.NAV_ACTIVATE)
        self.assertTrue(self.card.isHidden())
        self.assertEqual((self.played, self.runner.jobs), ([(limited, LIVE)], []))
        self.typed("http://other.example:8080/demo/secret/12")  # elsewhere: read first
        self.card.handle(Action.NAV_ACTIVATE)
        self.assertTrue(self.card.busy())
        self.assertEqual(len(self.runner.jobs), 1)
        self.assertEqual(self.played, [(limited, LIVE)])


class ProbeTests(CardCase):
    """The first bytes, read by the real runner off the GUI thread from a local server."""

    def setUp(self):
        super().setUp()
        self.jobs = JobRunner()
        self.addCleanup(lambda: self.assertTrue(wait_until(lambda: self.jobs.pending() == 0)))
        self.flow = self.make_flow(self.jobs)
        self.source = LocalSource(
            {
                "/lists/tv.m3u8": (200, {}, CHANNELS.encode()),
                "/live/index.m3u8": (200, {}, leaf("a.ts", "b.ts", ended=False)),
                "/vod/index.m3u8": (200, {}, leaf("a.ts", "b.ts")),
                "/gone.m3u8": (403, {}, b"Forbidden"),
                "/podcast": (200, {"Content-Type": "audio/mpeg"}, b"ID3\x04" + b"\x00" * 4096),
                "/radio": (200, {"Content-Type": "audio/mpeg"}, self.endless),
                "/song.mp3": (200, {"Content-Type": "audio/mpeg"}, b"ID3\x04" + b"\x00" * 4096),
                "/part.mp3": (206, {"Content-Range": "bytes 0-4099/9000000"}, b"\x00" * 4100),
                # A radio station names itself (icy-*): it never ends, whatever it says.
                "/coast.mp3": (200, {"icy-name": "Coast FM", "icy-br": "128"}, b"\x00" * 4096),
                "/onair.aac": (200, {"Content-Type": "audio/aac"}, self.endless),
                "/channel.ts": (200, {"Content-Type": "video/mp2t"}, self.endless),
                "/recorded.ts": (200, {"Content-Type": "video/mp2t"}, b"G" * 4096),
                "/big": (200, {}, b"#EXTM3U\n" + b"x" * (1024 * 1024)),
                "/slow": (200, {}, self.trickle),
            }
        )
        self.addCleanup(self.source.close)
        self.threads = []
        real = open_link.read_start

        def reading(url, verify_ssl, stop=None):
            self.threads.append(threading.current_thread())
            return real(url, verify_ssl, stop)

        patch = mock.patch.object(open_link, "read_start", side_effect=reading)
        patch.start()
        self.addCleanup(patch.stop)

    def trickle(self, handler):
        """A stream that never ends, 1 KB every quarter of a second (64 KB in 16 s)."""
        for _ in range(240):
            if self.source.stop.is_set():
                return
            handler.wfile.write(b"x" * 1024)
            handler.wfile.flush()
            time.sleep(0.25)

    def endless(self, handler):
        """Internet radio: sound that never ends (no size), until the read lets go."""
        for _ in range(200):
            if self.source.stop.is_set():
                return
            try:
                handler.wfile.write(b"\xff\xfb" + b"\x00" * 4094)
                handler.wfile.flush()
            except OSError:
                return
            time.sleep(0.01)

    def play(self, path):
        url = self.source.url + path
        self.typed(url)
        self.card.handle(Action.NAV_ACTIVATE)
        return url

    def test_a_dropped_read_stops_at_its_next_chunk(self):
        """Cancelled (or opened again), the read on its way ends at once: two reads of one
        link never run together."""
        with mock.patch.object(open_link, "_CHUNK", 1024):
            self.play("/slow")
            self.assertTrue(wait_until(lambda: self.source.requests))
            self.card.handle(Action.ESCAPE)
            self.assertTrue(self.card.isHidden())
            # Read on, it would take 16 s to reach 64 KB.
            self.assertTrue(wait_until(lambda: self.jobs.pending() == 0, 3000))
        self.assertEqual((self.played, self.added), ([], []))

    def test_probe_runs_off_the_gui_thread_and_swaps_to_provider_for_a_channel_list(self):
        url = self.play("/lists/tv.m3u8")
        self.assertTrue(self.card.busy())  # "Checking…": the buttons wait, Cancel works
        self.assertTrue(wait_until(lambda: not self.card.busy()))
        self.assertEqual(len(self.threads), 1)
        self.assertIsNot(self.threads[0], threading.main_thread())
        self.assertFalse(self.card.isHidden())
        offer = ("✓ M3U playlist at 127.0.0.1", WHOLE, ["Cancel", "Add as a provider…"])
        self.assertEqual(self.offer(), (*offer, "provider", []))
        self.assertEqual(self.card.values(), {"link": url})  # the link as it was given
        self.assertEqual(self.played, [])
        # Asked with QiTV's user agent, once.
        self.assertEqual(len(self.source.requests), 1)
        self.assertEqual(self.source.requests[0][1].get("User-Agent"), DEFAULT_USER_AGENT)
        self.card.handle(Action.NAV_ACTIVATE)  # Add as a provider…, now the accent
        self.assertTrue(self.card.isHidden())
        self.assertEqual(self.added, [ProviderLink(M3U, url)])
        self.assertEqual(len(self.source.requests), 1)  # its answer was kept: no second read

    def test_a_stream_plays_with_the_kind_its_first_bytes_say(self):
        cases = (
            ("/live/index.m3u8", LIVE),
            ("/vod/index.m3u8", FILM),
            ("/gone.m3u8", LIVE),
            ("/podcast", FILM),  # final review I2: a sized answer is a file that ends
            ("/radio", LIVE),  # its sound never ends
            ("/song.mp3", FILM),  # a song: its size is given
            ("/part.mp3", FILM),  # a part of it, with its whole size
            ("/coast.mp3", LIVE),  # a station: icy headers
            ("/onair.aac", LIVE),  # no size: it streams
            ("/channel.ts", LIVE),
            ("/recorded.ts", FILM),
        )
        for path, kind in cases:  # a refusal plays as live: VLC (and the player) say why
            with self.subTest(path):
                self.played.clear()
                url = self.play(path)
                self.assertTrue(wait_until(lambda: self.card.isHidden()))
                self.assertEqual(self.played, [(url, kind)])
        self.assertTrue(all(t is not threading.main_thread() for t in self.threads))
        self.assertEqual(self.added, [])

    def test_at_most_64_kb_are_read_with_the_tls_setting(self):
        with mock.patch("requests.get", wraps=open_link.requests.get) as get:
            text = open_link.read_start(self.source.url + "/big", False).text  # a 1 MB answer
        self.assertTrue(text.startswith("#EXTM3U\n"))
        self.assertEqual(len(text.encode()), PROBE_BYTES)
        self.assertIs(get.call_args.kwargs["verify"], False)
        self.assertTrue(get.call_args.kwargs["stream"])  # read as it comes, never whole
        self.verify = False  # Settings' TLS check off
        with mock.patch("requests.get", wraps=open_link.requests.get) as get:
            url = self.play("/live/index.m3u8")
            self.assertTrue(wait_until(lambda: self.card.isHidden()))
        self.assertIs(get.call_args.kwargs["verify"], False)
        self.assertEqual(self.played, [(url, LIVE)])


class EntryPointTests(WindowCase):
    def test_menu_and_drawer_row_open_the_card(self):
        self.show()
        drawer = self.wait_for_drawer()
        window, card = self.window, self.window.open_link_card
        self.assertIs(card.parentWidget(), window.host)  # over the picture, as Settings' cards
        file_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "File")
        entries = file_menu.actions()
        self.assertEqual(
            [a.text() for a in entries[:3]], ["Open File…", "Open Link…", "Open Folder…"]
        )
        self.assertTrue(entries[1].shortcut().isEmpty())  # no key of its own
        # The channel list's row, between Open file and Open a folder, by the keys.
        actions = [n.key for n in window.nav.children(None) if n.layer == "action"]
        at = actions.index(OPEN_FILE_ROW)
        self.assertEqual(actions[at : at + 3], [OPEN_FILE_ROW, OPEN_LINK_ROW, OPEN_FOLDER_ROW])
        for key in (Qt.Key.Key_Left, Qt.Key.Key_Left):
            QTest.keyClick(window, key)  # the sources column
        for _ in range(5):
            QTest.keyClick(window, Qt.Key.Key_Down)  # ⚙, ＋, ＋ free, Open file, Open a link
        self.assertEqual(drawer.highlighted().key, OPEN_LINK_ROW)
        self.assertEqual(drawer.highlighted().title, "Open a link")
        QTest.keyClick(window, Qt.Key.Key_Return)
        self.assertFalse(card.isHidden())
        self.assertEqual(card.title(), "Open a link")
        # Its keys never reach the list under it: letters and Space type, ↓ moves in the card.
        highlighted, find = drawer.highlighted(), drawer.find_text
        for key in (Qt.Key.Key_F, Qt.Key.Key_M, Qt.Key.Key_Space):
            QTest.keyClick(window, key)
        self.assertEqual(card.values(), {"link": "fm "})
        QTest.keyClick(window, Qt.Key.Key_Down)
        self.assertEqual(card.focus(), ("button", "play"))
        self.assertEqual((drawer.highlighted(), drawer.find_text), (highlighted, find))
        self.assertTrue(drawer.isVisible())
        QTest.keyClick(window, Qt.Key.Key_Escape)  # Cancel: the list as it was
        self.assertTrue(card.isHidden())
        self.assertTrue(drawer.isVisible())
        self.assertEqual(drawer.highlighted(), highlighted)
        # File → Open Link… over the player: F, M and Space type (no fullscreen, no mute), Esc
        # cancels it (and only it), and what it hands over reaches the window.
        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.assertFalse(drawer.isVisible())
        self.menu_actions()["Open Link…"].trigger()
        self.assertFalse(card.isHidden())
        self.assertEqual(card.values(), {"link": ""})  # afresh
        self.assertTrue(window.controller.overlay_open())  # Esc is the card's
        self.assertEqual(window.controller.key_context(), "drawer")  # the keys type
        muted = window.controller.main_muted()
        for key in (Qt.Key.Key_F, Qt.Key.Key_M, Qt.Key.Key_Space):
            QTest.keyClick(window, key)
        self.assertEqual(card.values(), {"link": "fm "})
        self.assertFalse(window.isFullScreen())
        self.assertEqual(window.controller.main_muted(), muted)
        self.assertFalse(drawer.isVisible())
        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.assertTrue(card.isHidden())
        self.assertFalse(drawer.isVisible())
        self.assertFalse(window.isFullScreen())
        with (
            mock.patch.object(window, "play_network_link") as play,
            mock.patch.object(window, "add_provider") as add,
        ):
            for text in ("rtsp://cam.example/harbour", PLAYLIST):  # live by its address
                self.menu_actions()["Open Link…"].trigger()
                QTest.keyClicks(window, text)
                QTest.keyClick(window, Qt.Key.Key_Return)
                self.assertTrue(card.isHidden())
        play.assert_called_once_with("rtsp://cam.example/harbour", LIVE)
        link = ProviderLink(XTREAM, "http://panel.example", "demo", "secret")
        add.assert_called_once_with(link, from_drawer=False)  # over the player, not the list

    def test_settings_or_the_library_over_the_card_cancel_it(self):
        """What takes the screen over the card (Settings by Ctrl/⌘+, or the app menu, the
        Library by Ctrl/⌘+2) cancels it, as its Esc would: the keys go to what shows."""
        self.show()
        self.wait_for_drawer()
        window, card = self.window, self.window.open_link_card
        window.open_link()
        QTest.keyClick(window, Qt.Key.Key_A)
        self.assertEqual(card.values(), {"link": "a"})
        window.open_settings()
        self.assertTrue(card.isHidden())
        self.assertTrue(window.settings.visible())
        row = window.settings_drawer.focused_row()
        QTest.keyClick(window, Qt.Key.Key_Down)  # Settings' own: its next row
        self.assertNotEqual(window.settings_drawer.focused_row(), row)
        window.settings.close()
        window.open_link()
        self.assertFalse(card.isHidden())
        window.screens.open_library("movies")
        self.assertTrue(card.isHidden())
        window.screens.back_to_tv()
        self.assertTrue(card.isHidden())  # not back by itself


class ProviderHostTests(WindowCase):
    """Links at the hosts of configured providers (never contacted: not the one shown)."""

    seed_fixture = True

    def provider_list(self):
        fixture = {
            "type": "M3UPLAYLIST",
            "name": PROVIDER_NAME,
            "url": str(Path(self.config_dir) / "fixture.m3u"),
        }
        return [
            fixture,
            {"type": "STB", "name": "Box", "url": "http://portal.example/c/", "mac": MAC},
            {
                "type": "XTREAM",
                "name": "Panel",
                "url": "http://panel.example:8080",
                "username": "demo",
                "password": "secret",
                "max_streams": 1,
            },
            {
                "type": "M3UPLAYLIST",
                "name": "Lists",
                "url": "http://lists.example:8000/tv.m3u",
                "max_streams": 2,
            },
            {
                "type": "M3UPLAYLIST",
                "name": "Free",
                "url": "https://free.example/tv.m3u",
                "ssl_verify": False,
            },
        ]

    def test_a_link_at_a_provider_with_a_stream_limit_is_never_read(self):
        window, card = self.window, self.window.open_link_card
        limited = (
            "http://portal.example/play/live.php?stream=5",  # an STB portal: one stream
            "http://panel.example:8080/demo/secret/12",  # Xtream's short live form, its max
            "http://lists.example:8000/stream/7",  # a playlist with max_streams
            "http://PORTAL.example:80/play/live.php?stream=6",  # its host, http's port written
            "http://panel.example:8080/demo/secret/12.ts",  # the account's .ts: live, unread
            "http://panel.example:8080/radio/coast.mp3",  # a song there: as it was, live
        )
        read = mock.patch.object(open_link, "read_start", return_value=CHANNELS)
        play = mock.patch.object(window, "play_network_link")
        with read as reading, play as playing:
            for url in limited:
                window.open_link(url)
                card.handle(Action.NAV_ACTIVATE)
                self.assertTrue(card.isHidden())
            reading.assert_not_called()
            self.assertEqual(playing.call_args_list, [mock.call(url, LIVE) for url in limited])
            # Elsewhere the link is read: with its provider's TLS check, else Settings'.
            default = bool(self.config.ssl_verify)
            for url, verify in (
                ("https://free.example/live/index.m3u8", False),
                ("http://lists.example/stream/7", default),  # another port: another host
                ("https://cdn.example/watch?v=7", default),
            ):
                with self.subTest(url):
                    window.open_link(url)
                    card.handle(Action.NAV_ACTIVATE)
                    self.assertTrue(wait_until(lambda: not card.busy()))
                    self.assertEqual(reading.call_args.args[:2], (url, verify))
                    self.assertFalse(card.isHidden())  # the channel list's provider offer
                    card.handle(Action.ESCAPE)
            self.assertEqual(reading.call_count, 3)
        self.assertTrue(wait_until(lambda: window.jobs.pending() == 0))


if __name__ == "__main__":
    unittest.main()
