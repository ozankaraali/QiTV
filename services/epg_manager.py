import copy
from datetime import datetime, timedelta
import gzip
import hashlib
import itertools
import logging
import os
import pickle
import shutil
import tempfile
import threading
from typing import Any, Dict, List, Optional, Set
import xml.etree.ElementTree as ET
import zipfile

import orjson as json
import tzlocal  # the GUI thread's guide lookups use it: loaded with the window

from services.content_loader import ContentLoader
from services.epg_store import PackedGuide, read_guide, write_guide
from services.epg_time import parse_stb, parse_xmltv, parse_xmltv_as
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


def _cache_key(source: str) -> str:
    """Name an EPG source's cache entry (index key and pickle file stem)."""
    return hashlib.md5(source.encode()).hexdigest()


def _xtream_cache_key(provider_url: str, username: str) -> str:
    return _cache_key(f"{provider_url}:{username}")


def _file_stamp(path: str) -> tuple[int, int, int]:
    """What changes when a file is written again: inode, size and modification time."""
    stat = os.stat(path)
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


_XMLTV_FORMATS = ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S")


def normalize_programme_stop(start: str, stop: str) -> str:
    """Move a stop time that precedes its start (programme ends after midnight) to the next day.

    Accepts XMLTV times with or without a UTC offset; unparseable values are returned unchanged.
    """
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


def xml_to_dict(element):
    """
    Recursively converts an XML element and its children into a dictionary.
    Handles multiple occurrences of the same child element by storing them in a list.
    Includes attributes of elements in the resulting dictionary.
    """

    def parse_element(element):
        parsed_data: Dict[str, Any] = {}

        # Include element attributes
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


_CHUNK = 65536


def write_xmltv_body(response, path: str) -> None:
    """Write an XMLTV download to ``path``, unpacking gzip or zip bodies by their bytes.

    `response` is a streamed requests response, or its body's chunks. Headers are not
    trusted: feeds are often served as ``application/octet-stream``, and requests has
    already decoded ``Content-Encoding``. The body is streamed to a temporary ``.download``
    file first, which is always removed. A cancelled job (the window closing) stops between
    chunks with Cancelled.
    """
    chunks = response.iter_content(_CHUNK) if hasattr(response, "iter_content") else response
    temp_path = path + ".download"
    try:
        with open(temp_path, "wb") as f:
            for chunk in chunks:
                if cancel_requested():
                    raise Cancelled()
                f.write(chunk)
        with open(temp_path, "rb") as f:
            magic = f.read(4)
        if magic[:2] == b"\x1f\x8b":
            with gzip.open(temp_path, "rb") as src, open(path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        elif magic == b"PK\x03\x04":
            with zipfile.ZipFile(temp_path) as z:
                name = next((n for n in z.namelist() if n.endswith(".xml")), None)
                if name is None:
                    raise ValueError("no XML in archive")
                with z.open(name) as src, open(path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


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

    def _pickle_path(self, cache_key):
        """Where versions before the piecewise format (epg_store) saved the guide."""
        return os.path.join(self._cache_dir(), f"{cache_key}.pkl")

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
        legacy = not os.path.exists(path)
        if legacy:
            path = self._pickle_path(cache_key)
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
                if legacy:
                    with open(path, "rb") as f:
                        guide = pickle.load(f)
                else:
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
        try:
            os.remove(self._pickle_path(cache_key))  # an earlier version's copy
        except FileNotFoundError:
            pass
        now = datetime.now()
        self.index[cache_key] = {
            "date": (source_date or now).strftime(_INDEX_TIME_FORMAT),
            "last_access": now.strftime(_INDEX_TIME_FORMAT),
        }

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

    def _source_key(self):
        """The cache key of the configured guide source, or None when there is none."""
        epg_source = self.config_manager.epg_source
        provider = self.provider_manager.current_provider
        if epg_source == "STB":
            provider_type = provider.get("type", "").upper()
            if provider_type == "STB":
                return _cache_key(provider.get("url", ""))
            if provider_type == "XTREAM":
                return _xtream_cache_key(provider.get("url", ""), provider.get("username", ""))
        elif epg_source == "Local File":
            return _cache_key(self.config_manager.epg_file)
        elif epg_source == "URL":
            return _cache_key(self.config_manager.epg_url)
        return None

    def _forget_other_source(self):
        """Drop a guide loaded from another source (a provider switch or a settings change).

        A reload of the same source keeps its guide visible until the new one is swapped in.
        """
        if self._epg_source != self._source_key():
            self._publish(None, MultiKeyDict())

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

    def clear_index(self):
        cache_dir = self._cache_dir()
        for file in os.listdir(cache_dir):
            file_path = os.path.join(cache_dir, file)
            if os.path.isfile(file_path):
                os.remove(file_path)
        self.index.clear()
        self.save_index()

    def _index_programs(self, xmltv_file):
        """
        Parse an XMLTV file and build a MultiKeyDict mapping channel identifiers to
        their program lists. In addition to the canonical channel id, also map
        common aliases (display-name values) to the same program list so lookups
        can succeed even when providers don't supply xmltv_id.
        """
        programs = MultiKeyDict()

        tree = ET.parse(xmltv_file).getroot()

        # 1) Build channel -> alias names map from <channel> entries
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
                # Include a lowercase variant for more forgiving matching
                names.add(txt.lower())

        # 2) Iterate over programmes and accumulate per-channel lists using a stable
        #     multikey tuple that includes id + aliases + any user-provided keys.
        tmp_lists: Dict[str, List[Dict[str, Any]]] = {}
        for programme in tree.findall("programme"):
            channel_id = programme.get("channel")
            start_time = programme.get("start")
            stop_time = programme.get("stop")

            if not channel_id or not start_time or not stop_time:
                # Skip malformed entries
                continue

            program_data = xml_to_dict(programme)["programme"]
            program_data["@stop"] = normalize_programme_stop(start_time, stop_time)
            tmp_lists.setdefault(channel_id, []).append(program_data)

        # 3) Commit lists into MultiKeyDict with expanded keys per channel
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

    # The _refresh_epg_* helpers refetch a cached source once it is out of date and
    # return True when they did so.

    def _refresh_epg_stb(self, provider_url, headers):
        epg_info = self.index.get(_cache_key(provider_url))
        if epg_info and self._is_expired(epg_info):
            self._fetch_epg_from_stb(provider_url, headers)
            return True
        return False

    def _refresh_epg_xtream(self, provider_url, username, password):
        epg_info = self.index.get(_xtream_cache_key(provider_url, username))
        if epg_info and self._is_expired(epg_info):
            self._fetch_epg_from_xtream(provider_url, username, password)
            return True
        return False

    def _refresh_epg_file(self, xmltv_file):
        xmltv_filehash = _cache_key(xmltv_file)
        epg_info = self.index.get(xmltv_filehash)
        if epg_info:
            # Check modified time
            epg_date = datetime.strptime(epg_info["date"], _INDEX_TIME_FORMAT)
            if (
                datetime.fromtimestamp(os.path.getmtime(xmltv_file)) - epg_date
            ).total_seconds() > 2:
                self._fetch_epg_from_file(xmltv_filehash, xmltv_file)
                return True
        return False

    def _refresh_epg_url(self, url):
        url_hash = _cache_key(url)
        epg_info = self.index.get(url_hash)
        if epg_info:
            # Check expiration time first, if expired check header for last-modified
            last_access = datetime.strptime(epg_info["last_access"], _INDEX_TIME_FORMAT)
            current_time = datetime.now()
            if (current_time - last_access).total_seconds() > self.config_manager.epg_expiration:
                epg_date = datetime.strptime(epg_info["date"], _INDEX_TIME_FORMAT)
                # Request the URL with "If-Modified-Since" header
                headers = {
                    **CLIENT_HEADERS,
                    "If-Modified-Since": epg_date.strftime("%a, %d %b %Y %H:%M:%S GMT"),
                }
                r = requests.get(
                    url, headers=headers, timeout=5, verify=self.config_manager.ssl_verify
                )
                if r.status_code == 304:
                    # EPG is still fresh
                    epg_info["last_access"] = current_time.strftime(_INDEX_TIME_FORMAT)
                    return False
                # EPG is not fresh, fetch it
                self._fetch_epg_from_url(url)
                return True
        return False

    def set_current_epg(self):
        if not self.config_manager.channel_epg:
            self._publish(None, MultiKeyDict())
            return
        self._forget_other_source()

        epg_source = self.config_manager.epg_source
        if epg_source == "STB":
            provider_type = self.provider_manager.current_provider.get("type", "").upper()
            if provider_type == "STB":
                self._set_epg_from_stb(
                    self.provider_manager.current_provider["url"],
                    self.provider_manager.headers,
                )
            elif provider_type == "XTREAM":
                self._set_epg_from_xtream(
                    self.provider_manager.current_provider["url"],
                    self.provider_manager.current_provider.get("username", ""),
                    self.provider_manager.current_provider.get("password", ""),
                )
        elif epg_source == "Local File":
            self._set_epg_from_file(self.config_manager.epg_file)
        elif epg_source == "URL":
            self._set_epg_from_url(self.config_manager.epg_url)

    # Each _set_epg_from_* loader follows the same steps (set_current_epg has already
    # dropped a guide of another source): a source indexed as None has no EPG; an
    # outdated one is refetched; otherwise its cached pickle is used. Sources that were
    # never indexed, or whose pickle is gone, are fetched.

    def _set_epg_from_stb(self, provider_url, headers):
        provider_hash = _cache_key(provider_url)
        if provider_hash in self.index:
            if self.index[provider_hash] is None:
                return
            if self._refresh_epg_stb(provider_url, headers):
                return
            if self._load_cached_epg(provider_hash):
                return
        self._fetch_epg_from_stb(provider_url, headers)

    def _set_epg_from_xtream(self, provider_url, username, password):
        provider_hash = _xtream_cache_key(provider_url, username)
        if provider_hash in self.index:
            if self.index[provider_hash] is None:
                return
            if self._refresh_epg_xtream(provider_url, username, password):
                return
            if self._load_cached_epg(provider_hash):
                return
        self._fetch_epg_from_xtream(provider_url, username, password)

    def _set_epg_from_file(self, xmltv_file):
        xmltv_filehash = _cache_key(xmltv_file)
        if xmltv_filehash in self.index:
            if self.index[xmltv_filehash] is None:
                return
            if self._refresh_epg_file(xmltv_file):
                return
            if self._load_cached_epg(xmltv_filehash):
                return
        self._fetch_epg_from_file(xmltv_filehash, xmltv_file)

    def _set_epg_from_url(self, url):
        url_hash = _cache_key(url)
        if url_hash in self.index:
            if self.index[url_hash] is None:
                return
            if self._refresh_epg_url(url):
                return
            if self._load_cached_epg(url_hash):
                return
        self._fetch_epg_from_url(url)

    def _fetch_epg_from_file(self, xmltv_filehash, xmltv_file):
        guide = self._index_programs(xmltv_file)
        if guide:
            modified = datetime.fromtimestamp(os.path.getmtime(xmltv_file))
            self._store_guide(xmltv_filehash, guide, modified)
        else:
            self._source_failed(xmltv_filehash)
        self.save_index()

    def _fetch_epg_from_stb(self, provider_url, headers):
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

        try:
            # Fetch the XMLTV data from Xtream endpoint, trying HTTPS first if preferred
            r = None
            body = None  # the chunks of the first non-empty answer, streamed (cancellable)
            for _url in https_fallback_candidates(xmltv_url, prefer_https):
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
                guide = self._index_programs(xmltv_file_path)
                os.remove(xmltv_file_path)
                if guide:
                    self._store_guide(provider_hash, guide)
                else:
                    self._source_failed(provider_hash)
        except Cancelled:
            raise  # not a failed source: it is tried again next time
        except Exception as e:
            logger.error("Error fetching Xtream EPG: %s", safe_exc(e))
            self._source_failed(provider_hash)

        self.save_index()

    def _fetch_epg_from_url(self, url):
        r = requests.get(
            url,
            stream=True,
            timeout=30,
            headers=CLIENT_HEADERS,
            verify=self.config_manager.ssl_verify,
        )
        if r.status_code == 200:
            url_hash = _cache_key(url)
            xmltv_file_path = os.path.join(self._cache_dir(), f"{url_hash}.xml")

            try:
                write_xmltv_body(r, xmltv_file_path)
            except ValueError as e:
                # An archive without XML leaves no file, so the index is left untouched.
                logger.warning("Error fetching XMLTV from %s: %s", redact_url(url), safe_exc(e))

            if os.path.exists(xmltv_file_path):
                guide = self._index_programs(xmltv_file_path)
                os.remove(xmltv_file_path)
                if guide:
                    last_modified = datetime.strptime(
                        r.headers.get(
                            "Last-Modified",
                            datetime.now().strftime("%a, %d %b %Y %H:%M:%S %Z"),
                        ),
                        "%a, %d %b %Y %H:%M:%S %Z",
                    )
                    self._store_guide(url_hash, guide, last_modified)
                else:
                    self._source_failed(url_hash)
        self.save_index()

    def force_refresh_current_epg(self):
        """Force refresh EPG cache for the active EPG source/provider.

        Ignores expiration and overwrites the existing cache/index so that
        subsequent lookups use freshly indexed data. The current guide stays visible until
        the new one is swapped in, and stays when the refresh fails.
        """
        self._forget_other_source()
        epg_source = self.config_manager.epg_source
        provider_type = self.provider_manager.current_provider.get("type", "").upper()

        if epg_source == "STB":
            if provider_type == "STB":
                self._fetch_epg_from_stb(
                    self.provider_manager.current_provider.get("url", ""),
                    self.provider_manager.headers,
                )
            elif provider_type == "XTREAM":
                self._fetch_epg_from_xtream(
                    self.provider_manager.current_provider.get("url", ""),
                    self.provider_manager.current_provider.get("username", ""),
                    self.provider_manager.current_provider.get("password", ""),
                )
        elif epg_source == "Local File":
            xmltv_file = self.config_manager.epg_file
            self._fetch_epg_from_file(_cache_key(xmltv_file), xmltv_file)
        elif epg_source == "URL":
            self._fetch_epg_from_url(self.config_manager.epg_url)

    def get_programs_for_channel(self, channel_data, start_time=None, max_programs=5):
        epg_source = self.config_manager.epg_source
        provider_type = self.provider_manager.current_provider.get("type", "").upper()

        if epg_source == "STB" and provider_type == "STB":
            channel_id = channel_data.get("id", "")
            return self._get_programs_for_channel_from_stb(channel_id, start_time, max_programs)
        else:
            # For Xtream, Local File, and URL sources, use XMLTV format
            return self._get_programs_for_channel_from_xmltv(channel_data, start_time, max_programs)

    def _get_programs_for_channel_from_stb(self, channel_id, start_time, max_programs):
        epg = self.epg  # one reference: a refresh may swap in a new guide meanwhile
        programs = epg.get(channel_id, [])
        # If unlimited requested and no windowing, return all entries sorted
        if (not max_programs or max_programs <= 0) and start_time is None:
            return sorted(programs, key=lambda program: parse_stb(program["time"]))
        if start_time is None:
            start_time = datetime.now()
        # Apply end-of-window limit based on config (0 = unlimited)
        window_hours = self.config_manager.epg_list_window_hours
        end_time = start_time + timedelta(hours=window_hours) if window_hours > 0 else None
        filtered = []
        for program in programs:
            p_start = parse_stb(program["time"])
            p_stop = parse_stb(program["time_to"])
            if p_start >= start_time or p_stop > start_time:
                if end_time is None or p_start < end_time:
                    filtered.append(program)
                    if max_programs and max_programs > 0 and len(filtered) >= max_programs:
                        break
        filtered.sort(key=lambda program: parse_stb(program["time"]))
        return filtered

    def _get_programs_for_channel_from_xmltv(self, channel_info, start_time, max_programs):
        epg = self.epg  # one reference: a refresh may swap in a new guide meanwhile
        if start_time is None:
            # Use local timezone-aware datetime to avoid naive/aware mismatches
            try:
                start_time = datetime.now(tzlocal.get_localzone())
            except Exception:
                start_time = datetime.now()

        # Candidate keys in priority order: explicit xmltv_id, then channel name variants
        candidates: List[str] = []
        xmltv_id = (channel_info.get("xmltv_id") or "").strip()
        if xmltv_id:
            candidates.append(xmltv_id)

        name = (channel_info.get("name") or "").strip()
        if name and name not in candidates:
            candidates.append(name)
        lname = name.lower()
        if lname and lname not in candidates:
            candidates.append(lname)

        chosen_key = None
        for key in candidates:
            if key in epg:
                chosen_key = key
                break
            # Also consider user-provided mapping keys
            mapped = self.config_manager.xmltv_channel_map.get_keys(key)
            if mapped:
                for mk in mapped:
                    if mk in epg:
                        chosen_key = mk
                        break
            if chosen_key:
                break

        if not chosen_key:
            return []

        # search the timezone used by programs by looking at the first/last program
        ref_time_str = epg[chosen_key][0]["@start"]
        ref_time = parse_xmltv(ref_time_str)
        ref_timezone = ref_time.tzinfo

        ref_time_str1 = epg[chosen_key][-1]["@start"]
        ref_time1 = parse_xmltv(ref_time_str1)
        ref_timezone1 = ref_time1.tzinfo
        need_check_tz = ref_timezone1 != ref_timezone

        # If unlimited requested and no windowing (window_hours <= 0), return all entries sorted
        window_hours = self.config_manager.epg_list_window_hours
        if (not max_programs or max_programs <= 0) and window_hours <= 0:
            programs = list(epg[chosen_key])
            programs.sort(key=lambda program: program["@start"])
            return programs

        # Get the start time in the timezone of the programs
        start_time_str = start_time.astimezone(ref_timezone).strftime("%Y%m%d%H%M%S %z")

        programs = []
        # Compute end-of-window cutoff if configured
        end_time = start_time + timedelta(hours=window_hours) if window_hours > 0 else None
        for entry in epg[chosen_key]:
            if need_check_tz:
                tz = parse_xmltv(entry["@start"]).tzinfo
                start_time_str = start_time.astimezone(tz).strftime("%Y%m%d%H%M%S %z")
            if entry["@start"] >= start_time_str or entry["@stop"] > start_time_str:
                if end_time is not None:
                    # Compare with end_time in the entry's timezone
                    entry_start_dt = parse_xmltv(entry["@start"])
                    if entry_start_dt >= end_time.astimezone(entry_start_dt.tzinfo):
                        continue
                programs.append(entry)
                if max_programs and max_programs > 0 and len(programs) >= max_programs:
                    break

        programs.sort(key=lambda program: program["@start"])
        # If unlimited requested (max_programs <= 0), return full filtered list
        if not max_programs or max_programs <= 0:
            return programs
        return programs[:max_programs]
