"""File → Export: one M3U export at a time, on a JobRunner worker.

An export first checks that it can run (`export_available`), then asks where to save, then runs
ExportWorker off the GUI thread on a snapshot of the provider's loaded content. Its progress is
a lasting toast with a Cancel button, and Esc cancels it too (the window asks `cancel()`
first). Its result says that the file may hold the account's sign-in. The worker's messages are
fixed strings (English, shown with `translate`) or its own words for a failure. While the page
shows "All providers" the export is still one provider's (the one the page returns to): the save
dialog and the result name it. All Live Channels from a Library whose provider has no channel
loaded exports the channel list's provider's, named so too.
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtWidgets import QFileDialog, QWidget

from services.catalog import ALL_GROUP_ID
from services.exporter import (
    MSG_CANCELLED,
    MSG_LOAD_LIVE,
    SECTION_KEYS,
    ExportKind,
    ExportWorker,
    export_available,
    has_items,
)
from services.i18n import N_, tr, tr_n, translate

logger = logging.getLogger(__name__)

BUSY_TEXT = N_("An export is already running")
CANCEL_TEXT = N_("Cancel")  # i18n: the button on an export's progress message (verb)
# i18n: said after "Exported 20 items to file.m3u": its links name the account (Xtream, STB)
HOLDS_SIGN_IN = N_("The file holds your sign-in: keep it private.")
# i18n: said after "Exported 20 items to file.m3u": a playlist's links may name the account
MAY_HOLD_SIGN_IN = N_("The file may hold your sign-in: keep it private.")
SIGNED_LINKS = ("XTREAM", "STB")  # every link of theirs carries the sign-in (or the MAC)
_WHAT = {"live": "channels", "movies": "movies", "series": "series"}
INFO_MS = 3000
ERROR_MS = 6000
RESULT_MS = 6000  # the result and its word on the sign-in: read twice as long
PROGRESS_MS = 10 * 60 * 1000  # stays up between progress reports; the last report replaces it
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

# text, kind ("info" | "error"), ms; and `action`, `on_action`: a button on the message
Notify = Callable[..., None]


def default_path(provider_name: str, kind: ExportKind) -> str:
    """~/Downloads/<provider>-<kind>.m3u, the provider name made safe for a file name."""
    name = _UNSAFE.sub("_", provider_name).strip(" ._") or "QiTV"
    return str(Path.home() / "Downloads" / f"{name}-{kind.value}.m3u")


def _what(kind: ExportKind, section: str) -> str:
    """What an export holds: "channels", "movies", "series" or "items"."""
    if kind is ExportKind.ALL_LIVE:
        return "channels"
    if kind is ExportKind.COMPLETE_SERIES:
        return "series"
    return _WHAT.get(section, "items")


def _dialog_title(kind: ExportKind, section: str, name: str) -> str:
    """The save dialog's title; in All providers it names the provider exported."""
    if not name:
        return tr("Export")  # i18n: the save dialog's title (verb)
    what = _what(kind, section)
    if what == "channels":
        return tr("Export {name}'s channels", name=name)  # i18n: name is a provider's
    if what == "movies":
        return tr("Export {name}'s movies", name=name)
    if what == "series":
        return tr("Export {name}'s series", name=name)
    return tr("Export {name}'s items", name=name)


def _has_live(page) -> bool:
    """Whether the page's provider has a live channel loaded."""
    return has_items(page.provider_manager.current_provider_content or {}, "live")


def _no_channels(content: dict) -> bool:
    """Whether the provider's live section has loaded, with no group of channels."""
    live = content.get(SECTION_KEYS["live"])
    if isinstance(live, list):
        return not live
    if not isinstance(live, dict) or "categories" not in live:
        return False  # not loaded yet
    groups = [g for g in live.get("categories") or [] if isinstance(g, dict)]
    return not any(str(g.get("id", "")) != ALL_GROUP_ID for g in groups)


def _snapshot(value: Any) -> Any:
    """A copy the worker can read while the catalog keeps loading: dicts and lists are copied
    (cheaply: the item dicts themselves are shared and never changed by an export)."""
    if isinstance(value, dict):
        return {key: _snapshot(item) for key, item in value.items()}
    if isinstance(value, list):
        return list(value)
    return value


class ExportFlow(QObject):
    def __init__(
        self,
        *,
        catalog,  # the TV page's (export() may name the Library's)
        config,
        runner,
        notify: Notify,
        dialog_parent: Optional[QWidget] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._config = config
        self._runner = runner
        self._notify = notify
        self._dialog_parent = dialog_parent
        self._cancel: Optional[threading.Event] = None  # set while an export runs
        self._path = ""
        self._named = ""  # the provider the result names (All providers), else ""
        self._signed = False  # the provider's links carry its sign-in (SIGNED_LINKS)
        self._progress = ""  # the progress message shown last
        self._closed = False

    def running(self) -> bool:
        """An export runs (also while a cancelled one winds down)."""
        return self._cancel is not None

    def export(self, kind: ExportKind, section: str, catalog=None) -> None:
        """Check, ask for a path, then export. `section` is what the screen shows and
        `catalog` the page's browsed catalog (its provider is what is exported)."""
        if self._closed:
            return
        if self.running():
            self._notify(translate(BUSY_TEXT), "info", INFO_MS)
            return
        page = catalog or self._catalog
        if kind is ExportKind.ALL_LIVE and not _has_live(page) and _has_live(self._catalog):
            page = self._catalog  # from a Library without channels: the channel list's, named
        manager = page.provider_manager
        provider = dict(manager.current_provider or {})
        content = manager.current_provider_content or {}
        name = str(provider.get("name") or "")
        reason = export_available(kind, provider, section, content)
        if reason == MSG_LOAD_LIVE and _no_channels(content):
            # i18n: File → Export → All Live Channels, for a provider without any (films only)
            self._notify(tr("{name} has no live channels.", name=name), "info", INFO_MS)
            return
        if reason is not None:
            self._notify(translate(reason), "info", INFO_MS)
            return
        # All providers lists every provider, but the export is this one's: say whose (and
        # whose channels an export from the Library's other provider took).
        all_mode = getattr(page, "all_providers", lambda: False)()
        named = name if all_mode or page is not (catalog or self._catalog) else ""
        path, _filter = QFileDialog.getSaveFileName(
            self._dialog_parent,
            _dialog_title(kind, section, named),
            default_path(name, kind),
            tr("M3U files (*.m3u)"),  # i18n: the save dialog's file type; keep "(*.m3u)"
        )
        if not path or self._closed or self.running():
            return
        cancel = threading.Event()
        worker = ExportWorker(
            kind,
            provider,
            {key: _snapshot(content[key]) for key in SECTION_KEYS.values() if key in content},
            section,
            path,
            verify_ssl=bool(provider.get("ssl_verify", self._config.ssl_verify)),
            headers=dict(manager.headers or {}),
            cancel=cancel,
        )
        self._cancel, self._path, self._named = cancel, path, named
        self._signed = str(provider.get("type") or "") in SIGNED_LINKS
        worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._show_progress(tr("Exporting…"))
        self._runner.run_worker(worker, self._on_finished, self._on_error)

    def _show_progress(self, text: str) -> None:
        """The lasting progress message, with its Cancel button (Esc does the same)."""
        self._progress = text
        self._notify(text, "info", PROGRESS_MS, action=CANCEL_TEXT, on_action=self.cancel)

    def cancel(self) -> bool:
        """Esc, or the progress message's Cancel, while exporting: True when this cancelled a
        running export. The message stays, without its button, until the export has stopped."""
        cancel = self._cancel
        if cancel is None or cancel.is_set():
            return False
        cancel.set()
        if not self._closed:
            self._notify(self._progress, "info", PROGRESS_MS)
        return True

    def shutdown(self) -> None:
        """The window closes: a running export stops (its worker ends within a moment)."""
        self._closed = True
        if self._cancel is not None:
            self._cancel.set()

    @Slot(int, int, str)
    def _on_progress(self, done: int, total: int, _label: str) -> None:
        if self._closed or self._cancel is None or self._cancel.is_set():
            return
        # i18n: an export's progress: done of total (series, for a complete series)
        self._show_progress(tr("Exporting… {done}/{total}", done=done, total=total))

    def _on_finished(self, _context, count) -> None:
        path, self._cancel = self._path, None
        if self._closed:
            return
        filename = Path(path).name
        if self._named:  # in All providers
            text = tr_n(
                "Exported {n} item from {name} to {filename}",
                "Exported {n} items from {name} to {filename}",
                count,
                name=self._named,
                filename=filename,
            )
        else:
            text = tr_n(
                "Exported {n} item to {filename}",
                "Exported {n} items to {filename}",
                count,
                filename=filename,
            )
        if not count:
            self._notify(text, "info", INFO_MS)
            return
        # The file's links reach the provider as this account: said where it is saved.
        note = translate(HOLDS_SIGN_IN if self._signed else MAY_HOLD_SIGN_IN)
        self._notify(f"{text} {note}", "info", RESULT_MS)

    def _on_error(self, _context, message: str) -> None:
        self._cancel = None
        if self._closed:
            return
        if message == MSG_CANCELLED:
            self._notify(translate(MSG_CANCELLED), "info", INFO_MS)
        else:
            logger.info("Export failed: %s", message)
            self._notify(translate(message), "error", ERROR_MS)
