"""A downloaded guide is streamed to disk as it comes; a gzip or zip guide, downloaded or a
local file, is unpacked as it is parsed (told by its bytes, not its name or headers)."""

import gzip
import io
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from services.epg_manager import EpgManager, _cache_key, _open_xmltv, write_xmltv_body
from services.multikeydict import MultiKeyDict

XML = b'<?xml version="1.0" encoding="UTF-8"?>\n<tv><channel id="news.test"/></tv>\n'
GUIDE = b"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="news.test"><display-name>News</display-name></channel>
  <programme channel="news.test" start="20261019200000 +0000" stop="20261019210000 +0000">
    <title>Packed</title>
  </programme>
</tv>
"""


def fake_response(data: bytes, headers=None):
    return SimpleNamespace(
        headers=headers or {},
        iter_content=lambda n: iter([data[:10], data[10:]]),
    )


def zip_bytes(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buffer.getvalue()


class _Directory(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(self.directory, "epg.xml")

    def write(self, data: bytes, name="epg.xml") -> str:
        path = os.path.join(self.directory, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def unpacked(self, path) -> bytes:
        with _open_xmltv(path) as f:
            return f.read()


class WriteXmltvBodyTests(_Directory):
    def test_the_body_is_written_as_it_came(self):
        for name, body, headers in (
            ("plain", XML, {"Content-Type": "text/xml"}),
            ("gzip", gzip.compress(XML), {"Content-Type": "application/octet-stream"}),
            # requests has already decoded Content-Encoding: gzip, so the body is plain XML.
            ("decoded", XML, {"Content-Encoding": "gzip"}),
            ("zip", zip_bytes({"readme.txt": b"hello", "channel.xml": XML}), {}),
        ):
            with self.subTest(name):
                write_xmltv_body(fake_response(body, headers), self.path)
                with open(self.path, "rb") as f:
                    self.assertEqual(f.read(), body)
                self.assertEqual(self.unpacked(self.path), XML)
                self.assertEqual(os.listdir(self.directory), ["epg.xml"])

    def test_a_failed_write_leaves_no_earlier_file_to_be_read(self):
        self.write(b"<tv><programme")  # an earlier download, cut short

        def broken(n):
            yield XML[:10]
            raise OSError("connection reset")

        with self.assertRaises(OSError):
            write_xmltv_body(SimpleNamespace(iter_content=broken), self.path)
        self.assertEqual(os.listdir(self.directory), [])


class OpenXmltvTests(_Directory):
    def test_plain_gzip_and_zip_files_read_as_xml(self):
        for name, data in (
            ("guide.xml", XML),
            ("guide.xml.gz", gzip.compress(XML)),
            ("guide.zip", zip_bytes({"readme.txt": b"hello", "channel.xml": XML})),
            ("named.xml", gzip.compress(XML)),  # the bytes tell, not the name
        ):
            with self.subTest(name):
                self.assertEqual(self.unpacked(self.write(data, name)), XML)

    def test_a_zip_reads_its_largest_xml_and_skips_mac_resource_forks(self):
        archive = zip_bytes(
            {"__MACOSX/._GUIDE.XML": b"\x00\x05" * 4000, "small.xml": b"<tv/>", "GUIDE.XML": XML}
        )
        self.assertEqual(self.unpacked(self.write(archive)), XML)

    def test_a_zip_without_xml_raises(self):
        with self.assertRaisesRegex(ValueError, "no XML in archive"):
            self.unpacked(self.write(zip_bytes({"readme.txt": b"hello"})))

    def test_a_cut_gzip_raises(self):
        body = gzip.compress(XML * 2000)
        with self.assertRaises(EOFError):
            self.unpacked(self.write(body[: len(body) // 2]))

    def test_a_damaged_zip_raises(self):
        with self.assertRaises(zipfile.BadZipFile):
            self.unpacked(self.write(b"PK\x03\x04" + b"\x00" * 64))


class PackedGuideTests(_Directory):
    """A Local File guide may be gzipped or zipped; a URL's archive without XML changes
    nothing, and leaves no file."""

    def manager(self, **config):
        values = dict(
            channel_epg=True,
            epg_source="Local File",
            epg_file="",
            epg_url="",
            epg_expiration=3600,
            epg_list_window_hours=0,
            ssl_verify=True,
            xmltv_channel_map=MultiKeyDict(),
            get_config_dir=lambda: self.directory,
        )
        values.update(config)
        return EpgManager(SimpleNamespace(**values), SimpleNamespace(current_provider={}))

    def titles(self, manager):
        return [p["title"] for p in manager.get_programs_for_channel({"name": "News"})]

    def test_a_local_gzipped_or_zipped_guide_is_read(self):
        for name, data in (
            ("guide.xml.gz", gzip.compress(GUIDE)),
            ("guide.zip", zip_bytes({"guide.xml": GUIDE})),
        ):
            with self.subTest(name):
                manager = self.manager(epg_file=self.write(data, name))
                manager.set_current_epg()
                self.assertEqual(self.titles(manager), ["Packed"])

    def test_a_downloaded_archive_without_xml_changes_nothing(self):
        url = "http://127.0.0.1:9/guide.zip"
        manager = self.manager(epg_source="URL", epg_url=url)
        answer = fake_response(zip_bytes({"readme.txt": b"hello"}))
        answer.status_code, answer.close = 200, lambda: None
        with (
            patch("services.epg_manager.requests.get", return_value=answer),
            self.assertLogs("services.epg_manager", "WARNING"),
        ):
            manager.set_current_epg()
        self.assertNotIn(_cache_key(url), manager.index)
        self.assertEqual(
            [name for name in os.listdir(manager._cache_dir()) if name.endswith(".xml")], []
        )


if __name__ == "__main__":
    unittest.main()
