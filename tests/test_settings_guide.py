"""Settings › Guide in the drawer (plan 3): the old Settings window's EPG tab, row by row, each
change applied at once: the guide loaded again in the background, the rows following its
source, the update period in a card, the steppers read live, and the channel matching as a
layer of its own (each entry in a card; imported and exported on a worker). Over a real TV
window; nothing reaches the network."""

import os
from pathlib import Path
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
import orjson
from timer_fixtures import wait_until

from services import settings_model as m
from services.config_manager import ConfigManager
from services.epg_manager import EpgManager
from services.i18n import translate
from services.multikeydict import MultiKeyDict
from tests.test_settings_drawer import tip
from tests.test_settings_general import GeneralCase
from ui import settings_guide as guide
from ui.keymap import Action
from ui.overlays import settings_layout
from ui.overlays.settings_card import CANCEL

PORTAL = {
    "type": "STB",
    "name": "Portal",
    "url": "http://portal.test/c/",
    "mac": "00:1A:79:00:00:01",
}


def mapping(*entries):
    """A channel map of (name, IDs) entries, in their order."""
    result = MultiKeyDict()
    for name, ids in entries:
        result[tuple(ids)] = {"name": name, "icon": ""}
    return result


class GuideCase(GeneralCase):
    source = "STB"
    channel_map: MultiKeyDict = MultiKeyDict()

    def prepare_config(self):
        self.config.epg_source = self.source
        self.config.xmltv_channel_map = MultiKeyDict.deserialize(self.channel_map.serialize())

    def keys(self):
        return [row.key for row in self.drawer._right_rows()]

    def note(self, key):
        return self.shown()[key].note

    def fit(self):
        """At 1280×720, every title of the right column in sight shows whole: hovering it says
        only its help (a title cut short says itself whole there too)."""
        host = self.drawer.parentWidget()
        self.window.resize(1280, 720)
        self.assertTrue(wait_until(lambda: self.drawer.height() == host.height()))
        self.drawer.grab()
        for index, row in enumerate(self.drawer._right_rows()):
            if self.drawer.row_rect("right", index).isEmpty():
                continue  # out of sight in a long list
            point = self.drawer.title_rect(index).center()
            self.assertEqual(self.drawer.tooltip_at(point), tip(row), row.key)

    def go_to(self, key):
        """The keys on the right column's row `key`: Settings opened at the Guide when it is
        not open, else from the top of the column it shows."""
        if not self.window.settings.visible():
            return self.open_at(m.GUIDE, key)
        if self.drawer.focus_side() != "right":
            self.key(Action.NAV_RIGHT)
        count = len(self.drawer._right_rows())
        for _ in range(count):
            self.key(Action.NAV_UP)
        for _ in range(count + 1):
            row = self.drawer.focused_row()
            if row is not None and row.key == key:
                return row
            self.key(Action.NAV_DOWN)
        self.fail(f"no row {key}")

    def card_focus(self, card, wanted):
        """The keys on the card's button `wanted` (↓ to its button row, then ←/→ along it)."""
        for _ in range(len(card._fields)):
            if card.focus()[0] == "button":
                break
            self.key(Action.NAV_DOWN)
        keys = [b.key for b in card._buttons]
        for _ in range(len(keys)):
            zone, here = card.focus()
            if (zone, here) == ("button", wanted):
                return
            self.key(Action.NAV_RIGHT if keys.index(wanted) > keys.index(here) else Action.NAV_LEFT)
        self.assertEqual(card.focus(), ("button", wanted))


class SourceTests(GuideCase):
    def test_source_changes_reload_in_the_background(self):
        with mock.patch.object(self.window.catalog, "refresh_epg") as refresh:
            row = self.open_at(m.GUIDE, "epg_source")
            self.assertEqual(
                [c.value for c in row.choices], ["No Source", "STB", "Local File", "URL"]
            )
            self.assertEqual(
                [c.shown() for c in row.choices],
                ["No Source", "Provider's own guide", "Local File", "URL"],
            )
            self.assertEqual(self.window.settings.value_text(row), "Provider's own guide")
            self.assertFalse(self.window.settings.lit(row))  # the config's default
            self.key(Action.NAV_RIGHT)
            self.assertEqual(self.config.epg_source, "Local File")
            refresh.assert_called_once_with(force=True)  # its worker loads it
            self.key(Action.NAV_RIGHT)
            self.assertEqual(self.config.epg_source, "URL")
            self.key(Action.NAV_LEFT)
            self.key(Action.NAV_LEFT)
            self.key(Action.NAV_LEFT)
            self.assertEqual(self.config.epg_source, "No Source")
            self.assertEqual(refresh.call_count, 5)
            self.assertEqual(self.drawer.focused_row().key, "epg_source")  # the place kept
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().epg_source, "No Source")
        self.assertNotIn("epg", m.LEGACY_TABS.values())  # no longer the Settings window's
        self.assertEqual(
            next(s for s in self.window.settings._sections if s.key == m.GUIDE).legacy_tab, ""
        )

    def test_url_and_file_rows_follow_the_source(self):
        with (
            mock.patch.object(self.window.catalog, "refresh_epg") as refresh,
            mock.patch.object(guide, "pick_file", return_value="/srv/guides/guide.xml") as pick,
        ):
            self.open_at(m.GUIDE, "epg_source")
            # The provider's own guide: how often it is checked, and how far the list reaches.
            self.assertEqual(self.keys(), ["epg_source", guide.EVERY, "epg_list_window_hours"])
            self.assertEqual(self.note(guide.EVERY), "2 hours")
            self.fit()
            self.key(Action.NAV_RIGHT)  # a local file: no update period; its channel matching
            self.assertEqual(
                self.keys(),
                ["epg_source", guide.FILE, "epg_list_window_hours", guide.MATCHING],
            )
            self.assertEqual(self.note(guide.FILE), "")
            self.fit()
            self.key(Action.NAV_DOWN)
            self.assertEqual(self.drawer.focused_row().key, guide.FILE)
            self.key(Action.NAV_ACTIVATE)  # the system's picker, then the guide loads again
            pick.assert_called_once()
            self.assertEqual(self.config.epg_file, "/srv/guides/guide.xml")
            self.assertEqual(self.note(guide.FILE), "/srv/guides/guide.xml")
            self.assertEqual(refresh.call_count, 2)
            pick.return_value = None  # none chosen: nothing changes
            self.key(Action.NAV_ACTIVATE)
            self.assertEqual(self.config.epg_file, "/srv/guides/guide.xml")
            self.assertEqual(refresh.call_count, 2)
            self.key(Action.NAV_UP)
            self.key(Action.NAV_RIGHT)  # a web address: edited in place, and loaded again
            self.assertEqual(
                self.keys(),
                [
                    "epg_source",
                    "epg_url",
                    guide.EVERY,
                    "epg_list_window_hours",
                    guide.MATCHING,
                ],
            )
            self.fit()
            self.key(Action.NAV_DOWN)
            self.assertEqual(self.drawer.focused_row().kind, m.TEXT)
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_TEXT, "https://guide.example/epg.xml")
            self.key(Action.NAV_ACTIVATE)
            self.assertEqual(self.config.epg_url, "https://guide.example/epg.xml")
            self.assertEqual(refresh.call_count, 4)
            self.key(Action.NAV_UP)
            self.key(Action.NAV_RIGHT)  # no guide: the list window only
            self.assertEqual(self.keys(), ["epg_source", "epg_list_window_hours"])
            self.fit()
        self.config.save_config(wait=True)
        saved = ConfigManager()
        self.assertEqual(
            (saved.epg_file, saved.epg_url),
            ("/srv/guides/guide.xml", "https://guide.example/epg.xml"),
        )


class UpdatePeriodTests(GuideCase):
    def open_card(self):
        self.go_to(guide.EVERY)
        self.key(Action.NAV_ACTIVATE)
        card = self.window.guide_rows.every_card
        self.assertTrue(self.window.settings.card_shown())
        self.assertEqual(card.title(), "Check for updates every")
        return card

    def unit(self, card):
        return next(b.text for b in card._buttons if b.key == guide.UNIT)

    def test_update_every_card_keeps_minutes(self):
        self.config.epg_expiration_value = 30
        self.config.epg_expiration_unit = "Minutes"  # an older QiTV's
        with mock.patch.object(self.window.catalog, "refresh_epg") as refresh:
            card = self.open_card()
            self.assertEqual(self.note(guide.EVERY), "30 minutes")
            self.assertEqual(card.values(), {guide.NUMBER: "30"})
            self.assertEqual(card.found(), ("30 minutes", True))
            self.assertEqual(self.unit(card), "Minutes")
            self.card_focus(card, guide.UNIT)
            units = []
            for _ in range(5):  # the unit steps through them all, Minutes kept among them
                self.key(Action.NAV_ACTIVATE)
                units.append(self.unit(card))
            self.assertEqual(units, ["Hours", "Days", "Weeks", "Months", "Minutes"])
            self.assertEqual(card.focus(), ("button", guide.UNIT))
            self.key(Action.NAV_ACTIVATE)  # Hours
            self.assertEqual(card.found(), ("30 hours", True))
            self.card_focus(card, CANCEL)  # Cancel: nothing kept
            self.key(Action.NAV_ACTIVATE)
            self.assertFalse(self.window.settings.card_shown())
            self.assertEqual(
                (self.config.epg_expiration_value, self.config.epg_expiration_unit),
                (30, "Minutes"),
            )
            card = self.open_card()  # Save as it is: Minutes stays Minutes
            self.assertEqual(self.unit(card), "Minutes")
            self.key(Action.NAV_ACTIVATE)  # from the field: Save
            self.assertFalse(self.window.settings.card_shown())
            self.assertEqual(self.config.epg_expiration_unit, "Minutes")
            refresh.assert_not_called()  # read when the guide is next looked at

    def test_the_number_is_digits_from_1_to_9999(self):
        with mock.patch.object(self.window.catalog, "refresh_epg") as refresh:
            card = self.open_card()
            self.assertEqual(self.unit(card), "Hours")
            self.key(Action.NAV_TEXT, "4a")  # a letter is no part of it
            self.assertEqual(card.values(), {guide.NUMBER: "24"})
            self.assertEqual(card.found(), ("24 hours", True))
            self.card_focus(card, guide.UNIT)
            for _ in range(4):  # Hours, Days, Weeks, Months, and round to Hours: no Minutes
                self.key(Action.NAV_ACTIVATE)
            self.assertEqual(self.unit(card), "Hours")
            self.key(Action.NAV_ACTIVATE)  # Days
            self.key(Action.NAV_UP)
            for _ in range(2):
                self.key(Action.NAV_BACKSPACE)
            self.key(Action.NAV_ACTIVATE)  # nothing to keep: said, and nothing written
            self.assertTrue(card.isVisible())
            text, ok = card.found()
            self.assertFalse(ok)
            self.assertEqual(text, translate(guide.RANGE_TEXT))
            self.assertEqual(
                (self.config.epg_expiration_value, self.config.epg_expiration_unit), (2, "Hours")
            )
            self.key(Action.NAV_TEXT, "10000")
            self.key(Action.NAV_ACTIVATE)
            self.assertFalse(card.found()[1])
            self.key(Action.NAV_BACKSPACE)
            self.key(Action.NAV_ACTIVATE)  # 1000 days
            self.assertFalse(self.window.settings.card_shown())
            self.assertEqual(
                (self.config.epg_expiration_value, self.config.epg_expiration_unit), (1000, "Days")
            )
            self.assertEqual(self.note(guide.EVERY), "1,000 days")
            self.assertEqual(self.drawer.focused_row().key, guide.EVERY)
            refresh.assert_not_called()
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().epg_expiration_unit, "Days")


class SteppersTests(GuideCase):
    def test_portal_period_only_with_a_portal(self):
        self.open_at(m.GUIDE, "epg_source")
        self.assertNotIn("epg_stb_period_hours", self.keys())  # a playlist only
        self.window.provider_manager.providers.append(dict(PORTAL))
        self.window.settings.refresh_sections()  # (a provider added: the sections are built again)
        self.assertIn("epg_stb_period_hours", self.keys())
        self.fit()
        row = self.go_to("epg_stb_period_hours")
        self.assertEqual([c.value for c in row.choices], [1, 2, 3, 5, 8, 12, 24, 48, 168])
        self.assertEqual(self.window.settings.value_text(row), "5 hours")
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.config.epg_stb_period_hours, 8)
        # Only the provider's own guide has one: a web address' has none.
        self.config.epg_source = "URL"
        self.window.settings.refresh_sections()
        self.assertNotIn("epg_stb_period_hours", self.keys())
        # A period the old window took that is none of the steps shows as it is.
        self.config.epg_source = "STB"
        self.config.epg_stb_period_hours = 7
        self.window.settings.refresh_sections()
        row = self.shown()["epg_stb_period_hours"]
        self.assertEqual([c.value for c in row.choices], [1, 2, 3, 5, 7, 8, 12, 24, 48, 168])
        self.assertEqual(self.window.settings.value_text(row), "7 hours")

    def test_list_window_steps_and_is_read_live(self):
        row = self.open_at(m.GUIDE, "epg_list_window_hours")
        self.assertEqual([c.value for c in row.choices], [0, 6, 12, 24, 48, 72, 168])
        self.assertEqual(row.choices[0].shown(), "Unlimited")
        self.assertEqual(self.window.settings.value_text(row), "24 hours")
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.config.epg_list_window_hours, 48)
        for _ in range(3):
            self.key(Action.NAV_RIGHT)  # 72, 168, round to Unlimited
        self.assertEqual(self.config.epg_list_window_hours, 0)
        self.assertTrue(self.window.settings.lit(self.drawer.focused_row()))
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().epg_list_window_hours, 0)
        self.config.epg_list_window_hours = 36  # the old window's spinner took any
        self.window.settings.refresh_sections()
        row = self.shown()["epg_list_window_hours"]
        self.assertEqual([c.value for c in row.choices], [0, 6, 12, 24, 36, 48, 72, 168])


class MatchingCase(GuideCase):
    source = "URL"

    def open_layer(self):
        self.open_at(m.GUIDE, guide.MATCHING)
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer.path(), (m.GUIDE, guide.MATCHING))
        self.assertEqual(self.drawer.focus_side(), "right")

    def stored(self):
        return [(value["name"], list(ids)) for ids, value in self.config.xmltv_channel_map.items()]


class MatchingTests(MatchingCase):
    def test_mapping_add_edit_delete_reindexes(self):
        with mock.patch.object(EpgManager, "reindex_programs", autospec=True) as reindex:
            self.open_layer()
            self.assertEqual(self.keys(), [guide.ADD, guide.IMPORT, guide.EXPORT])
            self.assertEqual(self.shown()[guide.ADD].text, "＋ Add")
            self.fit()
            self.go_to(guide.ADD)
            self.key(Action.NAV_ACTIVATE)
            card = self.window.guide_rows.mapping_card
            self.assertTrue(self.window.settings.card_shown())
            self.assertEqual(
                [f.label for f in card._fields], ["Channel Name", "Logo URL", "Channel IDs"]
            )
            self.assertEqual([b.key for b in card._buttons], [CANCEL, guide.SAVE])  # no Delete
            self.key(Action.NAV_TEXT, "News One")
            self.key(Action.NAV_DOWN)
            self.key(Action.NAV_DOWN)
            self.key(Action.NAV_ACTIVATE)  # the IDs are needed: said, nothing kept
            self.assertEqual(card.found(), (translate(guide.REQUIRED_TEXT), False))
            self.assertEqual(self.stored(), [])
            reindex.assert_not_called()
            self.key(Action.NAV_TEXT, " news1.example ,news1.hd, ")
            self.key(Action.NAV_ACTIVATE)
            self.assertFalse(self.window.settings.card_shown())
            self.assertEqual(self.stored(), [("News One", ["news1.example", "news1.hd"])])
            self.assertEqual(reindex.call_count, 1)
            row = self.shown()[guide.MAP_PREFIX + "0"]
            self.assertEqual(
                (row.text, row.sub, row.literal), ("News One", "news1.example, news1.hd", True)
            )
            self.assertEqual(self.drawer.focused_row().key, guide.ADD)  # where it was
            # A second, then the first changed: it keeps its place in the list.
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_TEXT, "Sport")
            self.key(Action.NAV_DOWN)
            self.key(Action.NAV_DOWN)
            self.key(Action.NAV_TEXT, "sport.example")
            self.key(Action.NAV_ACTIVATE)
            self.assertEqual(reindex.call_count, 2)
            self.key(Action.NAV_UP)
            self.key(Action.NAV_UP)
            self.assertEqual(self.drawer.focused_row().key, guide.MAP_PREFIX + "0")
            self.key(Action.NAV_ACTIVATE)
            self.assertEqual(card.title(), "News One")
            self.assertEqual(
                card.values(),
                {guide.NAME: "News One", guide.ICON: "", guide.IDS: "news1.example, news1.hd"},
            )
            self.assertEqual([b.key for b in card._buttons], [guide.DELETE, CANCEL, guide.SAVE])
            self.key(Action.NAV_TEXT, " HD")
            self.key(Action.NAV_DOWN)
            self.key(Action.NAV_TEXT, "http://logos.example/n1.png")
            self.key(Action.NAV_ACTIVATE)
            self.assertEqual(
                self.stored(),
                [("News One HD", ["news1.example", "news1.hd"]), ("Sport", ["sport.example"])],
            )
            icon = self.config.xmltv_channel_map.get("news1.hd")["icon"]
            self.assertEqual(icon, "http://logos.example/n1.png")
            self.assertEqual(reindex.call_count, 3)
            self.assertEqual(self.drawer.focused_row().key, guide.MAP_PREFIX + "0")
            # Delete: at once, from its card.
            self.key(Action.NAV_ACTIVATE)
            self.card_focus(card, guide.DELETE)
            self.key(Action.NAV_ACTIVATE)
            self.assertFalse(self.window.settings.card_shown())
            self.assertEqual(self.stored(), [("Sport", ["sport.example"])])
            self.assertEqual(reindex.call_count, 4)
            self.assertNotIn("news1.hd", self.config.xmltv_channel_map)
            self.assertEqual(self.drawer.focused_row().text, "Sport")  # its neighbour
            # Cancel keeps it as it was.
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_TEXT, "x")
            self.key(Action.ESCAPE)
            self.assertEqual(self.stored(), [("Sport", ["sport.example"])])
            self.assertEqual(reindex.call_count, 4)
        self.config.save_config(wait=True)
        self.assertEqual(
            ConfigManager().xmltv_channel_map.serialize(),
            [["sport.example", {"name": "Sport", "icon": ""}]],
        )

    def test_the_provider_guide_is_never_reindexed(self):
        # The provider's own guide (STB) is no XMLTV guide: changing the matching re-keys
        # nothing, as the old Save did.
        with mock.patch.object(EpgManager, "reindex_programs", autospec=True) as reindex:
            self.open_layer()
            self.config.epg_source = "STB"  # (changed elsewhere while it shows)
            self.go_to(guide.ADD)
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_TEXT, "News One")
            self.key(Action.NAV_DOWN)
            self.key(Action.NAV_DOWN)
            self.key(Action.NAV_TEXT, "news1.example")
            self.key(Action.NAV_ACTIVATE)
            self.assertEqual(self.stored(), [("News One", ["news1.example"])])
            reindex.assert_not_called()


class FilesTests(MatchingCase):
    channel_map = mapping(("Kept", ["kept.example"]))

    def recording(self, name):
        """The module's function `name`, run as it is, the threads it ran on recorded."""
        threads = []
        real = getattr(guide, name)

        def record(*args):
            threads.append(threading.current_thread())
            return real(*args)

        return mock.patch.object(guide, name, side_effect=record), threads

    def test_import_and_export_off_the_gui_thread(self):
        source = Path(self.config_dir) / "matching.json"
        source.write_bytes(
            orjson.dumps(
                {
                    "news1.example": {
                        "name": "News One",
                        "icon": "",
                        "xmltv_id": ["news1.example", "news1.hd"],
                    },
                    "sport.example": {
                        "name": "Sport",
                        "icon": "http://logos.example/s.png",
                        "xmltv_id": ["sport.example"],
                    },
                    "broken": {"name": "No IDs"},
                }
            )
        )
        reading, read_on = self.recording("read_mapping")
        writing, written_on = self.recording("write_mapping")
        target = Path(self.config_dir) / "out"
        with (
            reading,
            writing,
            mock.patch.object(guide, "pick_file", return_value=str(source)),
            mock.patch.object(guide, "pick_save_file", return_value=str(target)),
            mock.patch.object(EpgManager, "reindex_programs", autospec=True) as reindex,
        ):
            self.open_layer()
            self.go_to(guide.IMPORT)
            self.key(Action.NAV_ACTIVATE)  # in place of the list (as the old window's)
            self.assertTrue(wait_until(lambda: len(self.drawer._right_rows()) == 5))
            self.assertEqual(
                self.stored(),
                [("News One", ["news1.example", "news1.hd"]), ("Sport", ["sport.example"])],
            )
            self.assertEqual(
                self.config.xmltv_channel_map.get("sport.example")["icon"],
                "http://logos.example/s.png",
            )
            self.assertEqual(reindex.call_count, 1)
            self.assertIn((translate(guide.IMPORTED_TEXT), "info"), self.toasts)
            self.assertEqual(self.drawer.focused_row().key, guide.IMPORT)
            self.key(Action.NAV_DOWN)
            self.key(Action.NAV_ACTIVATE)  # Export…: ".json" added, as the old window did
            out = Path(str(target) + ".json")
            self.assertTrue(
                wait_until(lambda: (translate(guide.EXPORTED_TEXT), "info") in self.toasts)
            )
            self.assertEqual(
                orjson.loads(out.read_bytes()),
                {
                    "news1.example": {
                        "name": "News One",
                        "icon": "",
                        "xmltv_id": ["news1.example", "news1.hd"],
                    },
                    "sport.example": {
                        "name": "Sport",
                        "icon": "http://logos.example/s.png",
                        "xmltv_id": ["sport.example"],
                    },
                },
            )
            # The map itself is as it was: no "xmltv_id" written into its entries.
            self.assertNotIn("xmltv_id", self.config.xmltv_channel_map.get("news1.hd"))
            # A file of another shape: said, and the list stays.
            source.write_text("[1, 2", encoding="utf-8")
            self.key(Action.NAV_UP)
            self.key(Action.NAV_ACTIVATE)
            self.assertTrue(
                wait_until(lambda: (translate(guide.IMPORT_FAILED_TEXT), "error") in self.toasts)
            )
            self.assertEqual(len(self.stored()), 2)
            self.assertEqual(reindex.call_count, 1)
        self.assertEqual(len(read_on), 2)
        self.assertEqual(len(written_on), 1)
        for thread in read_on + written_on:
            self.assertIsNot(thread, threading.main_thread())


class LongListTests(MatchingCase):
    channel_map = mapping(*((f"Channel {i}", [f"ch{i}.example", f"ch{i}.hd"]) for i in range(500)))

    def test_a_long_mapping_list_scrolls(self):
        self.open_layer()
        self.assertEqual(len(self.drawer._right_rows()), 503)
        self.drawer.grab()
        right = self.drawer.scroll_indicator("right")
        self.assertTrue(right.scrollable())
        self.assertFalse(self.drawer.row_rect("right", 0).isEmpty())
        self.assertTrue(self.drawer.row_rect("right", 499).isEmpty())
        for _ in range(60):  # a page at a time, to the end: ＋ Add, Import…, Export…
            self.key(Action.NAV_PAGE_DOWN)
        self.assertEqual(self.drawer.focused_row().key, guide.EXPORT)
        self.drawer.grab()
        self.assertFalse(self.drawer.row_rect("right", 502).isEmpty())
        self.assertTrue(self.drawer.row_rect("right", 0).isEmpty())
        self.key(Action.NAV_UP)
        self.key(Action.NAV_UP)
        self.key(Action.NAV_UP)
        row = self.drawer.focused_row()
        self.assertEqual((row.text, row.sub), ("Channel 499", "ch499.example, ch499.hd"))
        # The pointer over it lays nothing out again.
        column = self.drawer.column_rect("right")
        with mock.patch.object(settings_layout, "wrap", wraps=settings_layout.wrap) as wrap:
            for i in range(50):
                point = QPointF(
                    column.center().x(), column.top() + 40 + (i * 37) % int(column.height() - 60)
                )
                self.drawer.tooltip_at(point)
                if i % 10 == 0:
                    self.drawer.grab()
            self.assertEqual(wrap.call_count, 0)
        self.fit()
        # The Guide's row says how many there are.
        self.key(Action.ESCAPE)
        self.assertEqual(self.drawer.focused_row().key, guide.MATCHING)
        self.assertEqual(self.drawer.focused_row().note, "500")


if __name__ == "__main__":
    unittest.main()
