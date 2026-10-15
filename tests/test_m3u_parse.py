import unittest

from services.m3u import parse_m3u

ENTRY = [
    "#EXTM3U",
    '#EXTINF:-1 tvg-id="one.test" group-title="News",Channel One',
    "#EXTVLCOPT:http-user-agent=UA/1",
    "http://stream.test/1.ts",
]


class ParseM3uLineEndingTests(unittest.TestCase):
    def _assert_clean(self, data):
        [item] = parse_m3u(data)
        self.assertEqual(item["name"], "Channel One")
        self.assertEqual(item["user_agent"], "UA/1")
        self.assertEqual(item["cmd"], "http://stream.test/1.ts")
        self.assertEqual(item["xmltv_id"], "one.test")

    def test_lf(self):
        self._assert_clean("\n".join(ENTRY) + "\n")

    def test_crlf(self):
        self._assert_clean("\r\n".join(ENTRY) + "\r\n")

    def test_classic_mac_cr(self):
        self._assert_clean("\r".join(ENTRY) + "\r")

    def test_utf8_bom(self):
        self._assert_clean("﻿" + "\r\n".join(ENTRY))


if __name__ == "__main__":
    unittest.main()
