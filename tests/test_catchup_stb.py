"""Catch-up on an STB portal's channel in the player: the portal makes the link to the recording
of one programme of its guide, the recording opens sought to the moment asked, the next
programme's link plays past its end, and every failure leaves the channel playing (or goes
live) with a plain toast. One stream at a time on a 1-stream portal; never a probe.

The controller's fakes (tests/test_tv_controller.py's) with a real connection budget; the
fake catalog answers the archive links the tests give it. Channels and addresses are
fictional.
"""

from datetime import timedelta
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_catchup_playback import STATE, CatchupCase
from test_tv_controller import NOW
from timer_fixtures import wait_until

from services.catalog import EMPTY_LINK_MESSAGE, UNEXPECTED_ANSWER_MESSAGE
from services.epg_view import Programme, Recording
from services.live.tuning import channel_key
from ui.catchup_controls import ARCHIVE_TEXT, NO_ANSWER_TEXT, NOT_RECORDED_TEXT
from ui.keymap import Action
from ui.overlays.guide import Guide

# A portal's channel marked archived (a week), and one not marked.
ARCHIVED = {
    "id": 11,
    "name": "Harbour News",
    "number": "1",
    "cmd": "ffrt http://localhost/ch/11_",
    "tv_archive": "1",
    "archive_days": 7,
    "catchup": "stb",
}
PLAIN = {"id": 12, "name": "Quiet Pier", "number": "2", "cmd": "ffrt http://localhost/ch/12_"}
CHANNELS = [ARCHIVED, PLAIN]
HALF = timedelta(minutes=30)
FIRST = NOW - 5 * HALF  # 18:00; NOW is 20:30
LIVE = {11: "http://portal.test/live/11.ts?play_token=a", 12: "http://portal.test/live/12.ts"}


def recording_url(programme_id):
    return f"http://portal.test/archive/{programme_id}.ts?token=t"


def guide(first_id=5000, marks=None):
    """Half-hour programmes from 18:00 to 22:00, ids from `first_id`: 18:00 is 5000, 19:30 is
    5003, 20:00 is 5004; `marks` (by id) overrides "recorded" (ended) and "not yet" (on now
    and later)."""
    shows = []
    for i in range(8):
        start = FIRST + i * HALF
        ident = str(first_id + i)
        marked = (marks or {}).get(ident, start + HALF <= NOW)
        shows.append(
            Programme(
                f"Show {i}",
                start,
                start + HALF,
                recording=Recording(ident, start, start + HALF, marked),
            )
        )
    return shows


class StbCase(CatchupCase):
    def setUp(self):
        super().setUp()
        self.catalog.ptype = "STB"  # one stream at a time
        self.catalog.live = {"10": list(CHANNELS)}
        self.catalog.epg = {11: guide(), 12: guide(6000)}
        epg = self.catalog.epg
        self.catalog.programmes = lambda channel, now, limit=8: [
            p for p in epg.get(channel.get("id"), []) if p.stop > now
        ][:limit]
        self.archive_links = []  # (request id, item, programme id)

        def request_archive_link(item, programme):
            request_id = 1000 + len(self.archive_links)
            self.archive_links.append((request_id, item, programme))
            return request_id

        self.catalog.request_archive_link = request_archive_link

    def start_live(self, channel=ARCHIVED, channels=CHANNELS):
        """Tune `channel` and play it through the time-shift buffer (STATE: the last ten
        minutes, played 9:55 behind live)."""
        count = len(self.starts())
        self.controller.tune(channel, group_key="10", channels=channels)
        self.catalog.linkReady.emit(self.catalog.links[-1][0], LIVE[channel["id"]])
        if self.live.calls and self.live.calls[-1] == ("stop",):
            self.live.released.emit()  # the channel before it let go
        self.assertTrue(wait_until(lambda: len(self.starts()) == count + 1))
        self.live.play_pipeline()
        self.live.timelineChanged.emit(dict(STATE))

    def link(self, url=None):
        """The portal's answer to the newest archive link asked: the recording's link."""
        request_id, _item, programme = self.archive_links[-1]
        self.catalog.linkReady.emit(request_id, url or recording_url(programme))

    def refuse(self, message=EMPTY_LINK_MESSAGE):
        self.catalog.linkFailed.emit(self.archive_links[-1][0], message)

    def asked(self):
        return [programme for _id, _item, programme in self.archive_links]

    def watch_programme(self, channel, programme_id):
        programme = next(
            p for p in self.catalog.epg[channel["id"]] if p.recording.id == programme_id
        )
        self.controller._guide.catchupRequested.emit(channel, programme, "10", CHANNELS)


class OfferTests(StbCase):
    def test_a_channel_the_portal_marks_archived_reaches_into_its_archive(self):
        self.start_live()
        self.controller.mouse_moved()
        state, _ticks = self.transport.named("update_timeline")[-1]
        self.assertEqual(state["buffer_start"], 3000.0)  # the dashed part before the buffer
        self.assertEqual(self.controller.archive_days(ARCHIVED), 7)  # the guide's reach

    def test_a_channel_not_marked_rewinds_as_before(self):
        self.start_live(PLAIN)
        self.controller.mouse_moved()
        state, _ticks = self.transport.named("update_timeline")[-1]
        self.assertNotIn("buffer_start", state)
        self.assertEqual(self.controller.archive_days(PLAIN), 0)
        self.controller.handle(Action.SEEK_BACK)
        self.assertEqual(self.live.named("seek_relative"), [(-10.0,)])
        self.assertEqual(self.archive_links, [])


class OpenTests(StbCase):
    def test_a_seek_into_the_archive_plays_the_programme_on_then_from_where_it_was(self):
        self.start_live()
        self.transport.seekToRequested.emit(1500.0)  # 35 min before now: 19:55
        self.assertEqual(self.asked(), ["5003"])  # 19:30-20:00
        self.assertIs(self.archive_links[-1][1], ARCHIVED)
        self.assertNotEqual(self.live.calls[-1], ("stop",))  # live plays while the link comes
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.link()
        self.assertEqual(self.live.calls[-1], ("stop",))  # live closes first ...
        self.assertEqual(self.archive_plays(), [])  # ... and nothing opens meanwhile
        self.live.released.emit()
        [(url, kwargs)] = self.archive_plays()
        self.assertEqual(url, recording_url("5003"))
        self.assertEqual(kwargs["start_ms"], 25 * 60_000)  # 19:55 is 25 minutes in
        self.assertEqual(self.budget.holders("Home"), ["main"])  # one stream, never two
        self.assertEqual(self.runner.jobs, [])  # never probed
        self.assertIn((ARCHIVE_TEXT, "info"), self.toast.messages())
        self.session.stateChanged.emit("playing")
        self.played_ms = 26 * 60_000
        self.assertEqual(self.catchup.position(), (FIRST + 3 * HALF).timestamp() + 26 * 60)

    def test_no_probe_even_where_the_portal_allows_more_streams(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 3}
        self.start_live()
        self.transport.seekToRequested.emit(1500.0)
        self.link()
        self.live.released.emit()
        self.assertEqual(self.runner.jobs, [])
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_watch_from_the_start_on_the_channel_playing(self):
        self.start_live()
        links = len(self.catalog.links)
        self.watch_programme(ARCHIVED, "5000")  # 18:00-18:30
        self.assertEqual(len(self.catalog.links), links)  # no live link
        self.assertEqual(self.asked(), ["5000"])
        self.link()
        self.live.released.emit()
        url, kwargs = self.archive_plays()[-1]
        self.assertEqual(url, recording_url("5000"))
        self.assertIsNone(kwargs["start_ms"])  # from its start
        self.assertTrue(self.catchup.active())

    def test_watch_from_the_start_on_another_channel(self):
        self.start_live(PLAIN)
        self.watch_programme(ARCHIVED, "5001")
        self.assertIs(self.catalog.links[-1][1], ARCHIVED)  # the channel's own tune first
        self.catalog.linkReady.emit(self.catalog.links[-1][0], LIVE[11])
        self.assertEqual(self.asked(), ["5001"])
        self.assertNotEqual(self.live.calls[-1], ("stop",))  # the other channel plays on
        self.link()
        self.live.released.emit()
        self.assertEqual(self.archive_plays()[-1][0], recording_url("5001"))
        self.assertIs(self.controller._current.item, ARCHIVED)
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_steps_while_the_link_comes_ask_once_and_play_the_newest_moment(self):
        self.start_live()
        self.transport.seekToRequested.emit(1500.0)  # 19:55
        self.transport.seekToRequested.emit(1200.0)  # 19:50: the same programme
        self.assertEqual(self.asked(), ["5003"])
        self.link()
        self.live.released.emit()
        self.assertEqual(self.archive_plays()[-1][1]["start_ms"], 20 * 60_000)

    def test_a_moment_the_guide_has_no_recording_of_says_so_and_live_plays_on(self):
        self.catalog.epg[11] = guide(marks={"5003": False})
        self.start_live()
        self.transport.seekToRequested.emit(1500.0)  # 19:55: not recorded
        self.assertEqual(self.archive_links, [])
        self.assertIn((NOT_RECORDED_TEXT, "info"), self.toast.messages())
        self.assertTrue(self.live.running)
        self.assertFalse(self.catchup.active())
        self.assertEqual(self.live.named("seek_to")[-1], (1500.0,))  # the buffer's start
        self.catalog.epg[11] = []  # no guide at all: nothing to ask the portal for
        self.transport.seekToRequested.emit(1500.0)
        self.assertEqual(self.archive_links, [])

    def test_go_live_or_a_zap_while_the_link_comes_never_opens_it(self):
        self.start_live()
        self.transport.seekToRequested.emit(1500.0)
        self.controller.handle(Action.GO_LIVE)
        self.link()
        self.assertEqual(self.archive_plays(), [])
        self.transport.seekToRequested.emit(1500.0)
        self.start_live(PLAIN)  # a zap
        self.link()
        self.assertEqual(self.archive_plays(), [])
        self.assertEqual(self.budget.holders("Home"), ["main"])


class PlayingTests(StbCase):
    def setUp(self):
        super().setUp()
        self.start_live()
        self.transport.seekToRequested.emit(1500.0)  # 19:55, in 5003 (19:30-20:00)
        self.link()
        self.live.released.emit()
        self.session.stateChanged.emit("playing")
        self.played_ms = 25 * 60_000

    def test_a_seek_within_the_programme_moves_within_its_recording(self):
        self.session.seekable = True
        self.transport.seekToRequested.emit((FIRST + 3 * HALF).timestamp() + 60)  # 19:31
        self.assertEqual(self.session.seeks, [60_000])
        self.assertEqual(self.asked(), ["5003"])

    def test_a_seek_past_its_end_plays_the_next_programmes_link(self):
        self.session.seekable = True
        plays = len(self.archive_plays())
        self.transport.seekToRequested.emit((FIRST + 4 * HALF).timestamp() + 300)  # 20:05
        self.assertEqual(self.session.seeks, [])
        self.assertEqual(self.asked(), ["5003", "5004"])
        self.assertEqual(len(self.archive_plays()), plays)  # it plays on until the link comes
        self.link()
        url, kwargs = self.archive_plays()[-1]
        self.assertEqual(url, recording_url("5004"))
        self.assertEqual(kwargs["start_ms"], 5 * 60_000)
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_its_end_at_a_programme_not_recorded_yet_goes_live_quietly(self):
        self.catalog.epg[11] = guide(marks={"5004": False})  # 20:00-20:30 not recorded yet
        self.played_ms = 30 * 60_000  # 20:00: 5003 is over
        links = len(self.catalog.links)
        self.session.ended.emit()
        self.assertFalse(self.catchup.active())
        self.assertEqual(len(self.catalog.links), links + 1)  # the channel live again
        self.assertEqual(self.asked(), ["5003"])  # nothing asked for 20:00
        self.assertNotIn((NOT_RECORDED_TEXT, "info"), self.toast.messages())
        self.assertNotIn((NO_ANSWER_TEXT, "info"), self.toast.messages())

    def test_its_end_plays_the_next_programme_from_its_start(self):
        self.played_ms = 29 * 60_000  # the recording ended a little before the guide said
        self.session.ended.emit()
        self.assertTrue(self.catchup.active())
        self.assertEqual(self.asked(), ["5003", "5004"])
        self.link()
        url, kwargs = self.archive_plays()[-1]
        self.assertEqual(url, recording_url("5004"))
        self.assertIsNone(kwargs["start_ms"])

    def test_a_failure_after_it_played_opens_it_again_once_where_it_was(self):
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.asked(), ["5003", "5003"])
        self.link()
        self.assertEqual(self.archive_plays()[-1][1]["start_ms"], 25 * 60_000)
        self.session.stateChanged.emit("playing")
        links = len(self.catalog.links)
        self.session.errorOccurred.emit("The stream could not be opened")  # again: live
        self.assertFalse(self.catchup.active())
        self.assertEqual(len(self.catalog.links), links + 1)
        self.assertEqual(self.asked(), ["5003", "5003"])


class FailureTests(StbCase):
    """A link that does not come is asked again once after the portal signs in again; then
    the channel plays on (or live) and the toast says why."""

    PORTAL = dict(ARCHIVED, _provider="Portal B")  # another provider's portal

    def setUp(self):
        super().setUp()
        self.catalog.catalogs.ready.add("Portal B")  # signed in: it may sign in again

    def start_portal(self):
        self.start_live(self.PORTAL, [self.PORTAL, PLAIN])
        self.transport.seekToRequested.emit(1500.0)
        self.assertEqual(self.asked(), ["5003"])

    def test_a_link_refused_once_signs_the_portal_in_again_and_asks_once_more(self):
        self.start_portal()
        self.refuse(UNEXPECTED_ANSWER_MESSAGE)  # e.g. "Authorization failed."
        self.assertEqual(self.catalog.setups, ["Portal B"])  # its session anew
        self.assertEqual(self.asked(), ["5003", "5003"])
        self.assertTrue(self.live.running)  # live plays on meanwhile
        self.link()
        self.live.released.emit()
        self.assertEqual(self.archive_plays()[-1][0], recording_url("5003"))
        self.assertEqual(self.budget.holders("Portal B"), ["main"])

    def test_a_link_refused_twice_says_so_and_live_plays_on(self):
        self.start_portal()
        self.refuse(UNEXPECTED_ANSWER_MESSAGE)
        self.refuse(UNEXPECTED_ANSWER_MESSAGE)
        self.assertIn((NO_ANSWER_TEXT, "info"), self.toast.messages())
        self.assertEqual(self.asked(), ["5003", "5003"])  # no third
        self.assertEqual(self.catalog.setups, ["Portal B"])  # one new sign-in
        self.assertTrue(self.live.running)
        self.assertNotIn(("stop",), self.live.calls[-1:])
        self.assertFalse(self.catchup.active())
        self.assertEqual(self.archive_plays(), [])

    def test_no_recording_says_so_and_twice_for_two_moments_is_learned(self):
        self.start_portal()
        self.refuse()  # the portal answered: nothing to play (its session works: no sign-in)
        self.assertIn((NOT_RECORDED_TEXT, "info"), self.toast.messages())
        self.assertEqual((self.asked(), self.catalog.setups), (["5003"], []))
        self.assertTrue(self.live.running)
        self.assertNotIn(("stop",), self.live.calls[-1:])
        key = channel_key("Portal B", self.PORTAL)
        self.assertFalse(self.memory.failed(key))  # once is no proof
        self.transport.seekToRequested.emit(3600.0 - 8400.0)  # 18:10, in 5000
        self.assertEqual(self.asked()[-1], "5000")
        self.refuse()
        self.assertTrue(self.memory.failed(key))
        self.assertEqual(self.controller.archive_days(self.PORTAL), 0)  # the guide offers none
        self.controller.mouse_moved()
        state, _ticks = self.transport.named("update_timeline")[-1]
        self.assertNotIn("buffer_start", state)  # nor the bar

    def test_the_browsed_portals_link_is_asked_once_more_as_a_channels_is(self):
        self.start_live()
        self.transport.seekToRequested.emit(1500.0)
        self.refuse(UNEXPECTED_ANSWER_MESSAGE)
        self.assertEqual(self.catalog.setups, [])  # its next link has a fresh token
        self.assertEqual(self.asked(), ["5003", "5003"])
        self.link()
        self.live.released.emit()
        self.assertEqual(self.archive_plays()[-1][0], recording_url("5003"))

    def test_a_recording_that_does_not_open_gets_a_fresh_link_once_then_live(self):
        self.start_portal()
        self.link()
        self.live.released.emit()
        self.session.errorOccurred.emit("The stream could not be opened")  # its token, maybe
        self.assertEqual(self.catalog.setups, ["Portal B"])
        self.assertEqual(self.asked(), ["5003", "5003"])
        self.link()
        self.assertEqual(len(self.archive_plays()), 2)
        links = len(self.catalog.links)
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertIn((NO_ANSWER_TEXT, "info"), self.toast.messages())
        self.assertFalse(self.catchup.active())
        self.assertEqual(len(self.catalog.links), links + 1)  # the channel live again
        self.assertEqual(self.runner.jobs, [])  # never probed
        self.assertLessEqual(len(self.budget.holders("Portal B")), 1)

    def test_watch_from_the_start_that_fails_plays_the_channel_live(self):
        self.start_live(PLAIN)
        self.watch_programme(ARCHIVED, "5001")
        self.catalog.linkReady.emit(self.catalog.links[-1][0], LIVE[11])
        self.refuse(UNEXPECTED_ANSWER_MESSAGE)
        self.refuse(UNEXPECTED_ANSWER_MESSAGE)
        self.assertIn((NO_ANSWER_TEXT, "info"), self.toast.messages())
        self.assertIs(self.catalog.links[-1][1], ARCHIVED)  # its live link asked again
        self.catalog.linkReady.emit(self.catalog.links[-1][0], LIVE[11])
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: self.starts()[-1][0] == LIVE[11]))
        self.assertEqual(self.archive_plays(), [])
        self.assertEqual(self.budget.holders("Home"), ["main"])


class GuideTests(StbCase):
    def test_the_guide_offers_only_programmes_the_portal_recorded(self):
        programmes = guide(marks={"5001": False})
        shown = Guide(None, lambda c, s, n: programmes, lambda: NOW, archive_days=lambda c: 7)
        self.addCleanup(shown.deleteLater)
        self.assertTrue(shown._playable(programmes[0], ARCHIVED))
        self.assertFalse(shown._playable(programmes[1], ARCHIVED))  # marked not recorded
        bare = Programme("From a file", FIRST, FIRST + HALF)  # an XMLTV guide's: no id
        self.assertFalse(shown._playable(bare, ARCHIVED))


if __name__ == "__main__":
    unittest.main()
