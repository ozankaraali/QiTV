"""The visualizer's helper process (Plan 4): a local file's sound read far faster than it plays,
as band levels (services.levels) for the audio screen's Stage.

Spawned like the captions process (multiprocessing "spawn", a Pipe; frozen builds stop it at
main.py's freeze_support), one for the song playing, only while the captions don't read it
(ui.visualizer: one decode). QiTV's own libVLC decodes the file alone through its stream output
(no player clock, no sound out):

    #transcode{vcodec=none,acodec=s16l,channels=1,samplerate=16000}:smem{...,no-time-sync}

at 95–290x real time on hel (mp3, flac, m4a, ogg, noise too); the levels themselves are the slow
part (about 70x). A block's place in the file is its pts against the first block's: always
read from the file's start, since `:start-time` lands seconds off there for VBR mp3, flac,
ogg and opus (measured on hel, libVLC 3.0.23); what comes before where the levels are wanted is
skipped unread (a seek far into a song waits a second or so). It reads at most AHEAD_S past the
player, which it is told of, and then waits.

To it: ("decode", from_ms, until_ms) (levels from there, to there; None: to the end; from a
place it has passed it reads again from the start), ("viewer", ms), ("quit",). From it:
("levels", start_ms, frame_ms, blob) chunks (services.levels.LevelStream), ("ended", ms, why)
("end": the file's end; "until": until_ms), ("failed", reason), ("log", level, text). It quits
when its pipe closes (its owner gone too). `LevelsHelper` is the owner's side: everything that
could wait (the spawn, the pipe, the join or kill) happens on its own thread.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import logging
import multiprocessing
from multiprocessing.connection import Connection, wait
import os
import queue
import sys
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

RATE = 16_000
AHEAD_S = 120.0  # read no further past the player
_POLL_S = 0.1
_QUIT_GRACE_S = 0.5  # its pipe closed, it ends within this; else it is killed
_OWNER_GRACE_S = 0.5

_PRERENDER = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t
)
_POSTRENDER = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_size_t,
    ctypes.c_int64,
)
INSTANCE_ARGS = (
    "--quiet",
    "--no-video",
    "--aout=dummy",
    "--no-stats",
    "--no-sub-autodetect-file",
    "--no-lua",
    "--no-media-library",
)


# ---- the owner's side ---------------------------------------------------------------------


class LevelsHelper:
    """The helper process for one file, from `start()` until `stop()`. `on_message(message)`
    hears what it says, on this helper's own thread (plain data)."""

    def __init__(
        self,
        location: str,
        on_message: Callable[[tuple], None],
        *,
        detailed_log: bool = False,
    ) -> None:
        self.location = location
        self._on_message = on_message
        self._detailed_log = detailed_log
        self._outbox: "queue.Queue[tuple]" = queue.Queue()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.pid: Optional[int] = None  # the child's, once it runs
        self.exitcode: Optional[int] = None  # ... once it is gone (0: it quit by itself)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="qitv-levels", daemon=True)
            self._thread.start()

    def decode(self, from_ms: int, until_ms: Optional[int] = None) -> None:
        self._outbox.put(("decode", int(from_ms), None if until_ms is None else int(until_ms)))

    def viewer(self, ms: int) -> None:
        self._outbox.put(("viewer", int(ms)))

    def stop(self) -> None:
        """Its pipe closes and it ends (killed if it doesn't at once); nothing waits here."""
        self._stopping.set()

    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def wait(self, timeout: float) -> bool:
        """Tests and the app's close: wait for it to be gone (True once it is)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.alive()

    def _run(self) -> None:
        if self._stopping.is_set():
            return
        context = multiprocessing.get_context("spawn")
        conn, child = context.Pipe(duplex=True)
        process = context.Process(
            target=helper_main,
            args=(child, self.location, self._detailed_log),
            name="qitv-levels",
            daemon=True,
        )
        try:
            process.start()
        except Exception as error:
            logger.warning("Visualizer: the helper did not start: %s", error)
            self._hear(("failed", type(error).__name__))
            return
        finally:
            child.close()
        self.pid = process.pid
        try:
            self._serve(conn, process)
        finally:
            try:
                conn.close()  # it reads EOF, and ends
            except OSError:
                pass
            process.join(_QUIT_GRACE_S)
            if process.is_alive():
                process.kill()
                process.join(1.0)
            self.exitcode = process.exitcode

    def _serve(self, conn: Connection, process: Any) -> None:
        while not self._stopping.is_set():
            try:
                while True:
                    conn.send(self._outbox.get_nowait())
            except queue.Empty:
                pass
            except OSError, ValueError:
                break
            try:
                if conn.poll(_POLL_S):
                    self._hear(conn.recv())
                    continue
            except EOFError, OSError:
                break
            if not process.is_alive():
                self._hear(("failed", "gone"))
                break

    def _hear(self, message: Any) -> None:
        if self._stopping.is_set() or not isinstance(message, tuple) or not message:
            return
        if message[0] == "log":
            logger.log(int(message[1]), "visualizer: %s", message[2])
            return
        try:
            self._on_message(message)
        except Exception as error:  # its owner went meanwhile
            logger.debug("Visualizer: a message found no one: %s", error)


# ---- the helper process -------------------------------------------------------------------


class _Link:
    """The Pipe, sent to from libVLC's thread and the command loop."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._lock = threading.Lock()

    def send(self, message: tuple) -> None:
        with self._lock:
            try:
                self.conn.send(message)
            except OSError, ValueError, EOFError:
                os._exit(0)  # the owner is gone


class _LogToOwner(logging.Handler):
    def __init__(self, link: _Link) -> None:
        super().__init__()
        self._link = link

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._link.send(("log", record.levelno, record.getMessage()))
        except Exception:
            pass


class FileLevels:
    """A local file's sound as band levels, read by libVLC's stream output from the file's start,
    `send(message)` for each chunk; `decode`, `viewer` and `stop` from one thread (the command
    loop), never from libVLC's (its callback only waits or skips)."""

    def __init__(
        self, location: str, send: Callable[[tuple], None], *, vlc_module: Any = None
    ) -> None:
        from services.levels import LevelStream

        if vlc_module is None:
            import vlc  # the bundled libVLC is loaded first (helper_main)

            vlc_module = vlc
        self._vlc = vlc_module
        self._location = location
        self._send = send
        self._stream = LevelStream(lambda s, f, b: send(("levels", s, f, b)), RATE)
        self._gate = threading.Condition()
        self._from_s = 0.0  # levels from here (what comes before is skipped unread)
        self._until_s: Optional[float] = None
        self._viewer_s = 0.0
        self._at_s = 0.0  # where the sound read last ends
        self._rest: Optional[tuple[float, Any]] = None  # (at, samples) read past `until`, held
        self._pts0: Optional[int] = None
        self._halted = False  # the callback returns at once (a stop, a restart)
        self._reached = False  # until_s was reached: libVLC waits there
        self._told = False  # ... and the owner was told
        self._ended = False  # libVLC read to the file's end (the owner was told)
        self._buffers: dict[int, Any] = {}
        self._instance: Any = None
        self._player: Any = None
        self._callbacks = self._make_callbacks()  # alive as long as libVLC may call them

    # ---- the command loop's side ------------------------------------------------------
    def decode(self, from_ms: int, until_ms: Optional[int]) -> None:
        """Levels from `from_ms` to `until_ms` (None: the end): on from where it reads, or
        from the file's start when that place is behind it (or it read to the file's end)."""
        from_s = max(0.0, from_ms / 1000.0)
        until_s = None if until_ms is None else until_ms / 1000.0
        with self._gate:
            read_to = self._rest[0] if self._rest is not None else self._at_s
            again = self._player is None or self._ended or from_s < read_to - 0.05
            if not again:
                self._from_s, self._until_s = from_s, until_s
                self._reached = until_s is not None and read_to >= until_s
                if not self._reached:
                    self._told = False  # (on past an end it was told of)
                self._gate.notify_all()
        if again:
            self._restart(from_s, until_s)

    def viewer(self, ms: int) -> None:
        with self._gate:
            self._viewer_s = ms / 1000.0
            self._gate.notify_all()

    def poll(self) -> None:
        """The command loop, between commands: has libVLC read to the file's end?"""
        player = self._player
        if player is None or self._ended:
            return
        vlc = self._vlc
        state = player.get_state()
        if state in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped):
            if state == vlc.State.Error:
                logger.info("Visualizer: libVLC could not read the file")
            self._end("end")
        elif self._reached and not self._told:
            self._told = True
            with self._gate:
                self._stream.flush()
                until = self._until_s if self._until_s is not None else self._at_s
            self._send(("ended", round(until * 1000), "until"))

    def stop(self) -> None:
        with self._gate:
            self._halted = True
            self._gate.notify_all()
        player, self._player = self._player, None
        if player is not None:
            player.stop()
            player.release()
        if self._instance is not None:
            self._instance.release()
            self._instance = None
        self._buffers.clear()

    def _restart(self, from_s: float, until_s: Optional[float]) -> None:
        self.stop()
        with self._gate:
            self._stream.flush()
            self._from_s, self._until_s = from_s, until_s
            self._at_s, self._pts0, self._rest = 0.0, None, None
            self._halted = self._reached = self._told = self._ended = False
        vlc = self._vlc
        if self._instance is None:
            self._instance = vlc.Instance(list(INSTANCE_ARGS))
        if "://" in self._location:
            media = self._instance.media_new_location(self._location)
        else:
            media = self._instance.media_new_path(self._location)
        _, _, pre, post = self._callbacks
        address = lambda f: ctypes.cast(f, ctypes.c_void_p).value  # noqa: E731
        media.add_option(
            ":sout=#transcode{vcodec=none,scodec=none,acodec=s16l,channels=1,samplerate="
            f"{RATE}}}:smem{{audio-prerender-callback={address(pre)},"
            f"audio-postrender-callback={address(post)},no-time-sync}}"
        )
        for option in (":no-sout-video", ":no-sout-spu", ":no-sout-smem-time-sync"):
            media.add_option(option)
        player = self._instance.media_player_new()
        player.set_media(media)
        media.release()
        self._player = player
        player.play()

    def _end(self, why: str) -> None:
        """libVLC read to the file's end: what is held goes, and the owner hears where."""
        if self._ended:
            return
        self._ended = True
        with self._gate:
            self._stream.flush()
            at = self._at_s
        self._send(("ended", round(at * 1000), why))

    # ---- libVLC's thread -----------------------------------------------------------------
    def _block(self, pts: int, pcm: bytes) -> None:
        """A decoded block (libVLC's thread): skipped before `from`, read up to `until` (then
        it waits there), and no further than AHEAD_S past the player (it waits there too)."""
        from services.levels import pcm_samples

        with self._gate:
            if self._halted:
                return
            if self._pts0 is None:
                self._pts0 = pts
            at = (pts - self._pts0) / 1e6
            end = at + len(pcm) // 2 / RATE
            if end <= self._from_s:
                self._at_s, self._rest = end, None
                return  # before where the levels are wanted: skipped unread
            self._at_s = at  # (where the sound not read yet begins, while it waits)
            while not self._halted and self._beyond(at):
                self._gate.wait(0.5)  # libVLC waits too: no more read than is wanted
            if self._halted:
                return
            rest, self._rest = self._rest, None
            if rest is not None:
                self._feed(*rest)  # the part of the block before past `until`, read on now
            self._feed(at, pcm_samples(pcm))
            self._at_s = end

    def _feed(self, at: float, samples: Any) -> None:
        """Sound from `at` into the levels: from `from` on, up to `until` (what lies past it is
        held for a stretch asked next, so the two meet with no frame lost)."""
        skip = int(max(0.0, self._from_s - at) * RATE)  # (the block the start is in)
        if skip:
            samples, at = samples[skip:], at + skip / RATE
        until = self._until_s
        if until is not None and at + len(samples) / RATE > until:
            keep = max(0, round((until - at) * RATE))
            self._rest = (at + keep / RATE, samples[keep:])
            samples = samples[:keep]
        if len(samples):
            self._stream.push(at, samples)

    def _beyond(self, at: float) -> bool:
        """`at` is past what is wanted now: `until` (reached: the command loop says so), or
        AHEAD_S past the player (or past where the levels are wanted from: a seek)."""
        until = self._until_s
        if until is not None and at >= until:
            self._reached = True
            return True
        return at > max(self._viewer_s, self._from_s) + AHEAD_S

    def _make_callbacks(self) -> tuple[Any, ...]:
        @_PRERENDER
        def prerender(data: Any, pp: Any, size: int) -> None:
            block = ctypes.create_string_buffer(max(1, size))
            address = ctypes.addressof(block)
            self._buffers[address] = block
            pp[0] = address

        @_POSTRENDER
        def postrender(
            data: Any,
            pcm: Any,
            channels: int,
            rate: int,
            samples: int,
            bits: int,
            size: int,
            pts: int,
        ) -> None:
            block = self._buffers.pop(pcm or 0, None)
            if block is None or self._halted:
                return
            if channels != 1 or rate != RATE or bits != 16:
                logger.warning("Visualizer: libVLC gave %d channels at %d Hz", channels, rate)
                return
            try:
                self._block(int(pts), bytes(block.raw[: int(size)]))
            except Exception as error:  # never into libVLC
                logger.warning("Visualizer: a sound block failed: %s", error)

        return None, None, prerender, postrender


def _lower_priority() -> None:
    try:
        if hasattr(os, "nice"):
            os.nice(10)
        elif sys.platform.startswith("win"):
            kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
    except Exception:
        pass


def _exit_with_owner() -> None:
    parent = multiprocessing.parent_process()
    if parent is not None:
        wait([parent.sentinel])
        time.sleep(_OWNER_GRACE_S)
        os._exit(0)


def helper_main(conn: Connection, location: str, detailed_log: bool = False) -> None:
    """Spawn entry point. It never returns: the process ends with `os._exit`."""
    try:
        with open(os.devnull, "wb") as discard:  # libVLC writes to fd 1 and 2
            os.dup2(discard.fileno(), 1)
            os.dup2(discard.fileno(), 2)
        threading.Thread(target=_exit_with_owner, name="qitv-levels-owner", daemon=True).start()
        _lower_priority()
        from services.vlc_bundle import use_bundled_vlc

        use_bundled_vlc()  # before anything imports vlc, as main.py does
        link = _Link(conn)
        root = logging.getLogger()
        root.handlers[:] = [_LogToOwner(link)]
        root.setLevel(logging.DEBUG if detailed_log else logging.INFO)
        _serve(link, location)
    except BaseException:
        pass
    finally:
        os._exit(0)


def _serve(link: _Link, location: str) -> None:
    levels = FileLevels(location, link.send)
    while True:
        try:
            ready = link.conn.poll(_POLL_S)
            message: Any = link.conn.recv() if ready else None
        except EOFError, OSError:
            break
        if message is None:
            levels.poll()
            continue
        kind = message[0]
        if kind == "quit":
            break
        if kind == "decode":
            levels.decode(int(message[1]), message[2])
        elif kind == "viewer":
            levels.viewer(int(message[1]))
    # (its pipe closed: helper_main ends the process at once, libVLC and all; nothing of it
    # needs stopping first)
