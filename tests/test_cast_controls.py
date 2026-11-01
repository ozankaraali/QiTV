"""Casting (2.8): the cast button's menu, sending what plays to a device and back."""

import os
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services.casting import CastDevices
from services.playback import PlaybackSession
from services.vlc_profile import PlayKind
from ui.cast_controls import CastControls

app = QApplication.instance() or QApplication([])


class FakeDiscovery:
    def __init__(self):
        self.found = self.lost = None
        self.released = []

    def start(self, found, lost):
        self.found, self.lost = found, lost
        return True

    def stop(self):
        pass

    def release(self, handle):
        self.released.append(handle)


class FakeSession:
    def __init__(self):
        self.renderers, self.reopens = [], []
        self.time = 61_000

    def cast_to(self, renderer):
        self.renderers.append(renderer)

    def casting(self):
        return bool(self.renderers) and self.renderers[-1] is not None

    def reopen(self, start_ms=None):
        self.reopens.append(start_ms)

    def time_ms(self):
        return self.time


class FakeLive:
    def __init__(self):
        self.reopened = 0

    def reopen_player(self):
        self.reopened += 1
        return True


class FakeTransport:
    def __init__(self):
        self.states = []

    def set_cast_state(self, available, casting):
        self.states.append((available, casting))


class FakeNotice:
    def __init__(self):
        self.text, self.shown = "", False

    def show_notice(self, text):
        self.text, self.shown = text, True

    def hide(self):
        self.shown = False


class CastControlsTests(unittest.TestCase):
    def setUp(self):
        self.discovery = FakeDiscovery()
        self.devices = CastDevices(self.discovery)
        self.session, self.live = FakeSession(), FakeLive()
        self.target = SimpleNamespace(live=True)
        self.transport, self.notice = FakeTransport(), FakeNotice()
        self.toasts, self.one_picture = [], []
        self.controls = CastControls(
            devices=self.devices,
            session=lambda: self.session,
            live=lambda: self.live,
            current=lambda: self.target,
            one_picture=lambda: self.one_picture.append(True),
            transport=self.transport,
            notice=self.notice,
            toast=lambda text, kind="info", ms=0: self.toasts.append((text, kind)),
        )

    def found(self, *names):
        self.devices.start()
        for name in names:
            self.discovery.found(name, "chromecast", f"handle:{name}")
        app.processEvents()

    def test_the_menu_looks_for_devices_until_one_is_found(self):
        self.assertEqual(self.controls.menu_entries(), [("Looking for devices…", None, False)])
        self.found("Living Room TV")
        self.assertEqual(
            self.controls.menu_entries(), [("Living Room TV", "Living Room TV", False)]
        )

    def test_without_casting_in_vlc_the_menu_says_so_and_the_button_goes(self):
        def refuse(found, lost):
            raise RuntimeError("no renderer modules")

        self.discovery.start = refuse
        with self.assertLogs("services.casting", "WARNING"):
            entries = self.controls.menu_entries()
        self.assertEqual(entries, [("Casting isn't available on this computer", None, False)])
        self.assertFalse(self.controls.available())
        self.assertEqual(self.transport.states[-1], (False, False))

    def test_casting_a_channel_sends_it_to_the_device_from_its_buffer(self):
        self.found("Living Room TV")
        self.controls.cast_to("Living Room TV")
        self.assertEqual(self.one_picture, [True])  # one picture: no multiview, no PiP
        self.assertEqual(self.session.renderers, ["handle:Living Room TV"])
        self.assertEqual(self.live.reopened, 1)
        self.assertEqual(self.session.reopens, [])
        self.assertEqual(self.transport.states[-1], (True, True))
        self.assertTrue(self.notice.shown)
        self.assertIn("Living Room TV", self.notice.text)
        entries = self.controls.menu_entries()
        self.assertEqual(entries[0], ("Living Room TV", "Living Room TV", True))
        self.assertEqual(entries[-1], ("Stop casting", "", False))

    def test_casting_a_film_goes_on_from_where_it_is(self):
        self.target = SimpleNamespace(live=False)
        self.found("Living Room TV")
        self.controls.cast_to("Living Room TV")
        self.assertEqual(self.session.reopens, [61_000])
        self.assertEqual(self.live.reopened, 0)

    def test_with_nothing_playing_the_next_play_goes_to_the_device(self):
        self.target = None
        self.found("Living Room TV")
        self.controls.cast_to("Living Room TV")
        self.assertEqual(self.session.renderers, ["handle:Living Room TV"])
        self.assertEqual((self.session.reopens, self.live.reopened), ([], 0))

    def test_a_swap_moves_the_cast_to_the_large_picture(self):  # 2.8 review M13
        self.found("Living Room TV")
        self.controls.cast_to("Living Room TV")
        small_session, small_live = self.session, self.live  # it goes small now
        small_target = self.target
        self.session, self.live = FakeSession(), FakeLive()  # the PiP's, now large
        self.target = SimpleNamespace(live=False)
        self.controls.swapped(small_session, small_live, small_target)
        self.assertEqual(small_session.renderers[-1], None)  # here again
        self.assertEqual(small_live.reopened, 2)  # from its buffer, where it was
        self.assertEqual(self.session.renderers, ["handle:Living Room TV"])
        self.assertEqual(self.session.reopens, [61_000])  # the film, from where it is
        self.assertTrue(self.controls.casting())

    def test_a_swap_without_a_cast_changes_nothing(self):
        other = FakeSession()
        self.controls.swapped(other, FakeLive(), self.target)
        self.assertEqual((other.renderers, self.session.renderers), ([], []))

    def test_stopping_brings_the_picture_back_here(self):
        self.target = SimpleNamespace(live=False)
        self.found("Living Room TV")
        self.controls.cast_to("Living Room TV")
        self.controls.stop()
        self.assertEqual(self.session.renderers[-1], None)
        self.assertEqual(self.session.reopens, [61_000, 61_000])
        self.assertFalse(self.notice.shown)
        self.assertEqual(self.transport.states[-1], (True, False))
        self.assertFalse(self.controls.casting())

    def test_a_device_that_goes_away_stops_the_cast_and_says_so(self):
        self.found("Living Room TV")
        self.controls.cast_to("Living Room TV")
        self.discovery.lost("Living Room TV")
        app.processEvents()
        self.assertEqual(self.session.renderers[-1], None)
        self.assertTrue(any("Living Room TV" in text for text, _kind in self.toasts))

    def test_an_unknown_device_casts_nothing(self):
        self.controls.cast_to("Nowhere")
        self.assertEqual(self.session.renderers, [])


class _Media:
    def __init__(self, url):
        self.url = url

    def add_option(self, option):
        pass

    def release(self):
        pass


class _Player:
    """libVLC's player, as far as an open, a stop and a renderer go: what it opened."""

    def __init__(self):
        self.opened = []

    def media_new(self, url):  # (the instance's)
        return _Media(url)

    def media_player_new(self):
        return self

    def video_set_mouse_input(self, on):
        pass

    video_set_key_input = video_set_mouse_input

    def set_media(self, media):
        if media is not None:
            self.opened.append(media.url)

    def set_renderer(self, renderer):
        return 0

    def play(self):
        return 0

    def stop(self):
        pass

    def release(self):
        pass


class _Runner:
    """Ends libVLC's input at once (the session runs it on a worker)."""

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        worker.run()
        on_finished(context, None)


class HandedOnTests(unittest.TestCase):
    """2.8 review: a channel handed to a phone's Watch (or Play in VLC) is stopped here, its
    stream's budget slot given back. A cast begun, stopped, or ended by its device going away
    then reopened the old stream in QiTV: a second stream on a one-stream provider, and the
    phone was told to stop."""

    def setUp(self):
        for name in ("hold", "release"):
            patched = mock.patch(f"services.casting.{name}", side_effect=lambda handle: handle)
            patched.start()
            self.addCleanup(patched.stop)
        self.player = _Player()
        self.session = PlaybackSession(self.player, runner=_Runner(), poll_ms=60_000)
        self.addCleanup(self.session.release)
        self.discovery = FakeDiscovery()
        self.devices = CastDevices(self.discovery)
        stopped_buffer = SimpleNamespace(reopen_player=lambda: False)  # its tune stopped too
        self.controls = CastControls(
            devices=self.devices,
            session=lambda: self.session,
            live=lambda: stopped_buffer,
            current=lambda: SimpleNamespace(live=True),  # what was handed on
            one_picture=lambda: None,
            transport=None,
            notice=None,
            toast=lambda *_args, **_kwargs: None,
        )
        self.devices.start()
        self.discovery.found("Den", "chromecast", "handle:Den")
        app.processEvents()

    def test_nothing_opens_here_after_a_hand_off(self):
        self.session.play("http://provider.test/live/u/p/1.ts", kind=PlayKind.LIVE)
        self.session.stop()  # handed on: the tuner let go of it
        self.controls.cast_to("Den")
        self.controls.stop()
        self.controls.cast_to("Den")
        self.discovery.lost("Den")  # the device turned off
        app.processEvents()
        self.assertEqual(self.player.opened, ["http://provider.test/live/u/p/1.ts"])

    def test_what_plays_here_opens_again_with_the_device(self):
        self.session.play("http://provider.test/live/u/p/1.ts", kind=PlayKind.LIVE)
        self.controls.cast_to("Den")
        self.assertEqual(self.player.opened, ["http://provider.test/live/u/p/1.ts"] * 2)


if __name__ == "__main__":
    unittest.main()
