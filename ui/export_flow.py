"""File → Export: one M3U export at a time, on a JobRunner worker (Task 12).

An export first checks that it can run (`export_available`), then asks where to save, then runs
ExportWorker off the GUI thread on a snapshot of the provider's loaded content. Its progress is
a lasting toast; Esc cancels it (the window asks `cancel()` first). The worker's messages are
fixed strings, so they are shown as they are.
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

logger = logging.getLogger(__name__)

BUSY_TEXT = "An export is already running"
STARTED_TEXT = "Exporting…"
PROGRESS_TEXT = "Exporting… {done}/{total}"
DONE_TEXT = "Exported {n} items to {filename}"
FILE_FILTER = "M3U files (*.m3u)"
DIALOG_TITLE = "Export"
INFO_MS = 3000
ERROR_MS = 6000
PROGRESS_MS = 10 * 60 * 1000  # stays up between progress reports; the last report replaces it
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

Notify = Callable[[str, str, int], None]  # text, kind ("info" | "error"), ms


def default_path(provider_name: str, kind: ExportKind) -> str:
    """~/Downloads/<provider>-<kind>.m3u, the provider name made safe for a file name."""
    name = _UNSAFE.sub("_", provider_name).strip(" ._") or "QiTV"
    return str(Path.home() / "Downloads" / f"{name}-{kind.value}.m3u")


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
            self._notify(BUSY_TEXT, "info", INFO_MS)
            return
        manager = (catalog or self._catalog).provider_manager
        provider = dict(manager.current_provider or {})
        content = manager.current_provider_content or {}
        reason = export_available(kind, provider, section, content)
        if reason is not None:
            self._notify(reason, "info", INFO_MS)
            return
        name = str(provider.get("name") or "")
        path, _filter = QFileDialog.getSaveFileName(
            self._dialog_parent, DIALOG_TITLE, default_path(name, kind), FILE_FILTER
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
            prefer_https=bool(self._config.prefer_https),
            headers=dict(manager.headers or {}),
            cancel=cancel,
        )
        self._cancel, self._path = cancel, path
        worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._notify(STARTED_TEXT, "info", PROGRESS_MS)
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
        self._notify(PROGRESS_TEXT.format(done=done, total=total), "info", PROGRESS_MS)

    def _on_finished(self, _context, count) -> None:
        path, self._cancel = self._path, None
        if not self._closed:
            self._notify(DONE_TEXT.format(n=count, filename=Path(path).name), "info", INFO_MS)

    def _on_error(self, _context, message: str) -> None:
        self._cancel = None
        if self._closed:
            return
        if message == MSG_CANCELLED:
            self._notify(MSG_CANCELLED, "info", INFO_MS)
        else:
            logger.info("Export failed: %s", message)
            self._notify(message, "error", ERROR_MS)
