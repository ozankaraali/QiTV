"""config.json written on a thread of its own: the GUI thread hands over the bytes (no Qt).

A save serialises the settings where it is asked (the GUI thread; some 7 ms for a 6 MB
config). orjson holds the interpreter while it runs, so serialising on another thread would
stall the GUI thread as long, and a copy to serialise later costs several times more. Writing
the file and swapping it in took 20 to 60 ms more, and up to a quarter of a second more while
the file stayed locked (Windows antivirus, a sync tool): that is done here. One writer, in
order; of the snapshots waiting, only the newest is written (each holds all the settings).
"""

from __future__ import annotations

import threading
from typing import Callable, Optional


class ConfigWriter:
    """Writes the snapshots `submit` hands it with `write(data, retries)`, which raises when
    it cannot (and says so). Thread-safe; `submit` never waits for the disk."""

    def __init__(self, write: Callable[[bytes, int], None], name: str = "config-save") -> None:
        self._write = write
        self._name = name
        self._lock = threading.Condition()
        self._next: Optional[tuple[int, bytes, int]] = None  # (its number, data, retries)
        self._submitted = 0  # the number of the newest snapshot handed over
        self._done = 0  # the number of the newest written (or failed): older ones count too
        self._failure: Optional[Exception] = None  # how that write failed
        self._thread: Optional[threading.Thread] = None

    def submit(self, data: bytes, retries: int) -> int:
        """Write `data` (instead of a snapshot still waiting); its number, for `wait`."""
        with self._lock:
            self._submitted += 1
            self._next = (self._submitted, data, retries)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            return self._submitted

    def wait(self, number: int, timeout: Optional[float] = None) -> None:
        """Until snapshot `number` (or a newer one, which holds it) is written; raises that
        write's failure, or TimeoutError once `timeout` passed."""
        with self._lock:
            if not self._lock.wait_for(
                lambda: self._done >= number or self._thread is None, timeout
            ):
                raise TimeoutError("config.json is still being written")
            if self._done < number:  # the writer ended without it (it never does)
                raise RuntimeError("config.json was not written")
            if self._failure is not None:
                raise self._failure

    def flush(self, timeout: float) -> bool:
        """Waits, `timeout` seconds at most, until every snapshot handed over is written
        (QiTV quitting); False when one still was not."""
        with self._lock:
            return self._lock.wait_for(lambda: self._thread is None, timeout)

    def _run(self) -> None:
        ended = False
        try:
            while True:
                with self._lock:
                    if self._next is None:  # ends with the lock held: a submit now starts anew
                        self._thread, ended = None, True
                        self._lock.notify_all()
                        return
                    number, data, retries = self._next
                    self._next = None
                failure: Optional[Exception] = None
                try:
                    self._write(data, retries)
                except Exception as error:  # `write` said so; a save that waits raises it
                    failure = error
                with self._lock:
                    self._done, self._failure = number, failure
                    self._lock.notify_all()
        finally:
            if not ended:  # it died (it never does): the next submit starts another
                with self._lock:
                    self._thread = None
                    self._lock.notify_all()
