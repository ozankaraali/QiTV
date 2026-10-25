"""File → Export: one M3U export at a time, on a JobRunner worker.

An export first checks that it can run (`export_available`), then asks where to save, then runs
ExportWorker off the GUI thread on a snapshot of the provider's loaded content. Its progress is
a lasting toast; Esc cancels it (the window asks `cancel()` first). The worker's messages are
fixed strings (English, shown with `translate`) or its own words for a failure. While the page
shows "All providers" the export is still one provider's (the one the page returns to): the save
dialog and the result name it.
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtWidgets import QFileDialog, QWidget

from services.exporter import (
    MSG_CANCELLED,
    SECTION_KEYS,
    ExportKind,
    ExportWorker,
    export_available,
)
from services.i18n import N_, tr, tr_n, translate

logger = logging.getLogger(__name__)

BUSY_TEXT = N_("An export is already running")
_WHAT = {"live": "channels", "movies": "movies", "series": "series"}
INFO_MS = 3000
ERROR_MS = 6000
PROGRESS_MS = 10 * 60 * 1000  # stays up between progress reports; the last report replaces it
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

Notify = Callable[[str, str, int], None]  # text, kind ("info" | "error"), ms


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
        manager = page.provider_manager
        provider = dict(manager.current_provider or {})
        content = manager.current_provider_content or {}
        reason = export_available(kind, provider, section, content)
        if reason is not None:
            self._notify(translate(reason), "info", INFO_MS)
            return
        name = str(provider.get("name") or "")
        # All providers lists every provider, but the export is this one's: say whose.
        named = name if getattr(page, "all_providers", lambda: False)() else ""
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
        worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._notify(tr("Exporting…"), "info", PROGRESS_MS)
        self._runner.run_worker(worker, self._on_finished, self._on_error)

    def cancel(self) -> bool:
        """Esc while exporting: True when this cancelled a running export."""
        cancel = self._cancel
        if cancel is None or cancel.is_set():
            return False
        cancel.set()
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
        self._notify(tr("Exporting… {done}/{total}", done=done, total=total), "info", PROGRESS_MS)

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
        self._notify(text, "info", INFO_MS)

    def _on_error(self, _context, message: str) -> None:
        self._cancel = None
        if self._closed:
            return
        if message == MSG_CANCELLED:
            self._notify(translate(MSG_CANCELLED), "info", INFO_MS)
        else:
            logger.info("Export failed: %s", message)
            self._notify(translate(message), "error", ERROR_MS)
