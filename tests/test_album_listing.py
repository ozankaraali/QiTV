"""A song's album and cover, read off the GUI thread (services.album_listing, ui.song_jobs).

The folder listing and its budget run on fakes (an injected parse); the legs that read real
files make tiny tagged songs with ffmpeg (skipped without it) and parse them with libVLC.
Every artist, album and song name here is made up, with a suffix per run: libVLC caches a
song's art under its artist and album, and a stale entry must never decide a test.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from timer_fixtures import wait_until

from services import album_listing
from services.album_listing import album_of, cover_candidates, list_album, music_files
from services.song_facts import BRAND_COLOURS, song_tags
from ui.song_jobs import AlbumJob, CoverJob

try:
    import vlc
except ImportError, OSError:
    vlc = None

FFMPEG = shutil.which("ffmpeg")
RUN = uuid.uuid4().hex[:6]  # this run's names: never an art cache entry from an earlier one
TEAL, GREEN, BLUE = "2A9D8F", "3BB273", "3A5BD9"


def _rgb(hex_colour):
    return tuple(int(hex_colour[i : i + 2], 16) for i in (0, 2, 4))


def _near(colour, target, tolerance=20):
    return all(abs(a - b) <= tolerance for a, b in zip(colour, target))


def _run(*args):
    subprocess.run(args, check=True, capture_output=True)


def _picture(path, hex_colour, size=48):
    _run(
        FFMPEG,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x{hex_colour}:s={size}x{size}",
        "-frames:v",
        "1",
        path,
    )


def _song(path, *, cover=None, **tags):
    """A one-second tone at `path`, with these tags (ffmpeg's names) and an embedded picture."""
    meta = []
    for key, value in tags.items():
        meta += ["-metadata", f"{key}={value}"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    if cover is None:
        _run(FFMPEG, "-y", *tone, "-c:a", "libmp3lame", *meta, path)
        return
    picture = os.path.join(os.path.dirname(path), f".pic-{uuid.uuid4().hex}.png")
    _picture(picture, cover)
    try:
        _run(
            FFMPEG, "-y", *tone, "-i", picture, "-map", "0", "-map", "1", "-c:a", "libmp3lame",
            "-c:v", "png", "-id3v2_version", "3", "-metadata:s:v", "comment=Cover (front)",
            *meta, path,
        )  # fmt: skip
    finally:
        os.remove(picture)


class _Folder:
    def __init__(self, test):
        self._dir = tempfile.TemporaryDirectory()
        test.addCleanup(self._dir.cleanup)
        self.path = self._dir.name

    def touch(self, *names):
        for name in names:
            Path(self.path, name).parent.mkdir(parents=True, exist_ok=True)
            Path(self.path, name).write_bytes(b"\x00" * 16)

    def __truediv__(self, name):
        return os.path.join(self.path, name)


def _fake_tags(album=""):
    return dict(song_tags({}), album=album)  # what libVLC read: only an album tag


class AlbumListingTests(unittest.TestCase):
    """The folder's audio files, each parsed in turn, within a budget and a cap."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _patched(self, parse):
        for target, value in (("_open_instance", lambda: object()), ("_parse", parse)):
            patcher = mock.patch.object(album_listing, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_the_folders_own_audio_files_in_natural_order(self):
        folder = _Folder(self)
        folder.touch("10 Quist.mp3", "2 Pell.FLAC", "1 Arn.m4a", "._1 Arn.m4a", ".hidden.mp3")
        folder.touch("cover.jpg", "notes.txt", "clip.mp4", "Disc 2/01 Deep.mp3")
        os.mkdir(folder / "folder.mp3")  # a folder named like a song
        self.assertEqual(
            [name for name, _size in music_files(folder.path)],
            ["1 Arn.m4a", "2 Pell.FLAC", "10 Quist.mp3"],
        )
        self.assertEqual(music_files(folder / "missing"), [])
        self.assertEqual(list_album(folder / "missing"), [])

    def test_the_album_listing_is_capped_and_never_blocks(self):
        folder = _Folder(self)
        folder.touch(*(f"{n:04d} Song.mp3" for n in range(1, 2001)), "._0001 Song.mp3")
        parsed = []

        def slow(_instance, path, timeout_ms, *, fetch=False):
            name = os.path.basename(path)
            parsed.append((name, timeout_ms))
            # Each file takes a while, and the fifth hangs until libVLC's timeout gives up.
            time.sleep(timeout_ms / 1000 if name == "0005 Song.mp3" else 0.05)
            return _fake_tags("Wexlo Tide")

        self._patched(slow)
        started = time.monotonic()
        tracks = list_album(folder.path, budget_s=0.6)
        took = time.monotonic() - started
        # A file's own 2 s timeout is cut to what is left of the budget.
        self.assertLess(took, 1.5, "the listing overran its budget (uncut, it takes 2.2 s)")
        self.assertTrue(0 < len(tracks) < 2000)
        self.assertTrue(all(t["parsed"] for t in tracks))
        self.assertEqual(tracks[0]["name"], "0001 Song.mp3")
        self.assertEqual([t["name"] for t in tracks], [n for n, _ms in parsed])
        self.assertTrue(all(0 < ms <= album_listing.PARSE_TIMEOUT_MS for _n, ms in parsed))
        self.assertLessEqual(dict(parsed)["0005 Song.mp3"], 600)
        self.assertEqual(tracks[0]["album"], "Wexlo Tide")
        self.assertEqual(tracks[0]["path"], folder / "0001 Song.mp3")
        self.assertEqual(tracks[0]["size"], 16)

        parsed.clear()
        self._patched(lambda _i, path, _ms, *, fetch=False: _fake_tags("Wexlo Tide"))
        tracks = list_album(folder.path, cap=25)
        self.assertEqual([t["name"] for t in tracks], [f"{n:04d} Song.mp3" for n in range(1, 26)])

        # From the GUI: the job returns at once, the album comes later, as plain data.
        self._patched(slow)
        job = AlbumJob()
        got = []
        job.listed.connect(lambda generation, album: got.append((generation, album)))
        playing = folder / "1500 Song.mp3"  # far past the cap: read first all the same
        started = time.monotonic()
        with mock.patch.object(album_listing, "LIST_BUDGET_S", 0.5):
            job.start(7, playing)
            self.assertLess(time.monotonic() - started, 0.5)  # returns at once: no parse here
            self.assertTrue(wait_until(lambda: got, 5000))
        generation, album = got[0]
        self.assertEqual(generation, 7)
        self.assertIsInstance(album, list)
        self.assertIn("1500 Song.mp3", [t["name"] for t in album])
        self.assertTrue(all(t["album"] == "Wexlo Tide" for t in album))

    def test_a_listing_cut_short_says_so(self):
        # Review item 12 (Task 5): an album its time, its cap or a newer start cut short can't
        # say what comes after its last track: `read` says whether the folder was read whole.
        folder = _Folder(self)
        folder.touch(*(f"{n:02d} Song.mp3" for n in range(1, 31)))
        self._patched(lambda _i, path, _ms, *, fetch=False: _fake_tags("Wexlo Tide"))
        playing = folder / "03 Song.mp3"
        album, whole = album_listing.read_album(playing)
        self.assertTrue(whole)
        self.assertEqual(len(album), 30)
        self.assertEqual(album_of(playing), album)  # album_of is the same album, without it
        album, whole = album_listing.read_album(playing, cap=10)
        self.assertFalse(whole)
        self.assertEqual(len(album), 10)
        album, whole = album_listing.read_album(playing, stop=lambda: True)
        self.assertFalse(whole)
        # From the GUI: `read` carries it beside the album; `listed` stays as it was.
        job = AlbumJob()
        got, listed = [], []

        def heard(generation, album, whole):
            got.append((generation, len(album), whole))

        job.read.connect(heard)
        job.listed.connect(lambda generation, album: listed.append((generation, len(album))))
        job.start(3, playing)
        self.assertTrue(wait_until(lambda: got and listed, 5000))
        self.assertEqual((got, listed), ([(3, 30, True)], [(3, 30)]))
        with mock.patch.object(album_listing, "LIST_CAP", 10):
            job.start(4, playing)
            self.assertTrue(wait_until(lambda: len(got) == 2, 5000))
        self.assertEqual(got[1], (4, 10, False))

    def test_a_newer_start_stops_the_older_listing(self):
        folder = _Folder(self)
        folder.touch(*(f"{n:03d} Song.mp3" for n in range(1, 400)))

        def slow(_instance, _path, _timeout_ms, *, fetch=False):
            time.sleep(0.02)
            return _fake_tags("Wexlo Tide")

        self._patched(slow)
        job = AlbumJob()
        got = []
        started = time.monotonic()
        job.listed.connect(lambda generation, _album: got.append((generation, time.monotonic())))
        with mock.patch.object(album_listing, "LIST_BUDGET_S", 2.0):
            job.start(1, folder / "001 Song.mp3")
            time.sleep(0.1)
            job.start(2, folder / "002 Song.mp3")
            self.assertTrue(wait_until(lambda: len(got) == 2, 15_000))
        answered = dict(got)
        self.assertEqual(sorted(answered), [1, 2])  # the older one is answered all the same
        self.assertLess(answered[1] - started, 1.0, "the older listing went on after a newer start")
        self.assertGreater(answered[2] - started, 1.5)  # the newer one ran its budget

    def test_the_listing_never_raises_and_a_file_not_read_is_no_albums(self):
        folder = _Folder(self)
        folder.touch("01 Arn.mp3", "02 Pell.mp3", "03 Quist.mp3", "04 Vale.mp3")
        failing = {"02 Pell.mp3"}

        def broken(_instance, path, _timeout_ms, *, fetch=False):
            name = os.path.basename(path)
            if name in failing:
                raise OSError("libVLC failed")
            if name == "04 Vale.mp3":
                return None  # its parse timed out (a slow share): not read either
            return _fake_tags()  # untagged: the numbered files make the album

        self._patched(broken)
        with self.assertLogs("services.album_listing", "INFO"):
            tracks = list_album(folder.path)
        self.assertEqual(
            [(t["name"], t["parsed"]) for t in tracks],
            [("01 Arn.mp3", True), ("02 Pell.mp3", False), ("03 Quist.mp3", True)]
            + [("04 Vale.mp3", False)],
        )
        self.assertEqual(tracks[1]["album"], "")  # not known: no tags
        # Unread files may be any album's: an untagged song's list never takes them.
        with self.assertLogs("services.album_listing", "INFO"):
            album = album_of(folder / "01 Arn.mp3")
        self.assertEqual([t["name"] for t in album], ["01 Arn.mp3", "03 Quist.mp3"])
        failing.add("01 Arn.mp3")  # the song's own tags not read: no list
        with self.assertLogs("services.album_listing", "INFO"):
            self.assertEqual(album_of(folder / "01 Arn.mp3"), [])
        with mock.patch.object(album_listing, "_open_instance", lambda: None):
            self.assertEqual(list_album(folder.path), [])  # no libVLC: no list
            self.assertEqual(album_of(folder / "03 Quist.mp3"), [])


@unittest.skipIf(FFMPEG is None or vlc is None, "ffmpeg makes the songs, libVLC reads them")
class RealSongTests(unittest.TestCase):
    """Songs made with ffmpeg, read by libVLC's parser (QiTV has no tag library)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_tags_are_read_and_only_the_album_is_listed(self):
        folder = _Folder(self)
        album, artist = f"Wexlo Tide {RUN}", f"Brannow Fell {RUN}"
        _song(
            folder / "01 Arn.mp3", title="Arn", artist=artist, album=album, track="1/3", disc="1/2"
        )
        _song(folder / "02 Pell.mp3", title="Pell", artist=artist, album=album, track="02")
        _song(folder / "03 Vale.mp3", title="Vale", artist=artist, album=f"Orrin Vale {RUN}")
        _song(folder / "04 Untagged.mp3")
        os.mkdir(folder / "Disc 2")  # a folder beside it is never read
        _song(folder / "Disc 2/01 Deep.mp3", title="Deep", artist=artist, album=album)
        folder.touch("._01 Arn.mp3", "notes.txt")
        tracks = list_album(folder.path)
        self.assertEqual(
            [t["name"] for t in tracks],
            ["01 Arn.mp3", "02 Pell.mp3", "03 Vale.mp3", "04 Untagged.mp3"],
        )
        first = tracks[0]
        self.assertEqual(
            (first["title"], first["artist"], first["album"], first["track"], first["disc"]),
            ("Arn", artist, album, 1, 1),
        )
        self.assertGreater(first["duration_ms"], 500)
        self.assertEqual(tracks[1]["track"], 2)
        self.assertEqual(tracks[3]["title"], "")  # libVLC's stand-in (the file name) is no title
        self.assertEqual(tracks[3]["album"], "")
        listed = album_of(folder / "02 Pell.mp3")
        self.assertEqual([t["name"] for t in listed], ["01 Arn.mp3", "02 Pell.mp3"])
        self.assertEqual(album_of(folder / "03 Vale.mp3"), [])  # alone in its album
        self.assertEqual(album_of(folder / "04 Untagged.mp3"), [])  # untagged, the only one so

    def _cover(self, path, art_url=""):
        job = CoverJob()
        got = []
        job.ready.connect(
            lambda generation, image, colours: got.append((generation, image, colours))
        )
        job.start(3, path, art_url)
        self.assertTrue(wait_until(lambda: got, 10_000))
        generation, image, colours = got[0]
        self.assertEqual(generation, 3)
        return image, colours

    def test_the_embedded_picture_is_the_cover(self):
        folder = _Folder(self)
        song = folder / "01 Arn.mp3"
        _song(song, cover=TEAL, title="Arn", artist=f"Kesh {RUN}", album=f"Thrum {RUN}")
        _picture(folder / "front.jpg", GREEN)  # the file's own picture comes first
        for art_url in ("", "attachment://cover.png"):
            with self.subTest(art_url=art_url):
                image, colours = self._cover(song, art_url)
                self.assertIsNotNone(image)
                self.assertLessEqual(max(image.width(), image.height()), 1024)
                self.assertTrue(_near(colours[0], _rgb(TEAL)), colours)

    def test_the_folders_cover_in_any_case_and_qitvs_order(self):
        folder = _Folder(self)
        song = folder / "01 Plain.mp3"
        _song(song, title="Plain", artist=f"Kesh {RUN}", album=f"Ober {RUN}")
        _picture(folder / "front.jpg", GREEN)  # libVLC's own guess (its exact-case names)
        _picture(folder / "Cover.PNG", BLUE)
        image, colours = self._cover(song)
        self.assertIsNotNone(image)
        self.assertTrue(_near(colours[0], _rgb(BLUE)), colours)
        self.assertEqual(cover_candidates(song)[0], folder / "Cover.PNG")

    def test_a_given_art_file_and_no_art_at_all(self):
        folder = _Folder(self)
        song = folder / "01 Plain.mp3"
        _song(song, title="Plain", artist=f"Kesh {RUN}", album=f"Lume {RUN}")
        art = _Folder(self)
        _picture(art / "art.png", TEAL)
        image, colours = self._cover(song, Path(art / "art.png").as_uri())
        self.assertIsNotNone(image)
        self.assertTrue(_near(colours[0], _rgb(TEAL)), colours)
        image, colours = self._cover(song)
        self.assertIsNone(image)
        self.assertEqual(colours, BRAND_COLOURS)
        image, colours = self._cover("http://radio.test/stream", "http://radio.test/logo.png")
        self.assertIsNone(image)  # never fetched: a radio's logo is the channel's
        self.assertEqual(colours, BRAND_COLOURS)


if __name__ == "__main__":
    unittest.main()
