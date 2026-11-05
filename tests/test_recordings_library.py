"""The Recordings tab's rail and library side (2.9.7): rows, tiles and their details."""

from types import SimpleNamespace
import unittest

from services import recordings_library as rl


def recording(path, channel, title="", start=1_000.0, stop=None, provider="Demo TV", size=10):
    return {
        "id": f"recording:{path.rsplit('/', 1)[-1]}",
        "name": title or channel,
        "path": path,
        "added": str(int(start)),
        "_recording": True,
        "_title": title,
        "_channel": channel,
        "_provider": provider,
        "_logo": "",
        "_start": start,
        "_stop": stop,
        "_size": size,
    }


FILES = [
    recording("/r/c.ts", "Harbor Sports", "Night Match", start=3_000.0, stop=3_000.0 + 6720),
    recording("/r/b.ts", "Northwind News", "Evening News", start=2_000.0, stop=2_000.0 + 2700),
    recording("/r/a.ts", "Northwind News", "", start=1_000.0),
    recording("/r/d.ts", "Copperline World", "Mountain Rivers", start=500.0, stop=4_100.0),
]


class FakeWatch:
    def __init__(self, resume=None):
        self.resume = resume or {}

    def resume_from(self, cid, item):
        return self.resume.get(cid)


class RecordingsLibraryTests(unittest.TestCase):
    def setUp(self):
        self.files = list(FILES)
        self.counts = {rl.COMING: 4, rl.RULES: 2, rl.ATTENTION: 1}
        self.config = SimpleNamespace(playback_positions={})
        self.watch = FakeWatch()
        self.mixed = False
        self.library = rl.RecordingsLibrary(
            lambda: self.files,
            lambda: self.counts,
            self.watch,
            self.config,
            mixed=lambda: self.mixed,
        )

    def test_the_rail_lists_jobs_then_recorded_by_channel_then_settings(self):
        rail = self.library.rail()
        self.assertEqual(
            [(g.id, g.title, g.count, g.pinned, g.header) for g in rail],
            [
                (rl.TONIGHT, "Tonight", None, True, False),
                (rl.COMING, "Coming up", 4, True, False),
                (rl.RULES, "Series & timers", 2, True, False),
                (rl.ATTENTION, "Needs attention", 1, True, False),
                ("", "RECORDED · 4", None, False, True),
                (rl.ALL, "All", 4, False, False),
                (rl.CHANNEL_PREFIX + "Northwind News", "Northwind News", 2, False, False),
                (rl.CHANNEL_PREFIX + "Copperline World", "Copperline World", 1, False, False),
                (rl.CHANNEL_PREFIX + "Harbor Sports", "Harbor Sports", 1, False, False),
                ("", "SETTINGS", None, False, True),
                (rl.SETTINGS, "Folders & streams", None, False, False),
            ],
        )  # channels with the most recordings first, then by name

    def test_counts_come_from_the_desk_and_the_files(self):
        self.counts = {}
        self.files = self.files[:1]
        rail = {g.id: g.count for g in self.library.rail()}
        self.assertEqual((rail[rl.COMING], rail[rl.ALL]), (None, 1))

    def test_a_channel_row_lists_its_recordings_newest_first(self):
        items = self.library.items(rl.CHANNEL_PREFIX + "Northwind News")
        self.assertEqual([i["path"] for i in items], ["/r/b.ts", "/r/a.ts"])
        self.assertEqual(
            [i["path"] for i in self.library.items(rl.ALL)],
            ["/r/c.ts", "/r/b.ts", "/r/a.ts", "/r/d.ts"],
        )

    def test_the_find_text_filters_titles_and_channels(self):
        self.assertEqual([i["path"] for i in self.library.items(rl.ALL, "match")], ["/r/c.ts"])
        self.assertEqual(
            [i["path"] for i in self.library.items(rl.ALL, "northwind")], ["/r/b.ts", "/r/a.ts"]
        )
        self.assertEqual(
            [
                g.title
                for g in self.library.rail("copper")
                if not g.pinned and not g.header and g.id.startswith(rl.CHANNEL_PREFIX)
            ],
            ["Copperline World"],
        )

    def test_a_recording_never_played_is_new_and_shows_its_length(self):
        night, news, plain, rivers = FILES
        self.assertEqual(self.library.badges(night), ("1h 52m", "NEW"))
        self.assertEqual(self.library.badges(news), ("45 min", "NEW"))
        self.assertEqual(self.library.badges(plain), ("", "NEW"))  # no end known
        self.config.playback_positions = {news["id"]: {"position": 1000, "duration": 2_700_000}}
        self.assertEqual(self.library.badges(news), ("45 min", ""))

    def test_details_say_channel_day_times_and_size(self):
        news = FILES[1]
        details = self.library.details(news)
        self.assertEqual((details.title, details.kind), ("Evening News", "recording"))
        self.assertEqual(details.genre, "Northwind News")
        self.assertEqual(details.provider, "")  # one provider: none named
        self.mixed = True
        self.assertEqual(self.library.details(news).provider, "Demo TV")
        self.watch.resume[news["id"]] = 60_000
        self.assertEqual(self.library.details(news).resume_ms, 60_000)

    def test_identity_is_the_files_path(self):
        self.assertEqual(self.library.identity(FILES[0]), "/r/c.ts")


if __name__ == "__main__":
    unittest.main()


class ParentalTests(unittest.TestCase):  # a hidden channel's recordings stay hidden (2.8's rule)
    def test_a_hidden_channels_recordings_are_not_listed_while_locked(self):
        from services import parental

        guard = parental.Parental(SimpleNamespace(parental=None, save_config=lambda: None))
        guard.set_pin("1234")
        guard.hide_item("live", {"id": "9", "name": "Harbor Sports", "_provider": "Demo TV"})
        parental.install(guard)
        self.addCleanup(parental.install, None)
        library = rl.RecordingsLibrary(
            lambda: list(FILES), lambda: {}, FakeWatch(), SimpleNamespace(playback_positions={})
        )
        self.assertNotIn("/r/c.ts", [i["path"] for i in library.items(rl.ALL)])
        rail = {g.id: g.count for g in library.rail()}
        self.assertNotIn(rl.CHANNEL_PREFIX + "Harbor Sports", rail)
        self.assertEqual(rail[rl.ALL], 3)


class DeskTileTests(unittest.TestCase):  # 2.9.7: Coming up, Series & timers and problems as tiles
    TODAY, TOMORROW = 100_000.0, 190_000.0

    def setUp(self):
        from services import recording_desk as desk

        self.desk = desk
        self.library = rl.RecordingsLibrary(
            lambda: list(FILES),
            lambda: {},
            FakeWatch(),
            SimpleNamespace(playback_positions={}),
            picture=lambda item: f"thumb:{item['path'].rsplit('/', 1)[-1]}.jpg",
        )
        self.rec = desk.Entry(
            "rec:sched:x",
            desk.NOW,
            "Evening News",
            detail="Northwind News · until 21:58",
            tone=desk.REC,
            status="REC",
            buttons=(desk.button(desk.WATCH), desk.button(desk.STOP, desk.DANGER)),
            channel="Northwind News",
        )
        self.late = desk.Entry(
            "airing:t@100000",
            desk.COMING,
            "Late film",
            detail="Thu 01:00–02:30 · Harbor News 24",
            note="Two would need 3 of Demo TV's 2 streams",
            tone=desk.WARN,
            status="Repeats",
            buttons=(desk.button(desk.EDIT), desk.button(desk.SKIP, enabled=False)),
            start=self.TODAY,
            channel="Harbor News 24",
            logo="https://logos.invalid/harbor.png",
        )
        self.kitchen = desk.Entry(
            "airing:s@190000",
            desk.COMING,
            "Kitchen Stories",
            status="Series",
            buttons=(desk.button(desk.SKIP),),
            start=self.TOMORROW,
            channel="Northwind News",
        )
        self.add = desk.Entry(
            "add:coming",
            desk.COMING,
            "",
            buttons=(desk.button(desk.ADD_TIMER, desk.ACCENT), desk.button(desk.OPEN_GUIDE)),
        )

    def day(self, moment):
        return "Today" if moment < 150_000 else "Tomorrow"

    def test_coming_up_goes_by_day_after_what_records_now(self):
        sections = self.library.desk_sections(
            [self.rec, self.late, self.kitchen, self.add], self.day
        )
        shown = [(title, [item["name"] for item in items]) for title, items in sections]
        self.assertEqual(
            shown,
            [
                ("Recording now", ["Evening News"]),
                ("Today", ["Late film"]),
                ("Tomorrow", ["Kitchen Stories"]),
                ("Add a recording", ["Record at a time…"]),
            ],
        )

    def test_a_tiles_strip_has_its_rows_buttons_and_says_its_note(self):
        ((_title, (tile,)),) = self.library.desk_sections([self.late], self.day)
        details = self.library.details(tile)
        self.assertEqual(details.kind, "desk")
        self.assertEqual(details.buttons, (("edit", "Edit…", "raised"), ("skip", "Skip", "off")))
        self.assertEqual(details.plot, "Two would need 3 of Demo TV's 2 streams")
        self.assertEqual(details.provider, "Thu 01:00–02:30 · Harbor News 24")
        rec = self.library.desk_sections([self.rec], self.day)[0][1][0]
        self.assertEqual(
            self.library.details(rec).buttons,
            (("watch", "Watch", "raised"), ("stop", "Stop", "danger")),
        )

    def test_tiles_show_their_time_status_caption_and_picture(self):
        sections = self.library.desk_sections([self.rec, self.late], self.day)
        rec, late = sections[0][1][0], sections[1][1][0]
        self.assertEqual(self.library.badges(late)[1:], ("REPEATS", "warn"))
        self.assertEqual(self.library.badges(rec)[1:], ("REC", "live"))
        self.assertEqual(self.library.caption(late), ("Late film", "Harbor News 24"))
        self.assertEqual(self.library.picture(late), ("https://logos.invalid/harbor.png", True))
        self.assertEqual(self.library.picture(rec), ("", False))  # no logo: its tinted card

    def test_a_recording_without_its_frame_yet_shows_its_channels_logo(self):
        library = rl.RecordingsLibrary(
            lambda: list(FILES), lambda: {}, FakeWatch(), SimpleNamespace(), picture=lambda item: ""
        )
        with_logo = dict(FILES[0], _logo="https://logos.invalid/harbor.png")
        self.assertEqual(library.picture(with_logo), ("https://logos.invalid/harbor.png", True))
        self.assertEqual(library.picture(FILES[1]), ("", False))  # no logo either: its card

    def test_recordings_show_their_thumbnail_and_caption(self):
        night = FILES[0]
        self.assertEqual(self.library.picture(night), ("thumb:c.ts.jpg", False))
        when = self.desk.short_when(3_000.0)
        self.assertEqual(self.library.caption(night), ("Night Match", f"Harbor Sports · {when}"))
        untitled = FILES[2]  # known by its channel alone: the line under says when
        self.assertEqual(
            self.library.caption(untitled), ("Northwind News", self.desk.short_when(1_000.0))
        )
