"""Smoke test of a radio station through the time-shift pipeline (2.14): the real helper
process, LiveSession and libVLC, offscreen and muted.

    QT_QPA_PLATFORM=offscreen QITV_SMOKE_MUTE=1 uv run python scripts/smoke_radio.py --out <dir>
        [--adts] [--url URL] [--seconds 45]

Without --url it serves a local station as an Icecast server sends one: a rising tone (ffmpeg
makes it: MP3, or AAC in ADTS with --adts) at real time after a 4 s burst at connect, with
`icy-metaint` blocks naming a new song every 8 s when asked, and its first connection broken
after 15 s (a reconnect, whose burst repeats what was sent). With --url it plays that station
instead, briefly (real streams play muted: libVLC's sound output is a dummy here, always).

Checked, each written to <out>/smoke-radio.json as {"name", "ok", "detail"}: VLC plays the
loopback stream (the pipeline, not a direct play); the position runs at about real time; the
song the station names reaches `LiveSession.stream_title`; the AI captions' side output decodes
to sound (`services.captions.decoder`, as the captions process reads it); a 4 s pause leaves it
at least 3 s behind live and Go live brings it back near its target; stay-live sets no rate
but 1x on the steady stream; the local station sees one connection at a time, and after its
reconnect the station plays on. The exit code is non-zero when a check failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional
from urllib.request import Request, urlopen

from services.live.server import ThreadingLoopbackHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("smoke_radio")
MiB = 1024 * 1024


class Report:
    def __init__(self, out: Path) -> None:
        self.out, self.checks = out, []

    def check(self, name: str, ok: bool, detail: Any = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        logger.info("%s %s: %s", "PASS" if ok else "FAIL", name, detail)
        (self.out / "smoke-radio.json").write_text(json.dumps(self.checks, indent=2, default=str))

    @property
    def failed(self) -> bool:
        return not all(check["ok"] for check in self.checks)


def tone(directory: Path, adts: bool) -> list[Any]:
    """A rising tone's frames (each one unlike the others), 30 s of them."""
    from services.live.raw_audio import AudioFramer

    path = directory / ("tone.aac" if adts else "tone.mp3")
    codec = ["-c:a", "aac", "-f", "adts"] if adts else ["-c:a", "libmp3lame", "-b:a", "128k"]
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i"]
        + ["aevalsrc=0.4*sin(2*PI*(300+15*t)*t):s=44100:d=30", "-ac", "2", *codec, str(path)],
        check=True,
    )
    return AudioFramer().feed(path.read_bytes())


class Station:
    """A local Icecast-like station on 127.0.0.1 (see the module)."""

    EVERY = 16000  # icy-metaint
    SONG_S = 8.0
    BURST_S = 4.0
    BREAK_S = 15.0

    def __init__(self, frames: list[Any], backlog_s: float = 4.0, over_s: float = 0.0) -> None:
        from http.server import BaseHTTPRequestHandler

        self.frames = frames
        self.BURST_S, self.over_s = backlog_s, over_s  # a connect's backlog, sent over this long
        self.frame_s = frames[0].samples / frames[0].rate
        self.started = time.monotonic()
        self.connections = 0
        self.open_now = 0
        self.most_open = 0
        self.asked_icy: list[bool] = []
        self.down_until = 0.0  # an outage (`outage`) lasts until then
        self.refused = 0
        lock = threading.Lock()
        station = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:
                icy = self.headers.get("Icy-MetaData") == "1"
                if time.monotonic() < station.down_until:  # an outage: no stream for now
                    station.refused += 1
                    self.send_error(503)
                    return
                with lock:
                    station.connections += 1
                    first = station.connections == 1
                    station.open_now += 1
                    station.most_open = max(station.most_open, station.open_now)
                    station.asked_icy.append(icy)
                try:
                    self.send_response(200)
                    kind = "audio/aac" if frames[0].stream_type == 0x0F else "audio/mpeg"
                    self.send_header("Content-Type", kind)
                    if icy:
                        self.send_header("icy-metaint", str(station.EVERY))
                    self.end_headers()
                    station.stream(self.wfile, icy, station.BREAK_S if first else None)
                except OSError:
                    pass
                finally:
                    with lock:
                        station.open_now -= 1

        self.server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/stream"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def on_air(self) -> int:
        """The frame on the air now (the station's own clock)."""
        return int((time.monotonic() - self.started) / self.frame_s) + int(
            self.BURST_S / self.frame_s
        )

    def stream(self, out: Any, icy: bool, lasts: Optional[float]) -> None:
        backlog = int(self.BURST_S / self.frame_s)
        first = index = max(0, self.on_air() - backlog)
        connected, sent = time.monotonic(), 0

        def due() -> int:  # the backlog at once (or paced over `over_s`), then real time
            elapsed = time.monotonic() - connected
            if self.over_s <= 0 or elapsed >= self.over_s:
                return self.on_air()
            return first + int((self.on_air() - first) * elapsed / self.over_s)

        while lasts is None or time.monotonic() - connected < lasts:
            while index >= min(due(), self.on_air()):
                time.sleep(0.02)
            if time.monotonic() < self.down_until:
                return  # the outage breaks the connection
            frame = self.frames[index % len(self.frames)].data
            song = f"Smoke - Song {1 + int(index * self.frame_s // self.SONG_S)}"
            index += 1
            if not icy:
                out.write(frame)
                continue
            at = 0
            while at < len(frame):
                take = min(len(frame) - at, self.EVERY - sent % self.EVERY)
                out.write(frame[at : at + take])
                at += take
                sent += take
                if sent % self.EVERY == 0:
                    text = f"StreamTitle='{song}';".encode()
                    text += b"\x00" * (-len(text) % 16)
                    out.write(bytes([len(text) // 16]) + text)

    def outage(self, seconds: float) -> None:
        """The station goes off the air for `seconds`: its connection breaks, and it answers
        503 meanwhile (its own clock goes on: what it sent then is missed)."""
        self.down_until = time.monotonic() + seconds

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def side_sound(url: str, seconds: float) -> tuple[int, float]:
    """The AI captions' read of `url` (the helper's side output) for `seconds`: how many 16 kHz
    samples the captions' decoder made of it, and their RMS."""
    from services.captions.decoder import AudioDecoder

    blocks: list[bytes] = []
    decoder = AudioDecoder(lambda _us, pcm: blocks.append(bytes(pcm)))
    decoder.start()
    deadline = time.monotonic() + seconds
    try:
        with urlopen(Request(url), timeout=10) as answer:
            while time.monotonic() < deadline:
                data = answer.read1(16 * 1024)
                if not data:
                    break
                decoder.feed(data)
    finally:
        decoder.end()
        wait = time.monotonic() + 5
        while not decoder.finished() and time.monotonic() < wait:
            time.sleep(0.05)
        decoder.stop()
    pcm = b"".join(blocks)
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0
    return len(samples), rms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--adts", action="store_true", help="the local station sends AAC")
    parser.add_argument("--url", help="play this station instead of the local one")
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--backlog", type=float, default=4.0, help="the local station's, s")
    parser.add_argument("--over", type=float, default=0.0, help="the backlog sent over, s")
    parser.add_argument("--outage", type=float, default=0.0, help="the local station off, s")
    parser.add_argument("--trace", action="store_true", help="log what plays each second")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(out / "smoke-radio.log"), logging.StreamHandler()],
    )
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication

    from services import playback
    from services.live.engine import LiveEngine
    from services.live.live_session import LiveSession
    from services.vlc_profile import DEFAULT_USER_AGENT, PlayKind

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    report = Report(out)
    work = Path(tempfile.mkdtemp(prefix="qitv-smoke-radio-"))
    station = None if args.url else Station(tone(work, args.adts), args.backlog, args.over)
    url = args.url or station.url  # type: ignore[union-attr]
    engine = LiveEngine(str(work / "timeshift"))
    engine.start()
    instance = playback.create_instance([*playback.default_args(), "--aout=dummy", "--vout=dummy"])
    session = playback.PlaybackSession(instance)
    live = LiveSession(engine, session)
    states: list[dict] = []
    live.timelineChanged.connect(lambda state: states.append(dict(state, at=time.monotonic())))
    started = time.monotonic()

    positions: list[tuple[float, float]] = []  # (wall, position) while it plays
    traced = [0.0]

    def trace() -> None:
        """--trace: each second, what plays and how (VLC's state, the timeline's)."""
        if not args.trace or time.monotonic() - traced[0] < 1.0:
            return
        traced[0] = time.monotonic()
        state = states[-1] if states else {}
        logger.info(
            "trace: vlc %s at %.2f s, position %.2f, edge %.2f, lag %.2f, stalled %s, held %s,"
            " reconnecting %s, rate %s",
            session.state().value,
            session.time_ms() / 1000.0,
            live.position(),
            float(state.get("live_edge", math.nan)),
            float(state.get("lag", math.nan)),
            state.get("stalled"),
            getattr(session, "_held", None),  # PlaybackSession.hold's (2.8.8)
            state.get("reconnecting"),
            state.get("rate"),
        )

    def run(seconds: float, until: Any = None) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            trace()
            if live.mode == "pipeline":
                positions.append((time.monotonic(), live.position()))
            if until is not None and until():
                return True
            time.sleep(0.02)
        return until is None or bool(until())

    def jumps(since: float, until: float) -> list[tuple[float, float]]:
        """The position's steps forward of more than a second between two looks (a start
        re-picked once the tune's backlog landed, a Go live), as (seconds in, step)."""
        seen = [(w, p) for w, p in positions if since <= w <= until]
        return [
            (round(b[0] - started, 1), round(b[1] - a[1] - (b[0] - a[0]), 2))
            for a, b in zip(seen, seen[1:])
            if b[1] - a[1] - (b[0] - a[0]) > 1.0
        ]

    def lag() -> float:
        state = states[-1] if states else {}
        return float(state.get("lag", math.nan))

    def tune(**more: Any) -> None:
        live.start(
            url,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            verify_ssl=True,
            latency="normal",
            user_agent=DEFAULT_USER_AGENT,
            disk=station is not None,  # a local station's buffer is kept on disk (park)
            max_bytes=64 * MiB,
            channel="smoke-station",
            radio=True,
            **more,
        )

    def plays() -> bool:
        return (
            live.mode == "pipeline"
            and session.kind == PlayKind.LIVE_PIPELINE
            and session.state().value == "playing"
        )

    def speed_over(seconds: float) -> float:
        p0, t0 = live.position(), time.monotonic()
        run(seconds)
        return (live.position() - p0) / (time.monotonic() - t0)

    tune()
    try:
        playing = run(20, plays)
        report.check(
            "plays from the loopback stream",
            playing,
            {"mode": live.mode, "kind": str(session.kind), "url": session.url},
        )
        if not playing:
            return 1
        report.check(
            "opened within 10 s", time.monotonic() - started < 10, time.monotonic() - started
        )
        landing = time.monotonic()
        run(11)  # the start's own landing: its backlog may move it forward once (_RELAND_S)
        landed = jumps(landing, time.monotonic())
        report.check("the start moves forward once at most", len(landed) <= 1, landed)
        p0, t0 = live.position(), time.monotonic()
        run(8)
        speed = (live.position() - p0) / (time.monotonic() - t0)
        report.check(
            "the position runs at real time",
            0.85 <= speed <= 1.15,
            {"speed": round(speed, 3), "jumps": jumps(t0, time.monotonic())},
        )
        title = live.stream_title()
        if station is not None:
            report.check("the station's song is named", title.startswith("Smoke - Song"), title)
        else:
            report.check("the station's song (if it names one)", True, title or "(none)")
        source = live.caption_source()
        report.check("the AI captions have a source", source is not None, source and source[0])
        if source is not None:
            heard: list[tuple[int, float]] = []
            reading = threading.Thread(target=lambda: heard.append(side_sound(source[0], 3.0)))
            reading.start()  # (the GUI thread goes on: the session's ticks run meanwhile)
            run(20, lambda: not reading.is_alive())
            count, rms = heard[0] if heard else (0, 0.0)
            report.check(
                "the captions' decoder makes sound of it",
                count > 16000 and (rms > 300 or station is None),
                {"samples": count, "rms": round(rms)},
            )
        steady = [round(float(s.get("rate", 1.0)), 3) for s in states if s["at"] >= t0]
        report.check(
            "stay-live keeps 1x on the steady stream", set(steady) == {1.0}, sorted(set(steady))
        )
        live.toggle_pause()
        run(4)
        live.toggle_pause()
        run(1.5)
        behind = lag()
        report.check("a 4 s pause leaves it behind live", behind >= 3.0, round(behind, 2))
        live.go_live()
        run(6)
        target = float(states[-1].get("target", 2.5)) if states else 2.5
        # As smoke_tv bounds it: the target, stay-live's band, a segment (a start lands on one:
        # 2 s of sound) and VLC's open.
        report.check(
            "Go live brings it near its target",
            lag() <= target + 0.75 + 2.0 + 0.5,
            (round(lag(), 2), target),
        )
        if station is not None:
            first_title = title
            run(max(0.0, args.seconds - (time.monotonic() - started)))
            p1, t1 = live.position(), time.monotonic()
            run(4)
            report.check(
                "it plays on after the station's reconnect",
                station.connections >= 2 and live.position() - p1 > 0.75 * (time.monotonic() - t1),
                {"connections": station.connections},
            )
            report.check(
                "one connection to the station at a time", station.most_open == 1, station.most_open
            )
            report.check("every request asks the song", all(station.asked_icy), station.asked_icy)
            report.check(
                "the song changes as the station names the next",
                live.stream_title() != first_title,
                (first_title, live.stream_title()),
            )
            breaks = engine.snapshot(live._sid).get("breaks") if live._sid else None
            report.check("no break in the timeline", not breaks, breaks)
            if args.outage > 0:
                # Off the air for a while: the buffer plays on, VLC waits at its edge, and once
                # the station is back it plays on at about 1x (its sound's clock ran on with
                # no gap: what the station sent meanwhile is missed, not sped through).
                before = station.connections
                station.outage(args.outage)
                back = run(args.outage + 90, lambda: station.connections > before)
                report.check(
                    "the station comes back after its outage",
                    back and live.mode == "pipeline",
                    {"refused": station.refused, "mode": live.mode},
                )
                run(4)
                returned = time.monotonic()
                after = speed_over(10)
                report.check(
                    "it plays on at about 1x after the outage",
                    0.85 <= after <= 1.15 and not jumps(returned, time.monotonic()),
                    {"speed": round(after, 3), "jumps": jumps(returned, time.monotonic())},
                )
            # A kept buffer (2.6): parked, taken back, live in the new run; then back across
            # the junction, the old run's sound and on into the new run's.
            kept = live.park()
            report.check("the station's buffer is kept", kept is not None, {"disk": bool(kept)})
            run(3)
            tune(kept=kept)
            report.check("taken back, it plays", run(20, plays), live.mode)
            run(2)
            report.check("live after the take-back", 0.85 <= speed_over(5) <= 1.15, "speed")
            parts = (states[-1].get("parts") or []) if states else []
            report.check("the take-back begins a part", len(parts) >= 2, parts)
            if len(parts) >= 2:
                live.seek_to(float(parts[-1][1]) - 6.0)  # 6 s into the old run's end
                run(20, plays)
                run(1)
                across = speed_over(10)
                report.check(
                    "played across the junction at real time",
                    0.85 <= across <= 1.15,
                    {"speed": round(across, 3), "from": float(parts[-1][1]) - 6.0},
                )
        rates = [round(float(s.get("rate", 1.0)), 3) for s in states]
        changes = sum(1 for a, b in zip(rates, rates[1:]) if a != b)
        # A start or a Go live that lands short builds its room once (one rate and back):
        # more is hunting.
        report.check(
            "stay-live never hunts", changes <= 2, {"changes": changes, "rates": sorted(set(rates))}
        )
    finally:
        live.stop()
        run(1)
        engine.shutdown()
        session.release()
        if station is not None:
            station.close()
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
