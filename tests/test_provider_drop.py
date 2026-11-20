"""A playlist file or a provider's link dropped on the TV window becomes a provider (2.2),
beside the subtitle files a drop loads into what plays; a portal's link without its MAC opens
the Add card in Settings, filled in (plan 2), and one stream's link the Open Link card (plan 4).
Fictional hosts."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse, wait_until

from services.config_manager import ConfigManager
from services.link_kind import LIVE
from services.provider_link import ProviderLink
from services.settings_model import PROVIDERS
from tests import test_tv_window as base
from ui import provider_drop

# A closed port on the loopback: a provider set up from a dropped link fails at once.
PANEL = "http://127.0.0.1:9"


def drop(target, *, paths=(), urls=(), text=None):
    """Drag a drop's data over `target` and drop it there: (entered, dropped)."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths] + [QUrl(url) for url in urls])
    if text is not None:
        mime.setText(text)
    copy, left = Qt.DropAction.CopyAction, Qt.MouseButton.LeftButton
    point = QPoint(20, 20)
    enter = QDragEnterEvent(point, copy, mime, left, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, enter)
    dropped = QDropEvent(QPointF(point), copy, mime, left, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, dropped)
    return enter.isAccepted(), dropped.isAccepted()


class HelperTests(unittest.TestCase):
    def test_only_playlists_are_playlist_files(self):
        paths = ["/x/News.m3u", "/x/All.M3U8", "/x/film.srt", "/x/clip.mkv", ""]
        self.assertEqual(provider_drop.playlist_files(paths), ["/x/News.m3u", "/x/All.M3U8"])

    def test_a_file_is_named_after_itself(self):
        self.assertEqual(
            provider_drop.file_entry("/x/My channels.m3u"),
            {"type": "M3UPLAYLIST", "name": "My channels", "url": "/x/My channels.m3u"},
        )

    def test_links_from_urls_and_text_once_each(self):
        xtream = f"{PANEL}/get.php?username=ann&password=pw1"
        found = provider_drop.links([xtream, f"see\n{xtream}\nhttp://example.test/page.html"])
        self.assertEqual(found, [ProviderLink("XTREAM", PANEL, "ann", "pw1")])

    def test_entries_for_links(self):
        xtream = ProviderLink("XTREAM", PANEL, "ann", "pw1")
        self.assertEqual(
            provider_drop.link_entry(xtream),
            {
                "type": "XTREAM",
                "name": "127.0.0.1",
                "url": PANEL,
                "username": "ann",
                "password": "pw1",
            },
        )
        portal = ProviderLink("STB", "http://portal.example.test/c/")
        self.assertIsNone(provider_drop.link_entry(portal))  # its MAC: the form asks
        entry = provider_drop.link_entry(ProviderLink("STB", portal.url, mac="00:1A:79:00:00:01"))
        self.assertEqual(entry["mac"], "00:1A:79:00:00:01")

    def test_names_and_sameness(self):
        self.assertEqual(provider_drop.unique_name("TV", ["TV", "TV (2)"]), "TV (3)")
        self.assertTrue(
            provider_drop.same_provider(
                {"type": "m3uplaylist", "url": "/x/a.m3u"},
                {"type": "M3UPLAYLIST", "url": "/x/a.m3u"},
            )
        )
        self.assertFalse(
            provider_drop.same_provider(
                {"type": "XTREAM", "url": PANEL, "username": "a"},
                {"type": "XTREAM", "url": PANEL, "username": "b"},
            )
        )

    def test_one_address_written_another_way_is_the_same_provider(self):
        listed = {"type": "STB", "url": "http://Portal.Example.test/c/"}
        for url in (
            "http://portal.example.test/c",
            "https://portal.example.test/c/",
            "http://portal.example.test:80/c/",
        ):
            with self.subTest(url=url):
                self.assertTrue(provider_drop.same_provider(listed, {"type": "STB", "url": url}))
        other = {"type": "STB", "url": "http://portal.example.test:8080/c/"}
        self.assertFalse(provider_drop.same_provider(listed, other))
        self.assertTrue(
            provider_drop.same_provider(
                {"type": "M3UPLAYLIST", "url": "/x/lists/../a.m3u"},
                {"type": "M3UPLAYLIST", "url": "/x/a.m3u"},
            )
        )


class WindowDropTests(base.WindowCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)

    def playlist(self, name="My channels.m3u"):
        path = self.folder / name
        path.write_text('#EXTM3U\n#EXTINF:-1 group-title="News",Dropped One\n' f"{PANEL}/1.ts\n")
        return path

    def names(self):
        return [p.get("name") for p in self.window.provider_manager.providers]

    def test_a_dropped_playlist_becomes_a_provider_and_is_shown(self):
        path = self.playlist()
        before = len(self.names())
        self.assertEqual(drop(self.window, paths=[path]), (True, True))
        self.assertTrue(wait_until(lambda: len(self.names()) == before + 1))
        added = self.window.provider_manager.providers[-1]
        self.assertEqual(added, {"type": "M3UPLAYLIST", "name": "My channels", "url": str(path)})
        self.assertIn(("Added My channels to your providers", "info"), self.toasts)
        self.assertEqual(self.config.selected_provider_name, "My channels")
        self.assertEqual(ConfigManager().selected_provider_name, "My channels")  # saved
        # The same file again is only switched to.
        self.assertEqual(drop(self.window.host, paths=[path]), (True, True))
        self.assertTrue(
            wait_until(lambda: ("My channels is already in your providers", "info") in self.toasts)
        )
        self.assertEqual(len(self.names()), before + 1)

    def test_a_playlist_and_a_subtitle_file_in_one_drop(self):
        srt = base.subtitle_file(self)
        path = self.playlist()
        # Nothing plays: the subtitle file has nowhere to go; the playlist still becomes one.
        self.assertEqual(drop(self.window, paths=[srt]), (False, False))
        self.assertEqual(drop(self.window, paths=[srt, path]), (True, True))
        self.assertTrue(wait_until(lambda: "My channels" in self.names()))
        self.assertFalse(any(text.startswith("Subtitles:") for text, _kind in self.toasts))

    def test_dropped_xtream_still_adds_at_once(self):
        # A whole provider's link is added at once, as Free channels adds one: no card, no
        # Settings (and an M3U playlist's link too).
        self.show()
        window = self.window
        link = f"{PANEL}/player_api.php?username=ann&password=pw1"
        self.assertEqual(drop(window, urls=[link], text=link), (True, True))
        self.assertTrue(wait_until(lambda: "127.0.0.1" in self.names()))
        added = window.provider_manager.providers[-1]
        self.assertEqual(
            {k: added[k] for k in ("type", "url", "username", "password")},
            {"type": "XTREAM", "url": PANEL, "username": "ann", "password": "pw1"},
        )
        self.assertIn(("Added 127.0.0.1 to your providers", "info"), self.toasts)
        self.assertEqual(self.config.selected_provider_name, "127.0.0.1")
        playlist = f"{PANEL}/lists/all.m3u"
        self.assertEqual(drop(window, text=playlist), (True, True))
        self.assertTrue(wait_until(lambda: "127.0.0.1 (2)" in self.names()))
        self.assertEqual(window.provider_manager.providers[-1]["url"], playlist)
        elapse(20)
        self.assertFalse(window.settings.visible())
        self.assertFalse(window.provider_cards.form.isVisible())

    def test_a_dropped_portal_link_without_mac_opens_the_card_filled(self):
        self.show()
        window = self.window
        before = self.names()
        portal = "http://portal.example.test/stalker_portal/c/"
        form = window.provider_cards.form
        self.assertEqual(drop(window, text=portal), (True, True))
        self.assertTrue(wait_until(form.isVisible))
        self.assertTrue(window.settings.visible())
        self.assertEqual(window.settings_drawer.path(), (PROVIDERS,))
        self.assertEqual(form.title(), "Add a provider")
        values = form.values()
        self.assertEqual(list(values), ["url", "mac", "serial_number", "device_id", "name"])
        self.assertEqual((values["url"], values["mac"]), (portal, ""))
        self.assertEqual(form.found(), ("✓ STB portal at portal.example.test", True))
        self.assertEqual(form.focus(), ("field", "mac"))  # the keys on what it lacks
        self.assertEqual(self.names(), before)  # nothing kept before its check
        elapse(QApplication.doubleClickInterval() + 80)  # no second-click guard left armed

    def test_dropping_a_stream_link_opens_the_card(self):
        # Plan 4: one stream's link opens the Open Link card, filled in (Enter plays it): an
        # Xtream stream's link too, which no longer adds its whole account, and a link only its
        # first bytes tell (an .m3u8, a web page's address), read once Play is pressed.
        self.show()
        window, card = self.window, self.window.open_link_card
        before = self.names()
        live = f"{PANEL}/live/ann/pw1/7.ts"
        for link, found in (
            (live, "✓ A live stream"),
            ("http://vod.example.test/films/harbour.mkv", "✓ A film"),
            ("https://cdn.example.test/live/harbour/index.m3u8?token=aaa", ""),
            ("http://example.test/page.html", ""),
        ):
            with self.subTest(link=link):
                self.assertEqual(drop(window, urls=[link], text=link), (True, True))
                self.assertTrue(wait_until(lambda: not card.isHidden()))
                self.assertEqual(card.values(), {"link": link})
                self.assertEqual(card.found()[0], found)
                QTest.keyClick(window, Qt.Key.Key_Escape)
                self.assertTrue(card.isHidden())
        elapse(20)
        self.assertEqual(self.names(), before)  # no provider for one stream
        with mock.patch.object(window, "play_network_link") as play:
            self.assertEqual(drop(window, text=live), (True, True))
            self.assertTrue(wait_until(lambda: not card.isHidden()))
            QTest.keyClick(window, Qt.Key.Key_Return)
        play.assert_called_once_with(live, LIVE)
        self.assertTrue(card.isHidden())

    def test_dropping_an_account_still_adds_it(self):
        # A whole provider's link is still added at once (an account typed without its
        # scheme, a playlist's address); dropped with a stream's link, the account is added
        # and the card opens for the stream.
        self.show()
        window, card = self.window, self.window.open_link_card
        for text, name in (
            ("127.0.0.1:9/player_api.php?username=bob&password=pw2", "127.0.0.1"),
            (f"{PANEL}/lists/all.m3u", "127.0.0.1 (2)"),
        ):
            self.assertEqual(drop(window, text=text), (True, True))
            self.assertTrue(wait_until(lambda: name in self.names()))
        elapse(20)
        self.assertTrue(card.isHidden())
        account = f"{PANEL}/get.php?username=ann&password=pw1"
        stream = "http://cdn.example.test/live/harbour.ts"
        self.assertEqual(drop(window, text=f"{account}\n{stream}"), (True, True))
        self.assertTrue(wait_until(lambda: "127.0.0.1 (3)" in self.names()))
        self.assertTrue(wait_until(lambda: not card.isHidden()))
        self.assertEqual(card.values(), {"link": stream})
        added = window.provider_manager.providers[-1]
        self.assertEqual((added["username"], added["password"]), ("ann", "pw1"))

    def test_other_text_is_refused_and_another_file_adds_no_provider(self):
        before = self.names()
        self.assertEqual(drop(self.window, text="Harbour News at nine"), (False, False))
        # A media file plays (tests/test_open_router.py). Another file is taken, as it may be a
        # folder, which only the disk tells (Task 11): it says QiTV can't play it.
        notes = self.folder / "notes.txt"
        notes.write_text("x", encoding="utf-8")
        self.assertEqual(drop(self.window, paths=[notes]), (True, True))
        text = "QiTV can't play “notes.txt”"
        self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        self.assertEqual(self.names(), before)


if __name__ == "__main__":
    unittest.main()
