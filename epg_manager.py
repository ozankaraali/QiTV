from datetime import datetime, timedelta
import gzip
import hashlib
import io
import logging
import os
import pickle
from typing import Any, Dict, List, Set
import xml.etree.ElementTree as ET
import zipfile

import orjson as json
import requests
import tzlocal
from urlobject import URLObject

from content_loader import ContentLoader
from multikeydict import MultiKeyDict
from services.provider_api import base_from_url, stb_endpoint, xtream_xmltv_url


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


class EpgManager:
    def __init__(self, config_manager, provider_manager):
        self.config_manager = config_manager
        self.provider_manager = provider_manager

        self.index: Dict[str, Any] = {}
        self.epg: MultiKeyDict = MultiKeyDict()
        self._load_index()

    def _cache_dir(self):
        d = os.path.join(self.config_manager.get_config_dir(), "cache", "epg")
        os.makedirs(d, exist_ok=True)
        return d

    def _index_file(self):
        cache_dir = self._cache_dir()
        return os.path.join(cache_dir, "index.json")

    def _load_index(self):
        index_file = self._index_file()
        self.index.clear()
        if os.path.exists(index_file):
            with open(index_file, "r", encoding="utf-8") as f:
                try:
                    self.index = json.loads(f.read())
                except (json.JSONDecodeError, IOError) as e:
                    logger.warning(f"Error loading index file: {e}")

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

            # Fix stop_time < start_time, which means the program ends on the next day
            if start_time > stop_time:
                stop_time = (
                    datetime.strptime(stop_time, "%Y%m%d%H%M%S %z") + timedelta(days=1)
                ).strftime("%Y%m%d%H%M%S %z")

            program_data = xml_to_dict(programme)["programme"]
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
        # Reindex existing epg
        new_epg = MultiKeyDict()
        for keys, programs in self.epg.items():
            for key in keys:
                new_keys = self.config_manager.xmltv_channel_map.get_keys(key)
                if new_keys:
                    new_epg[new_keys] = programs
                    break
        self.epg = new_epg

    def save_index(self):
        index_file = self._index_file()
        with open(index_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.index, option=json.OPT_INDENT_2).decode("utf-8"))

    def refresh_epg(self):
        epg_source = self.config_manager.epg_source

        if epg_source == "STB":
            provider_type = self.provider_manager.current_provider.get("type", "").upper()
            if provider_type == "STB":
                return self._refresh_epg_stb(
                    self.provider_manager.current_provider["url"],
                    self.provider_manager.headers,
                )
            elif provider_type == "XTREAM":
                return self._refresh_epg_xtream(
                    self.provider_manager.current_provider["url"],
                    self.provider_manager.current_provider.get("username", ""),
                    self.provider_manager.current_provider.get("password", ""),
                )
        elif epg_source == "Local File":
            return self._refresh_epg_file(self.config_manager.epg_file)
        elif epg_source == "URL":
            return self._refresh_epg_url(self.config_manager.epg_url)
        return False

    def _refresh_epg_stb(self, provider_url, headers):
        provider_hash = hashlib.md5(provider_url.encode()).hexdigest()
        if provider_hash in self.index:
            epg_info = self.index[provider_hash]
            if epg_info:
                current_time = datetime.now()
                # Check expiration time
                epg_date = datetime.strptime(epg_info["date"], "%Y-%m-%d %H:%M:%S")
                if (current_time - epg_date).total_seconds() > self.config_manager.epg_expiration:
                    self._fetch_epg_from_stb(provider_url, headers)
                    return True
        return False

    def _refresh_epg_xtream(self, provider_url, username, password):
        provider_hash = hashlib.md5(f"{provider_url}:{username}".encode()).hexdigest()
        if provider_hash in self.index:
            epg_info = self.index[provider_hash]
            if epg_info:
                current_time = datetime.now()
                # Check expiration time
                epg_date = datetime.strptime(epg_info["date"], "%Y-%m-%d %H:%M:%S")
                if (current_time - epg_date).total_seconds() > self.config_manager.epg_expiration:
                    self._fetch_epg_from_xtream(provider_url, username, password)
                    return True
        return False

    def _refresh_epg_file(self, xmltv_file):
        xmltv_filehash = hashlib.md5(xmltv_file.encode()).hexdigest()
        if xmltv_filehash in self.index:
            epg_info = self.index[xmltv_filehash]
            if epg_info:
                # Check modified time
                epg_date = datetime.strptime(epg_info["date"], "%Y-%m-%d %H:%M:%S")
                if (
                    datetime.fromtimestamp(os.path.getmtime(xmltv_file)) - epg_date
                ).total_seconds() > 2:
                    self._fetch_epg_from_file(xmltv_filehash, xmltv_file)
                    return True
        return False

    def _refresh_epg_url(self, url):
        url_hash = hashlib.md5(url.encode()).hexdigest()
        if url_hash in self.index:
            epg_info = self.index[url_hash]
            if epg_info:
                # Check expiration time first, if expired check header for last-modified
                last_access = datetime.strptime(epg_info["last_access"], "%Y-%m-%d %H:%M:%S")
                current_time = datetime.now()
                if (
                    current_time - last_access
                ).total_seconds() > self.config_manager.epg_expiration:
                    epg_date = datetime.strptime(epg_info["date"], "%Y-%m-%d %H:%M:%S")
                    # Request the URL with "If-Modified-Since" header
                    headers = {"If-Modified-Since": epg_date.strftime("%a, %d %b %Y %H:%M:%S GMT")}
                    r = requests.get(
                        url, headers=headers, timeout=5, verify=self.config_manager.ssl_verify
                    )
                    if r.status_code == 304:
                        # EPG is still fresh
                        self.index[url_hash]["last_access"] = current_time.strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        return False
                    # EPG is not fresh, fetch it
                    self._fetch_epg_from_url(url)
                    return True
        return False

    def set_current_epg(self):
        # Initialize with MultiKeyDict for compatibility with both STB and XMLTV formats
        self.epg = MultiKeyDict()
        if not self.config_manager.channel_epg:
            return

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

    def _set_epg_from_stb(self, provider_url, headers):
        provider_hash = hashlib.md5(provider_url.encode()).hexdigest()
        if provider_hash in self.index:
            epg_info = self.index[provider_hash]
            if epg_info is None:
                # STB EPG not available, keep MultiKeyDict
                return
            refreshed = self._refresh_epg_stb(provider_url, headers)
            if refreshed:
                return

            # EPG was fresh enough
            cache_dir = self._cache_dir()
            epg_file = os.path.join(cache_dir, f"{provider_hash}.pkl")
            if os.path.exists(epg_file):
                with open(epg_file, "rb") as f:
                    self.epg = pickle.load(f)
                    current_time = datetime.now()
                    self.index[provider_hash]["last_access"] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    return

        # no EPG or not fresh enough, fetch it
        self._fetch_epg_from_stb(provider_url, headers)

    def _set_epg_from_xtream(self, provider_url, username, password):
        provider_hash = hashlib.md5(f"{provider_url}:{username}".encode()).hexdigest()
        if provider_hash in self.index:
            epg_info = self.index[provider_hash]
            if epg_info is None:
                # Xtream uses XMLTV format, which requires MultiKeyDict
                if not isinstance(self.epg, MultiKeyDict):
                    self.epg = MultiKeyDict()
                return
            refreshed = self._refresh_epg_xtream(provider_url, username, password)
            if refreshed:
                return

            # EPG was fresh enough
            cache_dir = self._cache_dir()
            epg_file = os.path.join(cache_dir, f"{provider_hash}.pkl")
            if os.path.exists(epg_file):
                with open(epg_file, "rb") as f:
                    self.epg = pickle.load(f)
                    self.index[provider_hash]["last_access"] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    return

        # no EPG or not fresh enough, fetch it
        self._fetch_epg_from_xtream(provider_url, username, password)

    def _set_epg_from_file(self, xmltv_file):
        xmltv_filehash = hashlib.md5(xmltv_file.encode()).hexdigest()
        if xmltv_filehash in self.index:
            epg_info = self.index[xmltv_filehash]
            if epg_info is None:
                # XMLTV files require MultiKeyDict
                if not isinstance(self.epg, MultiKeyDict):
                    self.epg = MultiKeyDict()
                return
            refreshed = self._refresh_epg_file(xmltv_file)
            if refreshed:
                return

            # EPG is fresh enough
            cache_dir = self._cache_dir()
            programs_pickle = os.path.join(cache_dir, f"{xmltv_filehash}.pkl")
            if os.path.exists(programs_pickle):
                with open(programs_pickle, "rb") as f:
                    self.epg = pickle.load(f)
                    self.index[xmltv_filehash]["last_access"] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    return

        # no EPG or not fresh enough, fetch it
        self._fetch_epg_from_file(xmltv_filehash, xmltv_file)

    def _set_epg_from_url(self, url):
        url_hash = hashlib.md5(url.encode()).hexdigest()
        if url_hash in self.index:
            epg_info = self.index[url_hash]
            if epg_info is None:
                # XMLTV URLs require MultiKeyDict
                if not isinstance(self.epg, MultiKeyDict):
                    self.epg = MultiKeyDict()
                return
            refreshed = self._refresh_epg_url(url)
            if refreshed:
                return

            # EPG is fresh enough
            cache_dir = self._cache_dir()
            programs_pickle = os.path.join(cache_dir, f"{url_hash}.pkl")
            if os.path.exists(programs_pickle):
                with open(programs_pickle, "rb") as f:
                    self.epg = pickle.load(f)
                    self.index[url_hash]["last_access"] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    return

        # no EPG or not fresh enough, fetch it
        self._fetch_epg_from_url(url)

    def _fetch_epg_from_file(self, xmltv_filehash, xmltv_file):
        self.epg = self._index_programs(xmltv_file)
        if self.epg:
            cache_dir = self._cache_dir()
            programs_pickle = os.path.join(cache_dir, f"{xmltv_filehash}.pkl")
            with open(programs_pickle, "wb") as f:
                pickle.dump(self.epg, f)
            self.index[xmltv_filehash] = {
                "date": datetime.fromtimestamp(os.path.getmtime(xmltv_file)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "last_access": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        else:
            self.index[xmltv_filehash] = None
        self.save_index()

    def _fetch_epg_from_stb(self, provider_url, headers):
        provider_hash = hashlib.md5(provider_url.encode()).hexdigest()
        base = base_from_url(provider_url)
        prefer_https = self.provider_manager.current_provider.get(
            "prefer_https", self.config_manager.prefer_https
        )
        verify_ssl = self.provider_manager.current_provider.get(
            "ssl_verify", self.config_manager.ssl_verify
        )
        candidates = [base]
        # Honor per-provider HTTPS preference with graceful fallback
        try:
            if prefer_https and base.startswith("http://"):
                candidates = ["https://" + base[len("http://") :], base]
        except Exception:
            candidates = [base]

        content_loader = None
        for b in candidates:
            url = stb_endpoint(b)
            try:
                period = int(self.config_manager.epg_stb_period_hours)
            except Exception:
                period = 5
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
            if getattr(content_loader, "items", None):
                break
        items = getattr(content_loader, "items", []) if content_loader else []
        if items:
            self.epg = items[0]
            cache_dir = self._cache_dir()
            epg_file = os.path.join(cache_dir, f"{provider_hash}.pkl")
            with open(epg_file, "wb") as f:
                pickle.dump(self.epg, f)
            current_time = datetime.now()
            self.index[provider_hash] = {
                "date": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_access": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        else:
            self.index[provider_hash] = None
            self.epg = MultiKeyDict()
        self.save_index()

    def _fetch_epg_from_xtream(self, provider_url, username, password):
        """Fetch EPG from Xtream provider using XMLTV endpoint."""
        provider_hash = hashlib.md5(f"{provider_url}:{username}".encode()).hexdigest()
        xmltv_url = xtream_xmltv_url(provider_url, username, password)
        logger.debug("Xtream XMLTV URL: %s", xmltv_url)
        prefer_https = self.provider_manager.current_provider.get(
            "prefer_https", self.config_manager.prefer_https
        )
        verify_ssl = self.provider_manager.current_provider.get(
            "ssl_verify", self.config_manager.ssl_verify
        )
        urls = [xmltv_url]
        if prefer_https and xmltv_url.startswith("http://"):
            urls = ["https://" + xmltv_url[len("http://") :], xmltv_url]

        try:
            # Fetch the XMLTV data from Xtream endpoint, trying HTTPS first if preferred
            r = None
            for _url in urls:
                try:
                    r = requests.get(_url, stream=True, timeout=30, verify=verify_ssl)
                    if r.status_code == 200 and r.content:
                        xmltv_url = _url
                        break
                except requests.RequestException:
                    r = None
                    continue
            if r is None:
                raise RuntimeError("Failed to fetch Xtream XMLTV EPG")
            if r.status_code == 200:
                cache_dir = self._cache_dir()
                xmltv_file_path = os.path.join(cache_dir, f"{provider_hash}_xtream.xml")

                # Handle compressed responses
                content_type = r.headers.get("Content-Type", "")
                content_encoding = r.headers.get("Content-Encoding", "")

                if content_encoding == "gzip" or content_type == "application/gzip":
                    with (
                        gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz,
                        open(xmltv_file_path, "wb") as f,
                    ):
                        f.write(gz.read())
                else:
                    with open(xmltv_file_path, "wb") as f:
                        f.write(r.content)

                # Parse and index the XMLTV file
                if os.path.exists(xmltv_file_path):
                    self.epg = self._index_programs(xmltv_file_path)
                    os.remove(xmltv_file_path)

                    if self.epg:
                        # Cache the parsed EPG
                        epg_file = os.path.join(cache_dir, f"{provider_hash}.pkl")
                        with open(epg_file, "wb") as f:
                            pickle.dump(self.epg, f)

                        current_time = datetime.now()
                        self.index[provider_hash] = {
                            "date": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                            "last_access": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    else:
                        self.index[provider_hash] = None
                        self.epg = MultiKeyDict()
                else:
                    self.index[provider_hash] = None
                    self.epg = MultiKeyDict()
            else:
                logger.warning(
                    f"Failed to fetch Xtream EPG, status code: {r.status_code} for URL: {xmltv_url}"
                )
                self.index[provider_hash] = None
                self.epg = MultiKeyDict()
        except Exception as e:
            logger.error(f"Error fetching Xtream EPG: {e}")
            self.index[provider_hash] = None
            self.epg = MultiKeyDict()

        self.save_index()

    def _fetch_epg_from_url(self, url):
        r = requests.get(url, stream=True, timeout=10, verify=self.config_manager.ssl_verify)
        if r.status_code == 200:
            content_type = r.headers.get("Content-Type", "")
            xmltv_file_path = None
            cache_dir = self._cache_dir()
            url_hash = hashlib.md5(url.encode()).hexdigest()
            xmltv_file_path = os.path.join(cache_dir, f"{url_hash}.xml")

            if content_type == "application/zip":
                with zipfile.ZipFile(io.BytesIO(r.raw.read())) as z:
                    for name in z.namelist():
                        if name.endswith(".xml"):
                            with z.open(name) as xml_file, open(xmltv_file_path, "wb") as f:
                                f.write(xml_file.read())
                            break
            elif content_type == "application/gzip":
                with (
                    gzip.GzipFile(fileobj=io.BytesIO(r.raw.read())) as gz,
                    open(xmltv_file_path, "wb") as f,
                ):
                    f.write(gz.read())
            else:
                with open(xmltv_file_path, "wb") as f:
                    f.write(r.content)

            if os.path.exists(xmltv_file_path):
                self.epg = self._index_programs(xmltv_file_path)
                os.remove(xmltv_file_path)
                if self.epg:
                    programs_pickle = os.path.join(cache_dir, f"{url_hash}.pkl")
                    with open(programs_pickle, "wb") as f:
                        pickle.dump(self.epg, f)
                    current_time = datetime.now()
                    last_modified = datetime.strptime(
                        r.headers.get(
                            "Last-Modified",
                            current_time.strftime("%a, %d %b %Y %H:%M:%S %Z"),
                        ),
                        "%a, %d %b %Y %H:%M:%S %Z",
                    )
                    self.index[url_hash] = {
                        "date": last_modified.strftime("%Y-%m-%d %H:%M:%S"),
                        "last_access": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                else:
                    self.index[url_hash] = None
                    self.epg = MultiKeyDict()
        self.save_index()

    def force_refresh_current_epg(self):
        """Force refresh EPG cache for the active EPG source/provider.

        Ignores expiration and overwrites the existing cache/index so that
        subsequent lookups use freshly indexed data.
        """
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
            xmltv_filehash = hashlib.md5(xmltv_file.encode()).hexdigest()
            self._fetch_epg_from_file(xmltv_filehash, xmltv_file)
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
        programs = self.epg.get(channel_id, [])
        # If unlimited requested and no windowing, return all entries sorted
        if (not max_programs or max_programs <= 0) and start_time is None:
            return sorted(
                programs,
                key=lambda program: datetime.strptime(program["time"], "%Y-%m-%d %H:%M:%S"),
            )
        if start_time is None:
            start_time = datetime.now()
        # Apply end-of-window limit based on config (0 = unlimited)
        window_hours = 0
        try:
            window_hours = int(self.config_manager.epg_list_window_hours)
        except Exception:
            window_hours = 0
        end_time = start_time + timedelta(hours=window_hours) if window_hours > 0 else None
        filtered = []
        for program in programs:
            p_start = datetime.strptime(program["time"], "%Y-%m-%d %H:%M:%S")
            p_stop = datetime.strptime(program["time_to"], "%Y-%m-%d %H:%M:%S")
            if p_start >= start_time or p_stop > start_time:
                if end_time is None or p_start < end_time:
                    filtered.append(program)
                    if max_programs and max_programs > 0 and len(filtered) >= max_programs:
                        break
        filtered.sort(key=lambda program: datetime.strptime(program["time"], "%Y-%m-%d %H:%M:%S"))
        return filtered

    def _get_programs_for_channel_from_xmltv(self, channel_info, start_time, max_programs):
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
            if key in self.epg:
                chosen_key = key
                break
            # Also consider user-provided mapping keys
            mapped = self.config_manager.xmltv_channel_map.get_keys(key)
            if mapped:
                for mk in mapped:
                    if mk in self.epg:
                        chosen_key = mk
                        break
            if chosen_key:
                break

        if not chosen_key:
            return []

        # search the timezone used by programs by looking at the first/last program
        ref_time_str = self.epg[chosen_key][0]["@start"]
        ref_time = datetime.strptime(ref_time_str, "%Y%m%d%H%M%S %z")
        ref_timezone = ref_time.tzinfo

        ref_time_str1 = self.epg[chosen_key][-1]["@start"]
        ref_time1 = datetime.strptime(ref_time_str1, "%Y%m%d%H%M%S %z")
        ref_timezone1 = ref_time1.tzinfo
        need_check_tz = ref_timezone1 != ref_timezone

        # If unlimited requested and no windowing (window_hours <= 0), return all entries sorted
        window_hours = 0
        try:
            window_hours = int(self.config_manager.epg_list_window_hours)
        except Exception:
            window_hours = 0
        if (not max_programs or max_programs <= 0) and window_hours <= 0:
            programs = list(self.epg[chosen_key])
            programs.sort(key=lambda program: program["@start"])
            return programs

        # Get the start time in the timezone of the programs
        start_time_str = start_time.astimezone(ref_timezone).strftime("%Y%m%d%H%M%S %z")

        programs = []
        # Compute end-of-window cutoff if configured
        end_time = start_time + timedelta(hours=window_hours) if window_hours > 0 else None
        for entry in self.epg[chosen_key]:
            if need_check_tz:
                tz = datetime.strptime(entry["@start"], "%Y%m%d%H%M%S %z").tzinfo
                start_time_str = start_time.astimezone(tz).strftime("%Y%m%d%H%M%S %z")
            if entry["@start"] >= start_time_str or entry["@stop"] > start_time_str:
                if end_time is not None:
                    # Compare with end_time in the entry's timezone
                    entry_start_dt = datetime.strptime(entry["@start"], "%Y%m%d%H%M%S %z")
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

    def _filter_and_sort_programs(self, programs, start_time, max_programs):
        filtered_programs = []
        for program in programs:
            if (
                datetime.strptime(program["time"], "%Y-%m-%d %H:%M:%S") >= start_time
                or datetime.strptime(program["time_to"], "%Y-%m-%d %H:%M:%S") > start_time
            ):
                filtered_programs.append(program)
                if max_programs and max_programs > 0 and len(filtered_programs) >= max_programs:
                    break

        filtered_programs.sort(
            key=lambda program: datetime.strptime(program["time"], "%Y-%m-%d %H:%M:%S")
        )
        return filtered_programs[:max_programs]


logger = logging.getLogger(__name__)
