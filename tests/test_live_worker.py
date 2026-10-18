"""The live helper process: a real spawn child hosting sessions against loopback sources.

Ports main's recording-child tests (tests/test_timeshift.py: a stalled provider stops
promptly without exposing credentials, stopping one recording leaves its sibling playing,
and owner death closes the upstream socket) to one long-lived helper with many sessions.
"""

import json
import multiprocessing
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
import warnings

from http_fixtures import LocalSource, leaf, tls_server_context
from process_fixtures import still_running, terminate
from ts_fixtures import PMT_PID, make_stream
from urllib3.exceptions import InsecureRequestWarning

from services.live import source as source_module, worker
from services.live.ring import RamRing
from services.live.worker import helper_main

REPO = Path(__file__).resolve().parents[1]
SECRET_PATH = "/user/secret/live.ts"
RAM = {"kind": "ram", "max_bytes": 32 * 1024 * 1024}
STATE_KEYS = {
    "oldest",
    "newest",
    "open",
    "start",
    "end",
    "live_edge",
    "bytes",
    "ended",
    "burst_s",
    "received",
    "hls",
    "breaks",
    "caught_up",
    "backlog_s",
    "finite",
    "open_quality",
    "video",
}
DISK_LIMIT = "The disk buffer limit cannot fit one keyframe interval or an active read."
# The owner hands a copy of its pipe end to a holder process that outlives it, so the
# helper's command loop never sees EOF: only the owner-death watchdog can end the helper.
# The holder waits on the pipe, so it exits by itself once the helper is gone. It only
# isolates the watchdog while the helper sends nothing after hello: any message would end
# the holder's wait, close its copy and let the command loop see EOF instead.
OWNER = """
import multiprocessing, sys, time
from multiprocessing.connection import wait
from services.live.worker import helper_main
context = multiprocessing.get_context("spawn")
conn, child = context.Pipe(duplex=True)
process = context.Process(target=helper_main, args=(child, sys.argv[2]), daemon=True)
process.start()
child.close()
assert conn.recv()[0] == "hello"
holder = context.Process(target=wait, args=([conn], 60))
holder.start()
conn.send(("open", "owned", sys.argv[1], {}, True, {"kind": "ram", "max_bytes": 1 << 24}, 6.0))
print(process.pid, holder.pid, flush=True)
time.sleep(60)
"""
# An owner of one helper per plan (variant -> [url or None, ring]) that dies with them all
# running: it prints {variant: [pid, base_url]} once every session is ready.
OWNER_OF_MANY = """
import json, multiprocessing, sys, time
from services.live.worker import helper_main
context = multiprocessing.get_context("spawn")
plans = json.loads(sys.argv[1])
helpers = {}
for variant in plans:
    conn, child = context.Pipe(duplex=True)
    process = context.Process(target=helper_main, args=(child, sys.argv[2]), daemon=True)
    process.start()
    child.close()
    helpers[variant] = conn, process
report = {}
for variant, (url, ring) in plans.items():
    conn, process = helpers[variant]
    base_url = conn.recv()[1]
    if url:
        conn.send(("open", "s", url, {}, True, ring, 6.0))
        while conn.recv()[:2] != ("ready", "s"):
            pass
    report[variant] = [process.pid, base_url]
print(json.dumps(report), flush=True)
time.sleep(60)
"""


def second_pat(data):
    """The offset of the second PAT packet: where a stream cut half a second in begins (its
    program tables come every half second)."""
    seen = 0
    for at in range(0, len(data), 188):
        if data[at + 1] & 0x1F == 0 and data[at + 2] == 0:
            seen += 1
            if seen == 2:
                return at
    raise AssertionError("no second PAT")


def helper_with_a_lingering_thread(conn, root):
    """The real helper in a process that also runs a non-daemon thread, as any imported
    module could start, or a parent's unguarded __main__ module that spawn runs again."""
    threading.Thread(target=threading.Event().wait, name="lingering").start()
    helper_main(conn, root)


class Helper:
    """One spawned helper and the parent's end of its pipe; every message is kept."""

    def __init__(self, root, target=helper_main):
        context = multiprocessing.get_context("spawn")
        self.conn, child = context.Pipe(duplex=True)
        self.process = context.Process(
            target=target, args=(child, str(root)), name="qitv-live-test", daemon=True
        )
        self.process.start()
        child.close()
        self.messages = []
        self.base_url = self.wait_for(lambda message: message[0] == "hello", timeout=60)[1]

    def open(self, url, ring=RAM, live_delay=6.0, start=None):
        """`start`: the session's target and extra (2.1 stage 3); None sends the 2.0 command."""
        sid = secrets.token_hex(8)
        command = ("open", sid, url, {"User-Agent": "QiTV-test"}, True, ring, live_delay)
        self.conn.send(command if start is None else (*command, start))
        return sid

    def close(self, sid):
        self.conn.send(("close", sid))

    def wait_for(self, predicate, timeout=10.0, start=0):
        deadline = time.monotonic() + timeout
        index = start
        while True:
            while index < len(self.messages):
                message = self.messages[index]
                index += 1
                if predicate(message):
                    return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no matching helper message among {self.messages!r}")
            if self.conn.poll(min(remaining, 0.05)):
                self.messages.append(self.conn.recv())

    def drain(self, seconds):
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if self.conn.poll(min(remaining, 0.05)):
                self.messages.append(self.conn.recv())

    def of(self, sid, kind=None):
        return [
            message
            for message in self.messages
            if len(message) > 1 and message[1] == sid and kind in (None, message[0])
        ]

    def stream_url(self, sid, seq):
        return f"{self.base_url}/{sid}/stream/{seq}.ts"

    def stop(self):
        if self.process.is_alive():
            try:
                self.conn.send(("shutdown",))
            except OSError:
                pass
            self.process.join(5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(5)
        self.conn.close()


def rebuild(states):
    """The parent's span list, rebuilt from state deltas as main's supervisor did."""
    completed = []
    for _, _, _, first, additions in states:
        if first is None:
            completed = []
        else:
            completed = [item for item in completed + list(additions) if item[0] >= first]
    return completed


def spans(helper, sid):
    return rebuild(helper.of(sid, "state"))


def pid_of(packet):
    return ((packet[1] & 0x1F) << 8) | packet[2]


def split_header(body):
    """The PAT/PMT copies a stream starts with, and the source bytes after them."""
    header = 0
    while pid_of(body[header : header + 188]) in (0, PMT_PID):
        header += 188
    return body[:header], body[header:]


def counter_breaks(stream):
    """(packet index, PID, counter, expected) wherever a PID's continuity counter breaks:
    a packet with a payload takes the previous value plus one, adaptation only the same."""
    breaks, last = [], {}
    for index, at in enumerate(range(0, len(stream), 188)):
        packet = stream[at : at + 188]
        pid, afc, cc = pid_of(packet), (packet[3] >> 4) & 3, packet[3] & 0x0F
        if pid == 0x1FFF or not afc:
            continue
        if pid in last:
            expected = (last[pid] + 1) & 0x0F if afc & 1 else last[pid]
            if cc != expected:
                breaks.append((index, pid, cc, expected))
        last[pid] = cc
    return breaks


def pcr_of(packet):
    b = packet[6:11]
    return (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)


def paced(payload, stop, hung_up, *, block=188 * 20, interval=0.02):
    """A close-delimited body that streams at a few times real time until the client hangs up."""

    def body(request):
        try:
            for offset in range(0, len(payload), block):
                if stop.wait(interval):
                    return
                request.wfile.write(payload[offset : offset + block])
        except OSError:
            hung_up.set()

    return body


def paced_route(payload, stop, hung_up):
    """A route answering every request, reconnects included, with a fresh paced body."""
    return lambda _request, _count: (200, {}, paced(payload, stop, hung_up))


class StalledUpstream:
    """Accepts one HTTP request, sends `response` (part of an answer) and waits for EOF."""

    def __init__(self, response):
        self.response = response
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}{SECRET_PATH}"
        self.requested = threading.Event()
        self.hung_up = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            client, _address = self.listener.accept()
        except OSError:
            return
        with client:
            client.settimeout(30)
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    received = client.recv(4096)
                    if not received:
                        return
                    data += received
                self.requested.set()
                client.sendall(self.response)
                while client.recv(4096):
                    pass  # the client sends nothing more: EOF means it hung up
            except ConnectionError:
                pass  # Windows reports a reset or an abort where Unix reports EOF
            except OSError:
                return
            self.hung_up.set()

    def close(self):
        self.listener.close()


class StreamReader:
    """Reads a loopback stream on a thread and counts the bytes received."""

    def __init__(self, url):
        self.received = 0
        self.changed = threading.Condition()
        self.response = urlopen(url, timeout=10)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while data := self.response.read1(64 * 1024):
                with self.changed:
                    self.received += len(data)
                    self.changed.notify_all()
        except OSError, ValueError:
            pass  # closed by the test or by the helper's shutdown

    def wait_for(self, count, timeout=10.0):
        with self.changed:
            return self.changed.wait_for(lambda: self.received >= count, timeout)

    def close(self):
        self.response.close()


class LiveWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-live-worker-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stop = threading.Event()

    def source(self, routes):
        server = LocalSource(routes)
        self.addCleanup(server.close)
        self.addCleanup(self.stop.set)  # runs first: paced bodies stop writing
        return server

    def helper(self, target=helper_main):
        helper = Helper(self.root, target)
        self.addCleanup(helper.stop)
        return helper

    def plan(self, variant):
        """[url, ring] for a session in `variant` (a streaming one buffers on disk), and an
        event set once that session's upstream connection is closed."""
        if variant == "idle":
            nothing = threading.Event()
            nothing.set()
            return [None, RAM], nothing
        if variant == "stalled":
            # It sends a few seconds of stream, then nothing: the session waits in a read.
            head = b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n"
            upstream = StalledUpstream(head + make_stream(3))
            self.addCleanup(upstream.close)
            return [upstream.url, RAM], upstream.hung_up
        hung_up = threading.Event()
        source = self.source({SECRET_PATH: paced_route(make_stream(60), self.stop, hung_up)})
        directory = ".qitv-timeshift-" + secrets.token_hex(16)
        ring = {"kind": "disk", "root": str(self.root), "dir": directory, "max_bytes": 1 << 30}
        return [source.url + SECRET_PATH, ring], hung_up

    def pause_a_reader(self, url):
        """A player that read the stream's start and then stopped reading."""
        response = urlopen(url, timeout=10)
        self.addCleanup(response.close)
        self.assertEqual(len(response.read(188)), 188)

    def assert_released(self, variant, plan, hung_up):
        """The session's upstream connection is closed and its disk buffer removed."""
        self.assertTrue(hung_up.wait(2), f"the {variant} session kept its upstream connection")
        ring = plan[1]
        if ring["kind"] == "disk":
            self.assertFalse((self.root / ring["dir"]).exists(), f"the {variant} buffer was left")

    def assert_no_credentials(self, helper):
        for message in helper.messages:
            self.assertNotIn("secret", repr(message))

    def test_the_hello_url_serves_an_opened_session_from_its_oldest_segment(self):
        payload = make_stream(8)
        source = self.source({SECRET_PATH: (200, {}, payload)})
        helper = self.helper()
        self.assertRegex(helper.base_url, r"^http://127\.0\.0\.1:[0-9]+/[0-9a-f]{32}$")
        sid = helper.open(source.url + SECRET_PATH)
        helper.wait_for(lambda message: message == ("ready", sid))
        # The state before "ready" names the segment to start playback from.
        snapshot = helper.of(sid, "state")[-1][2]
        self.assertEqual(set(snapshot), STATE_KEYS)
        self.assertEqual(snapshot["oldest"], 0)
        self.assertEqual(snapshot["open"], 0)
        self.assertEqual(snapshot["burst_s"], 0.0)
        self.assertIs(snapshot["hls"], False)  # raw MPEG-TS
        self.assertIs(snapshot["video"], True)  # a video PID: its starts may be keyframes
        with urlopen(helper.stream_url(sid, snapshot["oldest"]), timeout=10) as response:
            self.assertEqual(response.headers["Content-Type"], "video/mp2t")
            body = response.read()  # the stream ends once the finite source has ended
        header, media = split_header(body)
        self.assertGreater(len(header), 0)
        start = payload.index(media[:188])
        self.assertLessEqual(start, 4 * 188, "the stream must start at the first keyframe")
        self.assertEqual(media, payload[start:])
        self.assertEqual(source.counts[SECRET_PATH], 1)
        self.assertEqual(source.requests[0][1].get("User-Agent"), "QiTV-test")
        self.assert_no_credentials(helper)

    def test_a_timestamp_jump_left_out_of_the_timeline_is_reported(self):
        # 2.1 stage 2: the player's clock jumps by what the timeline leaves out (a reconnect
        # after an outage, an HLS discontinuity): where on the timeline, and how much. A step
        # under 3 s stays media and is not reported.
        helper = self.helper()
        for path, jump, reported in (("/jump.ts", 5.0, [(4.0, 5.0)]), ("/step.ts", 2.0, [])):
            with self.subTest(jump=jump):
                first = make_stream(4)
                second = make_stream(4, pts_start=900000 + round((4 + jump) * 90000))
                source = self.source({path: (200, {}, first + second)})
                sid = helper.open(source.url + path)
                helper.wait_for(lambda message, sid=sid: message == ("ended", sid))
                breaks = helper.of(sid, "state")[-1][2]["breaks"]
                self.assertEqual(len(breaks), len(reported))
                for (where, seconds), (at, left_out) in zip(breaks, reported):
                    self.assertAlmostEqual(where, at, delta=0.05)
                    self.assertAlmostEqual(seconds, left_out, delta=0.05)

    def test_reported_jumps_stay_as_long_as_the_ring_holds_their_media(self):
        # A flaky raw-TS source that reconnects every few seconds: 20 jumps of 5 s, 2 s of
        # media apart. A viewer far behind must still find each break the ring still holds
        # (a count of eight dropped them); those whose media was evicted go with it, but for
        # those just before the ring's start (2.1 stage 5: a sparse-PCR clock's 2 s step and
        # a second more), which a player still reading evicted media may reach.
        pieces = b"".join(
            make_stream(2, pts_start=900000 + round(k * 7.0 * 90000)) for k in range(21)
        )
        source = self.source({"/flaky.ts": (200, {}, pieces)})
        helper = self.helper()
        for name, ring in (("all held", RAM), ("evicting", {"kind": "ram", "max_bytes": 1 << 18})):
            with self.subTest(ring=name):
                sid = helper.open(source.url + "/flaky.ts", ring=ring)
                helper.wait_for(lambda message, sid=sid: message == ("ended", sid))
                snapshot = helper.of(sid, "state")[-1][2]
                breaks = snapshot["breaks"]
                self.assertTrue(all(abs(seconds - 5.0) < 0.05 for _, seconds in breaks))
                if name == "all held":
                    self.assertEqual(len(breaks), 20)
                    self.assertEqual([round(where) for where, _ in breaks], list(range(2, 42, 2)))
                else:
                    self.assertLess(len(breaks), 20)
                    slack = worker._BREAK_SLACK_S
                    self.assertTrue(all(where >= snapshot["start"] - slack for where, _ in breaks))
                    self.assertLess(breaks[0][0], snapshot["start"])  # one just before it
                    self.assertEqual(round(breaks[-1][0]), 40)

    def test_a_finite_source_ends_after_its_last_segment(self):
        first, second = make_stream(4), make_stream(4, pts_start=900000 + 4 * 90000)
        source = self.source(
            {
                SECRET_PATH: (200, {}, make_stream(9)),
                "/user/secret/channel.m3u8": (200, {}, leaf("a.ts", "b.ts")),
                "/user/secret/a.ts": (200, {}, first),
                "/user/secret/b.ts": (200, {}, second),
            }
        )
        helper = self.helper()
        for path, seconds, burst, hls in (
            (SECRET_PATH, 9.0, 0.0, False),
            ("/user/secret/channel.m3u8", 8.0, 4.0, True),
        ):
            with self.subTest(path=path):
                sid = helper.open(source.url + path)
                ended = helper.wait_for(lambda message: message == ("ended", sid))
                states = helper.of(sid, "state")
                self.assertLess(helper.messages.index(states[-1]), helper.messages.index(ended))
                snapshot = states[-1][2]
                self.assertTrue(snapshot["ended"])
                self.assertIsNone(snapshot["open"])
                self.assertEqual(snapshot["burst_s"], burst)
                self.assertIs(snapshot["hls"], hls)  # 2.0.2: stay-live keeps HLS at 1x
                self.assertIs(snapshot["caught_up"], hls)  # HLS: every listed segment came
                self.assertGreater(snapshot["bytes"], 0)
                self.assertEqual(helper.of(sid, "error"), [])
                buffered = spans(helper, sid)
                self.assertGreaterEqual(len(buffered), 3)
                self.assertEqual([item[0] for item in buffered], list(range(len(buffered))))
                self.assertEqual(snapshot["oldest"], 0)
                self.assertEqual(snapshot["newest"], buffered[-1][0])
                for before, after in zip(buffered, buffered[1:]):
                    self.assertAlmostEqual(before[2], after[1])
                self.assertAlmostEqual(buffered[-1][2] - buffered[0][1], seconds, delta=0.25)
                self.assertAlmostEqual(snapshot["end"], buffered[-1][2])
                self.assertEqual({item[4] for item in buffered}, {"key"})  # IDR starts
        self.assert_no_credentials(helper)

    def test_an_hls_batch_reports_its_live_edge_once_delivered(self):
        # 2.1 stage 3: states are throttled to four a second, so the end of a batch that
        # arrived within a quarter second waited for the next batch (a 2 s keyframe interval
        # of a public HLS channel for 7 s): the start was chosen on a stale edge.
        routes = {
            "/user/secret/live.m3u8": (200, {}, leaf("a.ts", "b.ts", ended=False)),
            "/user/secret/a.ts": (200, {}, make_stream(4)),
            "/user/secret/b.ts": (200, {}, make_stream(4, pts_start=900000 + 4 * 90000)),
        }
        source = self.source(routes)
        helper = self.helper()
        sid = helper.open(source.url + "/user/secret/live.m3u8", live_delay=100.0)
        state = helper.wait_for(
            lambda message: message[:2] == ("state", sid) and message[2]["caught_up"], timeout=5
        )
        self.assertGreater(state[2]["live_edge"], 7.9)  # all 8 s, not the last cut's 6 s
        self.assertIs(helper.of(sid, "state")[0][2]["caught_up"], False)

    def backlog_states(self, names, *, extinf, seconds, missing=(), cut_first=False):
        """The states of an HLS session whose live playlist lists `names` (each `extinf` long
        by its EXTINF, `seconds` of media; those in `missing` answer 404) until caught up.
        `cut_first`: the first segment begins half a second in, before its first keyframe
        (keyframes every 2 s)."""
        text = f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:{extinf}\n"
        text += "#EXT-X-MEDIA-SEQUENCE:0\n"
        text += "".join(f"#EXTINF:{extinf},\n{name}\n" for name in names)
        routes = {"/user/secret/live.m3u8": (200, {}, text.encode())}
        start = 900000
        for name in names:
            if name in missing:
                routes[f"/user/secret/{name}"] = (404, {}, b"Not found")
                continue
            body = make_stream(seconds, pts_start=start, gop_s=2.0 if cut_first else 1.0)
            if cut_first and name == names[0]:
                body = body[second_pat(body) :]
            routes[f"/user/secret/{name}"] = (200, {}, body)
            start += round(seconds * 90000)
        source = self.source(routes)
        helper = self.helper()
        sid = helper.open(source.url + "/user/secret/live.m3u8", live_delay=100.0)
        helper.wait_for(
            lambda message: message[:2] == ("state", sid) and message[2]["caught_up"], timeout=5
        )
        states = [message[2] for message in helper.of(sid, "state")]
        return [state for state in states if state["oldest"] is not None]

    def test_an_hls_backlog_reports_the_media_still_to_come(self):
        # 2.1 stage 3: live edge + backlog_s is where the edge will be once every listed
        # segment arrived, so a tune can start before all of them did; measured once a
        # segment came whole, not the first (before: None).
        playable = self.backlog_states(["a.ts", "b.ts", "c.ts", "d.ts"], extinf=4, seconds=4)
        self.assertGreater(len(playable), 2)
        self.assertIsNone(playable[0]["backlog_s"])
        measured = [state for state in playable if state["backlog_s"] is not None]
        self.assertGreater(len(measured), 1)
        for state in measured:
            # Each segment measures a frame short (the edge stands at its last frame's
            # start): the prediction errs low, on the side that keeps room.
            predicted = state["live_edge"] + state["backlog_s"]
            self.assertAlmostEqual(predicted, 16.0, delta=0.2, msg=state)
        self.assertEqual(playable[-1]["backlog_s"], 0.0)
        self.assertEqual(playable[-1]["open_quality"], "key")  # the open segment's start
        self.assertIs(playable[-1]["finite"], False)

    def test_an_hls_backlog_follows_the_media_not_a_wrong_extinf(self):
        # Review I3: EXTINF 10 on segments of 8 s moved the start 2 s a segment.
        playable = self.backlog_states(["a.ts", "b.ts", "c.ts", "d.ts"], extinf=10, seconds=8)
        measured = [state for state in playable if state["backlog_s"] is not None]
        self.assertGreater(len(measured), 1)
        for state in measured:
            predicted = state["live_edge"] + state["backlog_s"]
            self.assertAlmostEqual(predicted, 32.0, delta=0.5, msg=state)

    def test_a_first_segment_without_a_keyframe_at_its_start_is_not_measured(self):
        # Re-review minor 3: its media counts from its first keyframe (1.5 s in here), so it
        # read short against its EXTINF and pulled the prediction low.
        playable = self.backlog_states(
            ["a.ts", "b.ts", "c.ts", "d.ts"], extinf=8, seconds=8, cut_first=True
        )
        final = playable[-1]["live_edge"]
        self.assertAlmostEqual(final, 30.0, delta=0.1)  # the first 2 s are before a keyframe
        measured = [state for state in playable if state["backlog_s"] is not None]
        self.assertTrue(measured)
        for state in measured:
            predicted = state["live_edge"] + state["backlog_s"]
            self.assertAlmostEqual(predicted, final, delta=0.5, msg=state)

    def test_a_missing_segment_leaves_the_backlog(self):
        # Review I3: a 404 segment was counted in, and never came: the start landed short.
        playable = self.backlog_states(
            ["a.ts", "b.ts", "c.ts", "d.ts"], extinf=4, seconds=4, missing=("c.ts",)
        )
        final = playable[-1]["live_edge"]
        self.assertAlmostEqual(final, 12.0, delta=0.1)
        for state in playable:
            if state["backlog_s"] is not None:
                predicted = state["live_edge"] + state["backlog_s"]
                self.assertLessEqual(predicted, final + 4.1, msg=state)  # c.ts while it fails
        late = [state for state in playable if state["live_edge"] > 8.0]
        self.assertTrue(late)
        for state in late:  # once c.ts failed: what d.ts brings
            self.assertAlmostEqual(state["live_edge"] + state["backlog_s"], final, delta=0.1)

    def test_a_finished_playlist_says_so_and_has_no_backlog(self):
        # Review I1: a finished (ENDLIST) playlist plays from its beginning.
        names = ["a.ts", "b.ts", "c.ts"]
        routes = {"/user/secret/clip.m3u8": (200, {}, leaf(*names))}
        for number, name in enumerate(names):
            body = make_stream(4, pts_start=900000 + number * 4 * 90000)
            routes[f"/user/secret/{name}"] = (200, {}, body)
        source = self.source(routes)
        helper = self.helper()
        sid = helper.open(source.url + "/user/secret/clip.m3u8", live_delay=100.0)
        helper.wait_for(lambda message: message == ("ended", sid))
        states = [message[2] for message in helper.of(sid, "state")]
        self.assertTrue(all(state["finite"] for state in states))
        self.assertTrue(all(state["backlog_s"] == 0.0 for state in states))

    def test_the_sessions_target_sets_how_far_back_hls_starts(self):
        names = [f"s{number}.ts" for number in range(6)]
        routes = {"/user/secret/live.m3u8": (200, {}, leaf(*names, ended=False))}
        routes.update({f"/user/secret/{name}": (200, {}, make_stream(4)) for name in names})
        helper = self.helper()
        for start, fetched in ((None, 2), ({"target": 2.5, "extra": 0.0}, 3)):
            with self.subTest(start=start):
                source = self.source(dict(routes))
                sid = helper.open(source.url + "/user/secret/live.m3u8", start=start)
                helper.wait_for(
                    lambda message: message[:2] == ("state", sid) and message[2]["caught_up"]
                )
                segments = [path for path, _ in source.requests if path.endswith(".ts")]
                # 2.0: 6 s back (two 4 s segments); 2.1: three target durations (12 s).
                self.assertEqual(len(segments), fetched)
                helper.close(sid)

    def test_hls_segments_packaged_on_their_own_are_served_with_running_counters(self):
        # 2.0.2: a CDN that packages each HLS segment on its own restarts every PID's
        # continuity counter (PAT's and PMT's too) at 0, which VLC takes as a break on every
        # PID. The served stream continues the counters, across a real discontinuity too,
        # whose first PCR then carries the discontinuity indicator. Raw TS passes as it came.
        segments = [make_stream(4, pts_start=900000 + i * 4 * 90000) for i in range(3)]
        segments.append(make_stream(4, pts_start=9_000_000))  # after the discontinuity
        playlist = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        playlist += "#EXT-X-MEDIA-SEQUENCE:0\n"
        playlist += "".join(f"#EXTINF:4,\n{name}.ts\n" for name in "abc")
        playlist += "#EXT-X-DISCONTINUITY\n#EXTINF:4,\nd.ts\n#EXT-X-ENDLIST\n"
        joined = b"".join(segments)
        routes = {f"/user/secret/{name}.ts": (200, {}, s) for name, s in zip("abcd", segments)}
        routes["/user/secret/channel.m3u8"] = (200, {}, playlist.encode())
        routes[SECRET_PATH] = (200, {}, joined)  # the same bytes as one raw TS stream
        source = self.source(routes)
        helper = self.helper()
        served = {}
        for path in ("/user/secret/channel.m3u8", SECRET_PATH):
            sid = helper.open(source.url + path)
            helper.wait_for(lambda message, sid=sid: message == ("ended", sid))
            with urlopen(helper.stream_url(sid, 0), timeout=10) as response:
                served[path] = split_header(response.read())[1]
        hls, raw = served["/user/secret/channel.m3u8"], served[SECRET_PATH]
        self.assertEqual(counter_breaks(hls), [])
        self.assertGreater(len(counter_breaks(raw)), 0)  # untouched: its breaks remain
        start = joined.index(raw[:188])
        self.assertEqual(raw, joined[start:])  # raw TS: byte for byte
        # HLS: only the counters change, and the one flag at the discontinuity.
        self.assertEqual(len(hls), len(joined) - start)
        flagged = []
        for at in range(0, len(hls), 188):
            got, was = bytearray(hls[at : at + 188]), bytearray(
                joined[start + at : start + at + 188]
            )
            if got[3] & 0x20 and got[4] and got[5] & 0x80 and not was[5] & 0x80:
                flagged.append(pcr_of(got))
                got[5] &= 0x7F
            got[3] &= 0xF0
            was[3] &= 0xF0
            self.assertEqual(got, was, f"packet {at // 188}")
        self.assertEqual(flagged, [9_000_000])  # the first PCR after the discontinuity
        self.assert_no_credentials(helper)

    def test_closing_one_session_leaves_its_sibling_streaming(self):
        payload = make_stream(60)
        hung_up = {name: threading.Event() for name in "ab"}
        sources = {
            name: self.source({SECRET_PATH: paced_route(payload, self.stop, hung_up[name])})
            for name in "ab"
        }
        helper = self.helper()
        sids = {name: helper.open(sources[name].url + SECRET_PATH) for name in "ab"}
        for sid in sids.values():
            helper.wait_for(lambda message, sid=sid: message == ("ready", sid))
        for sid in sids.values():  # both ingest concurrently
            helper.wait_for(lambda m, sid=sid: m[0] == "state" and m[1] == sid and m[4])
        reader = StreamReader(helper.stream_url(sids["a"], 0))
        self.addCleanup(reader.close)
        self.assertTrue(reader.wait_for(1), "the sibling's stream did not start")

        started = time.monotonic()
        helper.close(sids["b"])
        closed = helper.wait_for(lambda message: message == ("closed", sids["b"]), timeout=2)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(hung_up["b"].wait(3), "the closed session kept its upstream connection")
        mark = len(helper.messages)
        received = reader.received
        helper.wait_for(lambda m: m[0] == "state" and m[1] == sids["a"] and m[4], start=mark)
        self.assertTrue(reader.wait_for(received + 64 * 1024), "the sibling stopped streaming")
        self.assertFalse(hung_up["a"].is_set())
        with self.assertRaises(HTTPError) as missing:
            urlopen(helper.stream_url(sids["b"], 0), timeout=5)
        self.assertEqual(missing.exception.code, 404)
        missing.exception.close()
        helper.drain(0.5)
        self.assertEqual(helper.of(sids["b"])[-1], closed, "a closed session kept reporting")
        self.assertEqual(helper.of(sids["b"], "error"), [])
        self.assertTrue(helper.process.is_alive())
        self.assert_no_credentials(helper)

    def test_closing_a_stalled_session_is_prompt_and_keeps_the_helper(self):
        finite = self.source({SECRET_PATH: (200, {}, make_stream(6))})
        helper = self.helper()
        sibling = helper.open(finite.url + SECRET_PATH)
        helper.wait_for(lambda message: message == ("ended", sibling))
        head = b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n"
        for stage, response in (
            ("mid-header", b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n"),
            ("mid-body", head + make_stream(3)),
        ):
            with self.subTest(stage=stage):
                upstream = StalledUpstream(response)
                self.addCleanup(upstream.close)
                sid = helper.open(upstream.url)
                self.assertTrue(upstream.requested.wait(10))
                if stage == "mid-body":
                    helper.wait_for(lambda message, sid=sid: message == ("ready", sid))
                time.sleep(0.2)  # the session is now blocked on its upstream
                if os.name != "nt":
                    command = subprocess.run(
                        ["ps", "-o", "command=", "-p", str(helper.process.pid)],
                        capture_output=True,
                        check=True,
                        timeout=5,
                    ).stdout.decode()
                    self.assertIn("multiprocessing", command)
                    self.assertNotIn("secret", command)
                    self.assertNotIn(upstream.url, command)
                started = time.monotonic()
                helper.close(sid)
                helper.wait_for(lambda message, sid=sid: message == ("closed", sid), timeout=2)
                self.assertLess(time.monotonic() - started, 2.0, "a stalled upstream held close")
                self.assertTrue(upstream.hung_up.wait(2), "the stalled upstream stayed open")
                self.assertTrue(helper.process.is_alive())
                helper.drain(0.3)
                self.assertEqual(helper.of(sid)[-1], ("closed", sid))
                self.assertEqual(helper.of(sid, "error"), [])
                with urlopen(helper.stream_url(sibling, 0), timeout=5) as response:
                    self.assertGreater(len(response.read()), 0, "the sibling stopped serving")
        self.assert_no_credentials(helper)

    def test_unsupported_input_reports_exactly_one_unsupported_error(self):
        fragmented = (
            "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:4\n"
            '#EXT-X-MAP:URI="movie.mp4"\n#EXTINF:4,\nmovie.mp4\n#EXT-X-ENDLIST\n'
        ).encode()
        bodies = {
            "not-ts": b"<html>not a transport stream</html>" * 2000,
            "192-byte-packets": make_stream(4, packet_size=192),
            "scrambled": make_stream(4, scrambled=True),
            "too-short": b"<html>nope</html>",
            "fragmented-hls": fragmented,
        }
        routes = {f"/user/secret/{name}": (200, {}, body) for name, body in bodies.items()}
        # A live input that turns out scrambled: its upstream is released at the error.
        hung_up = threading.Event()
        live = make_stream(30, scrambled=True)
        routes["/user/secret/scrambled-live"] = paced_route(live, self.stop, hung_up)
        source = self.source(routes)
        helper = self.helper()
        for name in [*bodies, "scrambled-live"]:
            with self.subTest(input=name):
                sid = helper.open(f"{source.url}/user/secret/{name}")
                helper.wait_for(lambda m, sid=sid: m[0] == "error" and m[1] == sid)
                if name == "scrambled-live":
                    self.assertTrue(hung_up.wait(3), "a failed session kept its upstream")
                helper.close(sid)
                helper.wait_for(lambda message, sid=sid: message == ("closed", sid))
                errors = helper.of(sid, "error")
                self.assertEqual(len(errors), 1, errors)
                self.assertEqual(errors[0][2], "unsupported")
                self.assertTrue(errors[0][3])
                self.assertEqual(helper.of(sid, "ready"), [])
                self.assertEqual(helper.of(sid, "ended"), [])
        self.assertTrue(helper.process.is_alive())
        self.assert_no_credentials(helper)

    def test_bytes_before_anything_playable_are_reported_as_they_arrive(self):
        # An on-demand restream starting up: bytes flow, but no keyframe yet (2.0.1, L2a).
        null = b"\x47\x1f\xff\x10" + b"\xff" * 184  # MPEG-TS null packets
        stream = make_stream(4)

        def body(request):
            try:
                for _ in range(8):
                    if self.stop.wait(0.1):
                        return
                    request.wfile.write(null * 20)
                    request.wfile.flush()
                request.wfile.write(stream)
                request.wfile.flush()
                self.stop.wait(10)
            except OSError:
                pass

        source = self.source({SECRET_PATH: (200, {}, body)})
        helper = self.helper()
        sid = helper.open(source.url + SECRET_PATH)
        ready = helper.wait_for(lambda message: message == ("ready", sid))
        early = [
            message[2]
            for message in helper.messages[: helper.messages.index(ready)]
            if message[:2] == ("state", sid) and message[2]["oldest"] is None
        ]
        self.assertGreaterEqual(len(early), 2, "no state while bytes arrived before ready")
        self.assertEqual(set(early[0]), STATE_KEYS)
        received = [snapshot["received"] for snapshot in early]
        self.assertGreater(received[0], 0)
        self.assertEqual(received, sorted(received))
        self.assertEqual(helper.of(sid, "state")[-1][2]["oldest"], 0)  # then playable
        self.assertGreaterEqual(helper.of(sid, "state")[-1][2]["received"], 8 * 20 * 188)

    def test_a_source_that_sends_nothing_is_a_network_failure(self):
        source = self.source({SECRET_PATH: (200, {}, b"")})
        helper = self.helper()
        sid = helper.open(source.url + SECRET_PATH)
        helper.wait_for(lambda message: message[0] == "error" and message[1] == sid)
        helper.close(sid)
        helper.wait_for(lambda message: message == ("closed", sid))
        errors = helper.of(sid, "error")
        self.assertEqual(len(errors), 1, errors)
        self.assertEqual(errors[0][2], "network")
        self.assertEqual(helper.of(sid, "ready"), [])

    def test_an_unexpected_session_failure_reports_one_crashed_error(self):
        hung_up = threading.Event()
        source = self.source(
            {
                SECRET_PATH: (200, {}, make_stream(4)),
                "/user/secret/live": paced_route(make_stream(30), self.stop, hung_up),
            }
        )
        helper = self.helper()
        # The bad ring spec fails after the live upstream connected.
        broken = helper.open(source.url + "/user/secret/live", ring={"kind": "ram"})
        helper.wait_for(lambda message: message[0] == "error" and message[1] == broken)
        self.assertTrue(hung_up.wait(3), "a failed session kept its upstream")
        helper.conn.send(("open", "not/a-session", source.url + SECRET_PATH, {}, True, RAM, 6.0))
        helper.wait_for(lambda message: message[:2] == ("error", "not/a-session"))
        working = helper.open(source.url + SECRET_PATH)
        helper.wait_for(lambda message: message == ("ended", working))
        for sid in (broken, "not/a-session"):
            errors = helper.of(sid, "error")
            # "crashed": final, with the helper up ("internal" is the parent's, for its death).
            self.assertEqual(errors, [("error", sid, "crashed", errors[0][3])])
            self.assertNotIn("max_bytes", errors[0][3])
        helper.close(broken)
        helper.wait_for(lambda message: message == ("closed", broken))
        self.assert_no_credentials(helper)

    def test_a_failing_disk_ring_falls_back_to_ram_and_keeps_streaming(self):
        payload = make_stream(9)
        source = self.source({SECRET_PATH: (200, {}, payload)})
        helper = self.helper()
        cases = (
            ("unusable directory", "not-a-session", 1 << 30, 0),
            # Too small for one segment: an append fails after the first segment opened.
            ("full disk buffer", ".qitv-timeshift-" + secrets.token_hex(16), 16 * 1024, 1),
        )
        for case, directory, cap, oldest in cases:
            with self.subTest(case=case):
                ring = {"kind": "disk", "root": str(self.root), "dir": directory, "max_bytes": cap}
                sid = helper.open(source.url + SECRET_PATH, ring=ring)
                helper.wait_for(lambda message, sid=sid: message == ("ended", sid))
                errors = helper.of(sid, "error")
                self.assertEqual(len(errors), 1, errors)
                self.assertEqual(errors[0][2], "disk")
                if oldest:
                    self.assertEqual(errors[0][3], DISK_LIMIT)
                self.assertEqual(len(helper.of(sid, "ready")), 1)
                snapshot = helper.of(sid, "state")[-1][2]
                self.assertEqual(snapshot["oldest"], oldest)
                buffered = spans(helper, sid)
                self.assertEqual(buffered[0][0], oldest)
                with urlopen(helper.stream_url(sid, oldest), timeout=10) as response:
                    _header, media = split_header(response.read())
                self.assertEqual(media, payload[payload.index(media[:188]) :])
                self.assertEqual(list(self.root.iterdir()), [], "the disk buffer was left behind")
                helper.close(sid)
                helper.wait_for(lambda message, sid=sid: message == ("closed", sid))
        self.assert_no_credentials(helper)

    def test_a_disk_session_removes_its_directory_once_closed(self):
        payload = make_stream(60)
        hung_up = threading.Event()
        source = self.source({SECRET_PATH: paced_route(payload, self.stop, hung_up)})
        helper = self.helper()
        directory = ".qitv-timeshift-" + secrets.token_hex(16)
        ring = {"kind": "disk", "root": str(self.root), "dir": directory, "max_bytes": 1 << 30}
        sid = helper.open(source.url + SECRET_PATH, ring=ring)
        helper.wait_for(lambda message: message == ("ready", sid))
        helper.wait_for(lambda m: m[0] == "state" and m[1] == sid and m[4])
        segments = list((self.root / directory).iterdir())
        self.assertTrue(any(item.name == "segment-0.ts" for item in segments), segments)
        helper.close(sid)
        helper.wait_for(lambda message: message == ("closed", sid), timeout=2)
        deadline = time.monotonic() + 3
        while (self.root / directory).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse((self.root / directory).exists())
        self.assertTrue(hung_up.wait(3))
        self.assertEqual(helper.of(sid, "error"), [])

    def test_owner_death_closes_the_upstream_connection(self):
        # A multiprocessing daemon flag only handles orderly owner exit. An abruptly
        # killed GUI must not leave its helper holding provider connections.
        with socket.create_server(("127.0.0.1", 0)) as listener:
            listener.settimeout(30)
            url = f"http://127.0.0.1:{listener.getsockname()[1]}{SECRET_PATH}"
            owner = subprocess.Popen(
                [sys.executable, "-c", OWNER, url, str(self.root)],
                cwd=REPO,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            helper_pid = holder_pid = None
            try:
                with listener.accept()[0] as peer:
                    peer.settimeout(5)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += peer.recv(4096)
                    helper_pid, holder_pid = map(int, owner.stdout.readline().split())
                    owner.kill()
                    owner.wait(5)
                    peer.settimeout(3)
                    try:
                        remaining = peer.recv(1)
                    except ConnectionResetError:
                        # Windows reports a reset when the exiting helper closes its
                        # socket, rather than an orderly TCP EOF.
                        remaining = b""
                    self.assertEqual(remaining, b"", "the orphaned helper kept its socket")
                self.assertEqual(still_running([helper_pid], 3), [], "the orphan kept running")
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(5)
                terminate(still_running([holder_pid, helper_pid], 0))
                owner.stdout.close()

    def test_the_helper_exits_at_once_when_its_owner_closes_the_pipe(self):
        # Whatever the helper was doing, and whatever thread its process runs besides,
        # it must end: a helper left behind keeps its provider connections (two at most
        # per provider) and its disk buffer.
        for variant in ("idle", "streaming", "stalled", "reader", "lingering thread"):
            with self.subTest(variant=variant):
                lingering = variant == "lingering thread"
                plan, hung_up = self.plan("idle" if lingering else variant)
                helper = self.helper(helper_with_a_lingering_thread if lingering else helper_main)
                url, ring = plan
                if url:
                    sid = helper.open(url, ring=ring)
                    helper.wait_for(lambda message, sid=sid: message == ("ready", sid))
                    if variant == "reader":
                        self.pause_a_reader(helper.stream_url(sid, 0))
                    time.sleep(0.2)  # the stalled session now waits in its read
                started = time.monotonic()
                helper.conn.close()
                helper.process.join(3)
                self.assertEqual(helper.process.exitcode, 0, "the helper outlived its pipe")
                self.assertLess(time.monotonic() - started, 3.0)
                self.assert_released(variant, plan, hung_up)

    def test_the_helper_exits_soon_after_its_owner_dies(self):
        # The GUI crashed or was force-quit: its pipe end closes, and the watchdog sees it go.
        variants = ("idle", "streaming", "stalled", "reader")
        planned = {variant: self.plan(variant) for variant in variants}
        plans = {variant: plan for variant, (plan, _hung_up) in planned.items()}
        owner = subprocess.Popen(
            [sys.executable, "-c", OWNER_OF_MANY, json.dumps(plans), str(self.root)],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        pids = []
        try:
            line = owner.stdout.readline()
            self.assertTrue(line, "the owner failed before its sessions were ready")
            report = json.loads(line)
            pids = [pid for pid, _base_url in report.values()]
            self.pause_a_reader(f"{report['reader'][1]}/s/stream/0.ts")
            time.sleep(0.2)  # the stalled session now waits in its read
            owner.kill()
            owner.wait(5)
            running = still_running(pids, 3)
            survivors = [variant for variant, (pid, _) in report.items() if pid in running]
            self.assertEqual(survivors, [], "these helpers outlived their owner")
            for variant, (plan, hung_up) in planned.items():
                with self.subTest(variant=variant):
                    self.assert_released(variant, plan, hung_up)
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(5)
            terminate(still_running(pids, 0))
            owner.stdout.close()

    def test_the_helper_discards_its_standard_output_and_error(self):
        # Native code and library loggers can print provider URLs straight to fd 1 or 2.
        pid = str(self.helper().process.pid)
        if sys.platform.startswith("linux"):
            targets = [os.readlink(f"/proc/{pid}/fd/{fd}") for fd in (1, 2)]
        elif os.name != "nt" and shutil.which("lsof"):
            listing = subprocess.run(
                ["lsof", "-a", "-p", pid, "-d", "1,2", "-Fn"],
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout.decode()
            targets = [line[1:] for line in listing.splitlines() if line.startswith("n")]
        else:
            self.skipTest("no way to list another process's descriptors here")
        self.assertEqual(targets, [os.devnull, os.devnull])


class SpawnTargetTests(unittest.TestCase):
    def test_the_spawn_target_imports_neither_qt_nor_the_network_stack(self):
        # Spawn imports the target's module before anything else runs: keep it fast.
        probe = (
            "import sys, services.live.worker; "
            "print(sorted({name.split('.')[0] for name in sys.modules} & "
            "{'PySide6', 'requests', 'urllib3', 'm3u8', 'Cryptodome'}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=REPO, capture_output=True, check=True, timeout=30
        )
        self.assertEqual(result.stdout.decode().strip(), "[]")


class LocalSourceTests(unittest.TestCase):
    def test_a_process_that_never_closes_its_local_source_still_exits(self):
        # A probe that crashed or a test run that was interrupted skips close(). A
        # non-daemon server thread then held its process at interpreter exit for good, an
        # orphan once its shell went away, together with the helper it owned.
        probe = "import sys; sys.path[:0] = ['tests']; from http_fixtures import LocalSource; "
        probe += "LocalSource({})"
        try:
            subprocess.run(
                [sys.executable, "-c", probe], cwd=REPO, capture_output=True, check=True, timeout=20
            )
        except subprocess.TimeoutExpired:
            self.fail("an unclosed LocalSource kept its process running")


class GatedConnection:
    """A pipe end whose sends wait until the test opens the gate."""

    def __init__(self):
        self.gate = threading.Event()
        self.sent = []

    def send(self, message):
        self.gate.wait(10)
        self.sent.append(message)


def state(sid, seq, first):
    return ("state", sid, {"newest": seq}, first, [(seq, 2.0 * seq, 2.0 * seq + 2, False)])


class OutboxTests(unittest.TestCase):
    """In process: one queue and one writer, so a parent that stops reading blocks nobody."""

    def test_a_parent_that_stops_reading_never_blocks_the_command_loop(self):
        # The review's deadlock: a pipe buffers only 8 KiB per direction on macOS. With a
        # blocking send under one lock, a session blocked in send held the lock while the
        # command loop waited for it, and the parent's command sends then blocked as well.
        # Whether states merge here depends on the runner's pipe buffer and speed, so this
        # test asserts no merge count (the gated tests below make merging deterministic).
        temporary = tempfile.TemporaryDirectory(prefix="qitv-live-worker-test-")
        self.addCleanup(temporary.cleanup)
        parent, child = multiprocessing.Pipe(duplex=True)
        helper = worker._Helper(child, temporary.name)
        loop = threading.Thread(target=helper.run, daemon=True)
        loop.start()
        self.assertTrue(parent.poll(10))
        self.assertEqual(parent.recv()[0], "hello")
        session = types.SimpleNamespace(cancelled=False)
        flooding = threading.Event()
        flooding.set()
        queued = []

        def flood():  # a session reporting as fast as segments close
            seq = 0
            while flooding.is_set():
                queued.append(state("flood", seq, max(0, seq - 10)))
                helper.send(queued[-1], session)
                seq += 1
                time.sleep(0.001)

        flooder = threading.Thread(target=flood, daemon=True)
        flooder.start()
        time.sleep(0.5)  # the parent is busy elsewhere: the pipe to it fills up
        sent = []

        def burst():  # then it forwards queued commands (rapid zapping) without reading
            for index in range(40):
                parent.send(("close", f"s{index:02d}" + "x" * 500))
                sent.append(index)

        burster = threading.Thread(target=burst, daemon=True)
        burster.start()
        burster.join(2)
        self.assertEqual(len(sent), 40, "the helper stopped reading commands")
        self.assertTrue(loop.is_alive())
        flooding.clear()
        flooder.join(5)
        received = []
        deadline = time.monotonic() + 10
        latest = queued[-1][2]  # the newest snapshot; its additions arrive merged

        def delivered():
            closes = sum(message[0] == "closed" for message in received)
            return closes == 40 and any(message[2:3] == (latest,) for message in received)

        while time.monotonic() < deadline and not delivered():
            if parent.poll(0.1):
                received.append(parent.recv())
        closed = [message[1] for message in received if message[0] == "closed"]
        self.assertEqual(closed, [f"s{index:02d}" + "x" * 500 for index in range(40)])
        states = [message for message in received if message[0] == "state"]
        self.assertEqual(states[-1][2:4], queued[-1][2:4])
        self.assertEqual(rebuild(states), rebuild(queued))
        parent.send(("shutdown",))
        loop.join(5)
        self.assertFalse(loop.is_alive())
        helper.shutdown()
        parent.close()
        child.close()

    def test_states_queued_behind_a_blocked_writer_merge_in_order(self):
        connection = GatedConnection()
        outbox = worker._Outbox(connection)
        outbox.put(("hello", "url"))  # the writer takes it and waits at the gate
        queued = {"a": [], "b": []}
        for seq in range(200):
            for sid in ("a", "b"):
                queued[sid].append(state(sid, seq, max(0, seq - 10)))
                outbox.put(queued[sid][-1])
            if seq == 99:
                outbox.put(("ready", "a"))
        connection.gate.set()
        outbox.close(5)
        self.assertEqual(
            [message[:2] for message in connection.sent],
            [("hello", "url"), ("state", "a"), ("state", "b"), ("ready", "a"), ("state", "a")],
        )
        a_before, b_all, a_after = connection.sent[1], connection.sent[2], connection.sent[4]
        self.assertEqual(a_before[2:4], queued["a"][99][2:4])  # merged up to the "ready"
        self.assertEqual(b_all[2:4], queued["b"][-1][2:4])
        self.assertEqual(a_after[2:4], queued["a"][-1][2:4])
        self.assertEqual([item[0] for item in a_after[4]], list(range(189, 200)))
        for sid in ("a", "b"):
            sent = [message for message in connection.sent if message[:2] == ("state", sid)]
            self.assertEqual(rebuild(sent), rebuild(queued[sid]))

    def test_queued_states_merge_but_keep_their_order_around_other_messages(self):
        connection = GatedConnection()
        outbox = worker._Outbox(connection)
        outbox.put(("hello", "url"))
        outbox.put(state("a", 0, 0))
        outbox.put(state("b", 0, 0))
        outbox.put(state("a", 1, 0))
        outbox.put(("ready", "a"))
        outbox.put(state("a", 2, 1))
        outbox.put(state("a", 3, 3))
        outbox.put(state("b", 1, 1))
        outbox.put(state("c", 0, 0), types.SimpleNamespace(cancelled=True))
        outbox.put(("closed", "b"))
        connection.gate.set()
        outbox.close(5)
        outbox.put(("ended", "a"))  # after close: dropped
        self.assertEqual(
            connection.sent,
            [
                ("hello", "url"),
                ("state", "a", {"newest": 1}, 0, [(0, 0.0, 2.0, False), (1, 2.0, 4.0, False)]),
                ("state", "b", {"newest": 1}, 1, [(1, 2.0, 4.0, False)]),
                ("ready", "a"),
                ("state", "a", {"newest": 3}, 3, [(3, 6.0, 8.0, False)]),
                ("closed", "b"),
            ],
        )
        everything = [state("a", 0, 0), state("a", 1, 0), state("a", 2, 1), state("a", 3, 3)]
        self.assertEqual(
            rebuild(m for m in connection.sent if m[:2] == ("state", "a")), rebuild(everything)
        )

    def test_malformed_commands_get_a_plain_reply_and_never_stop_the_writer(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-live-worker-test-")
        self.addCleanup(temporary.cleanup)
        parent, child = multiprocessing.Pipe(duplex=True)
        helper = worker._Helper(child, temporary.name)
        loop = threading.Thread(target=helper.run, daemon=True)
        loop.start()
        self.assertTrue(parent.poll(10))
        self.assertEqual(parent.recv()[0], "hello")
        failure = ("error", None, "crashed", "The live buffer stopped unexpectedly.")
        for command in (
            ("close", ["not", "a", "sid"]),  # an unhashable sid is never echoed
            ("close",),
            ("open", {"x": 1}, "http://127.0.0.1:9" + SECRET_PATH, {}, True, RAM, 6.0),
            ("open", "short"),
        ):
            with self.subTest(command=command[:2]):
                parent.send(command)
                self.assertTrue(parent.poll(2), "no reply to a malformed command")
                self.assertEqual(parent.recv(), failure)
        parent.send(("close", "ok"))
        self.assertTrue(parent.poll(2), "the writer stopped after a malformed command")
        self.assertEqual(parent.recv(), ("closed", "ok"))
        parent.send(("shutdown",))
        loop.join(5)
        self.assertFalse(loop.is_alive())
        helper.shutdown()
        parent.close()
        child.close()

    def test_one_bad_message_never_stops_the_writer(self):
        parent, child = multiprocessing.Pipe(duplex=True)
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        outbox = worker._Outbox(child)
        received = []
        with self.assertLogs("services.live.worker", "WARNING") as logs:
            outbox.put(("closed", ["unhashable"]))  # no session to track: sent as it is
            outbox.put(("error", "a", "internal", lambda: None))  # cannot be pickled: dropped
            outbox.put(("closed", "a"))
            while len(received) < 2 and parent.poll(2):
                received.append(parent.recv())
        self.assertEqual(received, [("closed", ["unhashable"]), ("closed", "a")])
        self.assertIn("PicklingError", " ".join(logs.output))
        outbox.close(2)

    def test_the_final_flush_is_bounded_when_the_parent_never_reads(self):
        connection = GatedConnection()
        outbox = worker._Outbox(connection)
        outbox.put(("hello", "url"))
        outbox.put(("closed", "a"))
        started = time.monotonic()
        outbox.close(0.3)
        self.assertLess(time.monotonic() - started, 0.5)
        connection.gate.set()


class InProcessHelper:
    """A `_Helper` command loop on a thread, with the parent's end of its pipe."""

    def __init__(self, test):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-live-worker-test-")
        test.addCleanup(temporary.cleanup)
        self.parent, child = multiprocessing.Pipe(duplex=True)
        self.helper = worker._Helper(child, temporary.name)
        self.loop = threading.Thread(target=self.helper.run, daemon=True)
        self.loop.start()
        self.messages = []
        test.addCleanup(child.close)  # cleanups run last-in first-out: after `stop`
        test.addCleanup(self.stop)
        self.base_url = self.wait_for(lambda message: message[0] == "hello")[1]

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            if self.parent.poll(min(remaining, 0.05)):
                message = self.parent.recv()
                self.messages.append(message)
                if predicate(message):
                    return message
        raise AssertionError(f"no matching helper message among {self.messages!r}")

    def stop(self):
        self.parent.send(("shutdown",))
        self.loop.join(5)
        self.helper.shutdown()
        self.parent.close()


class StartQualityTests(unittest.TestCase):
    """In process: the spans message carries how cleanly each segment starts."""

    def test_each_addition_ends_with_its_segments_start_quality(self):
        # One IDR every 15 s: IDR starts at 0 and 15 s, a forced cut without one at 10 s.
        upstream = LocalSource({SECRET_PATH: (200, {}, make_stream(24, gop_s=15.0))})
        self.addCleanup(upstream.close)
        helper = InProcessHelper(self)
        helper.parent.send(("open", "q", upstream.url + SECRET_PATH, {}, True, RAM, 6.0))
        helper.wait_for(lambda message: message == ("ended", "q"))
        buffered = rebuild(m for m in helper.messages if m[:2] == ("state", "q"))
        # (seq, start, end, discontinuity) as before, then the start quality.
        self.assertEqual([item[0] for item in buffered], [0, 1, 2])
        self.assertEqual([round(item[1], 3) for item in buffered], [0.0, 10.0, 15.0])
        self.assertEqual(
            [item[3:] for item in buffered],
            [
                (False, "key"),
                (False, "none"),
                (False, "key"),
            ],
        )


class ClosedAfterReleaseTests(unittest.TestCase):
    """The connection budget takes "closed" for "the old stream is closed" (spec §4.2)."""

    def test_closed_follows_the_close_of_a_windows_tls_upstream(self):
        # Forced Windows mode: a TLS socket is never touched from another thread, so its
        # reader notices the close within one 0.25 s slice and only then closes it.
        created = []
        sliced = source_module._sliced_tls_socket

        def recording(interrupter):
            base = sliced(interrupter)

            class Recorded(base):
                @classmethod
                def _create(cls, *args, **kwargs):
                    sock = super()._create(*args, **kwargs)
                    created.append(sock)
                    return sock

            return Recorded

        release = threading.Event()
        self.addCleanup(release.set)

        def stalled(request):
            request.wfile.write(make_stream(4))
            request.wfile.flush()
            release.wait(10)  # the connection stays open, silent

        context = self.enterContext(tls_server_context())
        upstream = LocalSource({SECRET_PATH: (200, {}, stalled)}, tls=context)
        self.addCleanup(upstream.close)
        self.enterContext(patch.object(source_module, "_INTERRUPT_OWNERS", True))
        self.enterContext(patch.object(source_module, "_sliced_tls_socket", recording))
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", InsecureRequestWarning)
        helper = InProcessHelper(self)
        helper.parent.send(("open", "tls", upstream.url + SECRET_PATH, {}, False, RAM, 6.0))
        helper.wait_for(lambda message: message == ("ready", "tls"))
        time.sleep(0.3)  # the reader now waits in a TLS slice
        started = time.monotonic()
        helper.parent.send(("close", "tls"))
        helper.wait_for(lambda message: message == ("closed", "tls"))
        still_open = [sock for sock in created if sock.fileno() != -1]
        self.assertEqual(len(created), 1)
        self.assertEqual(still_open, [], '"closed" came before the upstream connection closed')
        self.assertLess(time.monotonic() - started, 1.0)


class StuckSessionTests(unittest.TestCase):
    """In process: a session stuck where its stop event cannot reach (a DNS lookup or a TCP
    connect) must not hold up `close`, and once abandoned it must stay silent."""

    def test_close_answers_at_once_and_the_abandoned_thread_stays_silent(self):
        entered, release = threading.Event(), threading.Event()
        closed_sources = []

        class Source:
            burst_s = 0.0

            def chunks(self):
                yield make_stream(4)

            def close(self):
                closed_sources.append(self)

        def stuck_open(_url, **_options):
            entered.set()
            release.wait(10)  # deaf to the session's stop event, like getaddrinfo
            return Source()

        temporary = tempfile.TemporaryDirectory(prefix="qitv-live-worker-test-")
        self.addCleanup(temporary.cleanup)
        parent, child = multiprocessing.Pipe(duplex=True)
        self.addCleanup(parent.close)
        helper = worker._Helper(child, temporary.name)
        loop = threading.Thread(target=helper.run, daemon=True)
        with patch("services.live.source.open_source", stuck_open):
            loop.start()
            try:
                self.assertTrue(parent.poll(10))
                base_url = parent.recv()[1]
                parent.send(("open", "stuck", "http://192.0.2.1" + SECRET_PATH, {}, True, RAM, 6))
                self.assertTrue(entered.wait(5))
                session = helper._sessions["stuck"]
                started = time.monotonic()
                parent.send(("close", "stuck"))
                self.assertTrue(parent.poll(2), "close waited for the stuck session")
                self.assertEqual(parent.recv(), ("closed", "stuck"))
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertTrue(session.thread.is_alive())
            finally:
                release.set()
            session.thread.join(5)
            self.assertFalse(session.thread.is_alive())
        self.assertEqual(len(closed_sources), 1, "the late source was not closed")
        self.assertFalse(parent.poll(0.3), "the abandoned session sent a message")
        with self.assertRaises(HTTPError) as missing:
            urlopen(f"{base_url}/stuck/stream/0.ts", timeout=5)
        self.assertEqual(missing.exception.code, 404)
        missing.exception.close()
        parent.send(("shutdown",))
        loop.join(5)
        self.assertFalse(loop.is_alive())
        helper.shutdown()
        child.close()


class RamFallbackSizeTests(unittest.TestCase):
    """The helper's own disk-failure fallback is sized by the ring spec's ram_bytes (H12)."""

    def session(self, root, directory):
        from services.live.worker import _Session

        adopted = []
        helper = SimpleNamespace(
            root=str(root),
            send=lambda message, session=None: None,
            server=SimpleNamespace(add_session=lambda sid, ring, ended: adopted.append(ring)),
        )
        ring = {"kind": "disk", "dir": directory, "max_bytes": 1 << 30, "ram_bytes": 3 * 1024**2}
        return _Session(helper, "s1", "http://provider.test/1.ts", {}, True, ring, 6.0), adopted

    def test_a_disk_ring_that_cannot_start_falls_back_to_that_size(self):
        with tempfile.TemporaryDirectory() as root:
            session, _ = self.session(root, "not-a-session")  # an unusable directory
            ring = session._make_ring()
        self.assertIsInstance(ring, RamRing)
        self.assertEqual(ring._max_bytes, 3 * 1024**2)

    def test_a_disk_ring_failing_midway_falls_back_to_that_size(self):
        from services.live.ring import RingError

        with tempfile.TemporaryDirectory() as root:
            session, adopted = self.session(root, ".qitv-timeshift-" + secrets.token_hex(16))
            failing = SimpleNamespace(close=lambda: None)
            session._ring, session._disk = failing, True

            def write(ring):
                if ring is failing:
                    raise RingError("The disk buffer failed.")

            session._write(write, new_segment=True)
        self.assertEqual(len(adopted), 1)
        self.assertIsInstance(adopted[0], RamRing)
        self.assertEqual(adopted[0]._max_bytes, 3 * 1024**2)


if __name__ == "__main__":
    unittest.main()
