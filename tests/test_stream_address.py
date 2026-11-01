import unittest

from services.stream_address import playable, shape


class PlayableTests(unittest.TestCase):
    """Only a stream's address reaches a player: never a share, a device or a bare path."""

    def test_streams_play(self):
        for url in (
            "http://h.example/live/1.ts",
            "HTTPS://h.example/a.m3u8",
            "rtmp://h.example/app/key",
            "rtmps://h.example/app/key",
            "rtsp://h.example:554/s",
            "rtp://@239.0.0.1:5004",
            "udp://@239.0.0.1:1234",
            "mms://h.example/s",
            "mmsh://h.example/s",
            "srt://h.example:9000",
        ):
            with self.subTest(url=url):
                self.assertTrue(playable(url))

    def test_a_file_on_this_computer_plays(self):
        for url in ("file:///Users/me/a.ts", "file:///C:/Videos/a%20b.mkv", "file://localhost/a"):
            with self.subTest(url=url):
                self.assertTrue(playable(url))

    def test_shares_devices_and_bare_paths_are_refused(self):
        for url in (
            "file://server/share/a.ts",  # Windows signs in to the server
            "file:////server/share/a.ts",
            "file:///%5C%5Cserver%5Cshare%5Ca.ts",
            "file:///dev/video0",
            "smb://server/share/a.ts",
            "ftp://h.example/a.ts",
            "dshow://",
            "screen://",
            "v4l2:///dev/video0",
            "qtcapture://",
            "/etc/hosts",
            "\\\\server\\share\\a.ts",
            "C:\\Videos\\a.ts",
            " http://h.example/a.ts",  # a scheme starts the address
            "http://h.example/a.ts\nsmb://server/x",
            "ffrt http://h.example/a.ts",
            "",
            None,
        ):
            with self.subTest(url=url):
                self.assertFalse(playable(url))

    def test_qitvs_own_file_plays_wherever_it_is_kept(self):
        self.assertTrue(playable("file://server/share/rec.ts", local=True))
        self.assertFalse(playable("smb://server/share/rec.ts", local=True))

    def test_a_log_names_the_scheme_alone(self):
        self.assertEqual(shape("smb://user:secret@server/x"), "smb")
        self.assertEqual(shape("/etc/hosts"), "no scheme")


if __name__ == "__main__":
    unittest.main()
