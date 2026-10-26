"""Casting (2.8): the cast button's menu, sending what plays to a device and back."""

import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services.casting import CastDevices
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


if __name__ == "__main__":
    unittest.main()
