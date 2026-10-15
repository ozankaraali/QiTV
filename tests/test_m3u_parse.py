import unittest

from services.m3u import decode_playlist, parse_m3u

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
        self._assert_clean("\ufeff" + "\r\n".join(ENTRY))


class ParseM3uUnicodeTests(unittest.TestCase):
    def test_c1_and_unicode_separators_do_not_split_lines(self):
        for ch in ("\x85", "\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"):
            data = f'#EXTM3U\n#EXTINF:-1 group-title="G{ch}x",Name{ch}y\nhttp://s.test/1.ts\n'
            [item] = parse_m3u(data)
            self.assertEqual(item["name"], f"Name{ch}y")
            self.assertEqual(item["group"], f"G{ch}x")


class _Resp:
    def __init__(self, content, content_type=None):
        self.content = content
        self.headers = {} if content_type is None else {"Content-Type": content_type}
        # requests decodes text/* without charset as ISO-8859-1
        self.text = content.decode("iso-8859-1" if content_type is None else "utf-8", "replace")


class DecodePlaylistTests(unittest.TestCase):
    PLAYLIST = '#EXTM3U\n#EXTINF:-1 group-title="أفلام",Мохнатый канал\nhttp://s.test/1.ts\n'

    def test_no_charset_decodes_utf8_and_parses(self):
        resp = _Resp(("\ufeff" + self.PLAYLIST).encode("utf-8"), "text/plain")
        resp.text = resp.content.decode("iso-8859-1")
        [item] = parse_m3u(decode_playlist(resp))
        self.assertEqual(item["group"], "أفلام")
        self.assertEqual(item["name"], "Мохнатый канал")

    def test_missing_content_type_header(self):
        resp = _Resp(self.PLAYLIST.encode("utf-8"))
        self.assertIn("أفلام", decode_playlist(resp))

    def test_declared_charset_uses_response_text(self):
        resp = _Resp(b"x", "text/plain; charset=windows-1251")
        resp.text = "from-text"
        self.assertEqual(decode_playlist(resp), "from-text")

    def test_invalid_bytes_are_replaced(self):
        resp = _Resp(b"#EXTM3U\n\xff\xfe", "application/octet-stream")
        self.assertIn("\ufffd", decode_playlist(resp))


if __name__ == "__main__":
    unittest.main()
