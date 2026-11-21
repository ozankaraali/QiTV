"""Pictures on a phone at home, QiTV's side: the tokens a phone's rows carry for their posters
and logos, and the copies QiTV scales when one is too large to hand as it is. GUI thread only.

A phone gets a token (services/remote/pictures.py) only on a session through the home door: one
through the relay gets none, so its page draws its cards and tiles as before. The sources are the
TV's own: a title's poster as the Library shows it (its provider's, else TMDB's when already known:
none is looked up for a phone), a channel's logo while Settings shows channel logos. A copy the
hub asks for (PictureWanted) comes from the TV's PosterCache or LogoCache, which decode and scale
on their loader thread: here the decoded picture is only encoded again.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject
from PySide6.QtGui import QPixmap

from services.remote.pictures import LOGO, POSTER, PictureShelf
from ui.library.poster_grid import poster_url

logger = logging.getLogger(__name__)

TITLE_KINDS = ("movie", "series", "episode")  # those with a poster (a recording keeps its card)
_NOW_KINDS = {"movies": "movie", "series": "episode"}  # what plays large, by its section
_WAITING = 200  # the copies asked for, waiting for the TV's caches, at most
JPEG_QUALITY = 85

Put = Callable[[str, str, bytes, str], None]


class PhonePictures(QObject):
    """`at_home(sid)`: whether the session is a phone's through the home door; `put(source,
    kind, data, content_type)` hands the hub a scaled copy."""

    def __init__(self, window: Any, at_home: Callable[[str], bool], put: Put, parent=None) -> None:
        super().__init__(parent)
        self._window = window
        self._at_home = at_home
        self._put = put
        self.shelf = PictureShelf()  # shared with the hub's thread (thread-safe)
        self._waiting: dict[str, OrderedDict[str, None]] = {
            POSTER: OrderedDict(),
            LOGO: OrderedDict(),
        }
        for kind in (POSTER, LOGO):
            cache = self._cache(kind)
            if cache is not None:
                cache.logoReady.connect(lambda url, kind=kind: self._ready(url, kind))

    # ---- tokens ----------------------------------------------------------------------------

    def token(self, sid: str, source: str, kind: str) -> str:
        """The token of the picture at `source` for phone `sid`: "" through the relay, for a
        logo while channel logos are off, or when there is none."""
        if not source or not self._at_home(sid):
            return ""
        config = self._window.config_manager
        if kind == LOGO and not getattr(config, "channel_logos", True):
            return ""
        verify = bool(getattr(config, "ssl_verify", True))
        return self.shelf.issue(sid, source, kind, verify=verify)

    def title(self, sid: str, item: dict, kind: str) -> str:
        """A title's poster's token (`kind`: the Library's, "movie", "series", "episode")."""
        if kind not in TITLE_KINDS or not isinstance(item, dict):
            return ""
        return self.token(sid, self._poster(item, kind), POSTER)

    def channel(self, sid: str, channel: dict) -> str:
        """A channel's logo's token."""
        if not isinstance(channel, dict):
            return ""
        return self.token(sid, str(channel.get("logo") or ""), LOGO)

    def now(self) -> Optional[tuple[str, str]]:
        """What plays large's picture, as (source, kind): a film's or an episode's poster, a
        channel's logo; None for a file of the disk's, or nothing playing."""
        controller = self._window.controller
        current = getattr(controller, "_current", None)
        channel = getattr(controller, "_channel", None)
        if current is not None and not current.live:  # as the now-playing card's (state())
            kind = _NOW_KINDS.get(current.section)
            if kind is None or current.local or not isinstance(current.item, dict):
                return None
            source = self._poster(current.item, kind)
            return (source, POSTER) if source else None
        if isinstance(channel, dict) and channel.get("logo"):
            return str(channel["logo"]), LOGO
        return None

    def _poster(self, item: dict, kind: str) -> str:
        library = getattr(self._window, "library_controller", None)
        poster_of = getattr(library, "poster_of", None)
        if callable(poster_of):
            return str(poster_of(item, kind) or "")
        return poster_url(item)

    # ---- scaled copies (PictureWanted) -----------------------------------------------------

    def wanted(self, source: str, kind: str) -> None:
        """The hub asks for `source`'s picture scaled: from the TV's cache now, or once it has
        loaded it."""
        cache = self._cache(kind)
        if cache is None or not source:
            return
        pixmap = cache.pixmap(source)
        if pixmap is not None:
            self._hand(source, kind, pixmap)
            return
        waiting = self._waiting[kind]
        waiting[source] = None
        waiting.move_to_end(source)
        while len(waiting) > _WAITING:
            waiting.popitem(last=False)
        cache.request([source])

    def _ready(self, url: str, kind: str) -> None:
        waiting = self._waiting[kind]
        if url not in waiting:
            return
        del waiting[url]
        cache = self._cache(kind)
        pixmap = cache.pixmap(url) if cache is not None else None
        if pixmap is not None:
            self._hand(url, kind, pixmap)

    def _hand(self, source: str, kind: str, pixmap: QPixmap) -> None:
        encoded = _encoded(pixmap, kind)
        if encoded is not None:
            self._put(source, kind, *encoded)

    def _cache(self, kind: str) -> Any:
        return getattr(self._window, "logos" if kind == LOGO else "posters", None)


def _encoded(pixmap: QPixmap, kind: str) -> Optional[tuple[bytes, str]]:
    """`pixmap` as a phone's browser shows it: a logo as a PNG (it may be transparent), a
    poster as a JPEG. The TV's caches hold it scaled already (ui/logos.py)."""
    if pixmap.isNull():
        return None
    image = pixmap.toImage()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if kind == LOGO:
        saved, content_type = image.save(buffer, "PNG"), "image/png"
    else:
        saved, content_type = image.save(buffer, "JPG", JPEG_QUALITY), "image/jpeg"
    buffer.close()
    if not saved:
        logger.info("Phone remote: a picture couldn't be encoded")
        return None
    return bytes(data.data()), content_type
