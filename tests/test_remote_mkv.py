from pathlib import Path
import unittest

import mkv_builder as mb

from services.remote import mkv

FILMS = Path(__file__).parent / "fixtures" / "film"


def read_all(data: bytes, tracks: dict, start: int, step: int = 0) -> list[mkv.Frame]:
    """Every frame of `tracks` from the Cluster at `start`, fed whole or `step` bytes at a time."""
    reader = mkv.ClusterReader(tracks, start)
    frames: list[mkv.Frame] = []
    rest = data[start:]
    for at in range(0, len(rest), step or len(rest)):
        frames += reader.feed(rest[at : at + (step or len(rest))])
        if reader.done:
            break
    return frames


class FixtureTests(unittest.TestCase):
    """The H.264 + AAC film ffmpeg wrote (8 s, 24 frames a second, a keyframe each second)."""

    @classmethod
    def setUpClass(cls):
        cls.data = (FILMS / "h264_aac.mkv").read_bytes()
        cls.head = mkv.read_head(cls.data[:4096])

    def test_the_head_holds_info_tracks_and_where_the_cues_are(self):
        head = self.head
        self.assertEqual(head.info.scale_ns, 1_000_000)
        self.assertAlmostEqual(head.info.duration, 8000, delta=50)
        video, audio = head.tracks
        self.assertEqual(
            (video.kind, video.codec, video.width, video.height),
            ("video", "V_MPEG4/ISO/AVC", 160, 90),
        )
        self.assertEqual(video.private[1], 100)  # High profile, 8-bit
        self.assertEqual(
            (audio.kind, audio.codec, audio.rate, audio.language),
            ("audio", "A_AAC", 48000.0, "eng"),
        )
        self.assertIn(mkv.CUES, head.seeks)
        self.assertGreater(head.first_cluster, 0)

    def test_the_cues_name_each_keyframes_cluster(self):
        position = self.head.seeks[mkv.CUES]
        _id, size, header = mkv.element_header(self.data, position)
        cues = mkv.read_cues(
            self.data[position + header : position + header + size], self.head.segment
        )
        video = [cue for cue in cues if cue.track == 1]
        self.assertEqual([cue.time for cue in video], list(range(0, 8000, 1000)))
        for cue in video:
            self.assertEqual(mkv.element_header(self.data, cue.cluster)[0], mkv.CLUSTER)

    def test_every_frame_comes_however_the_bytes_arrive(self):
        tracks = {track.number: track for track in self.head.tracks}
        whole = read_all(self.data, tracks, self.head.first_cluster)
        self.assertEqual(sum(frame.track == 1 for frame in whole), 192)
        self.assertEqual(sum(frame.track == 2 for frame in whole), 376)
        self.assertEqual(read_all(self.data, tracks, self.head.first_cluster, step=97), whole)
        self.assertEqual(
            read_all(self.data, tracks, self.head.first_cluster, step=1)[:50], whole[:50]
        )
        keys = [frame.time for frame in whole if frame.track == 1 and frame.key]
        self.assertEqual(keys, list(range(0, 8000, 1000)))

    def test_only_the_tracks_asked_for(self):
        audio = self.head.tracks[1]
        frames = read_all(self.data, {audio.number: audio}, self.head.first_cluster)
        self.assertEqual({frame.track for frame in frames}, {2})

    def test_the_cues_after_the_clusters_end_the_reading(self):
        tracks = {track.number: track for track in self.head.tracks}
        reader = mkv.ClusterReader(tracks, self.head.first_cluster)
        reader.feed(self.data[self.head.first_cluster :])
        self.assertTrue(reader.done)

    def test_no_matroska(self):
        with self.assertRaises(mkv.MkvError):
            mkv.read_head(b"\x00\x00\x00\x18ftypmp42" + bytes(64))
        with self.assertRaises(mkv.MkvError):
            mkv.read_head(mb.element(mkv.EBML, mb.text(mkv.DOC_TYPE, "something")) + bytes(32))


class BlockTests(unittest.TestCase):
    """What mkvmerge writes and ffmpeg doesn't: laced frames, header stripping, zlib,
    BlockGroups, Clusters of unknown size."""

    AUDIO = mkv.MkvTrack(number=2, kind="audio", codec="A_AC3")

    def frames(self, tracks, *clusters):
        data = b"".join(clusters)
        return read_all(data, {track.number: track for track in tracks}, 0)

    def test_laced_frames_of_each_kind(self):
        sizes = (300, 299, 600, 7)
        frames = [bytes([n]) * size for n, size in enumerate(sizes)]
        for lacing in ("xiph", "ebml"):
            with self.subTest(lacing=lacing):
                read = self.frames(
                    [self.AUDIO], mb.cluster(40, mb.simple_block(2, 5, frames, True, lacing))
                )
                self.assertEqual([frame.data for frame in read], frames)
                self.assertEqual([frame.lace for frame in read], [0, 1, 2, 3])
                self.assertEqual({frame.time for frame in read}, {45})
        even = [bytes([n]) * 100 for n in range(3)]
        read = self.frames([self.AUDIO], mb.cluster(0, mb.simple_block(2, 0, even, True, "fixed")))
        self.assertEqual([frame.data for frame in read], even)

    def test_a_header_stripped_frame_gets_its_bytes_back(self):
        track = mkv.MkvTrack(number=2, kind="audio", codec="A_AC3", strip=b"\x0b\x77")
        read = self.frames([track], mb.cluster(0, mb.simple_block(2, 0, [b"rest"], True)))
        self.assertEqual(read[0].data, b"\x0b\x77rest")

    def test_zlib_frames_are_inflated(self):
        track = mkv.MkvTrack(number=2, kind="audio", codec="A_AAC", zlib=True)
        read = self.frames(
            [track], mb.cluster(0, mb.simple_block(2, 0, [mb.zlibbed(b"x" * 500)], True))
        )
        self.assertEqual(read[0].data, b"x" * 500)

    def test_a_block_group_is_a_keyframe_unless_it_references_another(self):
        video = mkv.MkvTrack(number=1, kind="video", codec="V_MPEG4/ISO/AVC")
        read = self.frames(
            [video],
            mb.cluster(0, mb.block_group(1, 0, b"I", None), mb.block_group(1, 40, b"P", -40)),
        )
        self.assertEqual([(frame.data, frame.key) for frame in read], [(b"I", True), (b"P", False)])

    def test_clusters_of_unknown_size(self):
        video = mkv.MkvTrack(number=1, kind="video", codec="V_MPEG4/ISO/AVC")
        data = mb.cluster(0, mb.simple_block(1, 0, [b"a"], True), unknown=True)
        data += mb.cluster(1000, mb.simple_block(1, 0, [b"b"], True), unknown=True)
        data += mb.element(mkv.CUES, b"")
        reader = mkv.ClusterReader({1: video}, 0)
        read = reader.feed(data)
        self.assertEqual([(frame.data, frame.time) for frame in read], [(b"a", 0), (b"b", 1000)])
        self.assertTrue(reader.done)

    def test_a_film_made_here_reads_back(self):
        """The builder's file (used by the films' tests) is a Matroska the reader reads."""
        data = mb.film(
            [mb.track(1, 1, "V_MPEG4/ISO/AVC", bytes(8)), mb.track(2, 2, "A_DTS")],
            [mb.cluster(0, mb.simple_block(1, 0, [b"k"], True))],
            1000,
        )
        head = mkv.read_head(data)
        self.assertEqual([track.codec for track in head.tracks], ["V_MPEG4/ISO/AVC", "A_DTS"])
        self.assertEqual(head.info.duration, 1000)
        self.assertEqual(mkv.element_header(data, head.seeks[mkv.CUES])[0], mkv.CUES)


if __name__ == "__main__":
    unittest.main()
