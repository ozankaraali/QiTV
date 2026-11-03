"""AI captions' pipeline (2.9): what a speech model wrote that nobody said, the stream's clock,
the pieces speech is cut into, QiTV's libVLC decoding a stream's sound, and when the window
shows each caption."""

from array import array
import os
from pathlib import Path
import time
import unittest
import unittest.mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.captions import guards
from services.captions.pieces import RATE, WINDOW, Cutter
from services.captions.stream_clock import StreamClock, TsTimes, pts_seconds

FIXTURE = Path(__file__).parent / "fixtures" / "live.ts"


class GuardTests(unittest.TestCase):
    def test_what_the_broadcasts_showed_never_reaches_the_screen(self):
        self.assertEqual(guards.clean("system."), "")
        self.assertEqual(guards.clean("."), "")
        self.assertEqual(guards.clean("Altyazı M.K."), "")
        self.assertEqual(guards.clean("İzlediğiniz için teşekkür ederim."), "")
        self.assertEqual(guards.clean("Thanks for watching!"), "")
        self.assertEqual(guards.clean("Sen öyle. " * 40), "Sen öyle.")
        self.assertEqual(
            guards.clean("Kolay gelsin. Kolay gelsin. Hoş geldin."),
            "Kolay gelsin. Kolay gelsin. Hoş geldin.",
        )  # said twice: kept
        self.assertEqual(guards.clean("  Five others were  injured. "), "Five others were injured.")

    def test_a_piece_may_only_say_so_much(self):
        self.assertEqual(guards.token_cap(0), guards.TOKENS_BASE)
        self.assertGreater(guards.token_cap(6.0), guards.token_cap(2.0))


class TextLanguageTests(unittest.TestCase):
    """The language a caption is written in, from what the model wrote on the broadcasts."""

    def test_the_languages_the_model_wrote(self):
        from services.captions.text_language import guess

        cases = {
            "Tamam tamam problem yok ya kostümleri ben hallederim zaten merak etmeyin.": "Turkish",
            "Feda edenlerin canı şahidi. Bizler de söz veriyoruz.": "Turkish",
            "Rising food and fuel prices have been major issues for voters.": "English",
            "Die Regierung hat das nicht gesagt, und es ist auch nicht so.": "German",
            "Le gouvernement ne veut pas que les prix montent dans la ville.": "French",
            "El gobierno dijo que los precios no van a subir en la ciudad.": "Spanish",
            "好了。我们走吧。": "Chinese",
            "جد بكر، أنت بكر.": "Arabic",
            "И я огням лар, салю гню уленин": "Russian",
            "맛, 맛처럼 맛이.": "Korean",
        }
        for text, language in cases.items():
            with self.subTest(text=text):
                self.assertEqual(guess(text), language)

    def test_too_little_to_tell_says_so(self):
        from services.captions.text_language import guess

        self.assertIsNone(guess("Okay."))
        self.assertIsNone(guess("å ja."))

    def test_the_pieces_lock_the_language_once_they_agree(self):
        from services.captions.text_language import Votes

        votes = Votes()
        self.assertIsNone(votes.add("Turkish"))
        self.assertIsNone(votes.leading())  # one says nothing yet
        self.assertIsNone(votes.add("Chinese"))  # a short piece over music
        self.assertIsNone(votes.add("Turkish"))
        self.assertEqual(votes.leading(), "Turkish")
        self.assertEqual(votes.add("Turkish"), "Turkish")  # three of the last four
        self.assertEqual(votes.locked, "Turkish")
        self.assertIsNone(votes.add("Arabic"))  # locked: no more votes


def _packet(pid, payload, start=False):
    header = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, 0x10])
    body = payload[:184]
    return header + body + b"\xff" * (184 - len(body))


def _section(table_id, body):
    length = len(body) + 5 + 4  # the header after the length, the body, a CRC (unchecked)
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF, 0, 1, 0xC1, 0, 0])
    return b"\x00" + head + body + b"\0\0\0\0"


def _pts_bytes(pts):
    return bytes(
        [
            0x21 | ((pts >> 29) & 0x0E),
            (pts >> 22) & 0xFF,
            0x01 | ((pts >> 14) & 0xFE),
            (pts >> 7) & 0xFF,
            0x01 | ((pts << 1) & 0xFE),
        ]
    )


def _pes(stream_id, pts):
    return b"\x00\x00\x01" + bytes([stream_id, 0, 0, 0x80, 0x80, 5]) + _pts_bytes(pts) + b"\0" * 8


def _stream(video_pts, audio_pts, language=b"tur"):
    pmt_pid = 0x100
    pat = _section(0x00, bytes([0, 1, 0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF]))
    lang = bytes([0x0A, 4]) + language + b"\x00"
    streams = (
        bytes([0x1B, 0xE1, 0x01, 0xF0, 0x00]) + bytes([0x0F, 0xE1, 0x02, 0xF0, len(lang)]) + lang
    )
    pmt = _section(0x02, bytes([0xE1, 0x01, 0xF0, 0x00]) + streams)
    out = _packet(0, pat, True) + _packet(pmt_pid, pmt, True)
    for v, a in zip(video_pts, audio_pts):
        out += _packet(0x101, _pes(0xE0, v), True) + _packet(0x102, _pes(0xC0, a), True)
    return out


class StreamClockTests(unittest.TestCase):
    def test_the_tables_the_first_stamps_and_the_language_are_read_in_any_chunks(self):
        data = _stream([90_000, 93_600], [89_100, 92_700])
        times = TsTimes()
        for i in range(0, len(data), 100):  # chunks that cut packets
            times.feed(data[i : i + 100])
        self.assertEqual(times.video_pid, 0x101)
        self.assertEqual([(t.pid, t.language) for t in times.audio], [(0x102, "tur")])
        self.assertEqual(times.first_video_pts, 90_000)
        self.assertEqual(times.first_audio_pts, 89_100)
        self.assertFalse(times.jumped)

    def test_a_jump_in_the_pictures_stamps_ends_the_run(self):
        times = TsTimes()
        times.feed(_stream([90_000, 90_000 + 5 * 90_000], [90_000, 90_100]))
        self.assertTrue(times.jumped)

    def test_vlcs_blocks_land_on_the_timeline_from_the_segments_start(self):
        times = TsTimes()
        times.feed(_stream([90_000], [90_000 - 9_000]))  # the sound starts 0.1 s earlier
        clock = StreamClock(100.0, times)
        self.assertAlmostEqual(clock.place(5_000_000, 1600, RATE), 99.9)
        self.assertAlmostEqual(clock.place(5_100_000, 1600, RATE), 100.0)
        self.assertIsNone(clock.place(9_000_000, 1600, RATE))  # VLC's blocks stepped
        self.assertTrue(clock.broken)

    def test_a_radio_channel_is_timed_by_its_sound(self):
        # The helper cuts and times an audio-only channel by its sound (services.live.ts).
        pmt_pid = 0x100
        pat = _section(0x00, bytes([0, 1, 0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF]))
        pmt = _section(0x02, bytes([0xE1, 0x02, 0xF0, 0x00]) + bytes([0x0F, 0xE1, 0x02, 0xF0, 0]))
        data = _packet(0, pat, True) + _packet(pmt_pid, pmt, True)
        data += _packet(0x102, _pes(0xC0, 90_000), True) + _packet(0x102, _pes(0xC0, 91_800), True)
        times = TsTimes()
        times.feed(data)
        clock = StreamClock(30.0, times)
        self.assertTrue(clock.ready())
        self.assertAlmostEqual(clock.place(1_000_000, 1600, RATE), 30.0)

    def test_stamps_across_the_wrap(self):
        self.assertAlmostEqual(pts_seconds(100, (1 << 33) - 800), 0.01)


class CutterTests(unittest.TestCase):
    def cutter(self, speech):
        """A cutter whose detector hears speech where `speech(t)` says (t: the window's
        start, in seconds of what was pushed)."""
        clock = {"t": 0.0}

        def is_speech(window):
            said = speech(clock["t"])
            clock["t"] += WINDOW / RATE
            return said

        return Cutter(is_speech)

    def push(self, cutter, seconds, start=0.0):
        pieces = []
        step = 0.1
        for i in range(round(seconds / step)):
            pieces += cutter.push(start + i * step, array("f", bytes(4 * int(RATE * step))))
        return pieces

    def test_a_pause_ends_a_piece(self):
        cutter = self.cutter(lambda t: 1.0 <= t < 3.0 or 4.0 <= t < 5.0)
        pieces = self.push(cutter, 6.0)
        self.assertEqual(len(pieces), 2)
        self.assertAlmostEqual(pieces[0].start, 0.8, delta=0.07)
        self.assertAlmostEqual(pieces[0].end, 3.15, delta=0.07)
        self.assertGreater(pieces[0].speech_share, 0.8)

    def test_speech_without_a_pause_is_cut_at_the_length_the_lead_allows(self):
        cutter = self.cutter(lambda t: not (3.0 <= t < 3.1))  # one short pause between words
        cutter.set_max(4.0)
        pieces = self.push(cutter, 9.0)
        self.assertGreaterEqual(len(pieces), 2)
        self.assertAlmostEqual(pieces[0].end, 3.05, delta=0.1)  # at the pause, not at 4 s
        for piece in pieces:
            self.assertLessEqual(piece.end - piece.start, 4.0 + 1e-6)

    def test_the_length_stays_between_two_and_six_seconds(self):
        cutter = self.cutter(lambda t: True)
        cutter.set_max(0.5)
        self.assertEqual(cutter.max_s, 2.0)
        cutter.set_max(60)
        self.assertEqual(cutter.max_s, 6.0)

    def test_a_gap_in_the_sound_closes_the_open_piece(self):
        cutter = self.cutter(lambda t: True)
        pieces = self.push(cutter, 1.0) + self.push(cutter, 1.0, start=10.0)
        self.assertEqual(len(pieces), 1)
        self.assertLess(pieces[0].end, 1.01)


class DecoderTests(unittest.TestCase):
    """QiTV's own libVLC decodes a stream's sound as fast as its bytes come."""

    def test_the_fixtures_sound_decodes_at_16_khz_on_its_timeline(self):
        from services.vlc_bundle import use_bundled_vlc

        use_bundled_vlc()
        from services.captions.decoder import AudioDecoder

        data = FIXTURE.read_bytes()
        times = TsTimes()
        clock = StreamClock(50.0, times)
        placed = []
        decoder = AudioDecoder(
            lambda us, pcm: placed.append((clock.place(us, len(pcm) // 2, RATE), len(pcm)))
        )
        decoder.start()
        try:
            for i in range(0, len(data), 64 * 1024):
                times.feed(data[i : i + 64 * 1024])
                decoder.feed(data[i : i + 64 * 1024])
            decoder.end()
            deadline = time.monotonic() + 20
            while not decoder.finished() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            decoder.stop()
        seconds = sum(n for _, n in placed) / 2 / RATE
        self.assertAlmostEqual(seconds, 12.0, delta=0.3)
        starts = [at for at, _ in placed if at is not None]
        self.assertEqual(len(starts), len(placed))
        self.assertAlmostEqual(
            starts[0], 50.0 + pts_seconds(times.first_audio_pts, times.first_video_pts), delta=0.01
        )
        self.assertAlmostEqual(starts[-1], 50.0 + 12.0, delta=0.3)


class _Live:
    def __init__(self):
        self.position_s = 0.0
        self.floor = 0.0
        self.source = ("http://127.0.0.1:1/t/s1/side/3.ts", 3, 0.0)

    def position(self):
        return self.position_s

    def caption_source(self, after=None):
        self.asked_after = after
        if after is not None:
            return ("http://127.0.0.1:1/t/s1/side/9.ts", 9, after + 0.4)
        return self.source

    def set_caption_floor(self, seconds):
        self.floor = seconds


class _Overlay:
    def __init__(self):
        self.text = ""

    def show_text(self, text):
        self.text = text

    def clear(self):
        self.text = ""


class ControllerTimingTests(unittest.TestCase):
    """When the window shows a caption, with the process and the session faked."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication, QWidget

        from ui.captions_controller import CaptionsController

        self.app = QApplication.instance() or QApplication([])
        self.window = QWidget()
        self.window.live_session = _Live()
        self.overlay = _Overlay()
        self.captions = CaptionsController(self.window, overlay=self.overlay)
        self.sent = []
        self.captions._process.send = lambda *m: self.sent.append(m)
        self.captions._process.running = lambda: True
        self.captions._enabled = self.captions._ready = True
        self.captions._start_run()

    def at(self, position):
        self.window.live_session.position_s = position
        self.captions._last = None
        self.captions._tick()
        return self.overlay.text

    def test_a_caption_shows_from_its_words_start_for_what_they_last(self):
        run = self.captions._run
        self.assertEqual(self.sent[0][:4], ("start", run, "http://127.0.0.1:1/t/s1/side/3.ts", 0.0))
        self.captions._on_caption(run, 10.0, 12.0, "Merhaba")
        self.captions._on_caption(run, 12.2, 13.0, "Nasılsın")
        self.assertEqual(self.at(9.9), "")
        self.assertEqual(self.at(10.0), "Merhaba")
        self.assertEqual(self.at(12.1), "Merhaba")  # held a little after the words
        self.assertEqual(self.at(12.2), "Nasılsın")  # until the next one starts
        self.assertEqual(self.at(13.9), "")

    def test_a_caption_from_a_run_before_is_not_shown(self):
        self.captions._on_caption(self.captions._run - 1, 10.0, 12.0, "old")
        self.assertEqual(self.at(10.5), "")

    def test_a_late_caption_shows_for_what_is_left_and_a_too_late_one_never(self):
        run = self.captions._run
        self.window.live_session.position_s = 11.0
        self.captions._on_caption(run, 10.0, 12.0, "late")
        self.assertEqual(self.at(11.0), "late")
        self.captions._on_caption(run, 3.0, 5.0, "gone")
        self.assertEqual(self.at(11.5), "late")

    def test_another_session_reads_again_and_forgets_the_channel_before(self):
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 12.0, "one")
        self.window.live_session.source = ("http://127.0.0.1:1/t/s2/side/0.ts", 0, 0.0)
        self.at(10.5)
        self.assertEqual(self.captions._run, run + 1)
        self.assertEqual(self.captions._cues, [])

    def test_a_seek_past_what_was_read_reads_again_from_there(self):
        run = self.captions._run
        self.captions._on_progress(run, 20.0, 0.3)
        self.window.live_session.position_s = 10.0
        self.captions._last = (10.0, time.monotonic())
        self.window.live_session.position_s = 60.0
        self.captions._tick()
        self.assertEqual(self.captions._run, run + 1)

    def test_a_model_that_did_not_load_leaves_no_child_behind(self):
        stopped = []
        self.captions._process.stop = lambda: stopped.append(True)
        self.captions._on_failed("ModuleNotFoundError")
        self.assertEqual(stopped, [True])
        self.assertFalse(self.captions.enabled())

    def test_a_cancelled_downloads_late_answer_changes_nothing(self):
        from services.captions.download import CANCELLED

        class Download:
            def running(self):
                return False

        old, new = Download(), Download()
        toasts = []
        self.captions._toast = lambda text, kind="info": toasts.append(text)
        self.captions._enabled = False
        self.captions._leaving_download, self.captions._download = old, new
        self.captions._on_download_failed(old, CANCELLED)
        self.captions._on_downloaded(old)
        self.assertIs(self.captions._download, new)
        self.assertFalse(self.captions.enabled())
        self.assertEqual(toasts, [])
        self.captions._on_download_failed(new, CANCELLED)  # the viewer's own cancel: no toast
        self.assertEqual(toasts, [])

    def test_after_a_jump_it_reads_again_past_it_not_before(self):
        run = self.captions._run
        self.sent.clear()
        self.captions._on_resync(run, 20.0)
        self.captions._start_run()
        self.assertEqual(self.window.live_session.asked_after, 20.0)
        self.assertEqual(self.sent[0][2], "http://127.0.0.1:1/t/s1/side/9.ts")

    def test_a_language_the_pieces_agreed_on_is_the_runs_channel(self):
        self.captions._run_channel = "provider|A"
        self.captions._on_detected(self.captions._run, "Turkish")
        self.assertEqual(self.captions._detected, {"provider|A": "Turkish"})

    def test_an_ended_run_starts_again_once_and_keeps_its_captions(self):
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 12.0, "kept")
        self.captions._on_ended(run, "TimeoutError")
        self.at(10.5)  # the next tick: the same session, nothing cleared
        self.assertEqual(self.captions._cues, [(10.0, 12.0, "kept")])
        self.assertEqual(self.captions._run, run)
        self.captions._start_run()  # the restart
        self.assertEqual(self.captions._run, run + 1)
        self.assertFalse(self.captions._restart.isActive())

    def test_off_gives_the_picture_back_and_clears_the_screen(self):
        self.captions._on_caption(self.captions._run, 10.0, 12.0, "x")
        self.at(10.5)
        self.captions._process.stop = lambda: None
        self.captions._disable()
        self.assertEqual(self.overlay.text, "")
        self.assertEqual(self.window.live_session.floor, 0.0)


class _Target:
    def __init__(self, live, local=False, item=None):
        self.live, self.local, self.item = live, local, item or {}
        self.source, self.title = "films", "A film"


class _Controller:
    def __init__(self, target, session=None):
        self.target, self.session = target, session
        self.relayable = False  # a provider's film the film relay carries (TVController's)
        self.relays = 0

    def now_playing(self):
        return self.target

    def film_relay_wanted(self):
        return self.relayable

    def relay_film(self):
        self.relays += 1
        self.session.through = _Relay(self.session.url)
        return True


class _Relay:
    def __init__(self, source, followable=True):
        self.source, self.follower_url = source, "http://127.0.0.1:5000/t/follower.mkv"
        self.can_follow = followable
        self.stops = 0

    def followable(self):
        return self.can_follow

    def stop(self):
        self.stops += 1


class _Session:
    def __init__(self, url, ms):
        self.url, self.ms = url, ms
        self.through = None  # the film relay it plays through
        self.settled = True  # placed(): opened and where it resumes

    def time_ms(self):
        return self.ms

    def relay(self):
        return self.through

    def placed(self):
        return self.settled

    def audio_track(self):
        return 2

    def audio_tracks(self):
        return [(-1, "Disable"), (2, "Track 1")]


class _Catalogs:
    def __init__(self, kinds):
        self.kinds = kinds

    def provider(self, name):
        return {"type": self.kinds[name]}


class FilmCaptionsTests(unittest.TestCase):
    """A film's captions: its sound read a little ahead of the picture, on the film's clock."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication, QWidget

        from ui.captions_controller import FILM_AHEAD_S, CaptionsController

        self.ahead = FILM_AHEAD_S
        self.app = QApplication.instance() or QApplication([])
        self.window = QWidget()
        self.window.controller = _Controller(_Target(live=False, local=True))
        self.window.session = _Session("file:///films/a.mkv", 120_000)
        self.window.catalogs = _Catalogs({"Server": "JELLYFIN", "Provider": "XTREAM"})
        self.overlay = _Overlay()
        self.captions = CaptionsController(self.window, overlay=self.overlay)
        self.sent = []
        self.captions._process.send = lambda *m: self.sent.append(m)
        self.captions._process.running = lambda: True
        self.captions._supported = True
        self.captions._enabled = self.captions._ready = True

    def test_a_film_from_the_disk_reads_from_a_little_past_the_picture(self):
        self.assertTrue(self.captions.available())
        self.captions._start_run()
        kind, run, location, start = self.sent[0][:4]
        self.assertEqual((kind, location), ("film", "file:///films/a.mkv"))
        self.assertAlmostEqual(start, 120.0 + self.ahead)
        self.assertEqual(self.sent[1], ("viewer", 120.0))
        self.captions._on_caption(run, 127.0, 129.0, "Bonjour")
        self.window.session.ms = 127_500
        self.captions._last = None
        self.captions._tick()
        self.assertEqual(self.overlay.text, "Bonjour")

    def test_a_seek_in_the_film_reads_again_from_there(self):
        self.captions._start_run()
        run = self.captions._run
        self.captions._last = (120.0, time.monotonic())
        self.window.session.ms = 600_000
        self.captions._tick()
        self.assertEqual(self.captions._run, run + 1)
        self.assertAlmostEqual(self.sent[-2][3], 600.0 + self.ahead)

    def test_a_media_servers_film_is_read_as_it_is(self):
        self.window.session.url = "http://server.local/Videos/1/stream"
        self.window.controller.target = _Target(live=False, item={"_provider": "Server"})
        with unittest.mock.patch(
            "services.provider_ids.provider_of", lambda item: item["_provider"]
        ):
            self.assertTrue(self.captions.available())
            self.assertEqual(self.captions._film()[0], "http://server.local/Videos/1/stream")

    def provider_film(self):
        self.window.session.url = "http://provider.local/movie/u/p/7.mkv"
        self.window.controller = _Controller(
            _Target(live=False, item={"_provider": "Provider"}), self.window.session
        )
        patch = unittest.mock.patch(
            "services.provider_ids.provider_of", lambda item: item["_provider"]
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_providers_film_is_read_through_its_film_relay_never_again(self):  # 2.9
        self.provider_film()
        self.assertFalse(self.captions.available())  # one the relay can't carry (HLS)
        self.window.controller.relayable = True
        self.assertTrue(self.captions.available())  # offered: the relay will carry it
        self.assertIsNone(self.captions._film())  # not through one yet: nothing read
        self.window.session.through = _Relay(self.window.session.url)
        self.captions._start_run()
        kind, _run, location = self.sent[0][:3]
        self.assertEqual((kind, location), ("film", "http://127.0.0.1:5000/t/follower.mkv"))
        self.window.session.through = _Relay(self.window.session.url, followable=False)
        self.assertIsNone(self.captions._film())  # a relay the provider's answer declined

    def test_on_during_a_providers_film_sends_it_through_a_relay_once(self):
        self.provider_film()
        self.window.controller.relayable = True
        self.captions._enabled = False
        self.captions._enable("small")
        self.assertEqual(self.window.controller.relays, 1)
        self.captions._tick()
        self.captions._tick()
        self.assertEqual(self.window.controller.relays, 1)
        films = [m for m in self.sent if m[0] == "film"]
        self.assertEqual(len(films), 1)
        self.assertEqual(films[0][2], "http://127.0.0.1:5000/t/follower.mkv")

    def test_off_keeps_the_films_relay_and_on_again_reads_it_at_once(self):
        self.provider_film()
        self.window.controller.relayable = True
        relay = self.window.session.through = _Relay(self.window.session.url)
        self.captions._disable()
        self.assertIs(self.window.session.relay(), relay)  # no second reopen: kept to its end
        self.assertEqual(relay.stops, 0)
        self.captions._enable("small")
        self.assertEqual(self.window.controller.relays, 0)

    def test_a_film_still_opening_is_read_once_it_plays_where_it_resumes(self):
        self.window.session.settled = False
        self.captions._start_run()
        self.assertEqual(self.sent, [])
        self.captions._tick()
        self.assertEqual(self.sent, [])
        self.window.session.settled = True
        self.captions._tick()
        self.assertEqual(self.sent[0][:2], ("film", self.captions._run))
        self.assertAlmostEqual(self.sent[0][3], 120.0 + self.ahead)


if __name__ == "__main__":
    unittest.main()
