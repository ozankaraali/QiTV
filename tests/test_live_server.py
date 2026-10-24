"""Progressive loopback stream server: routes, live edge, slow readers and stop.

Port of main's loopback server tests (reverse-DNS-free bind, half-sent headers) plus the
progressive close-delimited stream across ring segments.
"""

import contextlib
import errno
import io
import logging
import os
from pathlib import Path
import secrets
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from services.live import server as server_module
from services.live.ring import DiskRing, RamRing, RingError
from services.live.server import StreamServer

QUIET_ERRORS = (ConnectionResetError, ConnectionAbortedError)
PATTERN = bytes(range(251)) * 4200  # about 1 MiB; 251 is prime, so offsets never align
# macOS autotunes loopback receive buffers to about 0.5 MiB even when SO_RCVBUF is set,
# so a paused reader only blocks mid-segment when segments and backlogs are larger.
SEGMENT = 2 * 1024 * 1024
LIVE = 0.6  # appends reach a reader waiting at the live edge within this many seconds
APPEND = 64 * 1024
BACKLOG = 4 * 1024 * 1024
# One completed segment plus the open one: a segment pinned by a reader would not fit, so
# any pin makes the writer raise.
CAP = SEGMENT * 3 // 2


def header(seq):
    """Two TS-packet-sized stand-ins for a segment's PAT/PMT copies."""
    return (bytes([0x47, 0x40 | seq % 32, seq % 256]) * 126)[:376]


def payload(seq, start, size):
    offset = (seq * 31 + start) % 251
    return PATTERN[offset : offset + size]


def fails_after_first_check(error):
    """An `is_ended` that answers once, so the response starts, then raises `error`."""
    calls = []

    def is_ended():
        calls.append(None)
        if len(calls) > 1:
            raise error
        return False

    return is_ended


class Writer:
    """Appends known bytes and records the progressive stream a reader should receive."""

    def __init__(self, ring):
        self.ring = ring
        self.seq = -1
        self.written = {}

    def open(self, seq=None):
        self.seq = self.seq + 1 if seq is None else seq
        self.ring.open_segment(self.seq, header(self.seq))
        self.written[self.seq] = bytearray()

    def append(self, size):
        data = bytearray()
        while len(data) < size:
            part = payload(self.seq, len(self.written[self.seq]) + len(data), size - len(data))
            data += part
        self.ring.append(bytes(data))
        self.written[self.seq] += data
        return bytes(data)

    def stream(self, first):
        return header(first) + b"".join(
            bytes(self.written[seq]) for seq in sorted(self.written) if seq >= first
        )


class Client:
    """A raw HTTP client so tests control the receive buffer and when it reads."""

    def __init__(self, url, *, rcvbuf=None, timeout=5.0):
        parts = urlsplit(url)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.settimeout(timeout)
        self.sock.connect((parts.hostname, parts.port))
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        request = f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nUser-Agent: test\r\n\r\n"
        self.sock.sendall(request.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError("the server closed before sending response headers")
            data += chunk
        head, _, self.buffer = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()
        self.received = len(self.buffer)
        self.eof = False

    def _recv(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the server did not send in time")
        self.sock.settimeout(remaining)
        try:
            chunk = self.sock.recv(65536)
        except QUIET_ERRORS:
            # Windows reports a closed loopback peer as a reset or abort (port map 8.5).
            chunk = b""
        if not chunk:
            self.eof = True
        self.received += len(chunk)
        self.buffer += chunk

    def read(self, size, timeout=2.0):
        """Exactly `size` body bytes within `timeout`, or a TimeoutError."""
        deadline = time.monotonic() + timeout
        while len(self.buffer) < size and not self.eof:
            self._recv(deadline)
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def read_to_eof(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.eof:
            self._recv(deadline)
        data, self.buffer = self.buffer, b""
        return data

    def close(self):
        self.sock.close()


class StreamServerTests(unittest.TestCase):
    def start_server(self):
        server = StreamServer()
        server.start()
        self.addCleanup(server.stop)
        return server

    def client(self, url, **kwargs):
        client = Client(url, **kwargs)
        self.addCleanup(client.close)
        return client

    def assert_not_found(self, url):
        client = self.client(url)
        self.assertEqual(client.status, 404, url)
        self.assertEqual(client.headers.get("content-length"), "0")
        self.assertEqual(client.read_to_eof(2.0), b"")

    @staticmethod
    def small_send_buffer(server):
        # Accepted sockets inherit the listener's buffer size; a small one makes a paused
        # reader block the response write quickly on every platform.
        server._server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)

    def test_bind_and_serving_never_need_reverse_dns(self):
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(1000)
        writer.ring.complete(0)
        with patch("socket.getfqdn", side_effect=OSError("Reverse DNS unavailable")):
            server = StreamServer()
            base = server.start()
            try:
                server.add_session("dns", writer.ring, lambda: True)
                client = self.client(server.stream_url("dns", 0))
                self.assertEqual(client.status, 200)
                self.assertEqual(client.read_to_eof(2.0), writer.stream(0))
            finally:
                server.stop()
        self.assertRegex(base, r"^http://127\.0\.0\.1:[0-9]+/[0-9a-f]{32}$")
        self.assertEqual(server.stream_url("dns", 7), f"{base}/dns/stream/7.ts")

    def test_bad_token_unknown_session_and_unavailable_seqs_return_404_without_body(self):
        server = self.start_server()
        writer = Writer(RamRing(3000))
        for _ in range(4):
            writer.open()
            writer.append(1000)
        self.assertEqual((writer.ring.oldest_seq, writer.ring.newest_seq), (2, 3))
        server.add_session("s", writer.ring, lambda: False)
        valid = server.stream_url("s", 2)
        token = valid.split("/")[3]
        self.assert_not_found(valid.replace(token, "0" * 32))
        self.assert_not_found(valid.replace(token, token[:-1]))
        self.assert_not_found(valid.replace(token, token + "0"))
        self.assert_not_found(server.stream_url("other", 2))
        self.assert_not_found(server.stream_url("s", 1))  # evicted
        self.assert_not_found(server.stream_url("s", 4))  # not opened yet
        self.assert_not_found(valid.replace("/stream/2.ts", "/stream/x.ts"))
        self.assert_not_found(valid.replace("/stream/2.ts", "/segment/2.ts"))
        self.assert_not_found(valid + "?x=1")
        with patch.object(writer.ring, "read_at", return_value=None):
            self.assert_not_found(valid)  # evicted before the first byte: 404, not 200
        client = self.client(valid)
        self.assertEqual(client.status, 200)
        self.assertEqual(client.read(len(writer.stream(2))), writer.stream(2))

    def test_live_edge_sends_one_header_then_the_payload_as_it_arrives(self):
        server = self.start_server()
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(200_000)  # several 64 KiB chunks
        writer.open()
        writer.append(2000)
        server.add_session("live", writer.ring, lambda: False)
        client = self.client(server.stream_url("live", writer.ring.oldest_seq))
        self.assertEqual(client.status, 200)
        self.assertEqual(client.headers["content-type"], "video/mp2t")
        self.assertEqual(client.headers["cache-control"], "no-store")
        self.assertEqual(client.headers["connection"], "close")
        self.assertNotIn("content-length", client.headers)
        self.assertNotIn("transfer-encoding", client.headers)
        latencies = []

        def arrive(size):
            started = time.monotonic()
            data = client.read(size, LIVE)
            latencies.append(time.monotonic() - started)
            return data

        received = client.read(len(writer.stream(0)))
        time.sleep(0.05)  # the response now waits on the open segment
        received += arrive(len(writer.append(500)))
        writer.open()  # completes segment 1; segment 2's header is not resent
        received += arrive(len(writer.append(700)))
        writer.append(100)
        writer.ring.complete(2)
        received += arrive(100)
        time.sleep(0.05)  # the response now waits for the next segment to open
        writer.open()
        received += arrive(len(writer.append(300)))
        self.assertEqual(received, writer.stream(0))
        self.assertFalse(client.eof)
        # Well under the 0.5 s wait: appends wake the response instead of being polled.
        self.assertLess(max(latencies), 0.25, "appends were polled, not delivered on wakeup")

    def test_the_server_says_which_segment_a_client_reads(self):
        # 2.6.2: a server part fetches only so far ahead of what the player reads.
        server = self.start_server()
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(1000)
        writer.open()
        writer.append(1000)
        server.add_session("part", writer.ring, lambda: False)
        self.assertIsNone(server.reading("part"))
        client = self.client(server.stream_url("part", 0))
        client.read(len(writer.stream(0)))
        deadline = time.monotonic() + LIVE
        while server.reading("part") != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(server.reading("part"), 1)  # on the open segment now
        self.assertIsNone(server.reading("unknown"))

    def test_response_ends_after_the_last_segment_of_an_ended_session(self):
        server = self.start_server()
        ended = threading.Event()
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(1000)
        writer.open()
        writer.append(1500)
        server.add_session("s", writer.ring, ended.is_set)
        client = self.client(server.stream_url("s", 0))
        received = client.read(len(writer.stream(0)))
        writer.append(300)
        writer.ring.complete(1)
        ended.set()
        started = time.monotonic()
        received += client.read_to_eof(3.0)
        # One 0.5 s wait (the end is not notified) plus scheduling; never an endless wait.
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(received, writer.stream(0))
        # A session that already ended serves what is left and closes without any wait.
        started = time.monotonic()
        self.assertEqual(self.client(server.stream_url("s", 1)).read_to_eof(2.0), writer.stream(1))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_a_junction_segment_is_sent_with_its_own_header(self):
        # 2.6: a kept buffer's new run may bring other tables (a new PMT after hours away):
        # following on into a segment that begins a run sends its header too.
        server = self.start_server()
        ring = RamRing()
        writer = Writer(ring)
        writer.open(0)
        writer.append(1000)
        ring.open_segment(1, header(1), junction=True)
        writer.seq, writer.written[1] = 1, bytearray()
        writer.append(1500)
        writer.open()
        writer.append(700)
        ring.complete(2)
        server.add_session("s", ring, lambda: True)
        expected = header(0) + bytes(writer.written[0]) + header(1)
        expected += bytes(writer.written[1]) + bytes(writer.written[2])
        self.assertEqual(self.client(server.stream_url("s", 0)).read_to_eof(2.0), expected)
        self.assertTrue(ring.junction(1))
        self.assertFalse(ring.junction(2))
        self.assertFalse(ring.junction(9))

    def test_ended_session_with_an_unfinished_last_segment_still_closes(self):
        server = self.start_server()
        ended = threading.Event()
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(1000)
        server.add_session("s", writer.ring, ended.is_set)
        client = self.client(server.stream_url("s", 0))
        received = client.read(len(writer.stream(0)))
        writer.append(10)
        ended.set()
        received += client.read_to_eof(3.0)
        self.assertEqual(received, writer.stream(0))

    def test_a_gap_in_sequence_numbers_neither_spins_nor_skips_ahead(self):
        server = self.start_server()
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(1000)
        server.add_session("s", writer.ring, lambda: False)
        client = self.client(server.stream_url("s", 0))
        received = client.read(len(writer.stream(0)))
        writer.ring.complete(0)
        writer.open(2)  # segment 1 never exists
        writer.append(1000)
        reads = []
        original = writer.ring.read_at
        with patch.object(
            writer.ring, "read_at", side_effect=lambda *args: reads.append(args) or original(*args)
        ):
            time.sleep(1.0)
        # Two or three 0.5 s waits; a busy loop reads tens of thousands of times.
        self.assertLessEqual(len(reads), 6, "the response polled in a busy loop")
        with self.assertRaises(TimeoutError):
            client.read(1, timeout=0.2)
        self.assertEqual(received, writer.stream(0)[: len(received)])

    def check_paused_reader_falls_behind(self, ring, budget):
        server = self.start_server()
        self.small_send_buffer(server)
        writer = Writer(ring)
        writer.open(0)
        for _ in range(SEGMENT // APPEND):
            writer.append(APPEND)
        server.add_session("slow", ring, lambda: False)
        # Responses read with read_at, which pins nothing (the ring has no leases any more).
        client = self.client(server.stream_url("slow", 0), rcvbuf=4096)
        self.assertEqual(client.status, 200)
        paused = time.monotonic()
        time.sleep(0.3)  # the response fills the socket buffers and blocks mid-segment 0
        durations = []
        for _ in range(12 * SEGMENT // APPEND):  # 24 MiB through a 3 MiB ring
            if len(writer.written[writer.seq]) >= SEGMENT:
                writer.open()
            started = time.monotonic()
            writer.append(APPEND)  # never raises: the paused response pins nothing
            durations.append(time.monotonic() - started)
        windows = [sum(durations[index : index + 50]) for index in range(len(durations) - 49)]
        self.assertLess(max(windows), budget, "the paused reader slowed the writer")
        self.assertLessEqual(ring.charged, CAP)
        self.assertGreaterEqual(ring.oldest_seq, 3)
        retained = sum(
            len(header(seq)) + len(writer.written[seq])
            for seq in range(ring.oldest_seq, ring.newest_seq + 1)
        )
        self.assertEqual(ring.bytes, retained, "an evicted segment is still held")
        time.sleep(max(0.0, 2.0 - (time.monotonic() - paused)))
        body = client.read_to_eof(5.0)
        expected = writer.stream(0)
        self.assertTrue(client.eof)
        self.assertTrue(expected.startswith(body), "served bytes not in the original stream")
        self.assertLess(len(body), len(header(0)) + SEGMENT, "the reader was never blocked")

    def test_paused_reader_is_cut_off_cleanly_and_never_blocks_the_ram_writer(self):
        self.check_paused_reader_falls_behind(RamRing(CAP), budget=0.5)

    def test_paused_reader_is_cut_off_cleanly_and_never_blocks_the_disk_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            name = ".qitv-timeshift-" + secrets.token_hex(16)
            ring = DiskRing(Path(directory), name, CAP)
            try:
                # Disk appends write a file and query free space each time; 50 of them can
                # near 0.5 s on a slow Windows runner. A writer blocked by the paused reader
                # would stall for the whole 2 s pause, so 1.0 s still detects it.
                self.check_paused_reader_falls_behind(ring, budget=1.0)
                self.assertLessEqual(len(list((Path(directory) / name).iterdir())), 5)
            finally:
                ring.close()

    def test_header_reads_time_out_but_paused_body_writes_do_not(self):
        with patch.object(server_module, "_HEADER_TIMEOUT", 0.2):
            server = self.start_server()
            self.small_send_buffer(server)
            port = urlsplit(server.stream_url("s", 0)).port
            stalled = socket.create_connection(("127.0.0.1", port), timeout=2)
            self.addCleanup(stalled.close)
            stalled.sendall(b"GET / HTTP/1.1\r\nIncomplete:")
            writer = Writer(RamRing())
            writer.open(0)
            writer.append(BACKLOG)
            server.add_session("s", writer.ring, lambda: False)
            client = self.client(server.stream_url("s", 0), rcvbuf=4096)
            time.sleep(0.8)  # four header timeouts while the response write is blocked
            try:
                self.assertEqual(stalled.recv(100), b"")
            except QUIET_ERRORS:
                pass
            writer.append(1000)
            body = client.read(len(writer.stream(0)), 5.0)
            self.assertEqual(body, writer.stream(0))
            self.assertFalse(client.eof)

    def test_stop_interrupts_partial_request_headers_and_open_responses(self):
        server = self.start_server()
        self.small_send_buffer(server)
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(BACKLOG)
        server.add_session("s", writer.ring, lambda: False)
        url = server.stream_url("s", 0)
        port = urlsplit(url).port
        paused = self.client(url, rcvbuf=4096)  # its response write blocks
        waiting = self.client(url)
        waiting.read(len(writer.stream(0)), 5.0)  # its response waits at the live edge
        with socket.create_connection(("127.0.0.1", port), timeout=2) as stalled:
            stalled.sendall(b"GET / HTTP/1.1\r\nIncomplete:")
            time.sleep(0.03)
            started = time.monotonic()
            server.stop()
            self.assertLess(time.monotonic() - started, 2.0, "a local client prevented stop")
            # Its header read is interrupted, not left to the 2 s header timeout.
            stalled.settimeout(1.0)
            try:
                self.assertEqual(stalled.recv(100), b"")
            except QUIET_ERRORS:
                pass
        waiting.read_to_eof(1.0)
        paused.read_to_eof(2.0)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_two_sessions_are_streamed_concurrently(self):
        server = self.start_server()
        self.small_send_buffer(server)
        first = Writer(RamRing())
        first.open(0)
        first.append(BACKLOG)
        second = Writer(RamRing())
        second.open(0)
        second.append(1000)
        server.add_session("a", first.ring, lambda: False)
        server.add_session("b", second.ring, lambda: False)
        paused = self.client(server.stream_url("a", 0), rcvbuf=4096)
        time.sleep(0.2)  # session a's response is blocked in a write
        live = self.client(server.stream_url("b", 0))
        received = live.read(len(second.stream(0)))
        received += live.read(len(second.append(500)))
        second.open()
        received += live.read(len(second.append(500)))
        self.assertEqual(received, second.stream(0))
        first.append(1000)
        body = paused.read(len(first.stream(0)), 5.0)
        self.assertEqual(body, first.stream(0))

    def test_removing_a_session_ends_its_responses_and_later_requests(self):
        server = self.start_server()
        self.small_send_buffer(server)
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(BACKLOG)
        other = Writer(RamRing())
        other.open(0)
        other.append(100)
        server.add_session("s", writer.ring, lambda: False)
        server.add_session("other", other.ring, lambda: False)
        url = server.stream_url("s", 0)
        waiting_on = []  # the ring of every response currently waiting for data
        real_wait = server_module._wait

        def tracked_wait(session, before):
            waiting_on.append(session.ring)
            try:
                real_wait(session, before)
            finally:
                waiting_on.remove(session.ring)

        patch.object(server_module, "_wait", tracked_wait).start()
        self.addCleanup(patch.stopall)
        paused = self.client(url, rcvbuf=4096)
        waiting = self.client(url)
        waiting.read(len(writer.stream(0)), 5.0)
        kept = self.client(server.stream_url("other", 0))
        kept.read(len(other.stream(0)))
        started = time.monotonic()
        server.remove_session("s")
        waiting.read_to_eof(2.0)
        self.assertLess(time.monotonic() - started, 0.6)
        paused.read_to_eof(2.0)
        # Removal wakes the waiting response at once instead of after its 0.5 s wait.
        deadline = time.monotonic() + 0.3
        while writer.ring in waiting_on and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotIn(writer.ring, waiting_on, "a removed session's response still waits")
        # And no response of the removed session reads the ring again.
        reads = []
        original = writer.ring.read_at
        with patch.object(
            writer.ring, "read_at", side_effect=lambda *args: reads.append(args) or original(*args)
        ):
            for _ in range(5):
                writer.append(1000)
            time.sleep(0.2)
        self.assertEqual(reads, [])
        self.assert_not_found(url)
        data = other.append(100)
        self.assertEqual(kept.read(len(data)), data)

    def test_buffer_read_failures_end_only_that_response_quietly(self):
        server = self.start_server()
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(100_000)  # two 64 KiB chunks
        server.add_session("s", writer.ring, lambda: False)
        url = server.stream_url("s", 0)
        failure = RingError("The temporary buffer could not be read.")
        with patch.object(writer.ring, "read_at", side_effect=failure):
            with self.assertLogs(server_module.__name__, "DEBUG") as before_status:
                client = self.client(url)
                self.assertEqual(client.status, 503)  # before the status line: no 200
                self.assertEqual(client.headers.get("content-length"), "0")
                self.assertEqual(client.read_to_eof(2.0), b"")
        original = writer.ring.read_at
        calls = []

        def fail_after_first_chunk(*args):
            calls.append(args)
            if len(calls) > 1:
                raise failure
            return original(*args)

        with patch.object(writer.ring, "read_at", side_effect=fail_after_first_chunk):
            with self.assertLogs(server_module.__name__, "DEBUG") as mid_stream:
                client = self.client(url)
                self.assertEqual(client.status, 200)
                self.assertEqual(client.read_to_eof(2.0), writer.stream(0)[: 64 * 1024])
        for logs in (before_status, mid_stream):
            self.assertEqual([record.levelno for record in logs.records], [logging.DEBUG])
            self.assertNotIn(server.token, logs.output[0])
        self.assertEqual(self.client(url).read(len(writer.stream(0))), writer.stream(0))

    def test_client_resets_and_aborts_are_silent_and_leave_the_server_serving(self):
        server = self.start_server()
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(1000)
        server.add_session("s", writer.ring, lambda: False)
        url = server.stream_url("s", 0)
        port = urlsplit(url).port
        linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
        errors = io.StringIO()
        handle_error = patch.object(server_module._HTTPServer, "handle_error").start()
        self.addCleanup(patch.stopall)
        with contextlib.redirect_stderr(errors):
            client = Client(url)
            client.read(len(writer.stream(0)))
            client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
            client.close()  # a reset while the response waits at the live edge
            half = socket.create_connection(("127.0.0.1", port), timeout=2)
            half.sendall(b"GET / HTTP/1.1\r\nIncomp")
            half.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
            half.close()  # a reset while the request header is read
            for _ in range(20):
                writer.append(64 * 1024)
            time.sleep(0.3)
            fresh = self.client(url)
            self.assertEqual(fresh.read(len(writer.stream(0)), 2.0), writer.stream(0))
        self.assertEqual(errors.getvalue(), "")
        handle_error.assert_not_called()  # swallowed by the handler itself

    def test_unexpected_handler_failures_print_nothing(self):
        server = self.start_server()
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(1000)

        server.add_session("s", writer.ring, fails_after_first_check(RuntimeError("unexpected")))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            with self.assertLogs(server_module.__name__, "DEBUG") as logs:
                client = self.client(server.stream_url("s", 0))
                self.assertEqual(client.status, 200)
                self.assertEqual(client.read_to_eof(2.0), writer.stream(0))
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual([record.levelno for record in logs.records], [logging.DEBUG])
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn(server.token, logs.output[0])
        self.assertNotIn("/stream/", logs.output[0])

    def test_macos_send_during_reset_is_swallowed_like_a_reset(self):
        server = self.start_server()
        writer = Writer(RamRing())
        writer.open(0)
        writer.append(1000)
        prototype = OSError(errno.EPROTOTYPE, "Protocol wrong type for socket")
        server.add_session("s", writer.ring, fails_after_first_check(prototype))
        handle_error = patch.object(server_module._HTTPServer, "handle_error").start()
        self.addCleanup(patch.stopall)
        client = self.client(server.stream_url("s", 0))
        self.assertEqual(client.read_to_eof(2.0), writer.stream(0))
        handle_error.assert_not_called()

    def test_session_ids_must_be_url_safe(self):
        server = self.start_server()
        for bad in ("", "a/b", "a b", "x" * 65, "é"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                server.add_session(bad, RamRing(), lambda: False)


if __name__ == "__main__":
    unittest.main()
