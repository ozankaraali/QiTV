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

    def assert_nothing_written(self):
        self.assert_no_temp_file()
        self.assertFalse(os.path.exists(self.path + ".part"))
        self.assertFalse(os.path.exists(self.path))

    def test_a_cut_gzip_body_leaves_no_file(self):
        body = gzip.compress(XML * 2000)
        with self.assertRaises(EOFError):
            write_xmltv_body(fake_response(body[: len(body) // 2]), self.path)
        self.assert_nothing_written()

    def test_a_damaged_zip_body_leaves_no_file(self):
        with self.assertRaises(zipfile.BadZipFile):
            write_xmltv_body(fake_response(b"PK\x03\x04" + b"\x00" * 64), self.path)
        self.assert_nothing_written()

    def test_a_failed_write_leaves_no_earlier_file_to_be_read(self):
        with open(self.path, "wb") as f:
            f.write(b"<tv><programme")  # an earlier download, cut short
        with self.assertRaisesRegex(ValueError, "no XML in archive"):
            write_xmltv_body(fake_response(zip_bytes({"readme.txt": b"hello"})), self.path)
        self.assert_nothing_written()


if __name__ == "__main__":
    unittest.main()
