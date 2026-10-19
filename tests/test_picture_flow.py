"""The Picture panel in the TV shell (2.3): A steps the large picture's aspect ratio (the panel
shows it; without one, a toast), a live channel opens again in the shape kept for it and a film
in its own, Shift+I shows the statistics, "End of the programme" counts to the end of the
programme the guide lists, and the sleep timer's end stops playback and leaves fullscreen.

The controller's fakes are tests/test_tv_controller.py's.
"""

from datetime import timedelta
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.epg_view import Programme
from services.live.tuning import channel_key
from services.picture import ChannelPictures
from tests import test_tv_controller as tv
from tests.test_tv_controller import CHANNELS, NOW, ControllerTestCase, play_context
from ui.keymap import Action

ONE = channel_key("Home", CHANNELS[0])


class PictureKeysTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.store = ChannelPictures(None)
        self.controller = self.make_controller(self.column, channel_pictures=self.store)
        self.controller.start()

    def test_a_steps_the_aspect_and_the_channel_keeps_it(self):
        self.play(0)
        self.controller.handle(Action.PICTURE)
        self.controller.handle(Action.PICTURE)
        self.assertEqual(self.session.shape, "4:3")
        self.assertEqual(
            self.toast.messages()[-2:],
            [("Aspect ratio: 16:9", "info"), ("Aspect ratio: 4:3", "info")],
        )
        self.assertEqual(self.store.aspect(ONE), "4:3")
        self.play(1)
        self.assertEqual(self.session.shape, "auto")
        self.play(0)
        self.assertEqual(self.session.shape, "4:3")  # set before its play

    def test_a_film_opens_in_its_own_shape(self):
        self.play(0)
        self.controller.handle(Action.PICTURE)
        self.controller.play_vod(tv.VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.assertEqual(self.session.shape, "auto")
        self.controller.handle(Action.PICTURE)
        self.assertEqual(self.store.aspect(ONE), "16:9")  # the film's is never kept
        self.assertEqual(len(self.store._entries), 1)

    def test_nothing_playing_nothing_changes(self):
        self.controller.handle(Action.PICTURE)
        self.controller.handle(Action.STATS)  # no statistics overlay: nothing to show
        self.assertEqual((self.session.shapes, self.toast.messages()), ([], []))

    def test_the_end_of_the_programme_is_the_guides(self):
        self.catalog.epg[CHANNELS[0]["id"]] = [
            Programme("News", NOW - timedelta(minutes=10), NOW + timedelta(minutes=35))
        ]
        self.assertIsNone(self.controller._programme_left())  # nothing plays
        self.play(0)
        self.assertEqual(self.controller._programme_left(), 35 * 60.0)
        self.play(1)
        self.assertIsNone(self.controller._programme_left())  # the guide has nothing

    def test_the_statistics_say_a_direct_play_is_direct(self):
        self.play(0)
        self.assertEqual(self.controller._live_info(), {"source": "direct", "lag": None})

    def test_the_sleep_timers_end_stops_playback_and_leaves_fullscreen(self):
        self.play(0)
        stops = self.session.stops
        self.controller.stop_for_sleep()
        self.assertIn("leave_fullscreen", self.window.calls)
        self.assertGreater(self.session.stops, stops)
        self.assertFalse(self.controller.playing())
        self.assertFalse(self.controller._flow.in_library())  # the player page stays
