from contextlib import closing, contextmanager
import copy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
import gzip
import hashlib
import itertools
import logging
import os
import pickle
import tempfile
import threading
from typing import Any, Dict, Generator, Iterator, List, Optional, Set, Tuple, cast
import xml.etree.ElementTree as ET
import zipfile

import orjson as json
import tzlocal  # the GUI thread's guide lookups use it: loaded with the window

from services.content_loader import ContentLoader
from services.epg_store import PackedGuide, read_guide, write_guide
from services.epg_time import parse_stb, parse_xmltv, parse_xmltv_as
from services.epg_view import guide_offset
from services.jobs import Cancelled, cancel_requested
from services.lazy_import import lazy_module
from services.multikeydict import MultiKeyDict
from services.provider_api import (
    CLIENT_HEADERS,
    base_from_url,
    https_fallback_candidates,
    provider_prefers_https,
    stb_endpoint,
    xtream_xmltv_url,
)
from services.redact import redact_url, safe_exc

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

# Timestamp format of the "date"/"last_access" fields in the EPG cache index.
_INDEX_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _http_date(moment: datetime) -> str:
    """An index time (GMT, naive) as an HTTP date: "Sun, 06 Nov 1994 08:49:37 GMT", in English
    whatever the locale (strftime follows it, and servers ignore a date in another language)."""
    return format_datetime(moment.replace(tzinfo=timezone.utc), usegmt=True)


def _cache_key(source: str) -> str:
    """Name an EPG source's cache entry (index key and guide file stem)."""
    return hashlib.md5(source.encode()).hexdigest()


def _xtream_cache_key(provider_url: str, username: str) -> str:
    return _cache_key(f"{provider_url}:{username}")


def _source_date(last_modified: Optional[str]) -> datetime:
    """A downloaded guide's date as the index keeps it (UTC, naive: the next refresh asks
    If-Modified-Since with it): the server's Last-Modified, or the download time when the
    server sends none or one that doesn't parse."""
    try:
        date = parsedate_to_datetime(last_modified or "")
    except TypeError, ValueError:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if date.tzinfo is not None:
        date = date.astimezone(timezone.utc).replace(tzinfo=None)
    return date


def _file_stamp(path: str) -> tuple[int, int, int]:
    """What changes when a file is written again: inode, size and modification time."""
    stat = os.stat(path)
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


_XMLTV_FORMATS = ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S")


def _stop_not_before(start: str, stop: str) -> bool:
    """True when the text alone shows `stop` is not before `start`: both begin with 14 ASCII
    digits ("YYYYmmddHHMMSS"), end the same (" +HHMM", or nothing), and stop's digits are not
    lower. Such times parse with the same offset and compare as their digits do, or one of
    them does not parse: either way normalize_programme_stop returns `stop` as it is."""
    if len(start) != len(stop) or len(stop) not in (14, 20) or start[14:] != stop[14:]:
        return False
    first, last = start[:14], stop[:14]
    return (
        first <= last
        and first.isascii()
        and last.isascii()
        and first.isdecimal()
        and last.isdecimal()
    )


def normalize_programme_stop(start: str, stop: str) -> str:
    """Move a stop time that precedes its start (programme ends after midnight) to the next day.

    Accepts XMLTV times with or without a UTC offset; unparseable values are returned unchanged.
    """
    if _stop_not_before(start, stop):  # most programmes: no time is parsed
        return stop
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


_SHARED_LENGTH = 64
_PROGRAMME_TEXTS = ("title", "desc")


def _programme(
    element: ET.Element, start: str, stop: str, shared: Dict[str, str]
) -> Dict[str, str]:
    """What the guide reads of a <programme> (epg_view.parse_programme): its start, its stop
    (see normalize_programme_stop), and the first of its titles and of its descriptions with
    text, without sub-elements, stripped (a programme without one has no such key).

    `shared`, a dict the caller keeps over the guide, makes equal strings of up to
    _SHARED_LENGTH characters one object (a guide's times and titles repeat over thousands
    of programmes).
    """
    stop = normalize_programme_stop(start, stop)
    programme = {"@start": shared.setdefault(start, start), "@stop": shared.setdefault(stop, stop)}
    for child in element:
        tag = child.tag
        if tag in _PROGRAMME_TEXTS and tag not in programme and not len(child):
            text = (child.text or "").strip()
            if text:
                programme[tag] = (
                    shared.setdefault(text, text) if len(text) <= _SHARED_LENGTH else text
                )
    return programme


_READ_BYTES = 1 << 20  # how much of an XMLTV file the guide parse reads at a time


class _NoXml(ValueError):
    """A zip guide without an .xml file in it."""


@contextmanager
def _open_xmltv(path: str) -> Iterator[Any]:
    """The XMLTV file at `path` opened to read, unpacked as it is read when it is gzip or zip.

    Its first bytes tell (a file's name, and a server's headers, are not trusted: feeds are
    often served as application/octet-stream). A zip without an .xml file raises _NoXml (a
    ValueError); a damaged archive raises as it is read (EOFError, BadGzipFile, BadZipFile).
    """
    with open(path, "rb") as f:
        magic = f.read(4)
        f.seek(0)
        if magic[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=f) as unpacked:
                yield unpacked
        elif magic == b"PK\x03\x04":
            with zipfile.ZipFile(f) as archive:
                name = next((n for n in archive.namelist() if n.endswith(".xml")), None)
                if name is None:
                    raise _NoXml("no XML in archive")
                with archive.open(name) as member:
                    yield member
        else:
            yield f


def _root_children(path: str) -> Generator[ET.Element, None, None]:
    """The elements directly under the root of the XML file at `path` (see _open_xmltv),
    each complete, in document order.

    The file is read a piece at a time and each element is let go once the next one is
    asked for, so the whole document is never in memory (ET.parse's tree took several times
    the file's size). Raises ET.ParseError where ET.parse would (after yielding the elements
    before the error), and Cancelled between pieces when the job is cancelled.
    """
    parser: ET.XMLPullParser = ET.XMLPullParser(events=("start", "end"))
    root: Optional[ET.Element] = None
    depth = 0
    with _open_xmltv(path) as f:
        while True:
            if cancel_requested():
                raise Cancelled()
            data = f.read(_READ_BYTES)
            if data:
                parser.feed(data)
            else:
                parser.close()
            events = cast(Iterator[Tuple[str, ET.Element]], parser.read_events())
            for event, element in events:
                if event == "start":
                    depth += 1
                    if depth == 1:
                        root = element
                    continue
                depth -= 1
                if depth == 1:
                    yield element
                    # Detach it from the root (and the siblings after it already read: they
                    # are whole elements still, each yielded as its end is read).
                    assert root is not None
                    root.clear()
            if not data:
                return


_CHUNK = 65536


def write_xmltv_body(response, path: str) -> None:
    """Stream an XMLTV download to ``path`` as it comes: a gzip or zip body stays packed
    (the parse unpacks it, see _open_xmltv; requests has already decoded Content-Encoding).

    `response` is a streamed requests response, or its body's chunks. When anything fails
    there is no file at ``path``. A cancelled job (the window closing) stops between chunks
    with Cancelled.
    """
    chunks = response.iter_content(_CHUNK) if hasattr(response, "iter_content") else response
    try:
        with open(path, "wb") as f:
            for chunk in chunks:
                if cancel_requested():
                    raise Cancelled()
                f.write(chunk)
    except BaseException:
        _remove_quietly(path)  # a part of it, which the caller must not read
        raise


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:  # not there (or, on Windows, still open somewhere)
        pass


# What reading a saved guide raises when the file is damaged or from another format version
# (not OSError: a file that cannot be opened now may be fine later).
_UNREADABLE_GUIDE = (
    ValueError,
    EOFError,
    pickle.UnpicklingError,
    AttributeError,
    ImportError,
    IndexError,
    TypeError,
)


# Every provider's manager saves the one shared index: loader threads run at once, so the
# read, merge and replace of one save finish before the next begins.
_INDEX_SAVE_LOCK = threading.Lock()

# The URL guide is global: each provider in use loads it with its own manager, on its own
# loader thread. One download of a source at a time, so no two write (or remove) its
# `<hash>.xml` and `.download` files at once (Windows fails to remove a file open elsewhere).
_SOURCE_LOCKS: Dict[str, threading.Lock] = {}
_SOURCE_LOCKS_GUARD = threading.Lock()


def _source_lock(cache_key: str) -> threading.Lock:
    with _SOURCE_LOCKS_GUARD:
        return _SOURCE_LOCKS.setdefault(cache_key, threading.Lock())


# The last guide each source got from any manager of this process, by (cache folder, cache
# key): (how many were stored, its index entry). A manager whose index predates it takes it
# instead of fetching the source again; one that waited for another's download knows by
# the count whether that one stored anything (the index file is never read for it outside
# _INDEX_SAVE_LOCK: on Windows a save's replace fails while it is open).
_STORED: Dict[tuple[str, str], tuple[int, Dict[str, Any]]] = {}


def _stored(where: tuple[str, str]) -> tuple[int, Optional[Dict[str, Any]]]:
    with _SOURCE_LOCKS_GUARD:
        count, entry = _STORED.get(where, (0, None))
        return count, (dict(entry) if entry is not None else None)


def _note_stored(where: tuple[str, str], entry: Dict[str, Any]) -> None:
    with _SOURCE_LOCKS_GUARD:
        _STORED[where] = (_STORED.get(where, (0, None))[0] + 1, dict(entry))


class EpgManager:
    def __init__(self, config_manager, provider_manager):
        self.config_manager = config_manager
        self.provider_manager = provider_manager

        self.index: Dict[str, Any] = {}
        # The index as last read from or written to the shared file: save_index() writes only
        # what this manager changed since, so providers' managers keep each other's entries.
        self._index_base: Dict[str, Any] = {}
        # The guide is replaced, never emptied and refilled: a loader thread builds the new
        # one and swaps it in (see _publish), while the GUI thread keeps reading.
        self.epg: MultiKeyDict = MultiKeyDict()
        self._epg_source: Optional[str] = None  # the cache key self.epg was loaded from
        # The guide last read from or written to its file: (path, file stamp, guide). While
        # it is still the one shown and the file is unchanged, it is not read again.
        self._saved: Optional[tuple[str, tuple[int, int, int], Any]] = None
        self._load_index()

    def _cache_dir(self):
        d = os.path.join(self.config_manager.get_config_dir(), "cache", "epg")
        os.makedirs(d, exist_ok=True)
        return d

    def _index_file(self):
        return os.path.join(self._cache_dir(), "index.json")

    def _guide_path(self, cache_key):
        return os.path.join(self._cache_dir(), f"{cache_key}.guide")

    def _is_expired(self, epg_info):
        epg_date = datetime.strptime(epg_info["date"], _INDEX_TIME_FORMAT)
        age = datetime.now() - epg_date
        return age.total_seconds() > self.config_manager.epg_expiration

    def _publish(self, cache_key, guide):
        """Swap in a fully built guide with one assignment.

        GUI-thread readers take one reference to ``self.epg`` per lookup, so a swap made by
        the loader thread never changes a guide under them.
        """
        self._epg_source = cache_key
        self.epg = guide

    def release(self) -> None:
        """Show an empty guide and free the memory of the one held; the disk cache stays."""
        self._publish(None, MultiKeyDict())
        self._saved = None

    def _load_cached_epg(self, cache_key):
        """Load a previously indexed EPG from disk; return False if it is missing.

        The guide shown is kept when it is the one in that file and the file is unchanged.
        """
        path = self._guide_path(cache_key)
        if not os.path.exists(path):
            return False
        stamp = _file_stamp(path)
        saved = self._saved
        shown = (
            saved is not None
            and self._epg_source == cache_key
            and self.epg is saved[2]
            and saved[:2] == (path, stamp)
        )
        if not shown:
            try:
                guide = read_guide(path)
            except _UNREADABLE_GUIDE as e:
                # Damaged, or written by another version: it stays so, so treat it as
                # missing (the caller fetches the guide and saves a new file).
                logger.warning("The saved guide is unreadable and was removed: %s", safe_exc(e))
                try:
                    os.remove(path)
                except OSError:
                    pass
                return False
            self._publish(cache_key, guide)
            self._saved = (path, stamp, guide)
        self.index[cache_key]["last_access"] = datetime.now().strftime(_INDEX_TIME_FORMAT)
        return True

    def _store_guide(self, cache_key, guide, source_date=None):
        """Show a freshly loaded guide, save it and record when its source was updated."""
        self._publish(cache_key, guide)
        path = self._guide_path(cache_key)
        saved = write_guide(path, guide)
        # The same guide as it loads from the file (programmes packed until looked up): the
        # one parsed above, about 1 KiB of objects per programme, can go.
        self._publish(cache_key, saved)
        self._saved = (path, _file_stamp(path), saved)
        now = datetime.now()
        self.index[cache_key] = {
            "date": (source_date or now).strftime(_INDEX_TIME_FORMAT),
            "last_access": now.strftime(_INDEX_TIME_FORMAT),
        }
        _note_stored(self._where(cache_key), self.index[cache_key])

    def _where(self, cache_key: str) -> tuple[str, str]:
        return (self._cache_dir(), cache_key)

    def _source_failed(self, cache_key):
        """Loading this source failed or found no guide.

        A refresh keeps the guide it has (in memory, else the one saved on disk) and leaves
        the index entry alone, so the next refresh tries again. Only a source without any
        guide is marked unavailable.
        """
        if self._epg_source == cache_key and len(self.epg):
            logger.info("Couldn't refresh the EPG; keeping the current guide")
            return
        if self.index.get(cache_key) and self._load_cached_epg(cache_key):
            logger.info("Couldn't refresh the EPG; showing the saved guide")
            return
        self.index[cache_key] = None
        self._publish(cache_key, MultiKeyDict())

    def _source(self) -> Optional[tuple[str, str, tuple]]:
        """The configured guide source as (kind, cache key, its fetch's arguments), or None
        when there is none. The kinds: "stb" and "xtream", the provider's own guide; "file"
        and "url", every provider's (see _take_shared_entry)."""
        epg_source = self.config_manager.epg_source
        if epg_source == "STB":
            provider = self.provider_manager.current_provider
            provider_type = provider.get("type", "").upper()
            url = provider.get("url", "")
            if provider_type == "STB":
                return "stb", _cache_key(url), (url,)
            if provider_type == "XTREAM":
                username = provider.get("username", "")
                password = provider.get("password", "")
                return "xtream", _xtream_cache_key(url, username), (url, username, password)
        elif epg_source == "Local File":
            path = self.config_manager.epg_file
            return "file", _cache_key(path), (path,)
        elif epg_source == "URL":
            url = self.config_manager.epg_url
            return "url", _cache_key(url), (url,)
        return None

    def _switch_source(self):
        """The configured source (see _source). A guide loaded from another source (a provider
        switch or a settings change) is dropped; a reload of the same source keeps its guide
        visible until the new one is swapped in."""
        source = self._source()
        if self._epg_source != (source[1] if source else None):
            self._publish(None, MultiKeyDict())
        return source

    def _load_index(self):
        self.index.clear()
        self.index.update(self._read_index_file())
        self._index_base = copy.deepcopy(self.index)

    def _read_index_file(self) -> Dict[str, Any]:
        index_file = self._index_file()
        if not os.path.exists(index_file):
            return {}
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error loading index file: {e}")
            return {}
        return data if isinstance(data, dict) else {}

    def _index_programs(self, xmltv_file):
        """
        Parse an XMLTV file and build a MultiKeyDict mapping channel identifiers to
        their program lists. In addition to the canonical channel id, also map
        common aliases (display-name values) to the same program list so lookups
        can succeed even when providers don't supply xmltv_id.

        The file is read as it is parsed (see _root_children), so a <channel> entry may come
        before or after its programmes. A programme keeps what the guide reads of it (see
        _programme).
        """
        programs = MultiKeyDict()

        # 1) Build channel -> alias names map from <channel> entries, and
        # 2) accumulate the programmes' per-channel lists, in one pass over the file.
        alias_map: Dict[str, Set[str]] = {}
        tmp_lists: Dict[str, List[Dict[str, Any]]] = {}
        shared: Dict[str, str] = {}
        # The generator is closed (its file too) also when this loop is left by an error.
        with closing(_root_children(xmltv_file)) as elements:
            for element in elements:
                if element.tag == "programme":
                    channel_id = element.get("channel")
                    start_time = element.get("start")
                    stop_time = element.get("stop")

                    if not channel_id or not start_time or not stop_time:
                        # Skip malformed entries
                        continue

                    program_data = _programme(element, start_time, stop_time, shared)
                    plist = tmp_lists.get(channel_id)
                    if plist is None:
                        plist = tmp_lists[channel_id] = []
                    plist.append(program_data)
                elif element.tag == "channel":
                    ch_id = element.get("id")
                    if not ch_id:
                        continue
                    names: Set[str] = alias_map.setdefault(ch_id, set())
                    for dn in element.findall("display-name"):
                        txt = (dn.text or "").strip()
                        if not txt:
                            continue
                        names.add(txt)
                        # Include a lowercase variant for more forgiving matching
                        names.add(txt.lower())

        # 3) Commit lists into MultiKeyDict with expanded keys per channel
        #     (a stable multikey tuple: id + aliases + any user-provided keys).
        for ch_id, plist in tmp_lists.items():
            # Start with user-provided key group if exists, otherwise the channel id
            user_keys = self.config_manager.xmltv_channel_map.get_keys(ch_id)
            if user_keys:
                key_set: Set[str] = set(user_keys)
            else:
                key_set = {ch_id}

            # Add display-name aliases for this channel
            for alias in alias_map.get(ch_id, set()):
                key_set.add(alias)

            # Keep tuple stable: channel id first, then sorted others
            other_keys = sorted(k for k in key_set if k != ch_id)
            multikeys = (ch_id, *other_keys)
            programs.setdefault(multikeys, []).extend(plist)

        return programs

    def _index_download(self, xmltv_file):
        """_index_programs on a downloaded file, then the file goes: also when the parse
        fails or is cancelled (a provider's guide can take hundreds of MB)."""
        try:
            return self._index_programs(xmltv_file)
        finally:
            _remove_quietly(xmltv_file)  # an error removing it must not hide the parse's

    def reindex_programs(self):
        """Re-key the guide shown after the XMLTV mapping changed (Settings, GUI thread).

        The stored values move to their new keys as they are: a PackedGuide's channels stay
        pickled, so this reads none of them. The new guide is swapped in through _publish,
        as every guide change is.
        """
        old_epg = self.epg
        if isinstance(old_epg, PackedGuide):
            new_epg: MultiKeyDict = PackedGuide()
            entries = old_epg.raw_items()
        else:
            new_epg = MultiKeyDict()
            entries = list(old_epg.items())
        for keys, programs in entries:
            for key in keys:
                new_keys = self.config_manager.xmltv_channel_map.get_keys(key)
                if new_keys:
                    new_epg[new_keys] = programs
                    break
        self._publish(self._epg_source, new_epg)

    def save_index(self):
        """Write this manager's changes into the index file every provider's manager shares.

        The file is read again first and only the entries changed (or removed) here since this
        manager last read or wrote it are updated, so a stale copy never wipes another
        provider's entries. The write goes through a temporary file.
        """
        with _INDEX_SAVE_LOCK:
            merged = self._read_index_file()
            for key, value in self.index.items():
                if key not in self._index_base or self._index_base[key] != value:
                    merged[key] = value
            for key in self._index_base:
                if key not in self.index:
                    merged.pop(key, None)
            index_file = self._index_file()
            handle, temporary = tempfile.mkstemp(
                dir=os.path.dirname(index_file), prefix="index-", suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as f:
                    f.write(json.dumps(merged, option=json.OPT_INDENT_2).decode("utf-8"))
                os.replace(temporary, index_file)
            except BaseException:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
                raise
        self._index_base = copy.deepcopy(merged)
        for key, value in merged.items():  # other providers' entries are known here now
            self.index.setdefault(key, value)

    def set_current_epg(self):
        """Show the configured source's guide: a source indexed as None has none; an outdated
        one is fetched again; otherwise its saved guide is shown. A source never indexed, or
        whose guide file is gone, is fetched."""
        if not self.config_manager.channel_epg:
            self._publish(None, MultiKeyDict())
            return
        source = self._switch_source()
        if source is None:
            return
        kind, key, args = source
        seen = None
        if kind in ("file", "url"):  # every provider's: another's manager may have stored it
            seen = _stored(self._where(key))[0]  # before the entry is taken: none is missed
            self._take_shared_entry(key)
        if key in self.index:
            epg_info = self.index[key]
            if epg_info is None:
                return
            if epg_info and self._out_of_date(kind, args, epg_info):
                self._fetch(kind, args, seen)
                return
            if self._load_cached_epg(key):
                return
        self._fetch(kind, args, seen)

    def _fetch(self, kind, args, seen=None):
        """Fetch the source's guide (see _source) whatever its index entry says; `seen`: see
        _fetch_epg_from_url."""
        if kind == "stb":
            self._fetch_epg_from_stb(*args)
        elif kind == "xtream":
            self._fetch_epg_from_xtream(*args)
        elif kind == "file":
            self._fetch_epg_from_file(*args)
        else:
            self._fetch_epg_from_url(args[0], seen)

    def _out_of_date(self, kind, args, epg_info):
        """Whether a cached source's guide is fetched again: a provider's once it is older
        than the update period, a local file's once the file changed, a URL's once the period
        passed since it was last checked and the server has a newer one."""
        if kind == "file":
            epg_date = datetime.strptime(epg_info["date"], _INDEX_TIME_FORMAT)
            modified = datetime.fromtimestamp(os.path.getmtime(args[0]))
            return (modified - epg_date).total_seconds() > 2
        if kind == "url":
            return self._url_changed(args[0], epg_info)
        return self._is_expired(epg_info)

    def _url_changed(self, url, epg_info) -> bool:
        last_access = datetime.strptime(epg_info["last_access"], _INDEX_TIME_FORMAT)
        current_time = datetime.now()
        if (current_time - last_access).total_seconds() <= self.config_manager.epg_expiration:
            return False
        epg_date = datetime.strptime(epg_info["date"], _INDEX_TIME_FORMAT)
        headers = {**CLIENT_HEADERS, "If-Modified-Since": _http_date(epg_date)}
        # Only the status is read: a server that ignores the header sends the whole guide,
        # which the fetch streams to disk.
        r = requests.get(
            url, headers=headers, timeout=5, verify=self.config_manager.ssl_verify, stream=True
        )
        r.close()
        if r.status_code == 304:  # still fresh
            epg_info["last_access"] = current_time.strftime(_INDEX_TIME_FORMAT)
            return False
        return True

    def _fetch_epg_from_file(self, xmltv_file):
        xmltv_filehash = _cache_key(xmltv_file)
        guide = self._index_programs(xmltv_file)
        if guide:
            modified = datetime.fromtimestamp(os.path.getmtime(xmltv_file))
            self._store_guide(xmltv_filehash, guide, modified)
        else:
            self._source_failed(xmltv_filehash)
        self.save_index()

    def _fetch_epg_from_stb(self, provider_url):
        headers = self.provider_manager.headers
        provider_hash = _cache_key(provider_url)
        base = base_from_url(provider_url)
        prefer_https = provider_prefers_https(
            self.provider_manager.current_provider, self.config_manager.prefer_https
        )
        verify_ssl = self.provider_manager.current_provider.get(
            "ssl_verify", self.config_manager.ssl_verify
        )
        # The setting is already coerced to an int and clamped by ConfigManager.
        period = self.config_manager.epg_stb_period_hours

        items = []
        # Honor per-provider HTTPS preference with graceful fallback
        for b in https_fallback_candidates(base, prefer_https):
            url = stb_endpoint(b)
            logger.debug("STB EPG endpoint: %s?action=get_epg_info&period=%s", url, period)
            content_loader = ContentLoader(
                url=url,
                headers=headers,
                content_type="itv",
                action="get_epg_info",
                period=period,
                verify_ssl=verify_ssl,
            )
            content_loader.run()
            if cancel_requested():
                raise Cancelled()  # not a failed source: it is tried again next time
            items = content_loader.items
            if items:
                break
        if items:
            self._store_guide(provider_hash, items[0])
        else:
            self._source_failed(provider_hash)
        self.save_index()

    def _fetch_epg_from_xtream(self, provider_url, username, password):
        """Fetch EPG from Xtream provider using XMLTV endpoint."""
        provider_hash = _xtream_cache_key(provider_url, username)
        xmltv_url = xtream_xmltv_url(provider_url, username, password)
        logger.debug("Xtream XMLTV URL: %s", redact_url(xmltv_url))
        prefer_https = provider_prefers_https(
            self.provider_manager.current_provider, self.config_manager.prefer_https
        )
        verify_ssl = self.provider_manager.current_provider.get(
            "ssl_verify", self.config_manager.ssl_verify
        )

        r = None  # the answer in use: every streamed answer is closed
        try:
            # Fetch the XMLTV data from Xtream endpoint, trying HTTPS first if preferred
            body = None  # the chunks of the first non-empty answer, streamed (cancellable)
            for _url in https_fallback_candidates(xmltv_url, prefer_https):
                if r is not None:
                    r.close()  # the answer of the candidate passed over
                    r = None
                try:
                    r = requests.get(
                        _url,
                        stream=True,
                        timeout=30,
                        headers=CLIENT_HEADERS,
                        verify=verify_ssl,
                    )
                    if r.status_code == 200:
                        chunks = r.iter_content(_CHUNK)
                        first = next(chunks, b"")
                        if first:
                            xmltv_url = _url
                            body = itertools.chain([first], chunks)
                            break
                except requests.RequestException:
                    if r is not None:
                        r.close()
                    r = None
            if r is None:
                raise RuntimeError("Failed to fetch Xtream XMLTV EPG")
            if r.status_code != 200:
                logger.warning(
                    "Failed to fetch Xtream EPG: status %s from %s",
                    r.status_code,
                    redact_url(xmltv_url),
                )
                self._source_failed(provider_hash)
            else:
                xmltv_file_path = os.path.join(self._cache_dir(), f"{provider_hash}_xtream.xml")
                write_xmltv_body(body if body is not None else iter(()), xmltv_file_path)

                # Parse and index the XMLTV file
                guide = self._index_download(xmltv_file_path)
                if guide:
                    self._store_guide(provider_hash, guide)
                else:
                    self._source_failed(provider_hash)
        except Cancelled:
            raise  # not a failed source: it is tried again next time
        except Exception as e:
            logger.error("Error fetching Xtream EPG: %s", safe_exc(e))
            self._source_failed(provider_hash)
        finally:
            if r is not None:
                r.close()

        self.save_index()

    def _fetch_epg_from_url(self, url, seen=None):
        """Download the URL guide, one manager at a time. `seen`: how many guides of it this
        process had stored when the load began (default: now); one stored since by another
        provider's manager, while this one waited, is read instead."""
        url_hash = _cache_key(url)
        where = self._where(url_hash)
        before = _stored(where)[0] if seen is None else seen
        with _source_lock(url_hash):
            if cancel_requested():
                raise Cancelled()  # it waited for another manager's download: not a failure
            if _stored(where)[0] != before:
                self._take_shared_entry(url_hash, newest=True)
                if self._load_cached_epg(url_hash):
                    return
            self._download_epg_from_url(url)

    def _take_shared_entry(self, cache_key, *, newest=False):
        """A URL or local file guide is every provider's: another provider's manager may have
        stored it since this one read the shared index. Its entry is taken when it is newer
        than this manager's, or as the `newest` (an entry saying the source has no guide is
        never stored: each manager finds that out)."""
        _count, shared = _stored(self._where(cache_key))
        if shared is None:
            return
        mine = self.index.get(cache_key)
        if not newest and isinstance(mine, dict):
            if mine.get("last_access", "") >= shared.get("last_access", ""):
                return
        self.index[cache_key] = shared

    def _download_epg_from_url(self, url):
        r = requests.get(
            url,
            stream=True,
            timeout=30,
            headers=CLIENT_HEADERS,
            verify=self.config_manager.ssl_verify,
        )
        try:
            if r.status_code == 200:
                url_hash = _cache_key(url)
                xmltv_file_path = os.path.join(self._cache_dir(), f"{url_hash}.xml")
                write_xmltv_body(r, xmltv_file_path)
                try:
                    guide = self._index_download(xmltv_file_path)
                except _NoXml as e:  # the index is left untouched
                    logger.warning("Error fetching XMLTV from %s: %s", redact_url(url), safe_exc(e))
                else:
                    if guide:
                        source_date = _source_date(r.headers.get("Last-Modified"))
                        self._store_guide(url_hash, guide, source_date)
                    else:
                        self._source_failed(url_hash)
        finally:
            r.close()
        self.save_index()

    def force_refresh_current_epg(self):
        """Force refresh EPG cache for the active EPG source/provider.

        Ignores expiration and overwrites the existing cache/index so that
        subsequent lookups use freshly indexed data. The current guide stays visible until
        the new one is swapped in, and stays when the refresh fails.
        """
        source = self._switch_source()
        if source is not None:
            self._fetch(source[0], source[2])

    def guide_offset(self) -> timedelta:
        """The provider's guide time offset (epg_view.GUIDE_OFFSET_KEY): its programmes move by
        it (epg_view.normalize), and the lookup window below moves the other way."""
        return guide_offset(getattr(self.provider_manager, "current_provider", None))

    def get_programs_for_channel(self, channel_data):
        """The guide's raw entries of a channel, oldest first: all of a portal's, and an XMLTV
        guide's on from now until the list window's end (epg_list_window_hours; 0: all of
        them), now moved by the provider's guide time offset into the guide's own times."""
        epg = self.epg  # one reference: a refresh may swap in a new guide meanwhile
        provider_type = self.provider_manager.current_provider.get("type", "").upper()
        if self.config_manager.epg_source == "STB" and provider_type == "STB":
            programs = epg.get(channel_data.get("id", ""), [])
            return sorted(programs, key=lambda program: parse_stb(program["time"]))
        # Xtream, Local File and URL guides are XMLTV.
        entries = self._xmltv_entries(epg, channel_data)
        if not entries:
            return []
        # The timezone of the programmes, by the first and the last.
        ref_timezone = parse_xmltv(entries[0]["@start"]).tzinfo
        need_check_tz = parse_xmltv(entries[-1]["@start"]).tzinfo != ref_timezone
        window_hours = self.config_manager.epg_list_window_hours
        if window_hours <= 0:
            return sorted(entries, key=lambda program: program["@start"])
        try:
            start_time = datetime.now(tzlocal.get_localzone())  # aware, as the guide's times
        except Exception:
            start_time = datetime.now()
        start_time -= self.guide_offset()  # in the guide's own times
        end_time = start_time + timedelta(hours=window_hours)
        start_time_str = start_time.astimezone(ref_timezone).strftime("%Y%m%d%H%M%S %z")
        programs = []
        for entry in entries:
            if need_check_tz:
                tz = parse_xmltv(entry["@start"]).tzinfo
                start_time_str = start_time.astimezone(tz).strftime("%Y%m%d%H%M%S %z")
            if entry["@start"] >= start_time_str or entry["@stop"] > start_time_str:
                entry_start = parse_xmltv(entry["@start"])  # the window's end in its timezone
                if entry_start < end_time.astimezone(entry_start.tzinfo):
                    programs.append(entry)
        programs.sort(key=lambda program: program["@start"])
        return programs

    def _xmltv_entries(self, epg, channel_info):
        """The channel's programme list in an XMLTV guide, by its xmltv_id, else its name (as
        it is, then in lower case), each also through the user's XMLTV mapping; None when the
        guide has none."""
        name = (channel_info.get("name") or "").strip()
        xmltv_id = (channel_info.get("xmltv_id") or "").strip()
        for key in dict.fromkeys(k for k in (xmltv_id, name, name.lower()) if k):
            for found in (key, *(self.config_manager.xmltv_channel_map.get_keys(key) or ())):
                entries = epg.get(found)
                if entries is not None:
                    return entries
        return None
