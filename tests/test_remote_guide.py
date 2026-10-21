"""A phone's Guide (ui/remote_guide.py): the channel list's groups as the TV lists them, a group's
channels with what's on, a channel's timetable; and what a tap on them does on the TV."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest import mock

from test_tv_window import WindowCase, wait_until

from services.epg_view import Programme
from services.remote.protocol import MAX_MESSAGE, Get, OpenItem
from tests.fixtures import local_provider
from tests.fixtures.local_provider import PROVIDER_NAME
from ui.remote_guide import GUIDE_PAGE, PhoneGuide


def programme(title, start, minutes=30):
    return Programme(start=start, stop=start + timedelta(minutes=minutes), title=title)


class PhoneGuideTests(WindowCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.guide = PhoneGuide(self.window, self.sent.append)
        self.addCleanup(self.guide.deleteLater)

    def start(self):
        """Channel 1 on, Channel 2 watched before; the phone's Guide open where the TV is."""
        self.open_first_channel()
        config = self.window.config_manager
        watched = local_provider.channels()[1]
        entry = {"item_data": watched, "item_type": "channel", "provider_name": PROVIDER_NAME}
        config.watch_history = [entry] + list(config.watch_history or [])
        self.guide.channels("", 0, "")
        self.assertTrue(wait_until(lambda: self.last() and self.last()["rows"]))

    def last(self, kind="guide"):
        found = [m for m in self.sent if m["t"] == kind]
        return found[-1] if found else None

    def group_keys(self):
        self.guide.groups(0, "")
        return {g["title"]: g["id"] for g in self.last("groups")["rows"] if "id" in g}

    def rows(self):
        return [row["name"] for row in self.last()["rows"]]

    def test_opens_where_the_tv_is_with_its_channel(self):
        self.start()
        message = self.last()
        self.assertEqual(self.rows(), ["Channel 1", "Channel 2"])
        self.assertEqual([r["name"] for r in message["rows"] if r["playing"]], ["Channel 1"])
        self.assertEqual((message["title"], message["count"]), ("All", 2))
        self.assertNotIn("file:", json.dumps(message))

    def test_lists_the_groups_as_keys(self):
        self.start()
        keys = self.group_keys()
        self.assertTrue(any("Recent" in title for title in keys), keys)
        self.assertIn("Fixture", keys)
        for key in keys.values():
            self.assertTrue(key.startswith("g"), key)  # the phone sees keys, not group ids
        self.guide.groups(0, "fix")  # the find box searches the groups
        self.assertEqual([g["title"] for g in self.last("groups")["rows"]], ["Fixture"])

    def test_a_group_by_its_key(self):
        self.start()
        recent = next(key for title, key in self.group_keys().items() if "Recent" in title)
        self.guide.channels(recent, 0, "")
        self.assertEqual(self.last()["list"], recent)
        self.assertEqual(self.rows()[-1], "Channel 2")

    def test_find_searches_the_whole_group(self):
        self.start()
        self.guide.channels(self.last()["list"], 0, "nnel 2")
        self.assertEqual(self.rows(), ["Channel 2"])
        self.guide.channels(self.last()["list"], 0, "2")  # a channel's number
        self.assertEqual(self.rows(), ["Channel 2"])

    def test_opens_on_the_page_of_the_channel_playing(self):
        self.start()
        many = [{"name": f"Channel {i}", "cmd": f"file:///{i}", "id": i} for i in range(200)]
        playing = many[130]
        with (
            mock.patch.object(self.window.controller, "_channel", playing),
            mock.patch.object(self.guide, "_channels", return_value=many),
        ):
            self.guide.channels("", 0, "")
        message = self.last()
        self.assertEqual((message["page"], message["pages"]), (130 // GUIDE_PAGE, 4))
        self.assertEqual([r["name"] for r in message["rows"] if r["playing"]], ["Channel 130"])

    def test_the_largest_messages_fit_a_frame_in_any_script(self):
        self.start()
        long = "字" * 300  # 3 bytes a letter; titles and descriptions are cut to what shows
        now = datetime.now(timezone.utc)
        listed = [
            Programme(
                title=long,
                start=now + timedelta(minutes=30 * i),
                stop=now + timedelta(minutes=30 * (i + 1)),
                description=long,
            )
            for i in range(80)
        ]
        many = [{"name": f"{long}{i}", "cmd": f"file:///{i}", "id": i} for i in range(80)]
        controller = self.window.controller
        with (
            mock.patch.object(self.window.catalog, "programmes", return_value=listed),
            mock.patch.object(controller, "now_next", return_value=(listed[0], listed[1])),
            mock.patch.object(self.guide, "_channels", return_value=many),
        ):
            self.guide.channels(self.last()["list"], 0, "")
            self.guide.timetable(self.last()["rows"][0]["id"])
        for message in (self.last(), self.last("timetable")):
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
            self.assertLessEqual(len(encoded), MAX_MESSAGE, message["t"])
        self.assertEqual(len(self.last()["rows"]), GUIDE_PAGE)

    def test_a_tap_tunes_within_the_group(self):
        self.start()
        fixture = self.group_keys()["Fixture"]
        self.guide.channels(fixture, 0, "")
        second = self.last()["rows"][1]
        remote = self.window.remote
        remote._guides["s1"] = self.guide
        self.addCleanup(remote._guides.pop, "s1", None)
        remote.bridge.post(OpenItem("s1", second["id"]))
        controller = self.window.controller
        self.assertTrue(wait_until(lambda: (controller._channel or {}).get("name") == "Channel 2"))
        self.assertNotEqual(controller.zap.context.group_key, "*")  # the group's, not every one

    def test_timetable_marks_what_is_on(self):
        self.start()
        now = datetime.now(timezone.utc)
        listed = [
            programme("Noon", now - timedelta(minutes=10)),
            programme("Evening", now + timedelta(minutes=20)),
        ]
        channel = self.last()["rows"][0]["id"]
        with mock.patch.object(self.window.catalog, "programmes", return_value=listed):
            self.guide.timetable(channel)
        table = self.last("timetable")
        self.assertEqual((table["id"], table["name"]), (channel, "Channel 1"))
        self.assertEqual([r["title"] for r in table["rows"]], ["Noon", "Evening"])
        on, later = table["rows"]
        self.assertTrue(on["on"])
        self.assertIn("id", on)  # watching what's on: the channel live
        self.assertNotIn("id", later)
        self.assertAlmostEqual(on["start"], listed[0].start.timestamp())
        self.assertNotIn("loading", table)

    def test_an_empty_timetable_says_when_the_guide_is_still_loading(self):
        self.start()
        channel = self.last()["rows"][0]["id"]
        controller = self.window.controller
        with (
            mock.patch.object(self.window.catalog, "programmes", return_value=[]),
            mock.patch.object(controller, "guide_loading", return_value=True),
        ):
            self.guide.timetable(channel)
        self.assertTrue(self.last("timetable")["loading"])
        now = datetime.now(timezone.utc)
        with mock.patch.object(
            self.window.catalog, "programmes", return_value=[programme("Noon", now)]
        ):
            self.window.catalog.epgReady.emit()  # the guide is in: the timetable comes again
            self.assertEqual([r["title"] for r in self.last("timetable")["rows"]], ["Noon"])

    def test_timetable_offers_the_archive(self):
        self.start()
        now = datetime.now(timezone.utc)
        past = programme("Morning", now - timedelta(minutes=90))
        controller = self.window.controller
        with (
            mock.patch.object(controller, "archive_days", return_value=1),
            mock.patch.object(self.window.catalog, "programmes", return_value=[past]),
        ):
            self.guide.timetable(self.last()["rows"][0]["id"])
            [row] = self.last("timetable")["rows"]
        self.assertFalse(row["on"])
        remote = self.window.remote
        remote._guides["s1"] = self.guide
        self.addCleanup(remote._guides.pop, "s1", None)
        with mock.patch.object(self.window.screens, "watch_from_start") as watch:
            remote.bridge.post(OpenItem("s1", row["id"]))
            self.assertTrue(wait_until(lambda: watch.called))
        channel, shown, _group, _channels = watch.call_args.args
        self.assertEqual((channel["name"], shown.title), ("Channel 1", "Morning"))

    def test_a_programme_guide_arriving_pushes_whats_on(self):
        self.start()
        self.assertIsNone(self.last()["rows"][0]["now"])
        now = datetime.now(timezone.utc)
        on = programme("Noon", now - timedelta(minutes=5))
        with mock.patch.object(self.window.catalog, "programmes", return_value=[on]):
            self.window.catalog.epgReady.emit()
            self.assertTrue(wait_until(lambda: self.last()["rows"][0]["now"] is not None))
        self.assertEqual(self.last()["rows"][0]["now"]["title"], "Noon")

    def test_the_remote_answers_each_phone_and_loads_the_guides_it_lists(self):
        self.open_first_channel()
        remote = self.window.remote
        hub = mock.Mock()
        remote.hub = hub
        self.addCleanup(setattr, remote, "hub", None)
        controller = self.window.controller
        with mock.patch.object(controller, "use_phone_guides") as guides:
            remote.bridge.post(Get("s2", "guide", "", 0))
            self.assertTrue(
                wait_until(lambda: any(c.args[1]["t"] == "guide" for c in hub.send.call_args_list))
            )
        self.assertIn("s2", remote._guides)
        guides.assert_called_with({PROVIDER_NAME})


class GuidesInUseTests(WindowCase):
    def test_the_phones_providers_join_the_guides_in_use(self):
        controller = self.window.controller
        controller.use_phone_guides({"Another provider"})
        self.assertIn("Another provider", controller.guides_in_use())
        controller.use_phone_guides(set())
        self.assertNotIn("Another provider", controller.guides_in_use())


if __name__ == "__main__":
    unittest.main()
