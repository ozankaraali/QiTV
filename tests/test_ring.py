import multiprocessing
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from services.live import ring as ring_module
from services.live.ring import (
    _OWNER,
    _SESSION_PATTERN,
    STALE_S,
    DiskBudget,
    DiskRing,
    RamRing,
    RingError,
    cleanup_session_dir,
    read_index,
    remove_stale_sessions,
)


def hold_session_lock(path, ready, release):
    from PySide6.QtCore import QLockFile

    lock = QLockFile(path)
    lock.setStaleLockTime(0)
    if lock.tryLock(0):
        ready.set()
        release.wait(20)
        lock.unlock()


def session_name(character=None):
    return ".qitv-timeshift-" + (character * 32 if character else secrets.token_hex(16))


DRAIN_HEADER = b"h" * 376
DRAIN_CHUNK = 4096
DRAIN_SEGMENT = len(DRAIN_HEADER) + 16 * DRAIN_CHUNK


def drain_oldest(ring, stop, outcome):
    """Re-read the oldest segment forever, like a stream response lagging behind eviction."""
    seq, offset = None, 0
    try:
        while not stop.is_set():
            oldest = ring.oldest_seq
            if oldest is None:
                continue
            if seq != oldest:
                seq, offset = oldest, 0
            result = ring.read_at(seq, offset, 1024)
            if result is None:
                seq = None
                continue
            outcome["reads"] += 1
            data = result[0]
            offset = offset + len(data) if data else 0
    except Exception as error:  # surfaced by write_while_draining
        outcome["error"] = error


def write_while_draining(ring, segments):
    """Count writes that evicted more than one segment while a reader drains the oldest."""
    stop = threading.Event()
    outcome = {"reads": 0, "error": None}
    reader = threading.Thread(target=drain_oldest, args=(ring, stop, outcome), daemon=True)
    over_evictions = 0
    try:
        for seq in range(segments):
            before = ring.oldest_seq
            ring.open_segment(seq, DRAIN_HEADER)
            if before is not None and ring.oldest_seq - before > 1:
                over_evictions += 1
            if seq == 0:
                reader.start()
            for _ in range(16):
                before = ring.oldest_seq
                ring.append(b"x" * DRAIN_CHUNK)
                if ring.oldest_seq - before > 1:
                    over_evictions += 1
    finally:
        stop.set()
        if reader.is_alive():
            reader.join(5)
    if outcome["error"] is not None:
        raise outcome["error"]
    assert outcome["reads"] > segments, "the reader barely ran"
    return over_evictions


class RamRingTests(unittest.TestCase):
    def test_charged_never_exceeds_cap_and_oldest_completed_go_first(self):
        ring = RamRing(100)
        for seq in range(6):
            ring.open_segment(seq, b"h")
            for _ in range(4):
                ring.append(b"x" * 9)
                self.assertLessEqual(ring.charged, 100)
            ring.complete(seq)
        self.assertLessEqual(ring.charged, 100)
        self.assertEqual(ring.newest_seq, 5)
        self.assertEqual(ring.oldest_seq, 4)
        self.assertIsNone(ring.read_at(0, 0, 1))
        self.assertIsNotNone(ring.read_at(5, 0, 1))

    def test_a_protected_segment_is_never_retired(self):
        # 2.6.2: what the player reads in a server part, and what lies ahead, stays.
        ring = RamRing(10_000)
        for seq in range(6):
            ring.open_segment(seq, b"h")
            ring.append(b"x" * 100)
            ring.complete(seq)
        ring.protect_from(2)
        self.assertTrue(ring.retire_oldest())
        self.assertTrue(ring.retire_oldest())
        self.assertFalse(ring.retire_oldest())
        self.assertEqual(ring.oldest_seq, 2)
        ring.set_max_bytes(10)  # a smaller cap keeps them too
        self.assertEqual(ring.oldest_seq, 2)
        ring.protect_from(None)
        self.assertTrue(ring.retire_oldest())

    def test_a_full_ring_with_only_protected_segments_takes_more_without_failing(self):
        # The writer is paced instead (a disk RingError would send the session to memory).
        ring = RamRing(300)
        for seq in range(2):
            ring.open_segment(seq, b"h")
            ring.append(b"x" * 140)
            ring.complete(seq)
        ring.protect_from(0)
        ring.open_segment(2, b"h")
        ring.append(b"x" * 140)  # over the cap, nothing may go: no RingError
        self.assertEqual(ring.oldest_seq, 0)
        self.assertGreater(ring.charged, 300)

    def test_open_segment_never_evicted_and_oversize_raises(self):
        ring = RamRing(50)
        ring.open_segment(0, b"")
        ring.append(b"a" * 40)
        self.assertEqual(ring.open_seq, 0)
        with self.assertRaises(RingError) as caught:
            ring.append(b"b" * 20)
        self.assertEqual(
            str(caught.exception), "The memory buffer cannot fit one keyframe interval."
        )
        self.assertEqual(ring.open_seq, 0)
        with self.assertRaises(RingError) as latched:
            ring.append(b"c")
        self.assertEqual(str(latched.exception), str(caught.exception))
        self.assertEqual(ring.charged, 40)

    def test_positive_size_required(self):
        with self.assertRaisesRegex(RingError, "must be positive"):
            RamRing(0)

    def test_a_smaller_cap_evicts_the_oldest_at_once_and_keeps_the_open_segment(self):
        # A multiview tile's ring shrinks when it loses the focus, and grows when it has it.
        ring = RamRing(1000)
        for seq in range(5):
            ring.open_segment(seq, b"")
            ring.append(b"x" * 100)
            ring.complete(seq)
        ring.open_segment(5, b"")
        ring.append(b"y" * 150)
        ring.set_max_bytes(320)
        self.assertIsNone(ring.read_at(0, 0, 1))  # gone, as a reader of it learns
        self.assertEqual(ring.wait_for_seq(3, 0.1), "evicted")
        self.assertEqual((ring.oldest_seq, ring.open_seq, ring.max_bytes), (4, 5, 320))
        self.assertLessEqual(ring.charged, 320)
        ring.set_max_bytes(100)  # the open segment alone is larger: it stays, nothing else
        self.assertEqual((ring.oldest_seq, ring.charged), (5, 150))
        ring.set_max_bytes(1000)
        ring.append(b"z" * 500)  # room again
        self.assertEqual(ring.charged, 650)
        with self.assertRaisesRegex(RingError, "must be positive"):
            ring.set_max_bytes(0)
        ring.close()
        ring.set_max_bytes(10)  # a closed ring changes nothing

    def test_open_segment_is_read_progressively(self):
        ring = RamRing(1000)
        ring.open_segment(0, b"HDR")
        ring.append(b"abc")
        ring.append(b"def")
        self.assertEqual(ring.read_at(0, 0, 100), (b"HDRabcdef", False, 3))
        self.assertEqual(ring.read_at(0, 2, 4), (b"Rabc", False, 3))
        self.assertEqual(ring.read_at(0, 9, 4), (b"", False, 3))

    def test_changed_wakes_waiters_on_append_complete_and_close(self):
        ring = RamRing(1000)
        ring.open_segment(0, b"")
        for name, action, ready in (
            ("append", lambda: ring.append(b"x"), lambda: ring.bytes > 0),
            ("complete", lambda: ring.complete(0), lambda: ring.open_seq is None),
            ("close", ring.close, lambda: ring.closed),
        ):
            with self.subTest(name):
                woke = []

                def waiter(ready=ready, woke=woke):
                    with ring.changed:
                        woke.append(ring.changed.wait_for(ready, 5))
                    woke.append(time.monotonic())

                thread = threading.Thread(target=waiter)
                thread.start()
                time.sleep(0.05)
                acted = time.monotonic()
                action()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertTrue(woke[0])
                self.assertLess(woke[1] - acted, 1.0, "the waiter was not notified")

    def test_reads_across_appends_and_slow_reader_never_blocks_writer(self):
        ring = RamRing(10_000)
        ring.open_segment(0, b"")
        expected = bytearray()
        collected = bytearray()

        def reader():
            offset = 0
            while True:
                data, complete, _ = ring.read_at(0, offset, 7)
                if data:
                    offset += len(data)
                    collected.extend(data)
                elif complete:
                    break
                else:
                    with ring.changed:
                        ring.changed.wait(0.05)

        thread = threading.Thread(target=reader)
        thread.start()
        for index in range(200):
            piece = bytes([index % 256]) * (index % 13 + 1)
            expected.extend(piece)
            ring.append(piece)
        ring.complete(0)
        thread.join(5)
        self.assertEqual(bytes(collected), bytes(expected))

    def test_open_segment_auto_completes_previous_and_notifies_on_empty_header(self):
        ring = RamRing(1000)
        ring.open_segment(0, b"a")
        woke = []
        thread = threading.Thread(target=lambda: woke.append(ring.wait_for_seq(1, 5)))
        thread.start()
        time.sleep(0.05)
        ring.open_segment(1, b"")
        thread.join(2)
        self.assertEqual(woke, ["open"])
        self.assertEqual(ring.read_at(0, 0, 0), (b"", True, 1))  # complete
        self.assertEqual(ring.open_seq, 1)

    def test_wait_for_seq_open_evicted_closed_and_timeout(self):
        ring = RamRing(30)
        self.assertEqual(ring.wait_for_seq(0, 0.02), "timeout")
        ring.open_segment(0, b"")
        ring.append(b"a" * 10)
        ring.open_segment(1, b"")
        ring.append(b"b" * 10)
        ring.open_segment(2, b"")
        ring.append(b"c" * 15)
        self.assertIsNone(ring.read_at(0, 0, 1))
        self.assertLess(0, ring.oldest_seq)
        self.assertEqual(ring.wait_for_seq(0, 0.02), "evicted")
        self.assertEqual(ring.wait_for_seq(2, 0.02), "open")
        self.assertEqual(ring.wait_for_seq(3, 0.02), "timeout")
        result = []
        thread = threading.Thread(target=lambda: result.append(ring.wait_for_seq(9, 5)))
        thread.start()
        time.sleep(0.05)
        ring.close()
        thread.join(2)
        self.assertEqual(result, ["closed"])

    def test_public_condition_does_not_deadlock_with_properties(self):
        ring = RamRing(100)
        ring.open_segment(0, b"x")
        with ring.changed:
            self.assertTrue(ring.changed.wait_for(lambda: ring.newest_seq is not None, 1))

    def test_create_failure_still_completes_and_notifies_previous(self):
        ring = RamRing(1000)
        ring.open_segment(0, b"a")
        with patch.object(ring, "_create", side_effect=OSError("x")):
            with self.assertRaises(RingError):
                ring.open_segment(1, b"b")
        self.assertEqual(ring.read_at(0, 0, 0), (b"", True, 1))  # complete
        self.assertIsNone(ring.open_seq)

    def test_append_without_open_segment_and_after_close(self):
        ring = RamRing(100)
        with self.assertRaises(RingError):
            ring.append(b"x")
        ring.open_segment(0, b"h")
        ring.close()
        with self.assertRaises(RingError):
            ring.append(b"x")
        self.assertEqual(ring.charged, 0)

    def test_read_at_returns_data_completion_and_header_length(self):
        ring = RamRing(1000)
        ring.open_segment(0, b"hh")
        ring.append(b"abc")
        self.assertEqual(ring.read_at(0, 0, 100), (b"hhabc", False, 2))
        self.assertEqual(ring.read_at(0, 2, 2), (b"ab", False, 2))
        self.assertEqual(ring.read_at(0, 5, 10), (b"", False, 2))
        self.assertEqual(ring.read_at(0, 0, 0), (b"", False, 2))
        self.assertIsNone(ring.read_at(1, 0, 10))  # not opened yet
        ring.complete(0)
        self.assertEqual(ring.read_at(0, 4, 10), (b"c", True, 2))

    def test_read_at_returns_none_for_evicted_and_closed_segments(self):
        ring = RamRing(25)
        ring.open_segment(0, b"")
        ring.append(b"a" * 10)
        ring.open_segment(1, b"")
        ring.append(b"b" * 10)
        ring.open_segment(2, b"")
        ring.append(b"c" * 10)
        self.assertEqual(ring.charged, 20)  # an evicted segment is freed at once
        self.assertIsNone(ring.read_at(0, 0, 10))  # evicted
        self.assertEqual(ring.read_at(1, 0, 10), (b"b" * 10, True, 0))
        self.assertEqual(ring.read_at(2, 0, 10), (b"c" * 10, False, 0))
        ring.close()
        self.assertIsNone(ring.read_at(2, 0, 10))  # closed

    def test_draining_reader_never_causes_over_eviction(self):
        ring = RamRing(DRAIN_SEGMENT * 5 // 2)
        # A lease-based reader over-evicts several times per 1000 segments.
        over_evictions = write_while_draining(ring, segments=1000)
        self.assertEqual(over_evictions, 0, "a reader pinned an evicted segment")

    def test_draining_reader_never_makes_a_tight_ram_ring_fail(self):
        # One completed segment plus the open one: a pinned segment would not fit.
        ring = RamRing(DRAIN_SEGMENT * 3 // 2)
        self.assertEqual(write_while_draining(ring, segments=1000), 0)
        self.assertLessEqual(ring.charged, DRAIN_SEGMENT * 3 // 2)


class DiskRingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qitv-ring-test-")
        self.directory = Path(self.temporary.name)
        self.rings = []

    def tearDown(self):
        for ring in self.rings:
            ring.close()
        self.temporary.cleanup()

    def ring(self, max_bytes=12 * 1024, name=None):
        ring = DiskRing(self.directory, name or session_name(), max_bytes)
        self.rings.append(ring)
        return ring

    def test_cap_counts_open_segment_disk_blocks_and_eviction_frees_at_once(self):
        ring = self.ring()
        unit = ring._unit
        ring._max_bytes = 3 * unit

        def path(seq):
            return ring._directory / f"segment-{seq}.ts"

        ring.open_segment(0, bytes(unit))
        ring.complete(0)
        ring.open_segment(1, bytes(unit))
        ring.complete(1)
        ring.open_segment(2, bytes(unit))
        self.assertEqual(ring.oldest_seq, 0, "Age alone must not evict history that fits")
        ring.append(b"x")  # a second block for the open segment: the oldest goes
        self.assertEqual(ring.oldest_seq, 1)
        self.assertFalse(path(0).exists())
        self.assertTrue(path(1).exists())
        self.assertEqual(ring.charged, 3 * unit)
        self.assertEqual(ring.bytes, 2 * unit + 1)
        before = path(2).stat().st_size
        with self.assertRaises(RingError):
            ring.append(bytes(3 * unit))  # the open segment alone would pass the cap
        self.assertEqual(path(2).stat().st_size, before)
        self.assertLessEqual(ring.charged, ring._max_bytes)
        self.assertEqual(ring.read_at(2, 0, 0), (b"", False, unit))
        self.assertEqual(ring.bytes, unit + 1)
        directory = ring._directory
        ring.close()
        self.assertFalse(directory.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_stale_owned_sessions_only_and_active_sibling_lock_survives(self):
        stale_directories = []
        for character, newline in (("a", "\n"), ("d", "\r\n")):
            stale = self.directory / session_name(character)
            stale.mkdir()
            (stale / "owner").write_text(_OWNER, encoding="ascii", newline=newline)
            (stale / "segment-0.ts").write_bytes(b"old recording")
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os,sys; from PySide6.QtCore import QLockFile; "
                    "lock=QLockFile(sys.argv[1]); assert lock.tryLock(0); os._exit(0)",
                    str(stale / "session.lock"),
                ],
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(child.returncode, 0, child.stderr.decode())
            stale_directories.append(stale)
        unrelated = self.directory / session_name("b")
        unrelated.mkdir()
        (unrelated / "owner").write_text("not owned", encoding="ascii")
        outside = self.directory / "user-data"
        outside.mkdir()
        (outside / "precious").write_bytes(b"keep")
        link = self.directory / session_name("c")
        if os.name != "nt":
            link.symlink_to(outside, target_is_directory=True)
        active = self.ring()
        active_directory = active._directory
        second = self.ring()
        for stale in stale_directories:
            self.assertFalse(stale.exists())
        self.assertTrue(active_directory.exists())
        self.assertTrue((active_directory / "session.lock").exists())
        self.assertEqual((unrelated / "owner").read_text(), "not owned")
        self.assertEqual((outside / "precious").read_bytes(), b"keep")
        if os.name != "nt":
            self.assertTrue(link.is_symlink())
        for ring in (active, second):
            ring.close()
        self.assertFalse(active_directory.exists())

    def test_reserve_checked_at_start_and_on_growth(self):
        real = __import__("shutil").disk_usage
        with patch("services.live.ring.shutil.disk_usage") as usage:
            usage.return_value = real(self.directory)._replace(free=1024)
            with self.assertRaises(RingError) as caught:
                self.ring()
            self.assertEqual(
                str(caught.exception), "Not enough free disk space for the temporary buffer."
            )
        self.assertEqual(list(self.directory.iterdir()), [])
        ring = self.ring(max_bytes=64 * 1024 * 1024)
        ring.open_segment(0, b"x")
        with patch("services.live.ring.shutil.disk_usage") as usage:
            usage.return_value = real(self.directory)._replace(free=64 * 1024 * 1024)
            with self.assertRaises(RingError) as caught:
                ring.append(b"y" * (ring._unit + 1))
            self.assertEqual(
                str(caught.exception), "Recording stopped to preserve free disk space."
            )

    def test_read_across_appends_and_progressive_open_segment(self):
        ring = self.ring()
        ring.open_segment(0, b"HDR")
        ring.append(b"abc")
        self.assertEqual(ring.read_at(0, 0, 100), (b"HDRabc", False, 3))
        ring.append(b"def")
        self.assertEqual(ring.read_at(0, 4, 100), (b"bcdef", False, 3))
        ring.complete(0)
        self.assertEqual(ring.read_at(0, 9, 100), (b"", True, 3))

    def test_teardown_leaves_nothing_behind(self):
        ring = self.ring()
        ring.open_segment(0, b"a")
        ring.complete(0)
        ring.open_segment(1, b"b")
        directory = ring._directory
        ring.close()
        self.assertFalse(directory.exists())
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(ring.charged, 0)

    def test_invalid_name_and_size(self):
        with self.assertRaises(RingError):
            DiskRing(self.directory, "bad", 1024)
        with self.assertRaisesRegex(RingError, "must be positive"):
            DiskRing(self.directory, session_name(), 0)

    def test_read_at_on_disk_never_pins_and_maps_failures_to_ring_error(self):
        ring = self.ring(max_bytes=DRAIN_SEGMENT * 5 // 2 + 4 * 4096)
        self.assertEqual(write_while_draining(ring, segments=60), 0)
        self.assertEqual(ring.read_at(59, 0, 3), (b"hhh", False, len(DRAIN_HEADER)))
        ring.complete(59)  # closes the writer handle so Windows can unlink
        (ring._directory / "segment-59.ts").unlink()
        with self.assertRaises(RingError) as caught:
            ring.read_at(59, 0, 3)
        self.assertEqual(str(caught.exception), "The temporary buffer could not be read.")

    def test_stop_aborts_manager_lock_wait(self):
        from PySide6.QtCore import QLockFile

        manager = QLockFile(str(self.directory / ".qitv-timeshift-manager.lock"))
        manager.setStaleLockTime(0)
        self.assertTrue(manager.tryLock(0))
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        start = time.monotonic()
        try:
            with self.assertRaises(RingError) as caught:
                DiskRing(self.directory, session_name(), 1024 * 1024, stop)
        finally:
            manager.unlock()
        self.assertEqual(str(caught.exception), "Recording stopped.")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_finish_failure_latches_ring_error(self):
        ring = self.ring()
        ring.open_segment(0, b"a")
        with patch.object(ring, "_finish", side_effect=OSError("boom")):
            with self.assertRaises(RingError) as caught:
                ring.open_segment(1, b"b")
        self.assertEqual(
            str(caught.exception), "The temporary disk could not complete a buffer write."
        )
        with self.assertRaises(RingError):
            ring.append(b"x")

    def test_remove_stale_sessions_sweeps_under_the_manager_lock(self):
        # Run once at startup, so a crashed run's buffers go even if no disk session starts.
        from PySide6.QtCore import QLockFile

        def stale(character):
            directory = self.directory / session_name(character)
            directory.mkdir()
            (directory / "owner").write_text(_OWNER, encoding="ascii")
            (directory / "segment-0.ts").write_bytes(b"left over")
            return directory

        active = self.ring()
        first = stale("a")
        remove_stale_sessions(self.directory)
        self.assertFalse(first.exists())
        self.assertTrue(active._directory.exists(), "a live session was swept")
        second = stale("b")
        manager = QLockFile(str(self.directory / ".qitv-timeshift-manager.lock"))
        manager.setStaleLockTime(0)
        self.assertTrue(manager.tryLock(0))
        try:
            remove_stale_sessions(self.directory)  # busy: a disk session start sweeps instead
        finally:
            manager.unlock()
        self.assertTrue(second.exists())
        remove_stale_sessions(self.directory / "missing")  # no buffer root yet: nothing to do

    def test_cleanup_session_dir_reclaims_dead_owner(self):
        name = session_name()
        dead = self.directory / name
        dead.mkdir()
        (dead / "owner").write_text(_OWNER, encoding="ascii")
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; from PySide6.QtCore import QLockFile; "
                "lock=QLockFile(sys.argv[1]); assert lock.tryLock(0); os._exit(0)",
                str(dead / "session.lock"),
            ],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 0, child.stderr.decode())
        self.assertTrue(cleanup_session_dir(self.directory, name))
        self.assertFalse(dead.exists())

    def test_cleanup_session_dir_removes_dead_helper_dir(self):
        name = session_name()
        dead = self.directory / name
        dead.mkdir()
        (dead / "owner").write_text(_OWNER, encoding="ascii")
        (dead / "segment-3.ts").write_bytes(b"left over")
        self.assertTrue(cleanup_session_dir(self.directory, name))
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertTrue(cleanup_session_dir(self.directory, name))

    def test_cleanup_session_dir_refuses_unowned_and_symlink(self):
        name = session_name()
        folder = self.directory / name
        folder.mkdir()
        (folder / "mine.txt").write_text("user")
        with self.assertRaises(OSError):
            cleanup_session_dir(self.directory, name)
        self.assertTrue((folder / "mine.txt").exists())

    def test_cleanup_session_dir_leaves_live_lock_alone(self):
        name = session_name()
        live = self.directory / name
        live.mkdir()
        (live / "owner").write_text(_OWNER, encoding="ascii")
        (live / "segment-0.ts").write_bytes(b"playing")
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        child = context.Process(
            target=hold_session_lock, args=(str(live / "session.lock"), ready, release)
        )
        child.start()
        try:
            self.assertTrue(ready.wait(20), "child did not take the lock")
            self.assertFalse(cleanup_session_dir(self.directory, name))
            self.assertEqual((live / "segment-0.ts").read_bytes(), b"playing")
            self.assertTrue((live / "owner").exists())
        finally:
            release.set()
            child.join(10)
        self.assertEqual(child.exitcode, 0)
        self.assertTrue(cleanup_session_dir(self.directory, name))
        self.assertFalse(live.exists())


if __name__ == "__main__":
    unittest.main()


class DiskBudgetTests(unittest.TestCase):
    """2.6: every disk buffer shares the user's disk amount; kept (parked) ones give way first,
    the oldest first, and nothing older than a day stays."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-budget-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.now = 1_000_000.0
        clock = patch.object(ring_module, "_wall_time", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def ring(self):
        ring = DiskRing(self.directory, session_name(), 64 * 1024 * 1024)
        self.addCleanup(ring.close)
        return ring

    def fill(self, ring, count, *, size=8192):
        """`count` completed segments, a second apart on the wall clock."""
        start = ring.newest_seq + 1 if ring.newest_seq is not None else 0
        for seq in range(start, start + count):
            ring.open_segment(seq, b"h" * 376)
            ring.append(b"d" * size)
            ring.complete(seq)
            self.now += 1.0

    def test_kept_rings_give_way_first(self):
        kept, live = self.ring(), self.ring()
        self.fill(kept, 3)
        self.fill(live, 3)
        budget = DiskBudget(kept.charged + live.charged)
        budget.register(kept, parked=True)
        budget.register(live)
        self.fill(live, 1)
        emptied = budget.trim()
        self.assertEqual(kept.oldest_seq, 1)  # kept's oldest went ...
        self.assertEqual(live.oldest_seq, 0)  # ... the channel watched kept all of its own
        self.assertLessEqual(kept.charged + live.charged, budget.max_bytes)
        self.assertEqual(emptied, [kept])  # it changed: its owner says so

    def test_the_oldest_kept_goes_first(self):
        older, newer, live = self.ring(), self.ring(), self.ring()
        self.fill(older, 2)
        self.fill(newer, 2)
        self.fill(live, 1)
        budget = DiskBudget(older.charged + newer.charged + live.charged)
        for ring in (older, newer):
            budget.register(ring, parked=True)
        budget.register(live)
        self.fill(live, 1)
        budget.trim()
        self.assertEqual((older.oldest_seq, newer.oldest_seq), (1, 0))

    def test_without_kept_rings_the_oldest_of_any_goes(self):
        first, second = self.ring(), self.ring()
        self.fill(first, 2)
        self.fill(second, 2)
        budget = DiskBudget(first.charged + second.charged)
        budget.register(first)
        budget.register(second)
        self.fill(second, 1)
        budget.trim()
        self.assertEqual((first.oldest_seq, second.oldest_seq), (1, 0))

    def test_the_disk_budget_passes_over_a_protected_ring(self):
        # 2.6.2: a parked ring whose oldest is protected is no victim; the next one is.
        kept, live = self.ring(), self.ring()
        self.fill(kept, 3)
        self.fill(live, 3)
        budget = DiskBudget(kept.charged + live.charged)
        budget.register(kept, parked=True)
        budget.register(live)
        kept.protect_from(0)
        self.fill(live, 1)
        budget.trim()
        self.assertEqual(kept.oldest_seq, 0)
        self.assertEqual(live.oldest_seq, 1)

    def test_a_ring_that_unregisters_no_longer_counts(self):
        kept, live = self.ring(), self.ring()
        self.fill(kept, 2)
        self.fill(live, 2)
        budget = DiskBudget(live.charged)
        budget.register(kept, parked=True)
        budget.register(live)
        budget.unregister(kept)
        self.assertEqual(budget.trim(), [])
        self.assertEqual(kept.oldest_seq, 0)

    def test_a_day_old_segment_goes(self):
        ring = self.ring()
        self.fill(ring, 3)
        self.now += STALE_S - 2.5  # the first is over a day old, the others not yet
        self.assertEqual(ring.retire_before(self.now - STALE_S), 1)
        self.assertEqual(ring.oldest_seq, 1)
        self.assertEqual(STALE_S, 24 * 3600)

    def test_the_open_segment_never_goes(self):
        ring = self.ring()
        self.fill(ring, 1)
        ring.open_segment(1, b"h" * 376)
        self.now += 2 * STALE_S
        self.assertEqual(ring.retire_before(self.now - STALE_S), 1)
        self.assertEqual(ring.oldest_seq, 1)

    def test_a_kept_ring_whose_files_went_is_let_go(self):
        # Review I4: a segment that cannot be deleted never stops the trim; that ring is
        # closed (its owner says so), and the accounting stays right.
        kept, live = self.ring(), self.ring()
        self.fill(kept, 2)
        self.fill(live, 2)
        budget = DiskBudget(kept.charged + live.charged)
        budget.register(kept, parked=True)
        budget.register(live)
        with patch.object(DiskRing, "_unlink", side_effect=OSError("gone")):
            self.fill(live, 1)
            changed = budget.trim()
        self.assertEqual(changed, [kept])
        self.assertTrue(kept.closed)
        self.assertEqual(live.oldest_seq, 0)
        self.assertLessEqual(live.charged, budget.max_bytes)


def kept_name():
    return ".qitv-buffer-" + secrets.token_hex(16)


class IndexTests(unittest.TestCase):
    """2.6: a kept buffer's folder (v2) carries an index, so a later helper can take it back."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-index-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def ring(self, name=None):
        ring = DiskRing(self.root, name or kept_name(), 64 * 1024 * 1024)
        self.addCleanup(ring.close)
        return ring

    @staticmethod
    def fill(ring, count, start_seq=0):
        for seq in range(start_seq, start_seq + count):
            ring.open_segment(seq, b"h" * 376, junction=seq == 2)
            ring.append(b"d" * 4000)
            ring.complete(seq)
            ring.note_segment(seq, 2.0 * seq, 2.0 * seq + 2.0, seq == 2, "key")

    def folder(self, ring):
        return self.root / ring.dir_name

    def test_the_index_follows_the_ring(self):
        ring = self.ring()
        ring.note_part(0, 0.0, 1_000.0)
        self.fill(ring, 2)
        ring.note_part(2, 4.0, 9_000.0)
        self.fill(ring, 1, start_seq=2)
        ring.note_label({"channel": "k1"})
        ring.note_where({"position": 3.0, "paused": True})
        index = read_index(self.folder(ring))
        self.assertEqual([s["seg"] for s in index.segments], [0, 1, 2])
        third = index.segments[2]
        self.assertEqual((third["h"], third["n"], third["s"], third["e"]), (376, 4376, 4.0, 6.0))
        self.assertTrue(third["d"] and third["j"])
        self.assertGreater(third["w"], 0)
        self.assertEqual(
            [(p["part"], p["s"], p["w"]) for p in index.parts],
            [(0, 0.0, 1_000.0), (2, 4.0, 9_000.0)],
        )
        self.assertEqual(index.label, {"channel": "k1"})
        self.assertEqual(index.where, {"position": 3.0, "paused": True})

    def test_the_index_keeps_a_segments_media_sequence(self):
        # 2.6.2: a segment that begins an HLS segment notes its media sequence ("m").
        ring = self.ring()
        ring.open_segment(0, b"h" * 376)
        ring.append(b"d" * 4000)
        ring.complete(0)
        ring.note_segment(0, 0.0, 2.0, False, "key", media_seq=42)
        self.fill(ring, 1, start_seq=1)  # one without
        segments = read_index(self.folder(ring)).segments
        self.assertEqual([s.get("m") for s in segments], [42, None])

    def test_a_torn_last_line_is_ignored(self):
        ring = self.ring()
        self.fill(ring, 3)
        with open(self.folder(ring) / "index.jsonl", "ab") as handle:
            handle.write(b'{"seg": 3, "h')
        self.assertEqual([s["seg"] for s in read_index(self.folder(ring)).segments], [0, 1, 2])

    def test_a_missing_file_keeps_the_newest_run_after_it(self):
        ring = self.ring()
        self.fill(ring, 4)
        (self.folder(ring) / "segment-1.ts").unlink()
        self.assertEqual([s["seg"] for s in read_index(self.folder(ring)).segments], [2, 3])

    def test_a_short_file_counts_as_missing(self):
        ring = self.ring()
        self.fill(ring, 3)
        with open(self.folder(ring) / "segment-2.ts", "r+b") as handle:
            handle.truncate(100)
        self.assertEqual(read_index(self.folder(ring)).segments, [])

    def test_retired_segments_leave_the_index_and_it_stays_small(self):
        ring = self.ring()
        self.fill(ring, 200)
        for _ in range(190):
            ring.retire_oldest()
        index = read_index(self.folder(ring))
        self.assertEqual([s["seg"] for s in index.segments], list(range(190, 200)))
        lines = (self.folder(ring) / "index.jsonl").read_text().splitlines()
        self.assertLess(len(lines), 200)  # compacted on the way

    def test_close_keep_leaves_the_folder_unlocked(self):
        from PySide6.QtCore import QLockFile

        ring = self.ring()
        self.fill(ring, 2)
        folder = self.folder(ring)
        ring.close(keep=True)
        self.assertTrue((folder / "segment-1.ts").exists())
        lock = QLockFile(str(folder / "session.lock"))
        lock.setStaleLockTime(0)
        self.assertTrue(lock.tryLock(0))  # a later helper can take it
        lock.unlock()
        self.assertEqual(len(read_index(folder).segments), 2)

    def test_a_plain_close_removes_it(self):
        ring = self.ring()
        self.fill(ring, 2)
        folder = self.folder(ring)
        ring.close()
        self.assertFalse(folder.exists())

    def test_sweeps_for_old_buffers_leave_it_alone(self):
        ring = self.ring()
        self.fill(ring, 2)
        folder = self.folder(ring)
        ring.close(keep=True)  # its owner is gone, its lock free
        remove_stale_sessions(self.root)
        self.assertTrue(folder.exists())
        self.assertIsNone(_SESSION_PATTERN.fullmatch(folder.name))  # older builds never match

    def test_a_memory_ring_takes_notes_and_keeps_nothing(self):
        ring = RamRing(1 << 20)
        ring.note_part(0, 0.0, 1.0)
        ring.note_label({"channel": "k1"})
        ring.close(keep=True)

    @unittest.skipUnless(os.path.isdir("/dev/fd"), "counts open files through /dev/fd")
    def test_a_kept_buffer_holds_one_file_open(self):
        # Review B I5: its lock only; the index is opened for each line.
        before = len(os.listdir("/dev/fd"))
        rings = [self.ring() for _ in range(5)]
        for ring in rings:
            self.fill(ring, 2)
            ring.note_label({"channel": "k"})
        self.assertEqual(len(os.listdir("/dev/fd")) - before, 5)

    def test_dropping_the_open_segment_closes_its_file(self):
        ring = self.ring()
        ring.open_segment(0, b"h" * 376)
        ring.append(b"d" * 100)
        ring.drop_open()
        self.assertIsNone(ring._file)
        self.assertFalse((self.folder(ring) / "segment-0.ts").exists())
        self.assertEqual((ring.charged, ring.bytes), (0, 0))
