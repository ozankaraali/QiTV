from datetime import datetime, timedelta, timezone
import itertools
import unittest

from services.epg_time import (
    STB_FORMAT,
    XMLTV_FORMAT,
    XMLTV_NAIVE_FORMAT,
    parse_stb,
    parse_xmltv,
    parse_xmltv_naive,
)


def strptime_or_error(text, fmt):
    try:
        return datetime.strptime(text, fmt)
    except (ValueError, TypeError) as e:
        return type(e)


def parsed_or_error(parse, text):
    try:
        return parse(text)
    except (ValueError, TypeError) as e:
        return type(e)


def assert_same(case, parse, text, fmt):
    expected = strptime_or_error(text, fmt)
    actual = parsed_or_error(parse, text)
    case.assertEqual(actual, expected, text)
    if isinstance(expected, datetime):
        case.assertEqual(actual.tzinfo, expected.tzinfo, text)
        case.assertEqual(actual.utcoffset(), expected.utcoffset(), text)
        case.assertEqual(actual.tzname(), expected.tzname(), text)


class XmltvTests(unittest.TestCase):
    def test_parses_like_strptime(self):
        stamps = [
            "20261015203000",
            "20260229120000",  # not a leap year
            "20240229120000",
            "20261315120000",  # month 13: strptime may read other digits
            "20261000120000",
            "20261032120000",
            "20261015240000",
            "20261015236000",
            "20261015235960",
            "20261015235961",
            "00011015120000",
            "99991231235959",
        ]
        zones = ["+0000", "-0000", "+0200", "-0530", "+1400", "+2359", "+2400", "+0175", "+02:00"]
        for stamp, zone in itertools.product(stamps, zones):
            assert_same(self, parse_xmltv, f"{stamp} {zone}", XMLTV_FORMAT)
        for stamp in stamps:
            assert_same(self, parse_xmltv_naive, stamp, XMLTV_NAIVE_FORMAT)

    def test_odd_text_falls_back_to_strptime(self):
        for text in [
            "",
            "bad",
            "20261015203000",  # no zone
            "20261015203000  +0200",  # two spaces: strptime takes any whitespace
            "20261015203000\t+0200",
            " 20261015203000 +0200",
            "20261015203000 +0200 ",
            "2026101520300 +0200",
            "２０２６1015203000 +0200",  # full-width digits
            "20261015203000 +02",
            "20261015203000 Z",
            "20261015203000 +0200x",
        ]:
            assert_same(self, parse_xmltv, text, XMLTV_FORMAT)
            assert_same(self, parse_xmltv_naive, text, XMLTV_NAIVE_FORMAT)

    def test_zone_objects(self):
        self.assertIs(parse_xmltv("20261015203000 +0000").tzinfo, timezone.utc)
        self.assertEqual(
            parse_xmltv("20261015203000 -0530").utcoffset(), -timedelta(hours=5, minutes=30)
        )


class StbTests(unittest.TestCase):
    def test_parses_like_strptime(self):
        for text in [
            "2026-10-15 20:30:00",
            "2026-02-29 12:00:00",
            "2024-02-29 12:00:00",
            "2026-13-15 12:00:00",
            "2026-10-15 24:00:00",
            "2026-10-15 23:59:60",
            "2026-1-15 12:00:00",
            "2026-10-15  12:00:00",
            "2026-10-15T12:00:00",
            "2026/10/15 12:00:00",
            "2026-10-15 12:00",
            "",
            "garbage",
        ]:
            assert_same(self, parse_stb, text, STB_FORMAT)


if __name__ == "__main__":
    unittest.main()
