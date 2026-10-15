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

from services.live.ring import (
    _OWNER,
    DiskRing,
    RamRing,
    RingError,
    Segment,
    cleanup_session_dir,
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
        self.assertGreater(ring.oldest_seq, 0)
        self.assertIsNone(ring.lease(0))
        self.assertIsNotNone(ring.lease(5))

    def test_open_segment_never_evicted_and_oversize_raises(self):
        ring = RamRing(50)
        ring.open_segment(0, b"")
        ring.append(b"a" * 40)
        self.assertEqual(ring.open_seq, 0)
        with self.assertRaises(RingError) as caught:
            ring.append(b"b" * 20)
        self.assertEqual(
            str(caught.exception),
            "The disk buffer limit cannot fit one keyframe interval or an active read.",
        )
        self.assertEqual(ring.open_seq, 0)
        self.assertEqual(ring.charged, 40)

    def test_positive_size_required(self):
        with self.assertRaisesRegex(RingError, "must be positive"):
            RamRing(0)

    def test_leased_retired_segment_survives_until_release(self):
        ring = RamRing(30)
        ring.open_segment(0, b"")
        ring.append(b"a" * 10)
        ring.complete(0)
        lease = ring.lease(0)
        self.assertIsInstance(lease, Segment)
        ring.open_segment(1, b"")
        ring.append(b"b" * 10)
        ring.complete(1)
        ring.open_segment(2, b"")
        ring.append(b"c" * 10)
        ring.append(b"d" * 5)
        self.assertTrue(lease.retired)
        self.assertEqual(ring.read(lease, 0, 10), b"a" * 10)
        self.assertEqual(ring.charged, 25)
        ring.release(lease)
        self.assertEqual(ring.charged, 15)
        self.assertIsNone(ring.lease(0))

    def test_open_segment_can_be_leased_and_read_progressively(self):
        ring = RamRing(1000)
        ring.open_segment(0, b"HDR")
        lease = ring.lease(0)
        ring.append(b"abc")
        ring.append(b"def")
        self.assertEqual(ring.read(lease, 0, 100), b"HDRabcdef")
        self.assertEqual(ring.read(lease, 2, 4), b"Rabc")
        self.assertEqual(ring.read(lease, 9, 4), b"")

    def test_wait_for_wakes_on_append_complete_and_close(self):
        ring = RamRing(1000)
        ring.open_segment(0, b"")
        seg = ring.lease(0)
        self.assertFalse(ring.wait_for(seg, 0, 0.05))
        results = []

        def waiter(offset):
            results.append(ring.wait_for(seg, offset, 5))

        for action, offset in (
            (lambda: ring.append(b"x"), 0),
            (lambda: ring.complete(0), 1),
        ):
            thread = threading.Thread(target=waiter, args=(offset,))
            thread.start()
            time.sleep(0.05)
            action()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True, True])
        ring2 = RamRing(1000)
        ring2.open_segment(0, b"")
        seg2 = ring2.lease(0)
        thread = threading.Thread(target=lambda: results.append(ring2.wait_for(seg2, 0, 5)))
        thread.start()
        time.sleep(0.05)
        ring2.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[-1])

    def test_reads_across_appends_and_slow_reader_never_blocks_writer(self):
        ring = RamRing(10_000)
        ring.open_segment(0, b"")
        seg = ring.lease(0)
        expected = bytearray()
        collected = bytearray()
        done = threading.Event()

        def reader():
            offset = 0
            while not done.is_set() or offset < seg.size:
                if ring.wait_for(seg, offset, 0.05):
                    chunk = ring.read(seg, offset, 7)
                    offset += len(chunk)
                    collected.extend(chunk)
                    if seg.complete and not chunk:
                        break

        thread = threading.Thread(target=reader)
        thread.start()
        for index in range(200):
            piece = bytes([index % 256]) * (index % 13 + 1)
            expected.extend(piece)
            ring.append(piece)
        ring.complete(0)
        done.set()
        thread.join(5)
        self.assertEqual(bytes(collected), bytes(expected))

    def test_append_without_open_segment_and_after_close(self):
        ring = RamRing(100)
        with self.assertRaises(RingError):
            ring.append(b"x")
        ring.open_segment(0, b"h")
        ring.close()
        with self.assertRaises(RingError):
            ring.append(b"x")
        self.assertEqual(ring.charged, 0)


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

    def test_cap_counts_open_segment_disk_blocks_and_evicted_reader_lease(self):
        ring = self.ring()
        unit = ring._unit
        ring._max_bytes = 3 * unit

        def path(seq):
            return ring._directory / f"segment-{seq}.ts"

        ring.open_segment(0, bytes(unit))
        first = ring.lease(0)
        ring.complete(0)
        ring.open_segment(1, bytes(unit))
        ring.complete(1)
        second = ring._segments[1]
        self.assertFalse(first.retired, "Age alone must not evict history that fits in the cache")
        self.assertEqual(ring.oldest_seq, 0)
        ring.open_segment(2, bytes(unit))
        current = ring._segments[2]
        ring.append(b"x")
        self.assertTrue(first.retired)
        self.assertTrue(path(0).exists(), "leased bytes disappeared before reader closed")
        self.assertFalse(path(1).exists())
        self.assertTrue(second.retired)
        self.assertEqual(ring.charged, 3 * unit)
        self.assertEqual(ring.bytes, 2 * unit + 1)
        ring.release(first)
        self.assertFalse(path(0).exists())
        self.assertEqual(ring.bytes, unit + 1)
        before = path(2).stat().st_size
        with self.assertRaises(RingError):
            ring.append(bytes(3 * unit))
        self.assertEqual(path(2).stat().st_size, before)
        self.assertLessEqual(ring.charged, ring._max_bytes)
        self.assertEqual(current.size, unit + 1)
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
        seg = ring.lease(0)
        ring.append(b"abc")
        self.assertEqual(ring.read(seg, 0, 100), b"HDRabc")
        ring.append(b"def")
        self.assertEqual(ring.read(seg, 4, 100), b"bcdef")
        self.assertFalse(ring.wait_for(seg, 9, 0.05))
        ring.complete(0)
        self.assertTrue(ring.wait_for(seg, 9, 0.05))
        ring.release(seg)

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

    def test_cleanup_session_dir_removes_dead_helper_dir(self):
        name = session_name()
        dead = self.directory / name
        dead.mkdir()
        (dead / "owner").write_text(_OWNER, encoding="ascii")
        (dead / "segment-3.ts").write_bytes(b"left over")
        cleanup_session_dir(self.directory, name)
        self.assertEqual(list(self.directory.iterdir()), [])
        cleanup_session_dir(self.directory, name)

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
            cleanup_session_dir(self.directory, name)
            self.assertEqual((live / "segment-0.ts").read_bytes(), b"playing")
            self.assertTrue((live / "owner").exists())
        finally:
            release.set()
            child.join(10)
        cleanup_session_dir(self.directory, name)
        self.assertFalse(live.exists())


if __name__ == "__main__":
    unittest.main()
