"""A song's album and cover, read off the GUI thread (services.album_listing, ui.song_jobs).

The folder listing and its budget run on fakes (an injected parse, and a clock that moves only
as that parse says, so a runner's disk and speed never decide a budget test); the legs that read
real files make tiny tagged songs with ffmpeg (skipped without it) and parse them with libVLC.
Every artist, album and song name here is made up, with a suffix per run: libVLC caches a
song's art under its artist and album, and a stale entry must never decide a test.
"""

import contextlib
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import threading
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
    # an AIFF's tags are an ID3 chunk (ffmpeg writes one when asked), an MP3's its ID3 tag
    aiff = str(path).lower().endswith((".aif", ".aiff"))
    audio = ["-c:a", "pcm_s16be", "-write_id3v2", "1"] if aiff else ["-c:a", "libmp3lame"]
    if cover is None:
        _run(FFMPEG, "-y", *tone, *audio, *meta, path)
        return
    picture = os.path.join(os.path.dirname(path), f".pic-{uuid.uuid4().hex}.png")
    _picture(picture, cover)
    attached = ["-disposition:v", "attached_pic"] if aiff else []
    try:
        _run(
            FFMPEG, "-y", *tone, "-i", picture, "-map", "0", "-map", "1", *audio,
            "-c:v", "png", *attached, "-id3v2_version", "3",
            "-metadata:s:v", "comment=Cover (front)", *meta, path,
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


class _Clock:
    """album_listing's clock in a test (its `time`): it moves only as the fake parse says, so
    the budget is tested exactly, whatever the runner's disk and speed (binary fractions of a
    second: no rounding)."""

    def __init__(self):
        self._now = 1024.0
        self._lock = threading.Lock()

    def monotonic(self):
        with self._lock:
            return self._now

    def sleep(self, seconds):
        with self._lock:
            self._now += seconds

    advance = sleep


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

    def _clocked(self):
        clock = _Clock()
        patcher = mock.patch.object(album_listing, "time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return clock

    @contextlib.contextmanager
    def _scandir_in(self, folder, order):
        """`folder` listed by the disk in another order (APFS, NTFS and ext4 each have theirs)."""
        real = os.scandir

        @contextlib.contextmanager
        def scandir(path):
            with real(path) as entries:
                found = list(entries)
            if os.path.samefile(path, folder):
                found = order(found)
            yield iter(found)

        with mock.patch.object(album_listing.os, "scandir", scandir):
            yield

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

    def test_aiff_and_the_other_audio_formats_are_the_folders_songs(self):
        # 2.11.1: an album of AIFF, WMA, Musepack … files is listed as one of MP3s is
        folder = _Folder(self)
        names = ["01 Arn.aif", "02 Pell.AIFF", "03 Quist.aifc", "04 Vale.wma", "05 Rook.mka"]
        names += ["06 Deep.caf", "07 Tern.mpc", "08 Wick.tta", "09 Moss.wv", "10 Fen.ape"]
        folder.touch(*names, "11 Notes.txt")
        self.assertEqual([name for name, _size in music_files(folder.path)], names[:-1])

    def test_the_album_listing_is_capped_and_never_blocks(self):
        folder = _Folder(self)
        folder.touch(*(f"{n:04d} Song.mp3" for n in range(1, 2001)), "._0001 Song.mp3")
        clock = self._clocked()
        parsed = []

        def slow(_instance, path, timeout_ms, *, fetch=False):
            name = os.path.basename(path)
            parsed.append((name, timeout_ms))
            # Each file takes 1/16 s, and the fifth hangs until libVLC's timeout gives up.
            clock.advance(timeout_ms / 1000 if name == "0005 Song.mp3" else 0.0625)
            return _fake_tags("Wexlo Tide")

        self._patched(slow)
        started = clock.monotonic()
        tracks = list_album(folder.path, budget_s=0.5)
        # Four files in 1/4 s, then the fifth's own 2 s timeout cut to the 1/4 s left: the
        # listing ends with its budget (uncut, it would have taken 2.25 s).
        self.assertEqual(clock.monotonic() - started, 0.5)
        self.assertEqual(
            parsed, [(f"{n:04d} Song.mp3", ms) for n, ms in enumerate((500, 437, 375, 312, 250), 1)]
        )
        self.assertEqual([t["name"] for t in tracks], [name for name, _ms in parsed])
        self.assertTrue(all(t["parsed"] for t in tracks))
        self.assertEqual(tracks[0]["album"], "Wexlo Tide")
        self.assertEqual(tracks[0]["path"], folder / "0001 Song.mp3")
        self.assertEqual(tracks[0]["size"], 16)

        parsed.clear()
        self._patched(
            lambda _i, path, _ms, *, fetch=False: parsed.append(path) or _fake_tags("Wexlo Tide")
        )
        tracks = list_album(folder.path, cap=25)
        self.assertEqual([t["name"] for t in tracks], [f"{n:04d} Song.mp3" for n in range(1, 26)])
        self.assertEqual(len(parsed), 25)  # the cap: no file past it is read

        # From the GUI: start() returns while the first parse still waits, and the album comes
        # later, as plain data; the playing song (far past the cap) is read first.
        parsed.clear()
        go = threading.Event()

        def gated(_instance, path, timeout_ms, *, fetch=False):
            go.wait(10)
            parsed.append((os.path.basename(path), timeout_ms))
            clock.advance(0.0625)  # eight files fill the 1/2 s budget
            return _fake_tags("Wexlo Tide")

        self._patched(gated)
        job = AlbumJob()
        got = []
        job.listed.connect(lambda generation, album: got.append((generation, album)))
        with mock.patch.object(album_listing, "LIST_BUDGET_S", 0.5):
            job.start(7, folder / "1500 Song.mp3")
            self.assertEqual((parsed, got), ([], []))  # never waits for the listing
            go.set()
            self.assertTrue(wait_until(lambda: got, 10_000))
        self.assertEqual(
            [name for name, _ms in parsed],
            ["1500 Song.mp3", *(f"{n:04d} Song.mp3" for n in range(1, 8))],
        )
        generation, album = got[0]
        self.assertEqual(generation, 7)
        self.assertIsInstance(album, list)
        self.assertEqual(
            [t["name"] for t in album],
            [*(f"{n:04d} Song.mp3" for n in range(1, 8)), "1500 Song.mp3"],
        )
        self.assertTrue(all(t["album"] == "Wexlo Tide" for t in album))

    def test_files_are_read_in_name_order_whatever_the_disk_lists(self):
        folder = _Folder(self)
        names = [
            "10 Quist.mp3",
            "Bonus.mp3",
            "2 Pell.mp3",
            "1 Arn.mp3",
            "01 Arn.mp3",
            "001 Arn.mp3",
        ]
        folder.touch(*names)
        parsed = []
        self._patched(
            lambda _i, path, _ms, *, fetch=False: parsed.append(os.path.basename(path))
            or _fake_tags()
        )
        # "001", "01" and "1" are the same number: their names, as written, tell them apart.
        in_order = [
            "001 Arn.mp3",
            "01 Arn.mp3",
            "1 Arn.mp3",
            "2 Pell.mp3",
            "10 Quist.mp3",
            "Bonus.mp3",
        ]
        orders = {
            "reversed": lambda found: found[::-1],
            "by name": lambda found: sorted(found, key=lambda e: e.name),
            "by name, reversed": lambda found: sorted(found, key=lambda e: e.name, reverse=True),
            "shuffled": lambda found: random.Random(3).sample(found, len(found)),
        }
        for label, order in orders.items():
            with self.subTest(order=label), self._scandir_in(folder.path, order):
                parsed.clear()
                self.assertEqual([n for n, _size in music_files(folder.path)], in_order)
                self.assertEqual([t["name"] for t in list_album(folder.path)], in_order)
                self.assertEqual(parsed, in_order)
                parsed.clear()
                album_of(folder / "2 Pell.mp3")  # the playing song first, then the rest in order
                self.assertEqual(
                    parsed, ["2 Pell.mp3", *(n for n in in_order if n != "2 Pell.mp3")]
                )

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

    def test_a_playlists_songs_are_read_by_name_in_its_order(self):
        # Task 9: a playlist's songs are read as it names them: those files only, in its order
        # (never sorted, never another file of their folders), the first LIST_CAP within
        # LIST_BUDGET_S; `read` says whether every one was.
        first, second = _Folder(self), _Folder(self)
        first.touch("01 Arn.mp3", "02 Pell.mp3", "Other.mp3")
        second.touch("09 Rook.mp3")
        parsed = []
        self._patched(
            lambda _i, path, _ms, *, fetch=False: parsed.append(os.path.basename(path))
            or _fake_tags("Tidewick Isle")
        )
        files = [(second / "09 Rook.mp3", 9), (first / "02 Pell.mp3", 2), (first / "01 Arn.mp3", 1)]
        job = AlbumJob()
        got = []
        job.read.connect(lambda generation, tracks, whole: got.append((generation, tracks, whole)))
        with mock.patch.object(album_listing, "music_files", side_effect=AssertionError("listed")):
            job.start_files(5, files)
            self.assertTrue(wait_until(lambda: got, 5000))
            with mock.patch.object(album_listing, "LIST_CAP", 2):
                job.start_files(6, files)
                self.assertTrue(wait_until(lambda: len(got) == 2, 5000))
        generation, tracks, whole = got[0]
        self.assertEqual((generation, whole), (5, True))
        self.assertEqual([(t["path"], t["size"]) for t in tracks], files)
        self.assertTrue(all(t["parsed"] and t["album"] == "Tidewick Isle" for t in tracks))
        self.assertEqual(parsed[:3], ["09 Rook.mp3", "02 Pell.mp3", "01 Arn.mp3"])
        generation, tracks, whole = got[1]
        self.assertEqual((generation, len(tracks), whole), (6, 2, False))

    def test_a_playlists_read_stops_when_it_is_given_up(self):
        # Fix round 1 (ruling P4-21): stop() ends the read between two files; it is answered
        # all the same, with what it had, as cut short.
        folder = _Folder(self)
        names = [f"{n:02d} Song.mp3" for n in range(1, 21)]
        folder.touch(*names)
        reading, go = threading.Event(), threading.Event()
        parsed = []

        def slow(_instance, path, _timeout_ms, *, fetch=False):
            parsed.append(os.path.basename(path))
            if len(parsed) == 1:
                reading.set()  # the first file is being read: the list is given up now
                go.wait(10)
            return _fake_tags()

        self._patched(slow)
        job = AlbumJob()
        got = []
        job.read.connect(lambda generation, tracks, whole: got.append((generation, tracks, whole)))
        job.start_files(1, [(folder / name, None) for name in names])
        self.assertTrue(reading.wait(10))
        job.stop()
        go.set()
        self.assertTrue(wait_until(lambda: got, 5000))
        generation, tracks, whole = got[0]
        self.assertEqual((generation, [t["name"] for t in tracks], whole), (1, names[:1], False))
        self.assertEqual(parsed, names[:1])

    def test_a_newer_start_stops_the_older_listing(self):
        folder = _Folder(self)
        folder.touch(*(f"{n:03d} Song.mp3" for n in range(1, 400)))
        clock = self._clocked()
        reading, go = threading.Event(), threading.Event()
        parsed = []  # (the listing's thread, the file)
        lock = threading.Lock()

        def slow(_instance, path, _timeout_ms, *, fetch=False):
            with lock:
                parsed.append((threading.get_ident(), os.path.basename(path)))
            if os.path.basename(path) == "001 Song.mp3" and not go.is_set():
                reading.set()  # the older listing reads its song: the newer start comes now
                go.wait(10)
            clock.advance(0.0625)
            return _fake_tags("Wexlo Tide")

        self._patched(slow)
        job = AlbumJob()
        got = []
        job.listed.connect(lambda generation, album: got.append((generation, len(album))))
        with mock.patch.object(album_listing, "LIST_BUDGET_S", 2.0):
            job.start(1, folder / "001 Song.mp3")
            self.assertTrue(reading.wait(10))
            job.start(2, folder / "002 Song.mp3")
            go.set()
            self.assertTrue(wait_until(lambda: len(got) == 2, 15_000))
        answered = dict(got)
        self.assertEqual(sorted(answered), [1, 2])  # the older one is answered all the same
        threads = {}
        for thread, name in parsed:
            threads.setdefault(thread, []).append(name)
        older = next(names for names in threads.values() if names[0] == "001 Song.mp3")
        newer = next(names for names in threads.values() if names[0] == "002 Song.mp3")
        self.assertEqual(older, ["001 Song.mp3"], "the older listing went on after a newer start")
        self.assertEqual(answered[1], 0)  # its song alone: no list
        self.assertGreater(len(newer), 2)  # the newer one reads on
        self.assertEqual(newer[:3], ["002 Song.mp3", "001 Song.mp3", "003 Song.mp3"])

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

    def test_an_aiffs_tags_and_picture_are_read(self):
        # 2.11.1: libVLC reads an AIFF's ID3 chunk (its tag reader): the album is listed, the
        # embedded picture is the cover, as an MP3's
        folder = _Folder(self)
        album, artist = f"Wexlo Tide {RUN}", f"Brannow Fell {RUN}"
        first = folder / "01 Arn.aif"
        _song(first, cover=TEAL, title="Arn", artist=artist, album=album, track="1")
        _song(folder / "02 Pell.aiff", title="Pell", artist=artist, album=album, track="2")
        tracks = list_album(folder.path)
        self.assertEqual(
            [(t["name"], t["title"], t["album"], t["track"]) for t in tracks],
            [("01 Arn.aif", "Arn", album, 1), ("02 Pell.aiff", "Pell", album, 2)],
        )
        self.assertEqual(tracks[0]["artist"], artist)
        self.assertGreater(tracks[0]["duration_ms"], 500)
        self.assertEqual([t["name"] for t in album_of(first)], ["01 Arn.aif", "02 Pell.aiff"])
        image, colours = self._cover(first)
        self.assertIsNotNone(image)
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
