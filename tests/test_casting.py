"""Casting (2.8): the devices libVLC's renderer discovery finds, kept for the cast menu."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services.casting import CastDevices, renderer_text

app = QApplication.instance() or QApplication([])


class FakeDiscovery:
    """The libVLC side: started once with the two callbacks; releases what it held."""

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.released = []
        self.found = self.lost = None

    def start(self, found, lost):
        self.started += 1
        self.found, self.lost = found, lost
        return True

    def stop(self):
        self.stopped += 1

    def release(self, handle):
        self.released.append(handle)


class CastDevicesTests(unittest.TestCase):
    def setUp(self):
        self.discovery = FakeDiscovery()
        self.devices = CastDevices(self.discovery)
        self.changes = []
        self.devices.changed.connect(lambda: self.changes.append(True))

    def pump(self):
        app.processEvents()

    def test_discovery_starts_once_when_first_asked(self):
        self.assertEqual(self.discovery.started, 0)
        self.devices.start()
        self.devices.start()
        self.assertEqual(self.discovery.started, 1)
        self.assertTrue(self.devices.searching())

    def test_found_devices_are_listed_by_name(self):
        self.devices.start()
        self.discovery.found("Living Room TV", "chromecast", "handle-1")  # a libVLC thread's
        self.discovery.found("Kitchen", "chromecast", "handle-2")
        self.pump()
        self.assertEqual([d.name for d in self.devices.devices()], ["Kitchen", "Living Room TV"])
        self.assertEqual(self.devices.find("Living Room TV").handle, "handle-1")
        self.assertTrue(self.changes)

    def test_a_device_seen_again_keeps_the_newest_handle(self):
        self.devices.start()
        self.discovery.found("Living Room TV", "chromecast", "old")
        self.discovery.found("Living Room TV", "chromecast", "new")
        self.pump()
        self.assertEqual(self.devices.find("Living Room TV").handle, "new")
        self.assertEqual(self.discovery.released, ["old"])

    def test_a_lost_device_goes_and_its_handle_is_released(self):
        self.devices.start()
        self.discovery.found("Kitchen", "chromecast", "handle-2")
        self.pump()
        self.discovery.lost("Kitchen")
        self.pump()
        self.assertIsNone(self.devices.find("Kitchen"))
        self.assertEqual(self.discovery.released, ["handle-2"])

    def test_stopping_releases_every_handle(self):
        self.devices.start()
        self.discovery.found("Kitchen", "chromecast", "handle-2")
        self.pump()
        self.devices.stop()
        self.assertEqual(self.discovery.stopped, 1)
        self.assertEqual(self.discovery.released, ["handle-2"])
        self.assertEqual(self.devices.devices(), [])

    def test_names_come_as_text_whatever_libvlc_gives(self):
        self.assertEqual(renderer_text(b"Living Room"), "Living Room")
        self.assertEqual(renderer_text("Kitchen"), "Kitchen")
        self.assertEqual(renderer_text(None), "")


if __name__ == "__main__":
    unittest.main()
