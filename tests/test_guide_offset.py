"""A provider's guide time offset (2.2): its programmes move by it everywhere they show (the
grid, now/next, the banner, the time-shift bar's ticks), and Settings keeps it as an optional
per-provider key that older builds ignore."""

from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from services.epg_manager import EpgManager
from services.epg_view import (
    GUIDE_OFFSET_KEY,
    Programme,
    guide_offset,
    guide_offset_minutes,
    normalize,
    programmes_for_channel,
)
from services.multikeydict import MultiKeyDict
from tests import test_tv_controller as tv, test_tv_window as base
from ui.overlays.guide import Guide
from ui.settings_dialog import OptionsDialog, offset_text

HOUR = timedelta(hours=1)
NOW = tv.NOW  # the controller tests' clock


def xmltv_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def entry(title, start, stop):
    return {"@start": xmltv_time(start), "@stop": xmltv_time(stop), "title": {"__text": title}}


class _Config(SimpleNamespace):
    def __init__(self, directory, **overrides):
        values = dict(
            channel_epg=True,
            epg_source="URL",
            epg_expiration=3600,
            epg_stb_period_hours=5,
            epg_list_window_hours=0,
            ssl_verify=True,
            xmltv_channel_map=MultiKeyDict(),
        )
        values.update(overrides)
        super().__init__(**values)
        self._directory = directory

    def get_config_dir(self):
        return self._directory


class OffsetValueTests(unittest.TestCase):
    def test_the_key_is_optional_and_checked(self):
        cases = [
            (None, 0),
            (60, 60),
            (-90, -90),
            ("120", 120),
            (720, 720),
            (-720, -720),
            (45, 0),  # not a 30-minute step
            (750, 0),  # past 12 hours
            (True, 0),
            ("soon", 0),
            ([60], 0),
        ]
        for value, minutes in cases:
            with self.subTest(value=value):
                provider = {} if value is None else {GUIDE_OFFSET_KEY: value}
                self.assertEqual(guide_offset_minutes(provider), minutes)
        self.assertEqual(guide_offset({GUIDE_OFFSET_KEY: -30}), timedelta(minutes=-30))
        self.assertEqual(guide_offset(None), timedelta(0))

    def test_normalize_moves_every_programme(self):
        start = datetime(2026, 10, 19, 18, tzinfo=timezone.utc)
        raw = [
            entry("News", start, start + HOUR),
            {"time": "2026-10-19 21:00:00", "time_to": "2026-10-19 22:00:00", "name": "Film"},
        ]
        plain, moved = normalize(raw), normalize(raw, 2 * HOUR)
        self.assertEqual([p.title for p in moved], [p.title for p in plain])
        for before, after in zip(plain, moved):
            self.assertEqual(
                (after.start, after.stop), (before.start + 2 * HOUR, before.stop + 2 * HOUR)
            )


class ManagerTests(unittest.TestCase):
    """EpgManager's lookup window moves the other way, so a programme on now after the offset
    is found even when its guide times have ended."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-offset-")
        self.addCleanup(directory.cleanup)
        self.provider = {"type": "XTREAM", "name": "X", "url": "http://h", "username": "u"}
        self.config = _Config(directory.name, epg_list_window_hours=6)
        self.manager = EpgManager(
            self.config, SimpleNamespace(current_provider=self.provider, headers={})
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.now = now
        guide = MultiKeyDict()
        # The guide says 2 h earlier than the truth: on now is listed from 2.5 h to 1.5 h ago.
        guide["news.test"] = [
            entry("On now", now - 150 * timedelta(minutes=1), now - 90 * timedelta(minutes=1)),
            entry("Next", now - 90 * timedelta(minutes=1), now - 30 * timedelta(minutes=1)),
        ]
        self.manager.epg = guide

    def lookup(self):
        channel = {"xmltv_id": "news.test", "name": "News"}
        return programmes_for_channel(self.manager, channel, self.now, limit=4)

    def test_without_an_offset_the_guide_is_over(self):
        self.assertEqual(self.lookup(), [])

    def test_with_the_offset_now_and_next_are_right(self):
        self.provider[GUIDE_OFFSET_KEY] = 120
        found = self.lookup()
        self.assertEqual([p.title for p in found], ["On now", "Next"])
        self.assertTrue(found[0].is_on(self.now))
        self.assertEqual(found[0].start, self.now - 30 * timedelta(minutes=1))

    def test_a_portal_guide_moves_too(self):
        self.config.epg_source = "STB"
        self.provider.update(type="STB", **{GUIDE_OFFSET_KEY: -60})
        local = datetime.now().replace(microsecond=0)
        start = local + HOUR  # listed an hour late
        self.manager.epg = MultiKeyDict()
        self.manager.epg["9"] = [
            {
                "time": start.strftime("%Y-%m-%d %H:%M:%S"),
                "time_to": (start + HOUR).strftime("%Y-%m-%d %H:%M:%S"),
                "name": "Match",
            }
        ]
        found = programmes_for_channel(self.manager, {"id": "9"}, local.astimezone(), limit=2)
        self.assertEqual(found[0].start, local.astimezone())


class ListWindowTests(unittest.TestCase):
    """An XMLTV channel's entries run from now to the list window's end (a day by default),
    in the guide's own times: the offset moves the window the other way."""

    def setUp(self):
        ManagerTests.setUp(self)  # type: ignore[arg-type]
        self.config.epg_list_window_hours = 24
        now, minutes = self.now, timedelta(minutes=1)
        self.manager.epg = MultiKeyDict()
        self.manager.epg[("news.test", "News", "news")] = [
            entry("Beyond", now + 25 * HOUR, now + 26 * HOUR),
            entry("Ended", now - 3 * HOUR, now - HOUR),
            entry("On now", now - 30 * minutes, now + 30 * minutes),
            entry("Later", now + 23 * HOUR, now + 24 * HOUR),
        ]

    def titles(self, channel):
        return [p["title"]["__text"] for p in self.manager.get_programs_for_channel(channel)]

    def test_a_day_from_now(self):
        self.assertEqual(self.titles({"xmltv_id": "news.test"}), ["On now", "Later"])

    def test_the_offset_moves_the_window(self):
        self.provider[GUIDE_OFFSET_KEY] = 120  # the guide's now is two hours earlier
        self.assertEqual(self.titles({"xmltv_id": "news.test"}), ["Ended", "On now"])

    def test_without_a_window_every_entry_oldest_first(self):
        self.config.epg_list_window_hours = 0
        self.assertEqual(
            self.titles({"xmltv_id": "news.test"}), ["Ended", "On now", "Later", "Beyond"]
        )

    def test_the_channel_is_found_by_id_name_or_mapping(self):
        self.config.xmltv_channel_map[("news.test", "My News")] = None
        for channel in (
            {"xmltv_id": "news.test", "name": "Other"},
            {"xmltv_id": "missing.test", "name": "News"},
            {"name": "NEWS"},  # in lower case
            {"name": "My News"},  # through the user's mapping
        ):
            with self.subTest(channel=channel):
                self.assertEqual(self.titles(channel), ["On now", "Later"])
        self.assertEqual(self.titles({"xmltv_id": "missing.test", "name": "Other"}), [])


def fake_manager(offset_minutes, raw):
    provider = {GUIDE_OFFSET_KEY: offset_minutes}
    return SimpleNamespace(
        get_programs_for_channel=lambda channel: raw,
        guide_offset=lambda: guide_offset(provider),
    )


class EverywhereTests(tv.PipelineCase):
    """The grid, the info line, the banner and the time-shift ticks show the same, moved."""

    def setUp(self):
        super().setUp()
        # The guide lists programmes an hour early: its 19:00-20:00 is on at 20:00-21:00.
        start = NOW - 90 * timedelta(minutes=1)
        self.raw = [
            entry("Quiz", start, start + HOUR),
            entry("Film", start + HOUR, start + 3 * HOUR),
        ]
        self.manager = fake_manager(60, self.raw)
        self.catalog.programmes = lambda channel, now, limit=8: programmes_for_channel(
            self.manager, channel, now, limit
        )
        self.on_now = Programme("Quiz", start + HOUR, start + 2 * HOUR)

    def test_now_next_and_the_banner(self):
        self.assertTrue(self.on_now.is_on(NOW))
        title, _progress = self.controller.now_title(tv.CHANNELS[0])
        self.assertEqual(title, "Quiz")
        info = self.controller.banner_info(tv.CHANNELS[0])
        self.assertEqual((info.now.title, info.now.start), ("Quiz", self.on_now.start))
        self.assertEqual((info.next.title, info.next.start), ("Film", self.on_now.stop))

    def test_the_time_shift_ticks(self):
        state = {"live_edge": 1000.0, "start": 0.0, "position": 1000.0}
        ticks = self.controller._live_controls._programme_ticks(tv.CHANNELS[0], state)
        quiz = next(seconds for seconds, title in ticks if title == "Quiz")
        self.assertAlmostEqual(quiz, 1000.0 - (NOW - self.on_now.start).total_seconds())

    def test_the_grid(self):
        QApplication.instance() or QApplication([])
        host = QWidget()
        host.resize(1280, 720)
        self.addCleanup(host.deleteLater)
        guide = Guide(None, lambda c, s, n: self.catalog.programmes(c, s, n), lambda: NOW, host)
        guide.open_guide([tv.CHANNELS[0]], 0)
        quiz = next(p for p in guide._programmes_for(0) if p.title == "Quiz")
        self.assertEqual((quiz.start, quiz.stop), (self.on_now.start, self.on_now.stop))

    def test_without_an_offset_the_guide_shows_as_listed(self):
        self.manager = fake_manager(0, self.raw)
        title, _progress = self.controller.now_title(tv.CHANNELS[0])
        self.assertEqual(title, "Film")  # 20:00-22:00 as listed


class SettingsTests(base.WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_the_setting_lists_minus_to_plus_twelve_hours_in_half_hours(self):
        combo = self.dialog().guide_offset_combo
        values = [combo.itemData(i) for i in range(combo.count())]
        self.assertEqual(values, list(range(-720, 721, 30)))
        self.assertEqual(combo.currentText(), "No offset")
        self.assertEqual(offset_text(90), "+1 h 30 min")
        self.assertEqual(offset_text(-30), "−30 min")
        self.assertEqual(offset_text(-720), "−12 h")

    def test_it_round_trips_and_no_offset_writes_no_key(self):
        dialog = self.dialog()
        combo = dialog.guide_offset_combo
        combo.setCurrentIndex(combo.findData(-90))
        dialog.save_settings()
        saved = self.saved_providers()
        self.assertEqual(saved[0][GUIDE_OFFSET_KEY], -90)
        dialog = self.dialog()
        self.assertEqual(dialog.guide_offset_combo.currentData(), -90)
        dialog.guide_offset_combo.setCurrentIndex(dialog.guide_offset_combo.findData(0))
        dialog.save_settings()
        self.assertNotIn(GUIDE_OFFSET_KEY, self.saved_providers()[0])

    def test_an_offset_alone_keeps_the_provider_loaded(self):
        # The guide moves; nothing signs in again (the connection is the same).
        dialog = self.dialog()
        dialog.guide_offset_combo.setCurrentIndex(dialog.guide_offset_combo.findData(60))
        calls = []
        self.window.set_provider = lambda force_update=False: calls.append(force_update)
        dialog.save_settings()
        self.assertEqual(calls, [])
        manager = self.window.provider_manager
        self.assertEqual(
            manager.provider_cache_identity(manager.providers[0]),
            manager.provider_cache_identity({**manager.providers[0], GUIDE_OFFSET_KEY: 0}),
        )

    def saved_providers(self):
        index = os.path.join(self.config_dir, "cache", "provider", "index.json")
        with open(index, encoding="utf-8") as f:
            return json.load(f)


if __name__ == "__main__":
    unittest.main()
