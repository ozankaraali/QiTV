import gzip
import io
import os
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

from services.epg_manager import write_xmltv_body

XML = b'<?xml version="1.0" encoding="UTF-8"?>\n<tv><channel id="news.test"/></tv>\n'


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


class WriteXmltvBodyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "epg.xml")

    def tearDown(self):
        self.directory.cleanup()

    def assert_no_temp_file(self):
        self.assertFalse(os.path.exists(self.path + ".download"))

    def test_plain_xml_is_written_as_is(self):
        write_xmltv_body(fake_response(XML, {"Content-Type": "text/xml"}), self.path)

        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), XML)
        self.assert_no_temp_file()

    def test_gzip_body_served_as_octet_stream_is_unpacked(self):
        body = gzip.compress(XML)
        response = fake_response(body, {"Content-Type": "application/octet-stream"})

        write_xmltv_body(response, self.path)

        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), XML)
        self.assert_no_temp_file()

    def test_already_decoded_body_with_gzip_header_is_written_as_is(self):
        # requests has already decoded Content-Encoding: gzip, so the body is plain XML.
        response = fake_response(XML, {"Content-Encoding": "gzip"})

        write_xmltv_body(response, self.path)

        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), XML)
        self.assert_no_temp_file()

    def test_zip_archive_extracts_xml_member(self):
        body = zip_bytes({"readme.txt": b"hello", "channel.xml": XML})

        write_xmltv_body(fake_response(body), self.path)

        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), XML)
        self.assert_no_temp_file()

    def test_zip_without_xml_raises_and_leaves_no_temp_file(self):
        body = zip_bytes({"readme.txt": b"hello"})

        with self.assertRaisesRegex(ValueError, "no XML in archive"):
            write_xmltv_body(fake_response(body), self.path)

        self.assert_no_temp_file()
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
