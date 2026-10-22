"""A phone's Guide (ui/remote_guide.py): the channel list's groups as the TV lists them, a group's
channels with what's on, a channel's timetable; and what a tap on them does on the TV."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest import mock

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from test_tv_window import WindowCase, wait_until

from services.epg_view import Programme
from services.find import FoundChannel, FoundGenre
from services.navigation import NavNode
from services.provider_ids import ALL_PROVIDERS
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

    def test_keys_hold_across_reconnects(self):
        self.start()
        fixture = self.group_keys()["Fixture"]
        again = PhoneGuide(self.window, self.sent.append)  # a new session after a reconnect
        self.addCleanup(again.deleteLater)
        again.channels(fixture, 0, "", 3)
        message = self.last()
        self.assertEqual((message["list"], message["title"], message["n"]), (fixture, "Fixture", 3))
        self.assertEqual([r["name"] for r in message["rows"]], ["Channel 1", "Channel 2"])

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


class FakeFinder(QObject):
    """The remote's FindSession as the phone's lists use it."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.query, self.genres, self.channels, self.searching = "", [], [], False
        self.asked = []

    def set_query(self, text):
        self.query, self.searching = text, True
        self.asked.append(text)
        self.changed.emit()  # as FindSession narrows at once


class SharedFindTests(WindowCase):
    """Lists sharing the remote's find: only an ask takes it; a list pushing what changed never
    takes it back (two lists would trade it for ever), showing its own without the other's."""

    def test_a_push_never_takes_the_find_back(self):
        self.open_first_channel()
        finder, sent = FakeFinder(), []
        lists = [
            PhoneGuide(self.window, sent.append, bare=bare, finder=finder) for bare in (False, True)
        ]
        for made in lists:
            self.addCleanup(made.deleteLater)
        lists[0].groups(0, "cnn")
        lists[1].groups(0, "bbc")
        finder.changed.emit()
        QTest.qWait(600)  # past both lists' pushes
        self.assertEqual(finder.asked, ["cnn", "bbc"])
        guide = [m for m in sent if m["t"] == "groups"][-1]
        self.assertEqual((guide["find"], guide["channels"], guide["searching"]), ("cnn", [], False))


class GuideFindTests(WindowCase):
    """The phone's finds go on with the desktop channel list's find (services.find, the
    remote's FindSession): channels and genres at every provider, ranked as there, after the
    groups the find matches, or after the group's own channels."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.addCleanup(lambda: [g.deleteLater() for g in self.lists])
        self.lists = []

    def make(self, bare=False, catalog=None):
        remote = self.window.remote
        made = PhoneGuide(
            self.window, self.sent.append, bare=bare, catalog=catalog, finder=remote._finder()
        )
        self.lists.append(made)
        return made

    def last(self, kind):
        found = [m for m in self.sent if m["t"] == kind]
        return found[-1] if found else None

    def found(self, kind, find):
        def ready():
            message = self.last(kind)
            return (
                message
                and message["find"] == find
                and not message["loading"]
                and not message.get("searching")
            )

        self.assertTrue(wait_until(ready, timeout_ms=10000))
        return self.last(kind)

    def test_finds_channels_after_the_groups(self):
        self.open_first_channel()
        guide = self.make()
        guide.groups(0, "channel 2")
        message = self.found("groups", "channel 2")
        self.assertEqual([c["name"] for c in message["channels"]], ["Channel 2"])
        self.assertEqual(message["found"], 1)
        self.assertIn("now", message["channels"][0])  # the Guide's rows: with what's on

    def test_the_largest_find_fits_a_frame_in_any_script(self):
        """100 rows of groups (the shown provider's and others' genres), and the channels found
        below them with what's on, in a three-byte script."""
        self.open_first_channel()
        guide = self.make()
        long = "字" * 300  # 3 bytes a letter
        now = datetime.now(timezone.utc)
        on = programme(long, now - timedelta(minutes=5))
        genres = [FoundGenre("Other", str(i), f"{long}{i}", 9999) for i in range(150)]
        hits = [
            FoundChannel("P", f"{long}{i}", {"name": f"{long}{i}", "id": i}, "") for i in range(80)
        ]
        controller = self.window.controller
        for nodes in (
            [NavNode("group", f"g{i}", f"{long}{i}", None, 9999) for i in range(150)],
            [],
        ):
            with (
                mock.patch.object(guide, "_group_nodes", return_value=nodes),
                mock.patch.object(guide, "_everywhere", return_value=(genres, hits, False)),
                mock.patch.object(controller, "now_next", return_value=(on, on)),
            ):
                guide.groups(0, "字字")
            message = self.last("groups")
            self.assertEqual(len(message["rows"]), 100)
            self.assertTrue(message["channels"])
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
            self.assertLessEqual(len(encoded), MAX_MESSAGE)

    def test_the_guides_of_the_channels_found_load(self):
        self.open_first_channel()
        remote = self.window.remote
        remote.hub = mock.Mock()
        self.addCleanup(setattr, remote, "hub", None)
        with mock.patch.object(self.window.controller, "use_phone_guides") as guides:
            remote.bridge.post(Get("s7", "groups", "", 0, find="channel 2"))

            def loaded():
                return any(c.args and c.args[0] == {PROVIDER_NAME} for c in guides.call_args_list)

            self.assertTrue(wait_until(loaded, timeout_ms=10000))  # once the find's came

    def test_finding_in_a_group_goes_on_past_it(self):
        """ "Find a channel" inside a group: the group's own first, then what the find found
        elsewhere, in its order (the desktop's ranking), each tuned on its own."""
        self.open_first_channel()
        guide = self.make(bare=True)
        mine = {"name": "Channel 8 Mine", "id": 8, "cmd": "file:///8", "_provider": PROVIDER_NAME}
        other = {"name": "Channel 9 Other", "id": 9, "cmd": "file:///9", "_provider": "Other"}
        hits = [
            FoundChannel(PROVIDER_NAME, mine["name"], mine, ""),
            FoundChannel("Other", other["name"], other, ""),
        ]
        with mock.patch.object(guide, "_everywhere", return_value=([], hits, False)):
            guide.channels("", 0, "channel")
            message = self.found("channels", "channel")
        names = [r["name"] for r in message["rows"]]
        self.assertEqual(names, ["Channel 1", "Channel 2", "Channel 8 Mine", "Channel 9 Other"])
        self.assertEqual(message["count"], 4)
        kind, (channel, group, channels) = guide.handles.get(message["rows"][3]["id"])
        self.assertEqual((channel["name"], group, channels), ("Channel 9 Other", "", []))
        kind, (channel, group, channels) = guide.handles.get(message["rows"][0]["id"])
        self.assertTrue(group and channels)  # the group's own: tuned within it

    def test_the_group_s_own_are_not_listed_twice(self):
        self.open_first_channel()
        guide = self.make(bare=True)
        guide.channels("", 0, "channel")
        names = [r["name"] for r in self.found("channels", "channel")["rows"]]
        self.assertEqual(names, ["Channel 1", "Channel 2"])  # the find found them too

    def test_genres_found_at_another_provider_open_in_all_providers(self):
        self.open_first_channel()
        catalog = self.window.remote._phone_catalog("s4")
        guide = self.make(bare=True, catalog=catalog)
        genres = [
            FoundGenre("Other", "7", "Sports", 3),
            FoundGenre(PROVIDER_NAME, "x", "Fixture", 2),
        ]
        with mock.patch.object(guide, "_everywhere", return_value=(genres, [], False)):
            guide.groups(0, "s")
        rows = self.last("chgroups")["rows"]
        self.assertIn({"title": "Other", "header": True}, rows)
        sports = next(r for r in rows if r.get("title") == "Sports")
        self.assertEqual(sports["count"], 3)
        self.assertEqual([r["title"] for r in rows if r.get("header")], ["Other"])  # not mine
        with mock.patch.object(catalog, "select_provider") as select:
            guide.channels(sports["id"], 0, "")
        select.assert_called_once_with(ALL_PROVIDERS)

    def test_a_group_and_no_channel(self):
        self.open_first_channel()
        guide = self.make()
        guide.groups(0, "fixt")
        message = self.found("groups", "fixt")
        self.assertIn("Fixture", [g["title"] for g in message["rows"]])
        self.assertEqual((message["channels"], message["found"]), ([], 0))

    def test_the_remote_tabs_list_finds_channels_without_programmes(self):
        self.open_first_channel()
        listed = self.make(bare=True)
        listed.groups(0, "chan")
        message = self.found("chgroups", "chan")
        self.assertEqual(sorted(c["name"] for c in message["channels"]), ["Channel 1", "Channel 2"])
        self.assertFalse(any("now" in c for c in message["channels"]))

    def test_a_found_channel_tunes(self):
        self.open_first_channel()
        guide = self.make()
        guide.groups(0, "channel 2")
        handle = self.found("groups", "channel 2")["channels"][0]["id"]
        remote = self.window.remote
        remote._guides["s6"] = guide
        self.addCleanup(remote._guides.pop, "s6", None)
        remote.bridge.post(OpenItem("s6", handle))
        controller = self.window.controller
        self.assertTrue(wait_until(lambda: (controller._channel or {}).get("name") == "Channel 2"))

    def test_the_find_ranks_the_provider_the_phone_shows_first(self):
        remote = self.window.remote
        names = [PROVIDER_NAME, "Other"]
        with mock.patch.object(self.window.catalog, "provider_names", return_value=names):
            remote._prefer("Other")
            self.assertEqual(remote._find_preferences().providers[0], "Other")
            remote._prefer(PROVIDER_NAME)
            self.assertEqual(remote._find_preferences().providers[0], PROVIDER_NAME)


class ChannelListTests(WindowCase):
    """The Remote tab's channel list: the Guide's groups and channels, without programmes."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.list = PhoneGuide(self.window, self.sent.append, bare=True)
        self.addCleanup(self.list.deleteLater)

    def last(self, kind):
        found = [m for m in self.sent if m["t"] == kind]
        return found[-1] if found else None

    def test_lists_channels_without_programmes_and_loads_no_guide(self):
        self.open_first_channel()
        self.list.channels("", 0, "")
        self.assertTrue(wait_until(lambda: self.last("channels") and self.last("channels")["rows"]))
        message = self.last("channels")
        self.assertEqual([r["name"] for r in message["rows"]], ["Channel 1", "Channel 2"])
        self.assertEqual([r["name"] for r in message["rows"] if r["playing"]], ["Channel 1"])
        self.assertFalse(any("now" in r or "next" in r for r in message["rows"]))
        self.assertEqual(self.list.providers, set())  # nothing to show: no guide downloads
        self.assertIsNone(self.last("guide"))

    def test_lists_the_groups(self):
        self.list.groups(0, "")
        self.assertTrue(wait_until(lambda: self.last("chgroups") and self.last("chgroups")["rows"]))
        titles = [g["title"] for g in self.last("chgroups")["rows"]]
        self.assertIn("All", titles)
        self.assertIsNone(self.last("groups"))

    def test_the_remote_keeps_it_apart_from_the_guide_and_a_tap_tunes(self):
        self.open_first_channel()
        remote = self.window.remote
        hub = mock.Mock()
        remote.hub = hub
        self.addCleanup(setattr, remote, "hub", None)

        def sent(kind):
            return [c.args[1] for c in hub.send.call_args_list if c.args[1]["t"] == kind]

        remote.bridge.post(Get("s5", "guide", "", 0))
        remote.bridge.post(Get("s5", "channels", "", 0))
        self.assertTrue(wait_until(lambda: sent("guide") and sent("channels")))
        self.assertTrue(wait_until(lambda: sent("channels")[-1]["rows"]))
        self.assertIn("s5", remote._guides)
        self.assertIn("s5", remote._channel_lists)
        self.assertIsNot(remote._guides["s5"], remote._channel_lists["s5"])
        second = sent("channels")[-1]["rows"][1]
        remote.bridge.post(OpenItem("s5", second["id"]))
        controller = self.window.controller
        self.assertTrue(wait_until(lambda: (controller._channel or {}).get("name") == "Channel 2"))


class ProviderLevelTests(WindowCase):
    """With two providers or more, the phone's lists have the providers above the groups (All
    providers, then each); the phone's choice moves its own lists, never the TV's."""

    def setUp(self):
        super().setUp()
        self.sent = []
        remote = self.window.remote
        self.catalog = remote._phone_catalog("s9")
        self.list = PhoneGuide(self.window, self.sent.append, bare=True, catalog=self.catalog)
        self.addCleanup(self.list.deleteLater)

    def last(self, kind):
        found = [m for m in self.sent if m["t"] == kind]
        return found[-1] if found else None

    def two_providers(self):
        names = [PROVIDER_NAME, "Other"]
        return mock.patch.object(self.catalog, "provider_names", return_value=names)

    def test_one_provider_has_no_level_above_its_groups(self):
        self.list.groups(0, "")
        self.assertTrue(wait_until(lambda: self.last("chgroups") and self.last("chgroups")["rows"]))
        self.assertFalse(self.last("chgroups")["pick"])
        self.list.sources()
        self.assertEqual([r["title"] for r in self.last("chsources")["rows"]], [PROVIDER_NAME])

    def test_two_providers_all_first_the_shown_one_marked(self):
        with self.two_providers():
            self.list.sources(n=4)
            message = self.last("chsources")
            self.list.groups(0, "")
        titles = [r["title"] for r in message["rows"]]
        self.assertEqual(titles, ["All providers", PROVIDER_NAME, "Other"])
        self.assertEqual([r["title"] for r in message["rows"] if r.get("on")], [PROVIDER_NAME])
        self.assertEqual(message["n"], 4)
        groups = self.last("chgroups")
        self.assertTrue(groups["pick"])
        self.assertEqual(groups["source"], PROVIDER_NAME)

    def test_choosing_moves_the_phones_lists_only(self):
        config = self.window.config_manager
        before = (config.selected_provider_name, config.tv_all_providers)
        with self.two_providers():
            self.list.sources()
            every = self.last("chsources")["rows"][0]["id"]
            with mock.patch.object(self.catalog, "select_provider") as select:
                self.list.groups(0, "", source=every)
        select.assert_called_once_with(ALL_PROVIDERS)
        self.catalog.select_provider(ALL_PROVIDERS)  # the phone's catalog, for real
        self.assertTrue(self.catalog.all_providers())
        self.assertFalse(self.window.catalog.all_providers())  # the TV's list stays
        self.assertEqual((config.selected_provider_name, config.tv_all_providers), before)

    def test_a_phones_lists_share_its_choice_and_not_the_tvs(self):
        remote = self.window.remote
        guide, listed = remote._guide("s9"), remote._channel_list("s9")
        self.addCleanup(remote._guides.pop, "s9", None)
        self.addCleanup(remote._channel_lists.pop, "s9", None)
        self.assertIs(guide.nav.catalog, self.catalog)
        self.assertIs(listed.nav.catalog, self.catalog)
        self.assertIsNot(self.catalog, self.window.catalog)


class GuidesInUseTests(WindowCase):
    def test_the_phones_providers_join_the_guides_in_use(self):
        controller = self.window.controller
        controller.use_phone_guides({"Another provider"})
        self.assertIn("Another provider", controller.guides_in_use())
        controller.use_phone_guides(set())
        self.assertNotIn("Another provider", controller.guides_in_use())


if __name__ == "__main__":
    unittest.main()
