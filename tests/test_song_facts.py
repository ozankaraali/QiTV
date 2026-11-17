"""What a song is (services.song_facts): its album among the folder's files, the folder's
cover file and the cover's colours. Pure: no disk, no Qt. Every name here is made up."""

import os
import unittest

from services.song_facts import (
    BRAND_COLOURS,
    album_key,
    album_tracks,
    dominant_colours,
    folder_cover,
    local_path,
    song_tags,
    stand_in_names,
    track_number,
)

FOLDER = "/music/Wexlo Tide"


def _track(name, *, album="", artist="", album_artist="", disc=None, track=None, title=""):
    return {
        "path": f"{FOLDER}/{name}",
        "name": name,
        "size": 1000,
        "title": title,
        "artist": artist,
        "album_artist": album_artist,
        "album": album,
        "disc": disc,
        "track": track,
        "duration_ms": 180_000,
    }


def _names(tracks):
    return [t["name"] for t in tracks]


class AlbumTracksTests(unittest.TestCase):
    def test_album_never_mixes_albums(self):
        current = _track("02 Pell.mp3", album="Wexlo Tide", artist="Brannow Fell", track=2)
        folder = [
            _track("01 Arn.mp3", album="WEXLO  TIDE", artist="brannow fell", track=1),
            current,
            _track("03 Quist.mp3", album=" Wéxlo Tide ", artist="Brannow Fell", track=3),
            # The same album name by another artist is another album.
            _track("04 Dorran.mp3", album="Wexlo Tide", artist="Kesh Dorran", track=4),
            # Another album in the same folder, a file with no tags, a garbled tag.
            _track("05 Vale.mp3", album="Orrin Vale", artist="Brannow Fell", track=5),
            _track("06 Untagged.mp3", track=6),
            _track("07 Garbled.mp3", album="WÃ©xlo Tide", artist="Brannow Fell", track=7),
            # No artist on one side: the album tag alone decides.
            _track("08 Nameless.mp3", album="Wexlo Tide", track=8),
        ]
        self.assertEqual(
            _names(album_tracks(current, folder)),
            ["01 Arn.mp3", "02 Pell.mp3", "03 Quist.mp3", "08 Nameless.mp3"],
        )
        # A compilation: one album artist, each song its own artist.
        mix = [
            _track("01 Ober.mp3", album="Thrum Days", artist="Kesh", album_artist="Assorted Fen"),
            _track("02 Lume.mp3", album="Thrum Days", artist="Pell", album_artist="assorted fen"),
            _track("03 Yarr.mp3", album="Thrum Days", artist="Pell", album_artist="Other Fen"),
        ]
        self.assertEqual(_names(album_tracks(mix[0], mix)), ["01 Ober.mp3", "02 Lume.mp3"])
        # The numbered fallback never takes a numbered file of another album either.
        untagged = _track("02 Plain.mp3")
        folder = [
            _track("01 Plain.mp3"),
            untagged,
            _track("03 Vale.mp3", album="Orrin Vale"),
            _track("04 Plain.mp3"),
        ]
        self.assertEqual(
            _names(album_tracks(untagged, folder)), ["01 Plain.mp3", "02 Plain.mp3", "04 Plain.mp3"]
        )
        # A file whose tags were not read may be any album's: never in a list.
        unread = dict(_track("05 Unread.mp3"), parsed=False)
        self.assertEqual(
            _names(album_tracks(untagged, [*folder, unread])),
            ["01 Plain.mp3", "02 Plain.mp3", "04 Plain.mp3"],
        )
        self.assertEqual(album_tracks(dict(untagged, parsed=False), folder), [])
        tagged = [dict(t, album="Wexlo Tide", parsed=True) for t in folder]
        self.assertEqual(len(album_tracks(tagged[0], tagged)), 4)

    def test_album_keys_fold_case_marks_and_spaces(self):
        self.assertEqual(album_key("  Wéxlo \t  TIDE "), "wexlo tide")
        self.assertEqual(album_key("Kırık Sal"), album_key("KIRIK SAL"))
        self.assertEqual(album_key(""), "")
        self.assertEqual(album_key(None), "")
        self.assertNotEqual(album_key("WÃ©xlo Tide"), album_key("Wexlo Tide"))

    def test_numbered_files_without_tags(self):
        current = _track("02 Pell.mp3")
        folder = [
            _track("10 Quist.mp3"),
            _track("01 Arn.mp3"),
            current,
            _track("Bonus Thrum.mp3"),  # not numbered
            _track("2024-03-01 Memo.mp3"),  # a date, not a track number
            _track("3.Lume.mp3"),
            _track("004_Ober.mp3"),
            _track("5-Yarr.mp3"),
            _track("06Kesh.mp3"),  # a number with no separator after it
        ]
        self.assertEqual(
            _names(album_tracks(current, folder)),
            [
                "01 Arn.mp3",
                "02 Pell.mp3",
                "3.Lume.mp3",
                "004_Ober.mp3",
                "5-Yarr.mp3",
                "10 Quist.mp3",
            ],
        )

    def test_a_lone_song_has_no_list(self):
        alone = _track("Pell.mp3", album="Wexlo Tide", artist="Brannow Fell")
        self.assertEqual(album_tracks(alone, [alone]), [])
        others = [alone, _track("01 Arn.mp3", album="Orrin Vale"), _track("02 Quist.mp3")]
        self.assertEqual(album_tracks(alone, others), [])
        # No album tag and no number: no list, even beside numbered files.
        plain = _track("Pell.mp3")
        self.assertEqual(album_tracks(plain, [plain, _track("01 Arn.mp3")]), [])
        # Numbered, but the only numbered file without tags.
        numbered = _track("01 Pell.mp3")
        self.assertEqual(album_tracks(numbered, [numbered, _track("Arn.mp3")]), [])
        self.assertEqual(album_tracks(numbered, []), [])

    def test_the_current_song_is_in_its_list_when_the_listing_missed_it(self):
        current = _track("03 Quist.mp3", album="Wexlo Tide", track=3)
        folder = [_track("01 Arn.mp3", album="Wexlo Tide", track=1)]
        self.assertEqual(_names(album_tracks(current, folder)), ["01 Arn.mp3", "03 Quist.mp3"])

    def test_order_disc_track_name(self):
        tracks = [
            _track("d2t1.mp3", album="Thrum", disc=2, track=1),
            _track("d1t10.mp3", album="Thrum", disc=1, track=10),
            _track("Lume 10.mp3", album="Thrum", disc=1, track=2),
            _track("Lume 2.mp3", album="Thrum", disc=1, track=2),
            _track("d1none.mp3", album="Thrum", disc=1),  # no track number: after those with one
            _track("nodisc t1.mp3", album="Thrum", track=1),  # no disc: the first disc
        ]
        self.assertEqual(
            _names(album_tracks(tracks[0], tracks)),
            [
                "nodisc t1.mp3",
                "Lume 2.mp3",
                "Lume 10.mp3",
                "d1t10.mp3",
                "d1none.mp3",
                "d2t1.mp3",
            ],
        )

    def test_track_and_disc_numbers(self):
        for text, number in (
            ("3/12", 3),
            ("03", 3),
            (" 7 ", 7),
            ("1/2", 1),
            ("12", 12),
            (5, 5),
            ("", None),
            (None, None),
            ("A2", None),
            ("/12", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(track_number(text), number)


class TagTests(unittest.TestCase):
    def test_art_urls_name_local_files_only(self):
        self.assertEqual(
            os.path.basename(local_path("file:///art/Thrum%20Days.png")), "Thrum Days.png"
        )
        self.assertEqual(local_path("/art/front.jpg"), "/art/front.jpg")
        self.assertEqual(local_path(r"C:\art\front.jpg"), r"C:\art\front.jpg")
        for url in ("attachment://cover.png", "http://radio.test/logo.png", "", None):
            with self.subTest(url=url):
                self.assertIsNone(local_path(url))
        # A share's file URL: Windows reads it, elsewhere urllib refuses it (never raises).
        self.assertIn(local_path("file://server/share/a.png"), (None, r"\\server\share\a.png"))
        self.assertIn("a.mp3", stand_in_names("file://server/share/a.mp3"))

    def test_song_tags_from_what_libvlc_read(self):
        tags = song_tags(
            {
                "Title": " 03 Lume.mp3 ",
                "Album": " Thrum ",
                "TrackNumber": "3/12",
                "Date": "c. 2019",
            },
            stand_ins=stand_in_names("file:///music/03%20Lume.mp3"),
            duration_ms=-1,
        )
        self.assertEqual(
            (tags["title"], tags["album"], tags["track"], tags["year"], tags["duration_ms"]),
            ("", "Thrum", 3, "2019", None),
        )


class FolderCoverTests(unittest.TestCase):
    def test_folder_cover_any_case(self):
        self.assertEqual(
            folder_cover(["FRONT.png", "Folder.JPG", "cover.Jpeg", "notes.txt"]), "cover.Jpeg"
        )
        self.assertEqual(folder_cover(["front.jpg", "Folder.JPG"]), "Folder.JPG")
        self.assertEqual(folder_cover(["01 Arn.mp3", "Front.PNG"]), "Front.PNG")
        self.assertEqual(folder_cover(["cover.png", "COVER.JPG"]), "COVER.JPG")  # .jpg first
        self.assertEqual(folder_cover(iter(["cover.jpeg", "cover.png"])), "cover.jpeg")
        self.assertIsNone(folder_cover(["covers.jpg", "cover.gif", "._cover.jpg", "back.jpg"]))
        self.assertIsNone(folder_cover([]))


def _image(width, height, paint):
    data = bytearray()
    for y in range(height):
        for x in range(width):
            data += bytes(paint(x, y))
    return bytes(data)


def _near(colour, target, tolerance=24):
    return all(abs(a - b) <= tolerance for a, b in zip(colour, target))


class DominantColoursTests(unittest.TestCase):
    def test_dominant_colours_skip_greys_and_fall_back(self):
        red, blue = (200, 40, 40), (40, 70, 210)

        def mostly_grey(x, y):
            if y < 6:
                return red
            if y < 9:
                return blue
            return ((0, 0, 0), (255, 255, 255), (128, 128, 128), (60, 62, 61))[x % 4]

        colours = dominant_colours(_image(32, 32, mostly_grey), 32, 32)
        self.assertTrue(1 <= len(colours) <= 3)
        self.assertTrue(_near(colours[0], red), colours)  # the most of the colour comes first
        self.assertTrue(any(_near(c, blue) for c in colours), colours)
        for c in colours:
            self.assertGreater(max(c) - min(c), 40, f"a grey came through: {colours}")

        def all_grey(x, y):
            return ((0, 0, 0), (250, 250, 250), (120, 121, 119))[(x + y) % 3]

        self.assertEqual(dominant_colours(_image(32, 32, all_grey), 32, 32), BRAND_COLOURS)
        self.assertEqual(BRAND_COLOURS, [(0xE0, 0x8A, 0x5F), (0xC9, 0x6B, 0x43)])
        self.assertEqual(dominant_colours(b"", 0, 0), BRAND_COLOURS)
        # A few coloured pixels are no cover's colour: all grey still.
        speck = _image(32, 32, lambda x, y: red if (x, y) == (3, 3) else (90, 90, 90))
        self.assertEqual(dominant_colours(speck, 32, 32), BRAND_COLOURS)

    def test_a_large_picture_is_read_from_a_small_copy_and_k_bounds_the_colours(self):
        stripes = (
            (210, 50, 50),
            (50, 180, 60),
            (50, 60, 200),
            (220, 200, 40),
        )
        rgb = _image(64, 48, lambda x, y: stripes[x * 4 // 64])
        colours = dominant_colours(rgb, 64, 48, k=2)
        self.assertEqual(len(colours), 2)
        self.assertTrue(all(any(_near(c, s) for s in stripes) for c in colours), colours)
        one = dominant_colours(_image(8, 8, lambda x, y: (30, 140, 120)), 8, 8)
        self.assertEqual(len(one), 1)
        self.assertTrue(_near(one[0], (30, 140, 120), 8), one)
        # A buffer shorter than its size says: no picture.
        self.assertEqual(dominant_colours(b"\x00\x01", 4, 4), BRAND_COLOURS)


if __name__ == "__main__":
    unittest.main()
