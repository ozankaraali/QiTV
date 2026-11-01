"""Provider text cut mid-emoji (a lone surrogate): mended to U+FFFD as it is read, so that the
settings, the recording schedule and the caches can still be written."""

import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import orjson

from services import recording_schedule as rs, surrogates
from services.config_manager import ConfigManager
from services.surrogates import loads, mended, mended_text, response_json

HIGH, LOW = chr(0xD83D), chr(0xDE00)  # the halves of an emoji's UTF-16 pair
WHOLE = chr(0x1F600)  # the emoji they make together
FFFD = chr(0xFFFD)
# What a panel that cut a name sends: the standard json (requests' .json()) keeps the half.
CUT = json.dumps({"name": "News " + HIGH, "category_name": LOW + " Sport"})


class _Response:
    def __init__(self, text):
        self.text = text

    def json(self):
        return json.loads(self.text)


class MendedTests(unittest.TestCase):
    def test_a_lone_half_becomes_the_replacement_character(self):
        self.assertEqual(mended_text("News " + HIGH), "News " + FFFD)
        self.assertEqual(mended_text(LOW + "x" + HIGH), FFFD + "x" + FFFD)
        self.assertEqual(mended_text("café " + WHOLE), "café " + WHOLE)  # untouched
        self.assertEqual(mended_text(HIGH + LOW), WHOLE)  # two halves joined: the emoji again
        plain = "Plain"
        self.assertIs(mended_text(plain), plain)

    def test_parsed_json_is_mended_in_place_keys_too(self):
        data = {"items": [{"name": "A " + HIGH, HIGH + "key": [LOW, 1, None, 2.5]}], "n": 3}
        nested = data["items"][0]
        self.assertIs(mended(data), data)
        self.assertIs(data["items"][0], nested)
        self.assertEqual(nested, {"name": "A " + FFFD, FFFD + "key": [FFFD, 1, None, 2.5]})
        orjson.dumps(data)  # writable now
        self.assertEqual(mended("x" + HIGH), "x" + FFFD)
        self.assertEqual(mended(7), 7)

    def test_data_without_one_is_left_as_it_is(self):
        data = [{"name": "café " + WHOLE, "n": 1}]
        with patch.object(surrogates, "_mend_in_place") as walk:
            self.assertIs(mended(data), data)
        walk.assert_not_called()  # orjson found nothing to mend

    def test_deep_json_does_not_recurse(self):
        data = deep = []
        for _ in range(5000):
            deep.append([])
            deep = deep[0]
        deep.append(HIGH)
        mended(data)
        self.assertEqual(deep, [FFFD])

    def test_response_json_is_mended(self):
        self.assertEqual(
            response_json(_Response(CUT)),
            {"name": "News " + FFFD, "category_name": FFFD + " Sport"},
        )

    def test_json_orjson_refuses_for_a_half_is_read_and_mended(self):
        with self.assertRaises(orjson.JSONDecodeError):
            orjson.loads(CUT)  # an STB portal's page was lost whole
        self.assertEqual(loads(CUT.encode())["name"], "News " + FFFD)
        self.assertEqual(loads(b'{"js": [1]}'), {"js": [1]})
        with self.assertRaises(orjson.JSONDecodeError):
            loads(b"<html>busy</html>")  # not JSON: orjson's own error, as before
        with self.assertRaises(orjson.JSONDecodeError):
            loads(b'{"name": "\xff"}')


class _Page:
    """An aiohttp answer to a portal's page request (only what ContentLoader reads)."""

    status = 200

    def __init__(self, body):
        self.body = body

    async def read(self):
        return self.body

    def raise_for_status(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class StbPageTests(unittest.TestCase):
    def test_a_portals_page_with_a_cut_name_loads_mended(self):
        from services.content_loader import ContentLoader

        body = json.dumps({"js": {"data": [{"name": "Film " + HIGH}], "total_items": 1}})
        session = type("Session", (), {"get": lambda self, url, **kw: _Page(body.encode())})()
        loader = ContentLoader("http://portal.test/c/", {}, "vod", max_retries=1)
        with self.assertNoLogs("services.content_loader", "WARNING"):  # retried, then lost
            data, total, _ = asyncio.run(loader.fetch_page(session, 1))
        self.assertEqual((data, total), ([{"name": "Film " + FFFD}], 1))


class SavesTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-surrogates-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_a_cut_name_in_the_watch_history_no_longer_stops_every_save(self):
        path = self.folder / "config.json"
        with patch.object(ConfigManager, "_get_config_path", return_value=str(path)):
            config = ConfigManager()
            channel = json.loads(CUT)  # one that came in unmended
            config.add_to_watch_history(channel, "channel", "http://panel.test/1", "P")
            config.config["selected_provider_name"] = "P " + HIGH
            config.xmltv_channel_map["News " + HIGH] = {"name": LOW}
            with self.assertLogs("services.config_manager", "WARNING"):
                config.save_config()  # TypeError before: no save from then on
            config.favorites = ["Later"]
            config.save_config()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["watch_history"][0]["name"], "News " + FFFD)
            self.assertEqual(saved["selected_provider_name"], "P " + FFFD)
            self.assertEqual(saved["xmltv_channel_map"], [["News " + FFFD, {"name": FFFD}]])
            self.assertEqual(saved["favorites"], ["Later"])
            self.assertEqual(ConfigManager().watch_history[0]["name"], "News " + FFFD)

    def test_a_schedule_write_that_fails_does_not_stop_the_writer(self):
        # The "schedule-save" thread died on the cut name's UnicodeEncodeError and stayed
        # counted: later changes queued for ever (timers lost at restart), quit waited 2 s.
        first, second = self.folder / "first.json", self.folder / "second.json"
        with self.assertLogs("services.recording_schedule", "WARNING") as logs:
            rs.write_later(str(first), {"items": [{"title": "News " + HIGH}]})
            started = time.monotonic()
            rs.flush_writes(5)
            self.assertLess(time.monotonic() - started, 2)
        self.assertIn("UnicodeEncodeError", "\n".join(logs.output))
        self.assertIsNone(rs._writer)
        self.assertFalse(first.exists())
        rs.write_later(str(second), {"items": [{"title": "Plain"}]})
        rs.flush_writes(5)
        self.assertEqual(
            json.loads(second.read_text(encoding="utf-8"))["items"][0]["title"], "Plain"
        )

    def test_a_writer_that_dies_leaves_what_waits_to_the_next_one(self):
        waiting, later = self.folder / "waiting.json", self.folder / "later.json"
        write_now = rs.write_now

        def dies_once(path, data):
            if path.endswith("first.json"):
                rs.write_later(str(waiting), {"items": [1]})  # queued meanwhile
                raise SystemExit  # what write_now never lets out: the thread's end at worst
            write_now(path, data)

        with patch.object(rs, "write_now", dies_once):
            with self.assertLogs("services.recording_schedule", "WARNING"):
                rs.write_later(str(self.folder / "first.json"), {"items": []})
                rs.flush_writes(5)
            self.assertIsNone(rs._writer)  # not counted as running for ever
            self.assertFalse(waiting.exists())
            rs.write_later(str(later), {"items": [2]})
            rs.flush_writes(5)
        self.assertEqual(json.loads(waiting.read_text(encoding="utf-8")), {"items": [1]})
        self.assertEqual(json.loads(later.read_text(encoding="utf-8")), {"items": [2]})


if __name__ == "__main__":
    unittest.main()
