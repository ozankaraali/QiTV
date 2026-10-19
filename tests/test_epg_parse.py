"""The XMLTV guide is read as a stream and gives what the whole-tree parse gave.

`_old_index_programs` below is the parse as it was (ET.parse, then findall): the streaming
parse must give an equal guide, with its channels, keys and programme dicts in the same order.
"""

from datetime import timedelta
import gc
import itertools
import os
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
from typing import Any, Dict, List, Set
import unittest
from unittest.mock import patch
import weakref
import xml.etree.ElementTree as ET

import requests

from scripts.bench_epg import build_xmltv
from services import epg_manager as epg_manager_module
from services.epg_manager import EpgManager, _cache_key, normalize_programme_stop
from services.epg_time import parse_xmltv_as
from services.jobs import Cancelled
from services.multikeydict import MultiKeyDict

# ---- the parse as it was --------------------------------------------------------------------

_XMLTV_FORMATS = ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S")


def _old_normalize_programme_stop(start: str, stop: str) -> str:
    for fmt in _XMLTV_FORMATS:
        try:
            start_dt = parse_xmltv_as(start, fmt)
            stop_dt = parse_xmltv_as(stop, fmt)
        except ValueError:
            continue
        if stop_dt < start_dt:
            return (stop_dt + timedelta(days=1)).strftime(fmt)
        return stop
    return stop


def _old_xml_to_dict(element):
    def parse_element(element):
        parsed_data: Dict[str, Any] = {}
        if element.attrib:
            parsed_data.update(("@" + k, v) for k, v in element.attrib.items())
        for child in element:
            if len(child):
                child_data = parse_element(child)
            else:
                child_data = {"__text": child.text}
                if child.attrib:
                    child_data.update(("@" + k, v) for k, v in child.attrib.items())
            if child.tag in parsed_data:
                if isinstance(parsed_data[child.tag], list):
                    parsed_data[child.tag].append(child_data)
                else:
                    parsed_data[child.tag] = [parsed_data[child.tag], child_data]
            else:
                parsed_data[child.tag] = child_data
        return parsed_data

    return {element.tag: parse_element(element)}


def _old_index_programs(xmltv_file, channel_map):
    programs = MultiKeyDict()
    tree = ET.parse(xmltv_file).getroot()
    alias_map: Dict[str, Set[str]] = {}
    for ch in tree.findall("channel"):
        ch_id = ch.get("id")
        if not ch_id:
            continue
        names: Set[str] = alias_map.setdefault(ch_id, set())
        for dn in ch.findall("display-name"):
            txt = (dn.text or "").strip()
            if not txt:
                continue
            names.add(txt)
            names.add(txt.lower())
    tmp_lists: Dict[str, List[Dict[str, Any]]] = {}
    for programme in tree.findall("programme"):
        channel_id = programme.get("channel")
        start_time = programme.get("start")
        stop_time = programme.get("stop")
        if not channel_id or not start_time or not stop_time:
            continue
        program_data = _old_xml_to_dict(programme)["programme"]
        program_data["@stop"] = _old_normalize_programme_stop(start_time, stop_time)
        tmp_lists.setdefault(channel_id, []).append(program_data)
    for ch_id, plist in tmp_lists.items():
        user_keys = channel_map.get_keys(ch_id)
        if user_keys:
            key_set: Set[str] = set(user_keys)
        else:
            key_set = {ch_id}
        for alias in alias_map.get(ch_id, set()):
            key_set.add(alias)
        other_keys = sorted(k for k in key_set if k != ch_id)
        multikeys = (ch_id, *other_keys)
        programs.setdefault(multikeys, []).extend(plist)
    return programs


# ---- a guide with every case the parse handles ----------------------------------------------

LONG = "A long description, " * 8  # longer than the strings made one object

TRICKY = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE tv [
  <!ENTITY series "Harbour Tales">
]>
<!-- before the root -->
<tv source-info-name="fixture" generator-info-name="tests" xmlns:x="urn:example:extra">
  <?qitv an instruction?>
  <programme channel="late.test" start="20261019060000 +0200" stop="20261019070000 +0200">
    <title lang="en">Before its channel</title>
  </programme>
  <channel id="one.test">
    <display-name>  One  </display-name>
    <display-name lang="tr">Bir Ünlü Kanal</display-name>
    <display-name></display-name>
    <display-name>   </display-name>
    <display-name>ONE</display-name>
    <display-name>One<b>bold tail</b></display-name>
    <icon src="http://img.example.test/one.png"/>
  </channel>
  <channel id="two.test">
    <display-name>Two</display-name>
    <display-name>shared</display-name>
  </channel>
  <channel id="three.test">
    <display-name>Shared</display-name>
    <display-name>one.test</display-name>
  </channel>
  <channel id="one.test"><display-name>Uno</display-name></channel>
  <channel><display-name>No id</display-name></channel>
  <channel id=""><display-name>Empty id</display-name></channel>
  <channel id="mapped.test"><display-name>Mapped</display-name></channel>
  <channel id="quiet.test"><display-name>No programmes</display-name></channel>
  <unknown kind="top-level"><display-name>Not a channel</display-name></unknown>
  <programme channel="one.test" start="20261019233000 +0200" stop="20261019003000 +0200"
             clumpidx="0/1" pdc-start="20261019233000 +0200">
    <title lang="en">News &amp; Weather &#233;&#x263A;</title>
    <title lang="de">Nachrichten &amp; Wetter</title>
    <sub-title lang="ja">夜のニュース 🌙</sub-title>
    <desc lang="en"><![CDATA[A <b>bold</b> & "quoted" night.]]></desc>
    <desc lang="tr">{LONG}</desc>
    <credits>
      <director>Ada Example</director>
      <actor role="Host" guest="yes">Ben Example</actor>
      <actor role="Guest">Cem Örnek</actor>
      <actor><image type="person">http://img.example.test/a.png</image></actor>
    </credits>
    <date>2026</date>
    <category lang="en">News</category>
    <category lang="en">Weather</category>
    <category lang="en">Talk</category>
    <episode-num system="xmltv_ns">1.4.0/1</episode-num>
    <episode-num system="onscreen">S02E05</episode-num>
    <video><present>yes</present><aspect>16:9</aspect><quality>HDTV</quality></video>
    <previously-shown start="20261012233000 +0200"/>
    <new/>
    <subtitles type="teletext"><language>English</language></subtitles>
    <rating system="age"><value>12</value><icon src="http://img.example.test/12.png"/></rating>
    <star-rating><value>3/5</value></star-rating>
    <x:extra x:kind="namespaced">yes</x:extra>
    <mixed>text before <b>bold</b> and after</mixed>
    <channel id="nested.test"><display-name>A channel inside a programme</display-name></channel>
    <!-- inside a programme -->
  </programme>
  <programme channel="two.test" start="20261019060000" stop="20261019050000">
    <title>No offset, overnight</title>
  </programme>
  <programme channel="one.test" start="20261020003000 +0200" stop="20261020013000 +0000">
    <title lang="en">News &amp; Weather &#233;&#x263A;</title>
    <category lang="en">News</category>
  </programme>
  <programme channel="two.test" start="garbage" stop="20261019050000"><title>Bad</title></programme>
  <programme channel="two.test" start="20261019050000 +0000" stop="20261019040000 +0000"/>
  <programme channel="two.test" start="20261332250000 +0000" stop="20261019040000 +0000"/>
  <programme channel="undeclared.test" start="20261019080000 +0000" stop="20261019090000 +0000">
    <title>&series;</title>
  </programme>
  <programme channel="three.test" start="20261019080000 +0000" stop="20261019090000 +0000">
  </programme>
  <programme channel="mapped.test" start="20261019080000 +0000" stop="20261019090000 +0000">
    <title>Mapped</title>
  </programme>
  <programme start="20261019080000 +0000" stop="20261019090000 +0000"><title>No channel</title>
  </programme>
  <programme channel="one.test" stop="20261019090000 +0000"><title>No start</title></programme>
  <programme channel="one.test" start="20261019080000 +0000"><title>No stop</title></programme>
  <programme channel="one.test" start="20261019080000 +0000" stop=""><title>Empty</title>
  </programme>
  <programme channel="" start="20261019080000 +0000" stop="20261019090000 +0000"/>
  <group>
    <programme channel="one.test" start="20261019080000 +0000" stop="20261019090000 +0000">
      <title>A programme inside another element</title>
    </programme>
    <channel id="grouped.test"><display-name>A channel inside another element</display-name>
    </channel>
  </group>
  <channel id="late.test"><display-name>Late</display-name></channel>
  <programme channel="late.test" start="20261019070000 +0200" stop="20261019080000 +0200">
    <title lang="en">After its channel</title>
  </programme>
  <programme channel="nested.test" start="20261019080000 +0000" stop="20261019090000 +0000">
    <title>For the channel declared inside a programme</title>
  </programme>
  <x:programme channel="one.test" start="20261019080000 +0000" stop="20261019090000 +0000">
    <title>Namespaced</title>
  </x:programme>
  <channel id="grouped.test"><display-name>Grouped</display-name></channel>
  <programme channel="grouped.test" start="20261019080000 +0000" stop="20261019090000 +0000">
    <title lang="en">Before its channel</title>
  </programme>
</tv>
<!-- after the root -->
"""


def _channel_map() -> MultiKeyDict:
    channel_map = MultiKeyDict()
    channel_map[("mapped.test", "My Mapped", "mapped alias")] = None
    channel_map[("Uno", "My Uno")] = None  # keyed by an alias: the parse looks ids up only
    return channel_map


def _manager(channel_map=None) -> EpgManager:
    manager = EpgManager.__new__(EpgManager)
    manager.config_manager = SimpleNamespace(xmltv_channel_map=channel_map or MultiKeyDict())
    return manager


class _FileCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-epg-parse-")
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def write(self, text, name="guide.xml"):
        path = os.path.join(self.directory, name)
        with open(path, "wb") as f:
            f.write(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def assertSameGuide(self, new, old):
        # repr shows each programme dict's key order too.
        self.assertEqual(repr(list(new.items())), repr(list(old.items())))
        self.assertEqual(list(new._keys_map.items()), list(old._keys_map.items()))


class StreamingParseEqualsTheTreeParseTests(_FileCase):
    def test_the_tricky_guide_parses_as_before_whatever_the_read_size(self):
        path = self.write(TRICKY)
        old = _old_index_programs(path, _channel_map())
        for size in (1, 7, 64, 4096, epg_manager_module._READ_BYTES):
            with self.subTest(read_bytes=size):
                with patch.object(epg_manager_module, "_READ_BYTES", size):
                    new = _manager(_channel_map())._index_programs(path)
                self.assertSameGuide(new, old)

    def test_the_tricky_guide_has_what_the_comparison_relies_on(self):
        guide = _manager(_channel_map())._index_programs(self.write(TRICKY))
        channels = {keys[0]: (keys, programmes) for keys, programmes in guide.items()}
        ids = list(channels)
        # In the order of each channel's first programme; ignored entries are not there.
        self.assertEqual(
            ids,
            [
                "late.test",
                "one.test",
                "two.test",
                "undeclared.test",
                "three.test",
                "mapped.test",
                "nested.test",
                "grouped.test",
            ],
        )
        self.assertEqual(
            channels["one.test"][0],
            ("one.test", "Bir Ünlü Kanal", "ONE", "One", "Uno", "bir ünlü kanal", "one", "uno"),
        )
        # An alias equal to another channel's id takes that key over, as it did.
        self.assertEqual(guide.get_keys("one.test"), ("three.test", "Shared", "one.test", "shared"))
        self.assertEqual(guide.get_keys("late"), ("late.test", "Late", "late"))
        self.assertEqual(guide.get_keys("Grouped")[0], "grouped.test")
        self.assertEqual(guide.get_keys("shared")[0], "three.test")  # the later channel's
        self.assertEqual(guide.get_keys("mapped alias")[0], "mapped.test")
        self.assertNotIn("My Uno", guide)
        self.assertEqual(
            [p["title"]["__text"] for p in guide["late.test"]],
            ["Before its channel", "After its channel"],
        )
        night = channels["one.test"][1][0]
        self.assertEqual(night["@stop"], "20261020003000 +0200")
        self.assertEqual(night["title"][0]["__text"], "News & Weather é☺")
        self.assertEqual(night["desc"][0]["__text"], 'A <b>bold</b> & "quoted" night.')
        self.assertEqual(night["new"], {"__text": None})
        self.assertEqual(
            night["{urn:example:extra}extra"]["@{urn:example:extra}kind"], "namespaced"
        )
        self.assertEqual(len(guide["two.test"]), 4)
        self.assertEqual(guide["undeclared.test"][0]["title"]["__text"], "Harbour Tales")
        self.assertEqual(len(channels["one.test"][1]), 2)  # not the malformed or nested ones

    def test_a_guide_in_another_encoding_parses_as_before(self):
        text = (
            '<?xml version="1.0" encoding="ISO-8859-1"?>\n<tv>'
            '<channel id="a.test"><display-name>Ünlü Kanal</display-name></channel>'
            '<programme channel="a.test" start="20261019080000 +0000" '
            'stop="20261019090000 +0000"><title>Çay saati &#x263A;</title></programme></tv>'
        )
        path = self.write(text.encode("latin-1"))
        old = _old_index_programs(path, MultiKeyDict())
        for size in (1, epg_manager_module._READ_BYTES):
            with self.subTest(read_bytes=size):
                with patch.object(epg_manager_module, "_READ_BYTES", size):
                    new = _manager()._index_programs(path)
                self.assertSameGuide(new, old)
        self.assertEqual(new["ünlü kanal"][0]["title"]["__text"], "Çay saati ☺")

    def test_a_generated_guide_parses_as_before(self):
        # Larger than one read: elements are cut between reads.
        path = os.path.join(self.directory, "generated.xml")
        build_xmltv(Path(path), channels=40, days=2, per_day=25)
        self.assertGreater(os.path.getsize(path), epg_manager_module._READ_BYTES)
        channel_map = MultiKeyDict()
        channel_map[("c00003.bench.test", "Mine")] = None
        old = _old_index_programs(path, channel_map)
        for size in (997, epg_manager_module._READ_BYTES):
            with self.subTest(read_bytes=size):
                with patch.object(epg_manager_module, "_READ_BYTES", size):
                    new = _manager(channel_map)._index_programs(path)
                self.assertSameGuide(new, old)

    def test_a_damaged_file_raises_as_before(self):
        cut = TRICKY.index("<programme channel=\"mapped.test\"")
        for name, text in (
            ("empty", ""),
            ("cut short", TRICKY[:cut]),
            ("broken", TRICKY[:cut] + "<programme <title/>" + TRICKY[cut:]),
            ("undefined entity", TRICKY.replace("&series;", "&nothing;")),
            ("two roots", TRICKY + "<tv/>"),
        ):
            with self.subTest(name):
                path = self.write(text)
                with self.assertRaises(ET.ParseError) as old:
                    _old_index_programs(path, MultiKeyDict())
                with self.assertRaises(ET.ParseError) as new:
                    _manager()._index_programs(path)
                self.assertEqual(new.exception.position, old.exception.position)


class StreamingTests(_FileCase):
    def test_each_element_is_let_go_once_the_next_is_read(self):
        path = os.path.join(self.directory, "generated.xml")
        build_xmltv(Path(path), channels=10, days=1, per_day=25)
        refs: list = []
        gc.disable()  # what is let go must be freed by its count, not by the collector
        self.addCleanup(gc.enable)
        for element in epg_manager_module._root_children(path):
            self.assertEqual([ref() for ref in refs if ref() is not None], [])
            refs.append(weakref.ref(element))
            del element
            refs = refs[-1:]  # the newest one may still be held while it is current
        self.assertGreater(len(refs), 0)

    def test_a_cancelled_job_stops_the_parse(self):
        path = self.write(TRICKY)
        with patch.object(epg_manager_module, "cancel_requested", return_value=True):
            with self.assertRaises(Cancelled):
                _manager()._index_programs(path)

    def test_equal_short_strings_are_one_object(self):
        guide = _manager()._index_programs(self.write(TRICKY))
        first, second = next(p for keys, p in guide.items() if keys[0] == "one.test")
        first_lang = next(key for key in first["category"][0] if key == "@lang")
        second_lang = next(key for key in second["category"] if key == "@lang")
        self.assertIs(first_lang, second_lang)
        self.assertIs(first["category"][0]["__text"], second["category"]["__text"])
        self.assertIs(first["@channel"], second["@channel"])


class _Answer:
    """A streamed answer that notes whether it was closed."""

    def __init__(self, body=b"", status=200, broken=False):
        self.status_code = status
        self.headers: Dict[str, str] = {}
        self.closed = False
        self._body = body
        self._broken = broken

    def iter_content(self, size):
        if self._broken:
            raise requests.ConnectionError("reset")
        if self._body:
            yield self._body

    def close(self):
        self.closed = True


class DownloadedFileTests(_FileCase):
    """A downloaded guide's file is removed also when its parse fails or is cancelled."""

    def manager(self):
        config = SimpleNamespace(
            channel_epg=True,
            epg_source="URL",
            epg_url="http://127.0.0.1:9/guide.xml",
            epg_expiration=3600,
            ssl_verify=True,
            xmltv_channel_map=MultiKeyDict(),
            get_config_dir=lambda: self.directory,
        )
        return EpgManager(config, SimpleNamespace(current_provider={}))

    def download(self, manager, body, status=200):
        answer = _Answer(body, status)
        with patch("services.epg_manager.requests.get", return_value=answer):
            try:
                manager._download_epg_from_url(manager.config_manager.epg_url)
            finally:
                self.assertTrue(answer.closed)

    def leftovers(self, manager):
        return [name for name in os.listdir(manager._cache_dir()) if name.endswith(".xml")]

    def test_a_damaged_download_leaves_no_file(self):
        manager = self.manager()
        with self.assertRaises(ET.ParseError):
            self.download(manager, TRICKY[:500].encode())
        self.assertEqual(self.leftovers(manager), [])

    def test_a_refused_download_is_closed(self):
        manager = self.manager()
        self.download(manager, b"busy", status=503)
        self.assertEqual(self.leftovers(manager), [])

    def test_a_parse_error_is_not_hidden_by_the_file_being_gone(self):
        path = self.write(TRICKY, "download.xml")

        def gone_then_broken(xmltv_file):
            os.remove(xmltv_file)
            raise ET.ParseError("broken")

        manager = self.manager()
        with patch.object(manager, "_index_programs", side_effect=gone_then_broken):
            with self.assertRaises(ET.ParseError):
                manager._index_download(path)

    def test_a_cancelled_parse_leaves_no_file(self):
        path = self.write(TRICKY, "download.xml")
        with patch.object(epg_manager_module, "cancel_requested", return_value=True):
            with self.assertRaises(Cancelled):
                self.manager()._index_download(path)
        self.assertFalse(os.path.exists(path))

    def test_a_good_download_is_stored_and_its_file_removed(self):
        manager = self.manager()
        self.download(manager, TRICKY.encode())
        self.assertEqual(self.leftovers(manager), [])
        key = _cache_key(manager.config_manager.epg_url)
        self.assertEqual(manager._epg_source, key)
        self.assertEqual(len(manager.epg["two.test"]), 4)


class XtreamAnswersTests(_FileCase):
    """Every answer of the Xtream guide's candidates (HTTPS, then HTTP) is closed."""

    def fetch(self, *answers):
        config = SimpleNamespace(
            prefer_https=True,
            ssl_verify=True,
            epg_expiration=3600,
            xmltv_channel_map=MultiKeyDict(),
            get_config_dir=lambda: self.directory,
        )
        provider = {"type": "XTREAM", "url": "http://h", "username": "u", "password": "p"}
        manager = EpgManager(config, SimpleNamespace(current_provider=provider))
        with patch("services.epg_manager.requests.get", side_effect=list(answers)) as get:
            manager._fetch_epg_from_xtream("http://h", "u", "p")
        self.assertEqual(get.call_count, len(answers))
        self.assertEqual([answer.closed for answer in answers], [True] * len(answers))
        return manager

    def test_a_refused_candidate_is_closed_and_the_next_one_read(self):
        manager = self.fetch(_Answer(b"busy", 503), _Answer(TRICKY.encode()))
        self.assertEqual(len(manager.epg["two.test"]), 4)

    def test_an_empty_or_broken_answer_is_closed(self):
        manager = self.fetch(_Answer(b""), _Answer(TRICKY.encode(), broken=True))
        self.assertEqual(len(manager.epg), 0)

    def test_a_last_refused_answer_is_closed(self):
        manager = self.fetch(_Answer(b"", 503), _Answer(b"busy", 503))
        self.assertEqual(len(manager.epg), 0)


class NormalizeStopTests(unittest.TestCase):
    """The shortcut for times already in order gives what parsing both gave."""

    def test_it_agrees_with_parsing_both(self):
        rng = random.Random(3)
        stamps = [
            "20261019233000",
            "20261020003000",
            "20261019233000",
            "20261332250000",  # out of range
            "20261019240000",
            "20261019235960",
            "２０２６1019233000",  # not ASCII digits
            "2026101923300",
            "garbage-garbage",
        ]
        zones = ["", " +0200", " +0000", " -0130", " +0260", " +2400", " Z", " +02:00", "+0200"]
        values = [s + z for s, z in itertools.product(stamps, zones)]
        values += [rng.choice(values)[:-1] for _ in range(20)]
        for start, stop in itertools.product(values, repeat=2):
            self.assertEqual(
                normalize_programme_stop(start, stop),
                _old_normalize_programme_stop(start, stop),
                (start, stop),
            )


if __name__ == "__main__":
    unittest.main()
