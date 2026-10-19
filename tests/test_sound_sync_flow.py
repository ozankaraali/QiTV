"""The sound's sync in the TV shell (2.2): J and K move the large picture's sound, a live
channel keeps its sync for its next tune and for each reopen, a film's lasts while it plays,
and a swap moves nothing: the sync follows the channel, not the player.

The controller's fakes are tests/test_tv_controller.py's and tests/test_tv_pip.py's.
"""

import os
import tempfile
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from timer_fixtures import wait_until

from services.live.tuning import channel_key
from services.sound_sync import SoundSync
from tests.test_tv_controller import (
    CHANNELS,
    ControllerTestCase,
    PipelineCase,
    VodTests,
    play_context,
)
from tests.test_tv_pip import GROUP_B, PipCase
from ui.keymap import Action

ONE, TWO = channel_key("Home", CHANNELS[0]), channel_key("Home", CHANNELS[1])
FIVE = channel_key("Home", GROUP_B[0])


class SyncKeysTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SoundSync(os.path.join(directory.name, "sound_sync.json"))
        self.addCleanup(self.store.flush)
        self.controller = self.make_controller(self.column, sound_sync=self.store)
        self.controller.start()

    def test_j_and_k_move_the_sound_and_say_so(self):
        self.play(0)
        self.controller.handle(Action.SOUND_LATER)
        self.controller.handle(Action.SOUND_LATER)
        self.controller.handle(Action.SOUND_LATER)
        self.controller.handle(Action.SOUND_EARLIER)
        self.assertEqual(self.session.delay, 100)
        self.assertEqual(
            self.toast.messages()[-4:],
            [
                ("Sound 50 ms later", "info"),
                ("Sound 100 ms later", "info"),
                ("Sound 150 ms later", "info"),
                ("Sound 100 ms later", "info"),
            ],
        )
        self.assertEqual(self.store.audio_ms(ONE), 100)  # kept for the channel

    def test_nothing_playing_nothing_moves(self):
        self.controller.handle(Action.SOUND_LATER)
        self.assertEqual((self.session.delays, self.toast.messages()), ([], []))

    def test_a_channel_opens_again_with_its_sync_and_another_in_sync(self):
        self.play(0)
        for _ in range(3):
            self.controller.handle(Action.SOUND_LATER)
        self.play(1)
        self.assertEqual(self.session.play_delays[-1], 0)
        self.controller.handle(Action.SOUND_EARLIER)
        self.play(0)
        self.assertEqual(self.session.play_delays[-1], 150)  # set before its play
        self.play(1)
        self.assertEqual(self.session.play_delays[-1], -50)

    def test_the_sync_outlives_a_restart(self):
        self.play(0)
        self.controller.handle(Action.SOUND_LATER)
        self.store.flush()
        store = SoundSync(self.store._path)
        self.assertEqual(store.audio_ms(ONE), 50)

    def test_a_film_starts_in_sync_and_its_sync_is_never_kept(self):
        self.play(0)
        self.controller.handle(Action.SOUND_LATER)
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.assertEqual(self.session.play_delays[-1], 0)
        self.controller.handle(Action.SOUND_EARLIER)
        self.assertEqual(self.session.delay, -50)
        self.assertEqual(self.store.audio_ms(ONE), 50)
        self.assertEqual(len(self.store._entries), 1)  # nothing for the film

    def test_a_local_file_starts_in_sync(self):
        self.play(0)
        self.controller.handle(Action.SOUND_LATER)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            self.controller.play_file(f.name)
            self.assertEqual(self.session.play_delays[-1], 0)


class DirectReopenTests(ControllerTestCase):
    """A channel played directly: a reconnect opens the same player again, sync and all."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (40, 120)))
        self.catalog.ptype = "XTREAM"
        self.controller.start()

    def test_a_reconnect_keeps_the_channels_sync(self):
        self.play(0)
        self.controller.handle(Action.SOUND_LATER)
        self.session.holding = True
        self.session.ended.emit()
        self.assertTrue(wait_until(lambda: len(self.session.plays) == 2))
        self.assertEqual(self.session.play_delays, [0, 50])
        self.assertEqual(self.session.delays, [0, 50])  # the player kept it: never set again


class PipelineSyncTests(PipelineCase):
    def test_a_time_shift_channel_opens_with_its_sync_and_keeps_it_through_reopens(self):
        self.start_pipeline(0)
        for _ in range(4):
            self.controller.handle(Action.SOUND_LATER)
        self.start_pipeline(1)
        self.assertEqual(self.session.play_delays[-1], 0)
        self.start_pipeline(0)
        self.assertEqual(self.session.play_delays[-1], 200)
        delays = list(self.session.delays)
        self.live.play_pipeline()  # LiveSession opens the buffer again (a seek, Go live)
        self.assertEqual(self.session.play_delays[-1], 200)
        self.assertEqual(self.session.delays, delays)  # its player keeps it


class SyncFollowsTheChannelTests(PipCase):
    def test_a_channel_opening_small_takes_its_sync(self):
        self.controller._tracks._store.set_audio_ms(FIVE, 300)
        small = self.open_small(GROUP_B[0], "20", GROUP_B)
        self.assertIs(small.playback, self.pip_session)
        self.assertEqual(self.pip_session.play_delays[-1], 300)

    def test_after_a_swap_the_keys_move_the_new_large_pictures_channel(self):
        self.start_large(0)
        self.controller.handle(Action.SOUND_LATER)  # One: 50 ms later
        self.open_small(GROUP_B[0], "20", GROUP_B)
        self.controller.handle(Action.SWAP)  # Five is large now, in the other player
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.controller.handle(Action.SOUND_EARLIER)
        self.controller.handle(Action.SOUND_EARLIER)
        self.assertEqual(self.pip_session.delay, -100)
        self.assertEqual(self.session.delay, 50)  # One plays on small, with its sync
        store = self.controller._tracks._store
        self.assertEqual((store.audio_ms(ONE), store.audio_ms(FIVE)), (50, -100))
        self.controller.handle(Action.SWAP)  # back: nothing reopens or changes
        self.assertEqual((self.session.delay, self.pip_session.delay), (50, -100))
        self.controller.handle(Action.SOUND_LATER)
        self.assertEqual(store.audio_ms(ONE), 100)

    def test_a_zap_of_the_large_picture_after_a_swap_takes_that_channels_sync(self):
        store = self.controller._tracks._store
        store.set_audio_ms(TWO, 250)
        self.start_large(0)
        self.open_small(GROUP_B[0], "20", GROUP_B)
        self.controller.handle(Action.SWAP)
        self.controller.handle(Action.SWAP)
        self.start_large(1)
        self.assertEqual(self.session.play_delays[-1], 250)
