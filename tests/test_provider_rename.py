"""A provider renamed in Settings keeps what is stored under its name; a removed one's
scheduled recordings go; Settings refuses a name another provider has (2.8.12)."""

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from services import favorites, recording_schedule as rs, watch_state
from services.channel_store import rekeyed
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme
from services.live.kept import KeptBuffers, KeptRing, Where
from services.live.tuning import LiveTuning, channel_key
from services.parental import Parental
from services.provider_rename import (
    Renames,
    carry_over,
    hashed_keys,
    removed_between,
    rename_in_config,
    renames_between,
)
from services.subtitle_picks import pick_key
from ui.provider_drop import unique_name

app = QApplication.instance() or QApplication([])


class Config(SimpleNamespace):
    def __init__(self, **values):
        super().__init__(
            selected_provider_name="",
            library_provider_name="",
            library_mix=[],
            multiview=None,
            favorites=[],
            provider_favorites=[],
            watch_history=[],
            playback_positions={},
            last_watched=None,
            last_live_channel=None,
            parental=None,
            saves=0,
        )
        self.__dict__.update(values)

    def save_config(self, **_kw):
        self.saves += 1


def channel(name, provider, **extra):
    return {"id": name.lower(), "name": name, "cmd": f"http://x.test/{name}", "_provider": provider}


class RenamesTests(unittest.TestCase):
    def test_a_resume_id_is_renamed_by_its_longest_provider(self):
        renames = Renames({"A": "B"}, frozenset({"A", "A:1"}))
        self.assertEqual(renames.prefixed("A:7"), "B:7")
        self.assertEqual(renames.prefixed("A:1:7"), "A:1:7")  # provider "A:1"'s item 7
        self.assertEqual(renames.prefixed("C:7"), "C:7")

    def test_a_mixed_group_and_a_stamped_item(self):
        renames = Renames({"A": "B"})
        self.assertEqual(renames.group("A\x1fnews"), "B\x1fnews")
        self.assertEqual(renames.group("news"), "news")
        item = {"name": "One", "_provider": "A"}
        self.assertEqual(renames.item(item), {"name": "One", "_provider": "B"})
        self.assertEqual(item["_provider"], "A")  # a copy

    def test_entries_are_told_apart_by_identity(self):
        a, b, c = {"name": "A"}, {"name": "B"}, {"name": "C"}
        before = [(a, "A"), (b, "B"), (c, "C")]
        a["name"], b["name"] = "B", "A"  # a swap
        self.assertEqual(renames_between(before, [a, b]).names, {"A": "B", "B": "A"})
        self.assertEqual(removed_between(before, [a, b]), ["C"])
        self.assertFalse(renames_between(before, [c]))

    def test_unique_names_ignore_case(self):
        self.assertEqual(unique_name("tv", ["TV"]), "tv (2)")
        self.assertEqual(unique_name("TV", ["tv", "Tv (2)"]), "TV (3)")


class ConfigTests(unittest.TestCase):
    def test_every_reference_in_the_config_moves(self):
        config = Config(
            selected_provider_name="A",
            library_provider_name="A",
            library_mix=["A", "C"],
            provider_favorites=[
                {"name": "One", "provider": "A"},
                {"name": "Two", "provider": "*"},
                {"name": "Two", "provider": "A", "off": True},
            ],
            watch_history=[
                {
                    "content_id": "A:7",
                    "provider_name": "A",
                    "item_data": {"id": 7, "_provider": "A"},
                    "item_type": "movie",
                },
                {"content_id": "C:7", "provider_name": "C", "item_data": {"id": 7}},
            ],
            playback_positions={"A:7": {"position": 9000}, "C:7": {"position": 1}},
            last_watched={"provider_name": "A", "item_data": {"id": 7}},
            last_live_channel={"provider_name": "A", "group_id": "A\x1fg", "item": {"id": 1}},
            multiview={"version": 1, "cells": 2, "tiles": [{"provider_name": "A"}, None]},
        )
        renames = Renames({"A": "B"}, frozenset({"A", "C"}))
        self.assertTrue(rename_in_config(config, renames))
        self.assertEqual((config.selected_provider_name, config.library_provider_name), ("B", "B"))
        self.assertEqual(config.library_mix, ["B", "C"])
        self.assertEqual(
            config.provider_favorites,
            [
                {"name": "One", "provider": "B"},
                {"name": "Two", "provider": "*"},
                {"name": "Two", "provider": "B", "off": True},
            ],
        )
        self.assertEqual(config.favorites, ["One", "Two"])  # older builds' list: names only
        first, second = config.watch_history
        self.assertEqual((first["provider_name"], first["content_id"]), ("B", "B:7"))
        self.assertEqual(first["item_data"]["_provider"], "B")
        self.assertEqual(second["content_id"], "C:7")
        self.assertEqual(set(config.playback_positions), {"B:7", "C:7"})
        self.assertEqual(config.last_watched["provider_name"], "B")
        self.assertEqual(config.last_live_channel["group_id"], "B\x1fg")
        self.assertEqual(config.multiview["tiles"][0]["provider_name"], "B")
        self.assertIsNone(config.multiview["tiles"][1])

    def test_a_swap_moves_both_at_once(self):
        config = Config(
            provider_favorites=[{"name": "One", "provider": "A"}, {"name": "Two", "provider": "B"}],
            playback_positions={"A:1": {}, "B:2": {}},
        )
        renames = Renames({"A": "B", "B": "A"}, frozenset({"A", "B"}))
        rename_in_config(config, renames)
        self.assertEqual(
            favorites.entries(config),
            [{"name": "One", "provider": "B"}, {"name": "Two", "provider": "A"}],
        )
        self.assertEqual(set(config.playback_positions), {"B:1", "A:2"})

    def test_nothing_of_the_provider_changes_nothing(self):
        config = Config(watch_history=[{"provider_name": "C", "content_id": "C:1"}])
        self.assertFalse(rename_in_config(config, Renames({"A": "B"}, frozenset({"A", "C"}))))

    def test_carry_over_runs_every_hook_and_saves_once(self):
        config = Config(selected_provider_name="A")
        seen = []

        def failing(_renames):
            raise RuntimeError("store down")

        with self.assertLogs("services.provider_rename", "WARNING"):
            carry_over(config, Renames({"A": "B"}), [failing, seen.append])
        self.assertEqual(len(seen), 1)
        self.assertEqual((config.selected_provider_name, config.saves), ("B", 1))

    def test_the_last_record_keeps_its_object_when_untouched(self):
        record = {"provider_name": "C", "item": {"id": 1}}
        renames = Renames({"A": "B"})
        self.assertIs(watch_state.renamed_record(record, renames, item_key="item"), record)


class ParentalTests(unittest.TestCase):
    def test_what_is_hidden_stays_hidden_under_the_new_name(self):
        config = Config()
        guard = Parental(config)
        guard.set_pin("2468")
        news = {"id": "1", "name": "News", "_provider": "A", "tv_genre_id": "g"}
        self.assertTrue(guard.hide_item("live", news))
        self.assertTrue(guard.hide_group("live", "A", "adults", "Late"))
        version = guard.version
        self.assertTrue(guard.rename_providers(Renames({"A": "B"})))
        self.assertGreater(guard.version, version)
        moved = {**news, "_provider": "B"}
        self.assertTrue(guard.hides_item("live", moved))
        self.assertTrue(guard.hides_group("live", "B", "adults"))
        self.assertFalse(guard.hides_group("live", "A", "adults"))
        self.assertEqual({h["provider"] for h in guard.hidden()}, {"B"})
        self.assertFalse(guard.rename_providers(Renames({"Z": "Y"})))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / rs.FILE_NAME)
        self.written = []
        self.schedule = rs.Schedule(
            self.path, write=lambda _p, data: self.written.append(data), clock=lambda: 0
        )
        start = Programme("Quiz", _at(1000), _at(2000), "", None)
        self.schedule.add_once(channel("One", "A"), start)
        self.schedule.add_series(channel("Two", "C"), "Talk")

    def test_a_rename_records_from_the_new_name(self):
        self.assertEqual(self.schedule.rename_providers(Renames({"A": "B"})), 1)
        quiz = next(i for i in self.schedule.items() if i["title"] == "Quiz")
        self.assertEqual((quiz["provider"], quiz["channel"]["_provider"]), ("B", "B"))
        self.assertEqual(self.schedule.providers(), {"B", "C"})
        self.assertEqual(self.written[-1]["items"][0]["provider"], "B")

    def test_a_removal_drops_its_recordings_noted(self):
        dropped = self.schedule.drop_providers(["A"])
        self.assertEqual([d["title"] for d in dropped], ["Quiz"])
        self.assertEqual([i["title"] for i in self.schedule.items()], ["Talk"])
        outcome = self.schedule.outcomes()[0]
        self.assertEqual((outcome["state"], outcome["why"]), (rs.FAILED, "provider removed"))
        self.assertEqual(self.schedule.drop_providers(["Z"]), [])


def _at(seconds):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(seconds, timezone.utc)


class HashedTests(unittest.TestCase):
    def test_the_channels_and_films_of_a_loaded_list_get_their_new_keys(self):
        one, two = channel("One", "A"), channel("Two", "A")
        film = {"stream_id": 5, "name": "Film"}
        content = {"itv": {"contents": [one, two]}, "vod": {"contents": {"g": [film]}}}
        renames = Renames({"A": "B"})
        old_one = channel_key("A", one)
        old_film = pick_key("A", film, episode=False)
        keys = hashed_keys(renames, {"A": content}, wanted={old_one, old_film})
        self.assertEqual(
            keys, {old_one: channel_key("B", one), old_film: pick_key("B", film, episode=False)}
        )
        self.assertEqual(len(hashed_keys(renames, {"A": content})), 3)
        self.assertEqual(hashed_keys(renames, {"Z": content}), {})

    def test_a_store_moves_its_entries_in_order(self):
        self.assertEqual(
            list(rekeyed({"a": 1, "x": 2, "b": 3}, {"a": "b"}).items()), [("b", 1), ("x", 2)]
        )
        self.assertIsNone(rekeyed({"x": 1}, {"a": "b"}))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        tuning = LiveTuning(str(Path(directory.name) / "live_tuning.json"))
        tuning._entries = {"old": {"x": 1}, "other": {"x": 2}}
        self.assertEqual(tuning.rekey({"old": "new"}), 1)
        tuning.flush()
        self.assertEqual(tuning.keys(), {"new", "other"})
        self.assertEqual(tuning.rekey({"gone": "x"}), 0)

    def test_kept_buffers_follow_their_channel(self):
        engine = mock.Mock(spec=["close_session"])
        kept = KeptBuffers(engine)
        kept.put(KeptRing("s1", "old", True, Where(None, False)))
        kept.rekey({"old": "new"})
        self.assertIsNone(kept.take("old"))
        self.assertEqual(kept.take("new").sid, "s1")


class BudgetTests(unittest.TestCase):
    def test_streams_held_count_under_the_new_name(self):
        budget = ConnectionBudget()
        budget.request("A", 1, purpose="main")
        budget.learn("A", 1)
        waiting = budget.request("A", 1, purpose="pip")
        budget.rename_providers(Renames({"A": "B"}))
        self.assertEqual((budget.in_use("A"), budget.in_use("B")), (0, 1))
        self.assertEqual(budget.cap("B", 5), 1)
        self.assertEqual(budget.request("B", 1, purpose="other", queue=False), -1)
        budget.release(next(iter(budget._active)))
        self.assertEqual(budget.provider_of(waiting), "B")  # the waiter took the freed slot


from timer_fixtures import wait_until  # noqa: E402

from tests.fixtures.local_provider import PROVIDER_NAME  # noqa: E402
from tests.test_tv_window import WindowCase  # noqa: E402
from ui import parental_settings  # noqa: E402
from ui.settings_dialog import OptionsDialog  # noqa: E402

YES, NO = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No


class SettingsTests(WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def saved_names(self):
        return [p["name"] for p in type(self.provider_manager)(self.config).providers]

    def save(self, dialog):
        with mock.patch.object(self.window.catalog, "select_provider"):
            dialog.save_settings()

    def test_a_rename_carries_the_stars_the_schedule_and_what_is_hidden(self):
        star = {"name": "Channel 1", "_provider": PROVIDER_NAME, "id": 1}
        favorites.toggle(self.config, star)
        self.window.parental.set_pin("2468")
        self.window.parental.hide_group("live", PROVIDER_NAME, "Fixture", "Fixture")
        start = Programme("Quiz", _at(4_000_000_000), _at(4_000_003_600), "", None)
        self.window.recording_schedule.add_once(star, start)
        dialog = self.dialog()
        dialog.name_input.setText("Renamed")
        with mock.patch.object(parental_settings, "_ask_pin", return_value="2468") as asked:
            self.save(dialog)
        asked.assert_called_once()  # locked: the PIN was asked
        self.assertEqual(self.saved_names(), ["Renamed"])
        self.assertEqual(self.config.selected_provider_name, "Renamed")
        self.assertEqual(favorites.entries(self.config)[0]["provider"], "Renamed")
        self.assertTrue(self.window.parental.hides_group("live", "Renamed", "Fixture"))
        self.assertEqual(self.window.recording_schedule.providers(), {"Renamed"})

    def test_a_wrong_pin_keeps_the_name(self):
        self.window.parental.set_pin("2468")
        dialog = self.dialog()
        dialog.name_input.setText("Renamed")
        with (
            mock.patch.object(parental_settings, "_ask_pin", return_value="1111"),
            mock.patch.object(parental_settings.QMessageBox, "warning") as said,
        ):
            self.save(dialog)
        said.assert_called_once()
        self.assertEqual(self.saved_names(), [PROVIDER_NAME])

    def test_a_name_another_provider_has_is_refused_in_any_case(self):
        dialog = self.dialog()
        dialog.add_new_provider()
        dialog.type_M3UPLAYLIST.setChecked(True)
        dialog.url_input.setText("http://playlist.test/list.m3u")
        dialog.name_input.setText(PROVIDER_NAME.upper())
        with mock.patch("ui.settings_dialog.QMessageBox.warning") as said:
            self.save(dialog)
        self.assertIn("Choose another name", said.call_args.args[2])
        self.assertEqual(self.saved_names(), [PROVIDER_NAME])
        dialog.name_input.setText("Second")
        self.save(dialog)
        self.assertEqual(self.saved_names(), [PROVIDER_NAME, "Second"])

    def test_an_unnamed_provider_gets_a_name_of_its_own(self):
        dialog = self.dialog()
        for _ in range(2):  # one playlist file twice: named after the file, then numbered
            dialog.add_new_provider()
            dialog.type_M3UPLAYLIST.setChecked(True)
            dialog.url_input.setText(os.path.join(self.config_dir, "Sports.m3u"))
        self.save(dialog)
        self.assertEqual(self.saved_names(), [PROVIDER_NAME, "Sports", "Sports (2)"])

    def test_an_untouched_new_provider_is_not_saved_and_a_half_filled_one_is_refused(self):
        dialog = self.dialog()
        dialog.add_new_provider()
        self.save(dialog)
        self.assertEqual(self.saved_names(), [PROVIDER_NAME])
        self.assertEqual(self.config.selected_provider_name, PROVIDER_NAME)
        dialog = self.dialog()
        dialog.add_new_provider()
        dialog.name_input.setText("Portal")  # an STB portal: no address, no MAC
        with mock.patch("ui.settings_dialog.QMessageBox.warning") as said:
            self.save(dialog)
        self.assertIn("Server URL", said.call_args.args[2])
        dialog.url_input.setText("http://portal.test/c/")
        with mock.patch("ui.settings_dialog.QMessageBox.warning") as said:
            self.save(dialog)
        self.assertIn("MAC Address", said.call_args.args[2])
        self.assertEqual(self.saved_names(), [PROVIDER_NAME])

    def test_removing_asks_and_the_last_one_leaves_the_welcome_card(self):
        self.show()
        start = Programme("Quiz", _at(4_000_000_000), _at(4_000_003_600), "", None)
        self.window.recording_schedule.add_once(channel("One", PROVIDER_NAME), start)
        dialog = self.dialog()
        with mock.patch("ui.settings_dialog.QMessageBox.question", return_value=NO) as asked:
            dialog.remove_provider_button.click()
        self.assertIn(PROVIDER_NAME, asked.call_args.args[2])
        self.assertIn("recordings will be cancelled", asked.call_args.args[2])
        self.assertEqual(len(dialog.providers), 1)
        with mock.patch("ui.settings_dialog.QMessageBox.question", return_value=YES):
            dialog.remove_provider_button.click()
        self.assertEqual(dialog.providers, [])
        self.assertFalse(dialog.name_input.isEnabled())
        self.assertTrue(dialog.add_provider_button.isEnabled())
        dialog.save_settings()
        self.assertEqual(self.saved_names(), [])
        self.assertEqual(self.window.recording_schedule.items(), [])
        self.assertTrue(any("removed provider" in text for text, _kind in self.toasts))
        self.assertTrue(wait_until(self.window.overlays.welcome.isVisible))


if __name__ == "__main__":
    unittest.main()
