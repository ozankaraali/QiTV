"""A phone's Guide (ui/remote_guide.py): the channel list's groups as the TV lists them, a page of
channels with what's on, and a channel's timetable; and what a tap on them does on the TV."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest import mock

from test_tv_window import WindowCase, wait_until

from services.epg_view import Programme
from services.remote.protocol import Get, OpenItem
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
        """Channel 1 on, Channel 2 watched before; the phone's Guide open on the TV's group."""
        self.open_first_channel()
        config = self.window.config_manager
        watched = local_provider.channels()[1]
        entry = {"item_data": watched, "item_type": "channel", "provider_name": PROVIDER_NAME}
        config.watch_history = [entry] + list(config.watch_history or [])
        self.guide.ask("", 0, "")
        self.assertTrue(wait_until(lambda: self.last() and self.last()["rows"]))

    def last(self, kind="guide"):
        found = [m for m in self.sent if m["t"] == kind]
        return found[-1] if found else None

    def groups(self):
        return {g["title"]: g["id"] for g in self.last()["groups"] if not g.get("header")}

    def rows(self):
        return [row["name"] for row in self.last()["rows"]]

    def test_lists_the_groups_and_opens_on_the_tvs(self):
        self.start()
        message = self.last()
        titles = [g["title"] for g in message["groups"]]
        self.assertTrue(any("Recent" in t for t in titles), titles)
        self.assertIn("Fixture", titles)
        self.assertEqual(self.rows(), ["Channel 1", "Channel 2"])
        self.assertEqual([r["name"] for r in message["rows"] if r["playing"]], ["Channel 1"])
        self.assertEqual(message["page"], 0)
        self.assertNotIn("file:", json.dumps(message))
        for key in self.groups().values():
            self.assertTrue(key.startswith("g"), key)  # the phone sees keys, not group ids

    def test_another_group_by_its_key(self):
        self.start()
        recent = next(key for title, key in self.groups().items() if "Recent" in title)
        self.guide.ask(recent, 0, "")
        self.assertEqual(self.last()["list"], recent)
        self.assertEqual(self.rows()[-1], "Channel 2")

    def test_find_searches_the_whole_group(self):
        self.start()
        self.guide.ask(self.last()["list"], 0, "nnel 2")
        self.assertEqual(self.rows(), ["Channel 2"])
        self.guide.ask(self.last()["list"], 0, "2")  # a channel's number
        self.assertEqual(self.rows(), ["Channel 2"])

    def test_pages(self):
        many = [{"name": f"Channel {i}", "cmd": f"file:///{i}", "id": i} for i in range(120)]
        with mock.patch.object(self.guide.nav, "children", return_value=None):
            self.guide.ask("", 0, "")
        self.assertTrue(self.last()["loading"])
        self.guide._channels = lambda node: many
        self.guide._group_nodes = lambda: [
            mock.Mock(layer="group", key="k", title="Big", count=120)
        ]
        self.guide.ask("", 2, "")
        self.assertEqual(
            (self.last()["pages"], len(self.last()["rows"])), (3, 120 - 2 * GUIDE_PAGE)
        )

    def test_a_tap_tunes_within_the_group(self):
        self.start()
        fixture = self.groups()["Fixture"]
        self.guide.ask(fixture, 0, "")
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
            programme("Morning", now - timedelta(minutes=50)),
            programme("Noon", now - timedelta(minutes=10)),
            programme("Evening", now + timedelta(minutes=20)),
        ]
        channel = self.last()["rows"][0]["id"]
        with mock.patch.object(self.window.catalog, "programmes", return_value=listed[1:]):
            self.guide.timetable(channel)
        table = self.last("timetable")
        self.assertEqual((table["id"], table["name"]), (channel, "Channel 1"))
        self.assertEqual([r["title"] for r in table["rows"]], ["Noon", "Evening"])
        on, later = table["rows"]
        self.assertTrue(on["on"])
        self.assertIn("id", on)  # watching what's on: the channel live
        self.assertNotIn("id", later)
        self.assertAlmostEqual(on["start"], listed[1].start.timestamp())

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

    def test_the_remote_answers_each_phone_from_its_own_guide(self):
        self.open_first_channel()
        remote = self.window.remote
        hub = mock.Mock()
        remote.hub = hub
        self.addCleanup(setattr, remote, "hub", None)
        remote.bridge.post(Get("s2", "guide", "", 0))
        self.assertTrue(
            wait_until(lambda: any(c.args[1]["t"] == "guide" for c in hub.send.call_args_list))
        )
        self.assertIn("s2", remote._guides)


if __name__ == "__main__":
    unittest.main()
