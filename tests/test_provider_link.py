"""One provider link fills the add-provider form (2.2): services.provider_link reads it, and
Settings' "Paste link" (or a link pasted into the address) fills the form. Fictional hosts."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from services.provider_link import M3U, STB, XTREAM, ProviderLink, parse_provider_link
from tests import test_tv_window as base
from ui.settings_dialog import NO_LINK_TEXT, OptionsDialog

PANEL = "http://panel.example.test:8080"


class ParseTests(unittest.TestCase):
    def test_xtream_links_give_server_username_and_password(self):
        for text in (
            f"{PANEL}/get.php?username=ann&password=pw1&type=m3u_plus&output=ts",
            f"{PANEL}/player_api.php?username=ann&password=pw1",
            f"  {PANEL}/xmltv.php?username=ann&password=pw1\n",
            f"{PANEL}/live/ann/pw1/123.ts",
            "panel.example.test:8080/get.php?password=pw1&username=ann",  # as typed, no scheme
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_provider_link(text), ProviderLink(XTREAM, PANEL, "ann", "pw1")
                )

    def test_the_scheme_and_an_encoded_password_survive(self):
        link = parse_provider_link(
            "https://panel.example.test/player_api.php?username=ann&password=p%40ss%26w"
        )
        self.assertEqual((link.url, link.password), ("https://panel.example.test", "p@ss&w"))
        self.assertEqual(link.name, "panel.example.test")

    def test_a_playlist_address_is_an_m3u_playlist(self):
        for text, url in (
            ("https://lists.example.test/tv/all.m3u8?token=abc#top", None),
            ("http://lists.example.test/news.M3U", None),
            ("http://lists.example.test/get.php?type=m3u", None),  # a panel's, no login in it
        ):
            with self.subTest(text=text):
                link = parse_provider_link(text)
                self.assertEqual(link.type, M3U)
                self.assertEqual(link.url, url or text.split("#")[0])
                self.assertEqual((link.username, link.password), ("", ""))

    def test_a_portal_address_is_an_stb_portal(self):
        cases = {
            "http://portal.example.test:8080/c/": "http://portal.example.test:8080/c/",
            "http://portal.example.test:8080/c": "http://portal.example.test:8080/c/",
            "http://portal.example.test/stalker_portal/c/index.html": (
                "http://portal.example.test/stalker_portal/c/"
            ),
            "http://portal.example.test/stalker_portal/server/load.php": (
                "http://portal.example.test/stalker_portal/"
            ),
        }
        for text, url in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_provider_link(text), ProviderLink(STB, url))

    def test_a_portal_link_may_name_its_mac(self):
        link = parse_provider_link("http://portal.example.test/c/?mac=00:1a:79:aa:bb:cc")
        self.assertEqual(link.mac, "00:1A:79:AA:BB:CC")
        self.assertEqual(parse_provider_link("http://portal.example.test/c/?mac=nope").mac, "")

    def test_anything_else_is_no_link(self):
        for text in (
            "",
            "not a link",
            PANEL,  # a bare server: which type?
            f"{PANEL}/get.php?username=ann",  # no password
            "ftp://lists.example.test/all.m3u",
            "http://lists.example.test:port/all.m3u",
            "file:///home/someone/all.m3u",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_provider_link(text))

    def test_the_password_stays_out_of_its_repr(self):
        link = parse_provider_link(f"{PANEL}/get.php?username=ann&password=pw1")
        self.assertNotIn("pw1", repr(link))


class PasteLinkTests(base.WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        dialog.start_new_provider()
        return dialog

    def paste(self, text):
        QGuiApplication.clipboard().setText(text)
        self.addCleanup(QGuiApplication.clipboard().clear)

    def test_paste_link_fills_an_xtream_provider(self):
        dialog = self.dialog()
        self.paste(f"{PANEL}/get.php?username=ann&password=pw1&type=m3u_plus")
        dialog.paste_link_button.click()
        self.assertTrue(dialog.type_XTREAM.isChecked())
        self.assertEqual(dialog.url_input.text(), PANEL)
        self.assertEqual(dialog.username_input.text(), "ann")
        self.assertEqual(dialog.password_input.text(), "pw1")
        self.assertEqual(dialog.name_input.text(), "panel.example.test")
        self.assertFalse(dialog.username_input.isHidden())
        self.assertIn("Xtream", dialog.verify_result.text())
        provider = dialog._provider_from_inputs()
        self.assertEqual(
            (provider["type"], provider["url"], provider["username"], provider["password"]),
            ("XTREAM", PANEL, "ann", "pw1"),
        )

    def test_a_link_pasted_into_the_address_fills_the_form(self):
        dialog = self.dialog()
        dialog.url_input.setFocus()
        self.paste("http://portal.example.test/stalker_portal/c/")
        dialog.url_input.paste()
        QTest.qWait(10)  # it fills once the paste's own signals are out
        self.assertTrue(dialog.type_STB.isChecked())
        self.assertEqual(dialog.url_input.text(), "http://portal.example.test/stalker_portal/c/")
        self.assertIn("MAC address", dialog.verify_result.text())

    def test_typing_a_link_never_cuts_it_up(self):
        dialog = self.dialog()
        QTest.keyClicks(dialog.url_input, f"{PANEL}/get.php?username=ann&password=pw1")
        QTest.qWait(10)
        self.assertEqual(dialog.url_input.text(), f"{PANEL}/get.php?username=ann&password=pw1")
        self.assertTrue(dialog.type_STB.isChecked())  # a new provider's type, untouched

    def test_no_link_on_the_clipboard_is_said(self):
        dialog = self.dialog()
        self.paste("hello")
        dialog.paste_link_button.click()
        self.assertEqual(dialog.verify_result.text(), NO_LINK_TEXT)
        self.assertEqual(dialog.url_input.text(), "")

    def test_a_named_provider_keeps_its_name_and_an_m3u_stream_its_type(self):
        dialog = self.dialog()
        dialog.name_input.setText("Living room")
        dialog.type_M3USTREAM.setChecked(True)
        self.paste("https://lists.example.test/one/channel.m3u8")
        dialog.paste_link_button.click()
        self.assertEqual(dialog.name_input.text(), "Living room")
        self.assertTrue(dialog.type_M3USTREAM.isChecked())
        self.assertEqual(dialog.url_input.text(), "https://lists.example.test/one/channel.m3u8")

    def test_the_paste_logs_nothing_of_the_link(self):
        dialog = self.dialog()
        self.paste(f"{PANEL}/get.php?username=ann&password=pw1")
        with self.assertNoLogs(level="DEBUG"):
            dialog.paste_link_button.click()


if __name__ == "__main__":
    unittest.main()
