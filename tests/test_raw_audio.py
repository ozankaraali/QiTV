"""A radio station's raw sound as MPEG-TS (2.14, services/live/raw_audio.py): its frames found
and cut, the station's inline metadata taken out and its song read, a reconnect's repeated
burst left out, and the TS the pipeline's segmenter reads, its clock running on."""

import random
import unittest

from radio_fixtures import (
    adts_frame,
    adts_stream,
    id3_tag,
    metadata_block,
    mp3_frame,
    mp3_stream,
    mpeg_frame,
    packets,
    payload_of,
    pcr_of,
    pes_units,
    pid_of,
    stream_types,
    with_icy,
)

from services.live import raw_audio
from services.live.raw_audio import (
    AUDIO_PID,
    PMT_PID,
    AudioFramer,
    AudioMuxer,
    IcyReader,
    RepeatCut,
    Song,
    decoded,
    sniff,
    stream_title,
)
from services.live.ts import Closed, Opened, Segmenter, _crc32

MP3_TICKS = 1152 * 90000 / 44100
ADTS_TICKS = 1024 * 90000 / 44100


def cut(data: bytes, sizes=(1, 7, 100, 417, 3000)) -> list[bytes]:
    """`data` in pieces of varied sizes, as a connection delivers it."""
    rng, pieces, at = random.Random(7), [], 0
    while at < len(data):
        size = rng.choice(sizes)
        pieces.append(data[at : at + size])
        at += size
    return pieces


def framed(data: bytes, framer: AudioFramer | None = None) -> list[bytes]:
    framer = framer or AudioFramer()
    return [frame.data for piece in cut(data) for frame in framer.feed(piece)]


class FramerTests(unittest.TestCase):
    def test_mp3_and_adts_frames_come_whole_in_any_pieces(self):
        for name, stream, frame in (
            ("mpeg-1", mp3_stream(40), mp3_frame),
            ("mpeg-2", mp3_stream(40, mpeg1=False), lambda k: mp3_frame(k, mpeg1=False)),
            ("adts", adts_stream(40), adts_frame),
        ):
            with self.subTest(name):
                self.assertEqual(framed(stream), [frame(k) for k in range(40)])

    def test_the_frames_kinds_lengths_and_rates(self):
        framer = AudioFramer()
        cases = (
            (mp3_stream(4), 417, 1152, 44100, 0x03),
            (mp3_stream(4, mpeg1=False), 208, 576, 22050, 0x04),
            (adts_stream(4), 371, 1024, 44100, 0x0F),
        )
        for data, length, samples, rate, kind in cases:
            with self.subTest(kind=kind):
                framer.reset()
                frame = framer.feed(data)[0]
                self.assertEqual((len(frame.data), frame.samples), (length, samples))
                self.assertEqual((frame.rate, frame.stream_type), (rate, kind))

    def test_a_connection_starting_mid_frame_finds_the_next_one(self):
        stream = mp3_stream(30)
        self.assertEqual(framed(stream[150:]), [mp3_frame(k) for k in range(1, 30)])
        self.assertEqual(framed(adts_stream(30)[50:]), [adts_frame(k) for k in range(1, 30)])

    def test_id3_tags_before_and_between_frames_are_stepped_over(self):
        stream = id3_tag(20) + mp3_stream(10) + id3_tag(5000) + mp3_stream(10, 10)
        self.assertEqual(framed(stream), [mp3_frame(k) for k in range(20)])

    def test_padded_frames_and_every_rate_and_layer(self):
        # A real encoder pads every few frames to keep its bitrate: 417 and 418 bytes mixed.
        padded = [mpeg_frame(k, padding=k % 3 == 1) for k in range(30)]
        self.assertEqual(framed(b"".join(padded)), padded)
        self.assertEqual({len(f) for f in padded}, {417, 418})
        cases = (
            # (version, layer, bitrate index, rate index): length, samples, rate, stream type
            ((3, 1, 9, 1), 384, 1152, 48000, 0x03),  # MPEG-1 L3 128k 48 kHz
            ((3, 1, 9, 2), 576, 1152, 32000, 0x03),  # 32 kHz
            ((3, 2, 10, 1), 576, 1152, 48000, 0x03),  # MPEG-1 Layer II 192k
            ((3, 3, 4, 1), 128, 384, 48000, 0x03),  # Layer I 128k
            ((2, 1, 8, 1), 192, 576, 24000, 0x04),  # MPEG-2 L3 64k 24 kHz
            ((2, 1, 8, 2), 288, 576, 16000, 0x04),  # 16 kHz
            ((0, 1, 4, 0), 208, 576, 11025, 0x04),  # MPEG-2.5 L3 32k 11.025 kHz
            ((0, 1, 4, 2), 288, 576, 8000, 0x04),  # 8 kHz
        )
        for (version, layer, bitrate, rate), length, samples, hz, kind in cases:
            with self.subTest(version=version, layer=layer, rate=hz):
                stream = b"".join(
                    mpeg_frame(k, version=version, layer=layer, bitrate=bitrate, rate=rate)
                    for k in range(5)
                )
                frames = AudioFramer().feed(stream)
                self.assertEqual(len(frames), 5)
                frame = frames[0]
                self.assertEqual((len(frame.data), frame.samples), (length, samples))
                self.assertEqual((frame.rate, frame.stream_type), (hz, kind))
        for rate, hz in ((3, 48000), (5, 32000), (6, 24000), (8, 16000), (11, 8000)):
            with self.subTest(adts=hz):
                frames = AudioFramer().feed(b"".join(adts_frame(k, rate=rate) for k in range(4)))
                self.assertEqual([(f.rate, f.samples) for f in frames], [(hz, 1024)] * 4)

    def test_a_large_id3_tag_between_frames_is_no_loss(self):
        # A station may send a tag with a picture between songs: over the 64 KiB a stream
        # may go without a frame, and no sign of a stream gone bad.
        framer = AudioFramer()
        stream = mp3_stream(10) + id3_tag(200_000) + mp3_stream(10, 10)
        out = [f.data for piece in cut(stream, (4096, 65536)) for f in framer.feed(piece)]
        self.assertEqual(out, [mp3_frame(k) for k in range(20)])
        framer = AudioFramer()
        for piece in cut(mp3_stream(10) + id3_tag(200_000), (4096, 65536)):
            framer.feed(piece)
            self.assertFalse(framer.unreadable)

    def test_damage_loses_one_frame_and_the_sync_comes_back(self):
        stream = bytearray(mp3_stream(20))
        stream[417 * 5 + 1] = 0x00  # frame 5's header is no header
        out = framed(bytes(stream))
        self.assertEqual(out, [mp3_frame(k) for k in range(20) if k != 5])

    def test_what_is_no_sound_gives_no_frame_and_counts_as_lost(self):
        framer = AudioFramer()
        noise = random.Random(1).randbytes(100_000).replace(b"\xff", b"\xfe")
        self.assertEqual([f for piece in cut(noise) for f in framer.feed(piece)], [])
        self.assertTrue(framer.unreadable)
        framer = AudioFramer()
        framer.feed(mp3_stream(50))
        self.assertFalse(framer.unreadable)


class SniffTests(unittest.TestCase):
    def test_sound_more_and_unreadable(self):
        cases = {
            "mp3": (mp3_stream(5), raw_audio.SOUND),
            "adts": (adts_stream(5), raw_audio.SOUND),
            "mid-frame": (mp3_stream(5)[200:], raw_audio.SOUND),
            "a tag first": (id3_tag(300) + mp3_stream(5), raw_audio.SOUND),
            "two frames": (mp3_stream(2), raw_audio.MORE),
            "a tag still coming": (id3_tag(3000)[:500], raw_audio.MORE),
            "ogg": (b"OggS\x00\x02" + bytes(500), raw_audio.UNREADABLE),
            "flac": (b"fLaC\x00\x00\x00\x22" + bytes(500), raw_audio.UNREADABLE),
            "asf": (bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c") + bytes(64), "unreadable"),
            "a page": (b"\r\n<!DOCTYPE html><html>", raw_audio.UNREADABLE),
            "a list": (b"[playlist]\nFile1=http://x.test/a\n", raw_audio.UNREADABLE),
            "mp4": (b"\x00\x00\x00\x20ftypisom" + bytes(64), raw_audio.UNREADABLE),
            "noise": (bytes(20_000), raw_audio.UNREADABLE),
        }
        for name, (data, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(sniff(data), expected)


class IcyTests(unittest.TestCase):
    def test_the_metadata_is_taken_out_and_its_songs_read_in_order(self):
        sound = mp3_stream(100)
        body = with_icy(sound, 8000, {1: "First - Song", 3: "Second - Song"})
        for sizes in ((1,), (7, 100, 417, 3000), (len(body),)):
            with self.subTest(sizes=sizes):
                reader, out = IcyReader(8000), []
                for piece in cut(body, sizes):
                    out.extend(reader.feed(piece))
                audio = b"".join(p for p in out if isinstance(p, bytes))
                self.assertEqual(audio, sound)
                self.assertEqual(
                    [p.text for p in out if isinstance(p, Song)], ["First - Song", "Second - Song"]
                )
                # Each song comes where its block was: after that many bytes of sound.
                before, seen = 0, []
                for piece in out:
                    if isinstance(piece, Song):
                        seen.append(before)
                    else:
                        before += len(piece)
                self.assertEqual(seen, [16000, 32000])

    def test_without_a_metaint_the_body_is_all_sound(self):
        self.assertEqual(IcyReader(0).feed(b"abc"), [b"abc"])
        for headers, every in (
            ({"icy-metaint": "16000"}, 16000),
            ({}, 0),
            ({"icy-metaint": "x"}, 0),
            ({"icy-metaint": "-5"}, 0),
        ):
            self.assertEqual(raw_audio.metaint(headers), every)

    def test_a_blocks_song(self):
        cases = {
            b"StreamTitle='Artist - Don't Stop';StreamUrl='http://x';": "Artist - Don't Stop",
            "StreamTitle='Mira Sol - Dönüş Yolu';".encode(): "Mira Sol - Dönüş Yolu",
            b"StreamTitle='';": "",
            b"StreamTitle='  A\x01B  ';": "A B",
            b"StreamUrl='x';": None,
            b"": None,
        }
        for meta, expected in cases.items():
            with self.subTest(meta=meta):
                song = stream_title(meta)
                self.assertEqual(song, None if expected is None else Song(expected))
        self.assertEqual(len(metadata_block("x")) % 16, 1)

    def test_a_song_in_no_utf_8_reads_in_the_stations_code_page(self):
        # The owner's stations are mostly Turkish: an older one sends its songs in cp1254.
        title = "Mira Sol - Gökyüzü Şarkısı"
        song = stream_title(f"StreamTitle='{title}';".encode("cp1254") + b"\x00\x00")
        self.assertTrue(song.legacy)  # no UTF-8: its bytes, for the place it shows
        self.assertEqual(decoded(song.text, song.legacy, "cp1254"), title)
        self.assertNotEqual(decoded(song.text, song.legacy, "cp1252"), title)
        latin = stream_title(b"StreamTitle='Wexlo Tide - Caf\xe9 \x01';")
        self.assertEqual(decoded(latin.text, latin.legacy, "cp1252"), "Wexlo Tide - Café")
        self.assertEqual(decoded("Dönüş", False, "cp1251"), "Dönüş")  # UTF-8: as it came
        self.assertEqual(decoded(song.text, True, "no-such-page"), song.text)  # unknown: as is


class RepeatCutTests(unittest.TestCase):
    def frames(self, start, count):
        return AudioFramer().feed(mp3_stream(count + 1, start))[:count]

    def test_a_reconnects_burst_that_repeats_what_was_sent_is_left_out(self):
        cut_ = RepeatCut()
        self.assertEqual(len(cut_.feed(self.frames(0, 100))), 100)
        cut_.reconnect()
        # The station starts the new listener 40 frames back: 40 repeat, then new sound.
        out = cut_.feed(self.frames(60, 80))
        self.assertEqual([f.data for f in out], [mp3_frame(k) for k in range(100, 140)])
        self.assertEqual(cut_.dropped, 40)

    def test_a_reconnect_past_what_was_sent_drops_nothing(self):
        cut_ = RepeatCut()
        cut_.feed(self.frames(0, 50))
        cut_.reconnect()
        self.assertEqual(len(cut_.feed(self.frames(70, 30))), 30)
        # Disarmed: the same frames later are the station's own (a jingle twice).
        self.assertEqual(len(cut_.feed(self.frames(70, 5))), 5)

    def test_a_burst_that_starts_in_silence_is_aligned_by_its_run(self):
        # Digital silence: frames alike byte for byte. The burst starts at a short silence
        # (10-12) while the sound sent ends in another (50-54): its first frame alone would
        # match the latest silence, and the whole burst would play twice.
        silent = mp3_frame(999_999)
        sent = [mp3_frame(k) if not (10 <= k <= 12 or k >= 50) else silent for k in range(55)]
        burst = sent[10:] + [mp3_frame(k) for k in range(55, 70)]
        cut_ = RepeatCut()
        framer = AudioFramer()
        cut_.feed(framer.feed(b"".join(sent)))
        cut_.reconnect()
        out = cut_.feed(AudioFramer().feed(b"".join(burst)))
        out += cut_.flush()
        self.assertEqual([f.data for f in out], [mp3_frame(k) for k in range(55, 70)])

    def test_after_a_long_outage_the_same_sound_again_is_the_stations_own(self):
        # A burst repeats at most the station's last minute: a station that plays a looped
        # programme again after an outage of minutes sends sound the listener has not missed
        # (dropping it would leave the buffer without sound for as long).
        now = [0.0]
        cut_ = RepeatCut(clock=lambda: now[0])
        cut_.feed(self.frames(0, 100))
        now[0] = 61.0
        cut_.reconnect()
        out = cut_.feed(self.frames(60, 80)) + cut_.flush()
        self.assertEqual(len(out), 80)
        now[0] = 62.0  # a quick reconnect: its burst's repeat goes
        cut_.reconnect()
        out = cut_.feed(self.frames(130, 20)) + cut_.flush()
        self.assertEqual([f.data for f in out], [mp3_frame(k) for k in range(140, 150)])

    def test_a_connection_that_ends_inside_the_run_passes_what_it_held(self):
        cut_ = RepeatCut()
        cut_.feed(self.frames(0, 20))
        cut_.reconnect()
        self.assertEqual(cut_.feed(self.frames(40, 3)), [])  # held for the run
        held = cut_.reconnect()  # the next connection: what was held goes on
        self.assertEqual([f.data for f in held], [mp3_frame(k) for k in range(40, 43)])
        self.assertEqual(cut_.flush(), [])

    def test_without_a_reconnect_nothing_is_left_out(self):
        cut_ = RepeatCut()
        cut_.feed(self.frames(0, 20))
        self.assertEqual(len(cut_.feed(self.frames(0, 20))), 20)


class MuxerTests(unittest.TestCase):
    def mux(self, data: bytes) -> bytes:
        muxer, framer = AudioMuxer(), AudioFramer()
        return b"".join(muxer.mux(framer.feed(piece)) for piece in cut(data))

    def test_the_ts_is_whole_packets_with_tables_and_one_pes_a_frame(self):
        for name, stream, kind, ticks, count in (
            ("mpeg-1", mp3_stream(200), 0x03, MP3_TICKS, 200),
            ("mpeg-2", mp3_stream(200, mpeg1=False), 0x04, 576 * 90000 / 22050, 200),
            ("adts", adts_stream(200), 0x0F, ADTS_TICKS, 200),
        ):
            with self.subTest(name):
                ts = self.mux(stream)
                every = packets(ts)
                self.assertTrue(all(p[0] == 0x47 for p in every))
                self.assertEqual(pid_of(every[0]), 0)
                self.assertEqual(pid_of(every[1]), PMT_PID)
                self.assertEqual(stream_types(ts, PMT_PID), [kind])
                for packet in every[:2]:
                    payload = payload_of(packet)
                    section = payload[1 : 1 + 3 + ((payload[2] & 0x0F) << 8 | payload[3])]
                    self.assertEqual(_crc32(section), 0)
                units = pes_units(ts, AUDIO_PID)
                self.assertEqual(len(units), count)
                frames = AudioFramer().feed(stream)
                self.assertEqual([body for _pts, body in units], [f.data for f in frames])
                expected = [round(126000 + k * ticks) for k in range(count)]
                self.assertEqual([pts for pts, _body in units], expected)

    def test_each_pes_carries_its_pcr_and_the_counters_run_on(self):
        ts = self.mux(mp3_stream(100))
        counters, pcrs = {}, []
        for packet in packets(ts):
            pid = pid_of(packet)
            if pid in counters:
                self.assertEqual(packet[3] & 0x0F, (counters[pid] + 1) & 0x0F)
            counters[pid] = packet[3] & 0x0F
            if pid == AUDIO_PID and packet[1] & 0x40:
                pcrs.append(pcr_of(packet))
        units = pes_units(ts, AUDIO_PID)
        self.assertEqual(pcrs, [pts - 63000 for pts, _ in units])
        tables = sum(1 for p in packets(ts) if pid_of(p) == 0)
        self.assertEqual(tables, 5)  # every half second (20 frames) of the 2.6 s

    def test_the_segmenter_cuts_it_into_sound_segments(self):
        ts = self.mux(mp3_stream(400))  # 10.4 s
        segmenter = Segmenter(clock=lambda: 0.0)
        events = [e for piece in cut(ts, (188 * 7, 4096)) for e in segmenter.feed(piece)]
        events += segmenter.finish()
        opened = [e for e in events if isinstance(e, Opened)]
        closed = [e for e in events if isinstance(e, Closed)]
        self.assertGreaterEqual(len(opened), 4)
        self.assertEqual(len(closed), len(opened))
        self.assertIsNone(segmenter.video_pid)
        self.assertFalse(any(e.discontinuity for e in opened[1:]))

    def test_the_clock_runs_on_across_a_reconnect(self):
        muxer, framer, cut_ = AudioMuxer(), AudioFramer(), RepeatCut()
        ts = muxer.mux(cut_.feed(framer.feed(mp3_stream(101))))
        framer.reset()
        cut_.reconnect()
        # The new connection starts mid-frame, 30 frames back.
        ts += muxer.mux(cut_.feed(framer.feed(mp3_stream(80, 70)[100:])))
        units = pes_units(ts, AUDIO_PID)
        steps = {b[0] - a[0] for a, b in zip(units, units[1:])}
        self.assertEqual(steps, {2351, 2352})
        self.assertEqual([body for _, body in units], [mp3_frame(k) for k in range(150)])

    def test_a_packed_segment_takes_its_own_time(self):
        muxer = AudioMuxer()
        data = id3_tag(timestamp=900_000) + adts_stream(5)
        pts, at = raw_audio.packed_start(data)
        self.assertEqual(pts, 900_000)
        ts = muxer.mux(AudioFramer().feed(data[at:]), pts=pts)
        self.assertEqual(pes_units(ts, AUDIO_PID)[0][0], 900_000)
        self.assertEqual(muxer.next_pts, round(900_000 + 5 * ADTS_TICKS))

    def test_the_clock_wraps_at_33_bits_and_starts_near_zero(self):
        for first in ((1 << 33) - 5000, 10):
            with self.subTest(first=first):
                muxer, framer = AudioMuxer(first), AudioFramer()
                ts = muxer.mux(framer.feed(mp3_stream(41)))
                units = pes_units(ts, AUDIO_PID)
                expected = [round(first + k * MP3_TICKS) % (1 << 33) for k in range(41)]
                self.assertEqual([pts for pts, _ in units], expected)
                pcrs = [pcr_of(p) for p in packets(ts) if pid_of(p) == AUDIO_PID and p[1] & 0x40]
                self.assertEqual(pcrs, [(pts - 63000) % (1 << 33) for pts in expected])
                self.assertEqual(muxer.next_pts, round(first + 41 * MP3_TICKS) % (1 << 33))
                segmenter = Segmenter(clock=lambda: 0.0)
                events = segmenter.feed(ts + muxer.mux(framer.feed(mp3_stream(200, 41))))
                opened = [e for e in events if isinstance(e, Opened)]
                self.assertFalse(any(e.discontinuity for e in opened[1:]))
                self.assertEqual(segmenter.dropped, [])

    def test_a_new_codec_after_a_reconnect_gets_a_new_program_map(self):
        muxer = AudioMuxer()
        first = muxer.mux(AudioFramer().feed(mp3_stream(3)))
        second = muxer.mux(AudioFramer().feed(adts_stream(3)))
        self.assertEqual(stream_types(first, PMT_PID), [0x03])
        self.assertEqual(stream_types(second, PMT_PID), [0x0F])
        version = lambda ts: (payload_of(packets(ts)[1])[6] >> 1) & 0x1F  # noqa: E731
        self.assertEqual((version(first), version(second)), (0, 1))


if __name__ == "__main__":
    unittest.main()
