"""The audio screen as the controller shows it (spec §10): a song, a file with no video track or
a radio channel plays on it, and an album plays on, track after track (Plan 4, Task 5).

The controller's own tests run over fakes (the player, the dock, the screen); the album's
listing and the cover are the test's (AlbumJob.start and CoverJob.start captured, answered by
hand). The window's own legs (the Library from a folder's song, the corner player, the ⋯ menu)
run in a real TVWindow, over libVLC with no picture or sound and songs ffmpeg makes."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from test_tv_controller import ControllerTestCase, FakeOverlay, FakeSession, FakeTransport
from timer_fixtures import pause, wait_until

from services.local_episodes import list_folder
from services.playback import PlaybackState
from services.song_facts import song_tags
from tests import test_tv_pip, test_tv_window as base
from ui.keymap import Action
from ui.overlays.audio_screen import AudioScreen
from ui.page_stack import PageStack
from ui.tv_controller import NO_PICTURE_S

FINISHED = ("Finished", "info")
FFMPEG = shutil.which("ffmpeg")
RUN = uuid.uuid4().hex[:6]  # this run's album names (libVLC's art cache: see make_song)


class AudioSession(FakeSession):
    """The fake player, with what the audio screen asks of a real one: whether its media has
    a video track (None: not known yet), its tags, and its picture's first showing."""

    pictureShown = Signal()

    def __init__(self):
        super().__init__()
        self.video = None  # has_video_track()
        self.meta: dict = {}  # tags() beyond the empty ones

    def has_video_track(self):
        return self.video

    def tags(self):
        return {**song_tags({}), **self.meta}

    def seek(self, ms):
        super().seek(ms)
        self.now_ms = ms


class AudioTransport(FakeTransport):
    """The fake transport, with the audio dock's mode and signals (ui/overlays/transport.py)."""

    prevTrackRequested = Signal()
    nextTrackRequested = Signal()
    shuffleRequested = Signal()
    repeatRequested = Signal()
    moreRequested = Signal(QPoint)
    volumeSetRequested = Signal(int)
    docked = False

    def show_audio(self, title, subtitle):
        self.record("show_audio", title, subtitle)
        self.visible, self.docked = True, True

    next_on = False  # its ⏭ acts (set_audio_state's has_next, else has_list)

    def set_audio_state(self, shuffle, repeat, has_list, has_next=None):
        self.record("set_audio_state", shuffle, repeat, has_list)
        self.next_on = has_list if has_next is None else has_next

    def audio(self):
        return self.docked

    def end_audio(self):
        self.record("end_audio")
        if self.docked:
            self.docked, self.visible = False, False

    def show_vod(self, title, subtitle):
        super().show_vod(title, subtitle)
        self.docked = False

    def show_live(self, title, subtitle, logo=None):
        super().show_live(title, subtitle, logo)
        self.docked = False

    def dismiss(self):
        self.record("dismiss")
        if not self.docked:
            self.visible = False  # the dock stays (it ends with end_audio or another mode)


class FakeScreen(FakeOverlay):
    """The audio screen's API as the controller uses it (checked against AudioScreen below)."""

    viewPicked = Signal(str)
    trackPicked = Signal(int)
    wordsRequested = Signal()
    upNextRequested = Signal()
    backRequested = Signal()

    def __init__(self):
        super().__init__()
        self._view = "cover"
        self.song = None
        self.tracks: list = []
        self.current = -1
        self.cover = None
        self.words = (False, "")
        self.playing = False

    def show(self):
        self.record("show")
        self.visible = True

    def hide(self):
        self.record("hide")
        self.visible = False

    def isHidden(self):
        return not self.visible

    def show_song(self, title, artist, album, *, year=""):
        self.record("show_song", title, artist, album, year)
        self.song = (title, artist, album, year)

    def set_cover(self, image, colours):
        self.record("set_cover", image, colours)
        self.cover = (image, list(colours))

    def set_tracks(self, tracks, current):
        self.record("set_tracks", len(tracks), current)
        self.tracks, self.current = list(tracks), current

    def set_view(self, view):
        if view not in ("cover", "album", "stage"):
            raise ValueError(view)
        self.record("set_view", view)
        self._view = view

    def view(self):
        return self._view

    def shown_view(self):
        return "cover" if self._view == "album" and len(self.tracks) < 2 else self._view

    def set_words(self, on, language):
        self.words = (on, language)

    def set_playing(self, playing):
        self.playing = playing


class FakeCaptions(QObject):
    """The AI captions as the controller and the screen ask them (CaptionsController)."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.on = False
        self.toggles = 0
        self.target = "Turkish"
        self.can = True  # available(): this computer runs the model, and the stream is read
        self.slow = False  # this computer's speed isn't known yet: turned on, they wait for it
        self.waits = False

    def available(self):
        return self.can

    def toggle(self):
        self.toggles += 1
        if self.slow and not self.on:
            self.waits = not self.waits  # pressed again while it is measured: off
        else:
            self.on = not self.on
        self.changed.emit()

    def scored(self):
        """The speed is known: one turned on while it was measured comes on."""
        self.slow = False
        if self.waits:
            self.waits, self.on = False, True
            self.changed.emit()

    def enabled(self):
        return self.on

    def wanted(self):
        return self.on or self.waits

    def downloading(self):
        return False

    def asking(self):
        return False

    def translate_to(self):
        return ""

    def language(self):
        return (self.target, "")

    def handle(self, action):
        return False

    def target_changing(self, local_video):
        pass

    def local_video_playing(self):
        pass


def track(folder, number, title, *, album="Harbour Lights", size=None):
    path = str(Path(folder) / f"{number:02d} {title}.mp3")
    return {
        **song_tags({}),
        "path": path,
        "name": os.path.basename(path),
        "size": size,
        "title": title,
        "artist": "Wexlo Tide",
        "album": album,
        "track": number,
        "duration_ms": 180_000,
        "parsed": True,
    }


class AudioCase(ControllerTestCase):
    """A controller over the audio fakes; AlbumJob and CoverJob answer when the test says."""

    def setUp(self):
        super().setUp()
        self.session = AudioSession()
        self.transport = AudioTransport()
        self.screen = FakeScreen()
        self.controller = self.make_controller(self.column)
        self.watch.mark_audio = lambda key: None
        self.window.captions = FakeCaptions()
        self.window.opening_over = mock.Mock()
        self.controller._sound_clock = self.clock
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.albums = mock.patch.object(self.controller._album_job, "start").start()
        self.covers = mock.patch.object(self.controller._cover_job, "start").start()
        self.addCleanup(mock.patch.stopall)
        self.runs: list[str] = []
        self.controller.start()

    def overlays(self, column):
        overlays = super().overlays(column)
        overlays.audio_screen = getattr(self, "screen", None)
        return overlays

    # ---- driving it -------------------------------------------------------------------------

    def open(self, path, *, size=None, video=None, playing=True):
        """A file opened in QiTV (TVWindow.open_paths holds first), playing at once."""
        self.controller.hold_for_file(True)
        self.session.video = video
        self.controller.play_file(str(path), size)
        if playing:
            self.session.stateChanged.emit("playing")

    def list_album(self, tracks, *, complete=True):
        """The newest album listing answers (as AlbumJob's thread would)."""
        generation, _path = self.albums.call_args.args
        self.controller._on_album_read(generation, list(tracks), complete)

    def end(self):
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()

    def played(self):
        urls = self.played_urls()
        return [Path.from_uri(url) if url.startswith("file:") else url for url in urls]

    def when_idle(self):
        self.controller.when_idle(lambda: self.runs.append("update question"))

    def album(self, count=3):
        return [track(self.folder, n, f"Song {n}", size=1000 + n) for n in range(1, count + 1)]

    def last_mode(self):
        """The transport's last mode call: (name, title)."""
        modes = ("show_vod", "show_audio", "show_live", "end_audio")
        calls = [c for c in self.transport.calls if c[0] in modes]
        return calls[-1][:2] if calls else None


class ScreenFollowsTheMediaTests(AudioCase):
    def test_the_screen_follows_the_media_not_the_extension(self):
        # Review focus 3. A song: the dock at once; its screen as it plays (QiTV's mark opens out
        # then), even before libVLC lists its tracks.
        self.open(self.folder / "01 Arn.mp3", playing=False)
        self.assertEqual(self.transport.named("show_audio"), [("01 Arn.mp3", "")])
        self.assertNotIn(("show_vod", "01 Arn.mp3", ""), self.transport.calls)
        self.assertFalse(self.screen.visible)
        self.session.stateChanged.emit("playing")
        self.assertTrue(self.screen.visible)
        self.assertEqual(self.screen.song[0], "01 Arn")  # untagged: the file's own name
        # A "music" file whose media has a picture (an .m4a with video): the film's controls,
        # never the screen.
        self.open(self.folder / "clip.m4a", video=True)
        self.assertFalse(self.screen.visible)
        self.assertEqual(self.last_mode(), ("show_vod", "clip.m4a"))
        self.assertFalse(self.transport.audio())
        # A video file with no video track: the screen once it plays, QiTV's mark out of it.
        self.window.opening_over.reset_mock()
        self.open(self.folder / "concert.mkv", video=False)
        self.assertTrue(self.screen.visible)
        self.assertTrue(self.transport.audio())
        self.assertEqual(self.screen.song[0], "concert")
        self.window.opening_over.assert_called()
        # A film: never.
        self.open(self.folder / "film.mkv", video=True)
        self.assertFalse(self.screen.visible)
        self.assertFalse(self.transport.audio())
        self.session.pictureShown.emit()
        self.assertFalse(self.screen.visible)
        # A film whose tracks aren't listed yet as it plays: its picture shows, and its controls
        # are left as they are (dismissed ones aren't shown again).
        self.open(self.folder / "film2.mkv", video=None)
        self.assertTrue(self.controller._sound_poll.isActive())
        self.transport.dismiss()
        shown = len(self.transport.named("show_vod"))
        self.session.pictureShown.emit()
        self.assertEqual(len(self.transport.named("show_vod")), shown)
        self.assertFalse(self.transport.visible)
        self.assertFalse(self.controller._sound_poll.isActive())
        # A file whose tracks aren't listed yet as it plays: looked at again until they are.
        self.open(self.folder / "show.mp4", video=None)
        self.assertFalse(self.screen.visible)
        self.assertTrue(self.controller._sound_poll.isActive())
        self.session.video = False
        self.controller._sound_watch()
        self.assertTrue(self.screen.visible)
        # And a song whose picture shows after all: the film's controls, the screen gone.
        self.open(self.folder / "02 Pell.mp3")
        self.assertTrue(self.screen.visible)
        self.session.pictureShown.emit()
        self.assertFalse(self.screen.visible)
        self.assertEqual(self.last_mode(), ("show_vod", "02 Pell.mp3"))

    def test_a_windows_path_keeps_its_album_row_and_cover(self):
        # Review M5: the album lists a song as play_file was given it; a file URL's round trip
        # spells it otherwise (Windows: slashes for backslashes). The song's own spelling is
        # what's looked up, so the next track keeps its row, its listing's tags and its cover.
        tracks = self.album(3)
        for t in tracks:
            t["path"] = "C:\\Music\\Harbour Lights\\" + t["name"]
        self.open(tracks[0]["path"], size=1001)
        self.list_album(tracks)
        generation = self.covers.call_args.args[0]
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        self.controller._on_cover_ready(generation, image, [(10, 20, 30)])
        self.end()
        self.assertEqual(self.session.plays[-1][0], Path(tracks[1]["path"]).absolute().as_uri())
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.screen.current, 1)
        self.assertEqual(self.screen.song[0], "Song 2")
        self.assertIs(self.screen.cover[0], image)  # no stand-in between the album's songs
        self.assertEqual(self.albums.call_count, 1)

    def test_tags_are_read_once_it_plays_and_the_cover_follows(self):
        # Review item 15: libVLC has read nothing before it plays.
        self.session.meta = {"title": "Pell", "artist": "Wexlo Tide", "album": "Harbour Lights"}
        self.open(self.folder / "02 Pell.mp3", playing=False)
        self.assertNotIn("show_song", [c[0] for c in self.screen.calls])
        self.covers.assert_not_called()
        self.session.meta.update(year=2024, art_url="file:///art/cache.png")
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.screen.song, ("Pell", "Wexlo Tide", "Harbour Lights", "2024"))
        generation, path, art = self.covers.call_args.args
        self.assertEqual((path, art), (str(self.folder / "02 Pell.mp3"), "file:///art/cache.png"))
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        self.controller._on_cover_ready(generation, image, [(10, 20, 30)])
        self.assertEqual(self.screen.cover, (image, [(10, 20, 30)]))
        # A pause and play again read nothing more.
        self.session.stateChanged.emit("paused")
        self.assertFalse(self.screen.playing)
        self.session.stateChanged.emit("playing")
        self.assertTrue(self.screen.playing)
        self.assertEqual(self.covers.call_count, 1)
        # A newer song of no album with it shows QiTV's drawn cover until its own comes, and a
        # late cover for what played before never reaches it.
        self.open(self.folder / "03 Rook.mp3")
        self.assertIsNone(self.screen.cover[0])
        self.controller._on_cover_ready(generation, image, [(1, 2, 3)])
        self.assertIsNone(self.screen.cover[0])

    def test_the_media_panel_has_a_songs_facts_while_its_screen_shows_them(self):
        # Task 8: the system's media panel has what the screen shows of the song playing now;
        # a song still opening after another's, or a film, has its title alone.
        self.session.meta = {"title": "Pell", "artist": "Wexlo Tide", "album": "Harbour Lights"}
        self.open(self.folder / "02 Pell.mp3", playing=False)
        self.assertIsNone(self.controller.panel_facts())  # its screen shows at its `playing`
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.controller.panel_facts(), ("Pell", "Wexlo Tide", "Harbour Lights"))
        self.session.meta = {"title": "Rook", "artist": "Kel Arno", "album": "Far Shore"}
        self.open(self.folder / "03 Rook.mp3", playing=False)
        self.assertTrue(self.screen.visible)  # Pell's facts still show on it
        self.assertIsNone(self.controller.panel_facts())
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.controller.panel_facts(), ("Rook", "Kel Arno", "Far Shore"))
        # An album's next track: its listing's facts as it opens.
        self.session.meta = {}
        tracks = self.album(3)
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks)
        self.assertEqual(self.controller.panel_facts(), ("01 Song 1", "", ""))  # as it shows
        self.controller.handle(Action.MEDIA_NEXT)
        self.assertEqual(self.controller.panel_facts(), ("Song 2", "Wexlo Tide", "Harbour Lights"))
        self.open(self.folder / "film.mkv", video=True)
        self.assertIsNone(self.controller.panel_facts())
        self.controller.stop()
        self.assertIsNone(self.controller.panel_facts())

    def test_a_stop_or_a_channel_ends_the_screen_and_the_dock(self):
        self.open(self.folder / "01 Arn.mp3")
        self.assertTrue(self.screen.visible)
        self.controller.stop()
        self.assertFalse(self.screen.visible)
        self.assertFalse(self.transport.audio())
        self.assertIn(("end_audio",), self.transport.calls)
        self.open(self.folder / "01 Arn.mp3")
        self.play(0)  # a channel's link came: it replaces the song
        self.assertFalse(self.screen.visible)
        self.assertFalse(self.transport.audio())


class RadioTests(AudioCase):
    def tune_radio(self):
        self.play(0)
        self.session.video = False
        self.session.stateChanged.emit("playing")

    def reopen(self):
        """The same channel opens again (a reconnect): its next `playing`."""
        self.session.stateChanged.emit("opening")
        self.session.stateChanged.emit("playing")

    def test_radio_screen_follows_the_picture(self):
        # Review focus 4 and item 13: a live stream may list its sound before its picture, so
        # the screen waits until there has been no picture for NO_PICTURE_S while it plays.
        # libVLC 3's track list only grows within an open (measured on hel, fix round 1): a
        # picture listed, or shown, ends the watch until the channel's next `playing`.
        poll = self.controller._sound_poll
        self.tune_radio()
        self.assertTrue(poll.isActive())
        self.assertFalse(self.screen.visible)
        self.clock.now += NO_PICTURE_S - 0.5
        self.controller._sound_watch()
        self.assertFalse(self.screen.visible)
        self.session.video = True  # its picture is listed after all: a TV channel
        self.controller._sound_watch()
        self.assertFalse(poll.isActive())  # the watch rests (review M7)
        self.session.video = False
        self.clock.now += 5 * NO_PICTURE_S
        self.controller._sound_watch()
        self.assertFalse(self.screen.visible)  # never for this open
        # A reopen with no picture: counted anew, shown after NO_PICTURE_S.
        self.reopen()
        self.assertTrue(poll.isActive())
        self.clock.now += NO_PICTURE_S - 0.5
        self.controller._sound_watch()
        self.assertFalse(self.screen.visible)
        self.clock.now += 1.0
        self.controller._sound_watch()
        self.assertTrue(self.screen.visible)
        self.assertTrue(self.screen.playing)
        # The Cover view, the channel's name, and the live controls unchanged (no dock).
        self.assertEqual(self.screen.view(), "cover")
        self.assertEqual(self.screen.song[0], "One")
        self.assertNotIn("show_audio", [c[0] for c in self.transport.calls])
        # Its picture shows: the screen goes at once, and the watch rests for this open.
        self.session.pictureShown.emit()
        self.assertFalse(self.screen.visible)
        self.assertFalse(poll.isActive())
        # Back after a reopen with no picture; a picture listed then hides it at once too.
        self.reopen()
        self.clock.now += NO_PICTURE_S + 0.1
        self.controller._sound_watch()
        self.assertTrue(self.screen.visible)
        self.session.video = True
        self.controller._sound_watch()
        self.assertFalse(self.screen.visible)
        self.assertFalse(poll.isActive())
        # Paused or not listed (None): nothing changes, nothing counts.
        self.session.video = False
        self.reopen()
        self.session.stateChanged.emit("paused")
        self.clock.now += 10 * NO_PICTURE_S
        self.controller._sound_watch()
        self.assertFalse(self.screen.visible)
        self.session.stateChanged.emit("playing")
        self.session.video = None
        self.clock.now += 10 * NO_PICTURE_S
        self.controller._sound_watch()
        self.assertFalse(self.screen.visible)
        # Another channel: the screen goes with the old one, and the new one is counted anew.
        self.session.video = False
        self.clock.now += NO_PICTURE_S + 0.1
        self.controller._sound_watch()
        self.assertTrue(self.screen.visible)
        self.play(1)
        self.assertFalse(self.screen.visible)
        self.session.stateChanged.emit("playing")
        self.controller._sound_watch()
        self.assertFalse(self.screen.visible)

    def test_a_stream_that_ends_rests_its_screen_and_its_watch(self):
        # Review I1 and M7: ended (or failed) with no reconnect, the light holds still and the
        # watch stops; a reconnect's `playing` starts both again.
        self.tune_radio()
        self.clock.now += NO_PICTURE_S + 0.1
        self.controller._sound_watch()
        self.assertTrue(self.screen.playing)
        self.session.stateChanged.emit("ended")
        self.assertFalse(self.screen.playing)
        self.assertFalse(self.controller._sound_poll.isActive())
        self.reopen()
        self.assertTrue(self.screen.playing)
        self.assertTrue(self.controller._sound_poll.isActive())
        self.session.stateChanged.emit("error")
        self.assertFalse(self.screen.playing)
        self.assertFalse(self.controller._sound_poll.isActive())

    def test_a_radio_screen_never_sticks_when_its_channel_goes_without_a_new_one(self):
        # Review I2: a provider that keeps only its newest stream stops the large picture for
        # a recording (RecordGuard): nothing new comes, and the screen goes at the next look.
        self.tune_radio()
        self.clock.now += NO_PICTURE_S + 0.1
        self.controller._sound_watch()
        self.assertTrue(self.screen.visible)
        slot = SimpleNamespace(name="main")
        with mock.patch.object(self.controller, "sessions", SimpleNamespace(large=lambda: slot)):
            self.controller._stop_for_guard(slot)
        self.assertIsNone(self.controller.now_playing())
        self.controller._sound_watch()
        self.assertFalse(self.screen.visible)
        self.assertFalse(self.controller._sound_poll.isActive())

    def test_a_radio_channel_has_no_album_dock_or_queue(self):
        self.tune_radio()
        self.clock.now += NO_PICTURE_S + 0.1
        self.controller._sound_watch()
        self.assertTrue(self.screen.visible)
        self.albums.assert_not_called()
        self.covers.assert_not_called()  # its logo is its cover
        self.assertEqual(self.screen.tracks, [])
        self.assertEqual(self.screen.cover, (None, self.screen.cover[1]))  # no logo: drawn
        self.controller.handle(Action.MEDIA_NEXT)  # live TV's: the next channel
        self.assertEqual(self.requested()[-1], "Two")

    def test_the_media_panel_has_a_radio_channels_name_alone(self):
        # Ruling 2 (Task 8): its stream's song or programme is no artist for the panel.
        self.session.meta = {"now_playing": "Wexlo Tide - Pell"}
        self.tune_radio()
        self.assertIsNone(self.controller.panel_facts())  # no screen yet: as a channel
        self.clock.now += NO_PICTURE_S + 0.1
        self.controller._sound_watch()
        self.assertEqual(self.screen.song[1], "Wexlo Tide - Pell")  # the screen shows it
        self.assertEqual(self.controller.panel_facts(), ("One", "", ""))
        self.session.pictureShown.emit()  # a picture: a channel again
        self.assertIsNone(self.controller.panel_facts())


class SwapTests(test_tv_pip.PipCase):
    """A radio channel taken from the small picture: the real SessionManager and tuners (the
    picture-in-picture tests' harness), over the audio fakes."""

    def setUp(self):
        self.screen = FakeScreen()
        with (
            mock.patch("tests.test_tv_controller.FakeSession", AudioSession),
            mock.patch("tests.test_tv_pip.FakeSession", AudioSession),
        ):
            super().setUp()
        self.controller._sound_clock = self.clock

    def overlays(self, column):
        overlays = super().overlays(column)
        overlays.audio_screen = getattr(self, "screen", None)
        return overlays

    def test_a_radio_channel_swapped_in_shows_its_screen_without_a_new_state(self):
        # Review M3: the small picture plays a radio channel (its `playing` heard small). ⇄
        # brings no state change with it: its screen comes all the same, NO_PICTURE_S on.
        self.start_large(0)
        self.session.video = True
        self.open_small(test_tv_pip.GROUP_B[0], "20", test_tv_pip.GROUP_B)
        self.pip_session.video = False
        self.pip_session.stateChanged.emit("playing")
        self.controller.handle(Action.SWAP)
        self.assertIs(self.controller.session, self.pip_session)
        self.assertTrue(self.controller._sound_poll.isActive())
        self.clock.now += NO_PICTURE_S + 0.1
        self.controller._sound_watch()
        self.assertTrue(self.screen.visible)
        self.assertEqual(self.screen.song[0], "Five")


class AlbumTests(AudioCase):
    def test_album_plays_on_without_a_card_and_stops_after_the_last(self):
        tracks = self.album(3)
        first, second, third = (Path(t["path"]) for t in tracks)
        self.open(first, size=1001)
        self.when_idle()
        self.assertEqual(self.albums.call_count, 1)
        self.assertEqual(self.albums.call_args.args[1], str(first))
        self.list_album(tracks)
        self.assertEqual((len(self.screen.tracks), self.screen.current), (3, 0))
        self.assertEqual(self.screen.view(), "album")  # a list arrived: Album
        self.assertEqual(self.transport.named("set_audio_state")[-1], (False, "off", True))
        self.screen.calls.clear()
        self.end()
        self.assertEqual(self.played()[-1], second)
        self.assertEqual(self.session.plays[-1][1]["start_ms"], 0)
        self.assertEqual(self.next_card.named("show_next"), [])
        self.assertNotIn(FINISHED, self.toast.messages())
        self.assertEqual(self.runs, [])  # held: what waits for the film waits on
        # The screen and the dock stayed up between the two (no black page between songs).
        self.assertNotIn(("hide",), self.screen.calls)
        self.assertNotIn(("end_audio",), self.transport.calls)
        self.assertEqual(self.screen.current, 1)
        self.assertEqual(self.screen.song[0], "Song 2")  # from the listing, before it plays
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.albums.call_count, 1)  # one listing for the album (item 16)
        self.assertEqual(self.screen.named("show"), [])  # it showed all along
        self.end()
        self.assertEqual(self.played()[-1], third)
        self.session.stateChanged.emit("playing")
        self.assertTrue(self.screen.playing)
        self.end()  # the last: "Finished", and the hold goes
        self.assertEqual(self.played()[-1], third)
        self.assertIn(FINISHED, self.toast.messages())
        self.assertEqual(self.runs, ["update question"])
        self.assertEqual(self.next_card.named("show_next"), [])
        self.assertFalse(self.screen.playing)  # review I1: its light and row bars rest

    def test_a_cut_album_listing_is_no_album(self):
        # Review item 12: a listing its time or cap cut short can't say what comes after its
        # last; "N tracks", shuffle and repeat all would be wrong. So it is no album at all.
        tracks = self.album(3)
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks, complete=False)
        self.assertEqual(self.screen.tracks, [])
        self.assertEqual(self.screen.view(), "cover")
        self.assertEqual(self.transport.named("set_audio_state")[-1][2], False)
        self.end()
        self.assertIn(FINISHED, self.toast.messages())
        self.assertEqual(len(self.session.plays), 1)

    def test_a_lone_song_has_no_list_and_plays_nothing_after_itself(self):
        self.open(self.folder / "Lone.mp3")
        self.list_album([])
        self.assertEqual(self.screen.view(), "cover")
        self.end()
        self.assertIn(FINISHED, self.toast.messages())
        self.assertEqual(len(self.session.plays), 1)

    def test_one_album_listing_per_folder(self):
        # Review item 16: the next track of the same album takes the list it has; a song of
        # the same folder that isn't on it (another album there) is listed anew.
        tracks = self.album(2)
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks)
        self.open(Path(tracks[1]["path"]))  # opened from the folder again, not by the queue
        self.assertEqual(self.albums.call_count, 1)
        self.assertEqual(self.screen.current, 1)
        other = self.folder / "Interlude.mp3"
        self.open(other)
        self.assertEqual(self.albums.call_count, 2)
        self.assertEqual(self.screen.tracks, [])  # the old album's list goes at once

    def test_a_late_album_listing_never_reaches_a_newer_title(self):
        tracks = self.album(3)
        self.open(Path(tracks[0]["path"]))
        late = self.albums.call_args.args
        other = Path(tempfile.mkdtemp(dir=self.folder)) / "Elsewhere.mp3"
        self.open(other)  # another song before the listing came
        self.controller._on_album_read(late[0], tracks, True)
        self.assertEqual(self.screen.tracks, [])
        self.assertIsNone(self.controller._queue)
        # A channel chosen while the song plays: its link on its way, the song still current.
        self.open(Path(tracks[0]["path"]))
        late = self.albums.call_args.args
        self.tune(1)
        self.controller._on_album_read(late[0], tracks, True)
        self.assertEqual(self.screen.tracks, [])
        # A film opened since.
        self.ready("http://stream/2")
        self.open(Path(tracks[0]["path"]))
        late = self.albums.call_args.args
        self.open(self.folder / "film.mkv", video=True)
        self.controller._on_album_read(late[0], tracks, True)
        self.assertEqual(self.screen.tracks, [])
        self.assertIsNone(self.controller._queue)

    def test_a_picked_row_plays_that_track(self):
        tracks = self.album(3)
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks)
        self.screen.trackPicked.emit(2)
        self.assertEqual(self.played()[-1], Path(tracks[2]["path"]))
        self.assertEqual(self.screen.current, 2)
        self.session.stateChanged.emit("playing")
        self.end()  # the last
        self.assertIn(FINISHED, self.toast.messages())

    def test_songs_of_a_playlist_have_no_album_of_their_own(self):
        # Until Task 9 a playlist's songs follow the list (Ruling P4-1), not their folders.
        tracks = self.album(2)
        self.controller.play_list("Evening", [t["path"] for t in tracks], [1001, 1002])
        self.session.stateChanged.emit("playing")
        self.assertTrue(self.screen.visible)
        self.albums.assert_not_called()
        self.end()
        self.assertEqual(len(self.next_card.named("show_next")), 1)  # the list's next

    def test_media_keys_go_to_tracks_with_a_queue_and_seek_without(self):
        self.session.seekable = True
        # A lone song: ±10 s, as a film.
        self.open(self.folder / "Lone.mp3")
        self.list_album([])
        self.controller.handle(Action.MEDIA_NEXT)
        self.controller.handle(Action.MEDIA_PREV)
        self.assertEqual(self.session.relative, [10_000, -10_000])
        # An album: the next track, and back.
        tracks = self.album(3)
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks)
        self.controller.handle(Action.MEDIA_NEXT)
        self.assertEqual(self.played()[-1], Path(tracks[1]["path"]))
        self.session.stateChanged.emit("playing")
        self.session.now_ms = 5_000  # past 3 s: this one from its start
        self.controller.handle(Action.MEDIA_PREV)
        self.assertEqual(self.session.seeks[-1], 0)
        self.assertEqual(self.played()[-1], Path(tracks[1]["path"]))
        self.controller.handle(Action.MEDIA_PREV)  # near its start: the one before
        self.assertEqual(self.played()[-1], Path(tracks[0]["path"]))
        self.session.stateChanged.emit("playing")
        self.session.now_ms = 0
        seeks = len(self.session.seeks)
        self.controller.handle(Action.MEDIA_PREV)  # the first: from its start
        self.assertEqual(len(self.session.seeks), seeks + 1)
        self.assertEqual(self.played()[-1], Path(tracks[0]["path"]))
        self.assertEqual(self.session.relative, [10_000, -10_000])  # no seek by 10 s
        # The dock's buttons go the same way.
        self.transport.nextTrackRequested.emit()
        self.assertEqual(self.played()[-1], Path(tracks[1]["path"]))
        self.session.stateChanged.emit("playing")
        self.transport.prevTrackRequested.emit()
        self.assertEqual(self.played()[-1], Path(tracks[0]["path"]))
        # While the channel list is open, they do nothing.
        self.drawer.visible = True
        count = len(self.session.plays)
        self.controller.handle(Action.MEDIA_NEXT)
        self.assertEqual(len(self.session.plays), count)
        self.drawer.visible = False
        # A film: ±10 s again.
        self.open(self.folder / "film.mkv", video=True)
        self.controller.handle(Action.MEDIA_NEXT)
        self.assertEqual(self.session.relative[-1], 10_000)

    def test_the_docks_next_plays_an_audio_only_episodes_next_one(self):
        # Review M8: no album, but the folder's next episode follows it: ⏭ lights and plays it,
        # as a film's Next does.
        listings = mock.patch.object(self.controller._listing, "start").start()
        first, second = self.folder / "Show.S01E01.mkv", self.folder / "Show.S01E02.mkv"
        first.write_bytes(b"one")
        second.write_bytes(b"two!")
        self.open(first, size=3, video=False)
        self.assertTrue(self.screen.visible)
        self.assertTrue(self.transport.audio())
        self.assertFalse(self.transport.next_on)
        generation, folder = listings.call_args.args
        self.controller._on_folder_listed(generation, list_folder(folder))
        self.assertTrue(self.transport.next_on)
        self.transport.nextTrackRequested.emit()
        self.assertEqual(self.played()[-1], second)

    def test_the_media_keys_next_plays_an_audio_only_episodes_next_one(self):
        # Task 5's deferred minor (Task 8, ruling 5): the media keys do what the dock's ⏭ and
        # ⏮ do on the audio screen: the next episode; the episode from its start.
        listings = mock.patch.object(self.controller._listing, "start").start()
        first, second = self.folder / "Show.S01E01.mkv", self.folder / "Show.S01E02.mkv"
        first.write_bytes(b"one")
        second.write_bytes(b"two!")
        self.session.seekable = True
        self.open(first, size=3, video=False)
        generation, folder = listings.call_args.args
        self.controller._on_folder_listed(generation, list_folder(folder))
        self.assertTrue(self.transport.next_on)
        self.session.now_ms = 40_000
        self.controller.handle(Action.MEDIA_PREV)  # the dock's ⏮: from its start
        self.assertEqual(self.session.seeks, [0])
        self.controller.handle(Action.MEDIA_NEXT)
        self.assertEqual(self.played()[-1], second)
        self.assertEqual(self.session.relative, [])  # no seek by 10 s

    def test_the_docks_previous_restarts_a_lone_song(self):
        self.session.seekable = True
        self.open(self.folder / "Lone.mp3")
        self.list_album([])
        self.session.now_ms = 40_000
        self.transport.prevTrackRequested.emit()
        self.assertEqual(self.session.seeks, [0])
        self.transport.nextTrackRequested.emit()  # no list: nothing
        self.assertEqual(len(self.session.plays), 1)

    def test_the_docks_volume_bar_sets_the_level(self):
        self.open(self.folder / "Lone.mp3")
        self.transport.volumeSetRequested.emit(30)
        self.assertEqual(self.session.level, 30)
        self.assertEqual(self.transport.named("set_level")[-1], (30,))
        self.assertFalse(self.controller._mixer.visible())


class RememberedTests(AudioCase):
    def test_picked_view_is_remembered(self):
        tracks = self.album(2)
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks)
        self.assertEqual(self.screen.view(), "album")
        saves = self.config.saves
        self.screen.upNextRequested.emit()  # Up next: the Album view, not a pick
        self.assertEqual(getattr(self.config, "audio_view", ""), "")
        self.screen.set_view("stage")  # the switch sets its view, then says it was picked
        self.screen.viewPicked.emit("stage")
        self.assertEqual(self.config.audio_view, "stage")
        self.assertEqual(self.config.saves, saves + 1)
        # The next song, from another folder, opens on it; its album's list doesn't change it.
        other = Path(tempfile.mkdtemp(dir=self.folder))
        others = [track(other, n, f"Other {n}") for n in (1, 2)]
        self.screen.set_view("cover")
        self.open(Path(others[0]["path"]))
        self.assertEqual(self.screen.view(), "stage")
        self.list_album(others)
        self.assertEqual(self.screen.view(), "stage")
        # Words turned on from the screen leave a picked view alone.
        self.screen.viewPicked.emit("cover")
        self.screen.set_view("cover")
        self.screen.wordsRequested.emit()
        self.assertEqual(self.window.captions.toggles, 1)
        self.assertEqual(self.screen.view(), "cover")
        self.assertEqual(self.screen.words, (True, "Türkçe"))

    def test_words_where_captions_cant_run_say_so_and_change_nothing(self):
        # As the Subtitles panel offers its row only where AI captions can run: no toggle, no
        # Stage, and the row's own words for it.
        self.window.captions.can = False
        self.open(self.folder / "Lone.mp3")
        self.screen.wordsRequested.emit()
        self.assertEqual(self.window.captions.toggles, 0)
        self.assertEqual(self.screen.view(), "cover")
        self.assertIn(("Not available for this stream", "info"), self.toast.messages())

    def test_words_waiting_for_the_speed_keep_their_stage(self):
        # Review M9: turned on before this computer's speed is known, AI captions wait for it
        # (CaptionsController.wanted). The Stage they opened stays their opening view through
        # the wait; pressed again meanwhile, they're called off.
        captions = self.window.captions
        captions.slow = True
        tracks = self.album(2)
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks)
        self.screen.wordsRequested.emit()
        self.assertTrue(captions.waits)
        self.assertEqual(self.screen.view(), "stage")
        self.controller.words_changed()  # their `changed`, as the window wires it
        self.assertEqual(self.screen.words, (False, ""))
        captions.scored()
        self.controller.words_changed()
        self.assertEqual(self.screen.words, (True, "Türkçe"))
        other = Path(tempfile.mkdtemp(dir=self.folder))
        others = [track(other, n, f"Other {n}") for n in (1, 2)]
        self.open(Path(others[0]["path"]))
        self.assertEqual(self.screen.view(), "stage")  # while those Words stay on
        self.list_album(others)
        self.assertEqual(self.screen.view(), "stage")
        self.screen.wordsRequested.emit()  # off
        self.assertFalse(captions.on)
        captions.slow = True
        self.screen.wordsRequested.emit()  # on, waiting for the speed
        self.assertTrue(captions.waits)
        self.screen.wordsRequested.emit()  # pressed again meanwhile: called off
        self.assertFalse(captions.waits or captions.on)
        self.assertEqual(captions.toggles, 4)

    def test_words_from_the_audio_screen_open_stage_unless_picked(self):
        tracks = self.album(2)
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks)
        self.assertEqual(self.screen.view(), "album")
        self.screen.wordsRequested.emit()
        self.assertEqual(self.window.captions.toggles, 1)
        self.assertEqual(self.screen.view(), "stage")
        self.assertEqual(self.screen.words, (True, "Türkçe"))
        self.assertEqual(getattr(self.config, "audio_view", ""), "")  # no pick
        # The next album opens on the Stage while those Words stay on, its list arriving too.
        other = Path(tempfile.mkdtemp(dir=self.folder))
        others = [track(other, n, f"Other {n}") for n in (1, 2)]
        self.open(Path(others[0]["path"]))
        self.assertEqual(self.screen.view(), "stage")
        self.list_album(others)
        self.assertEqual(self.screen.view(), "stage")
        self.screen.wordsRequested.emit()  # off again: the view stays
        self.assertEqual(self.screen.view(), "stage")
        self.assertEqual(self.screen.words[0], False)
        self.open(Path(tracks[1]["path"]))  # off: the next album opens on its list
        self.list_album(tracks)
        self.assertEqual(self.screen.view(), "album")
        # A view picked by hand: Words turned on leave it, now and for the next song.
        self.screen.set_view("cover")
        self.screen.viewPicked.emit("cover")
        self.screen.wordsRequested.emit()
        self.assertEqual((self.window.captions.toggles, self.screen.view()), (3, "cover"))
        self.open(Path(others[1]["path"]))
        self.list_album(others)
        self.assertEqual(self.screen.view(), "cover")

    def test_captions_already_on_keep_the_opening_view(self):
        # AI captions on from something else (the Subtitles panel, the phone, a film before):
        # the screen opens on its own view, and they come and go without moving it.
        captions = self.window.captions
        captions.on = True
        tracks = self.album(2)
        self.open(Path(tracks[0]["path"]))
        self.assertEqual(self.screen.view(), "cover")
        self.list_album(tracks)
        self.assertEqual(self.screen.view(), "album")
        self.assertEqual(self.screen.words, (True, "Türkçe"))
        for _ in range(2):  # off and on again from the Subtitles panel's row
            captions.toggle()
            self.controller.words_changed()  # their `changed`, as the window wires it
        self.assertEqual(self.screen.view(), "album")
        other = Path(tempfile.mkdtemp(dir=self.folder))
        others = [track(other, n, f"Other {n}") for n in (1, 2)]
        self.open(Path(others[0]["path"]))
        self.assertEqual(self.screen.view(), "cover")
        self.list_album(others)
        self.assertEqual(self.screen.view(), "album")
        # The pill while they're on turns them off: still no Stage, then or after.
        self.screen.wordsRequested.emit()
        self.assertFalse(captions.on)
        self.assertEqual(self.screen.view(), "album")
        captions.toggle()  # on from the panel again
        self.controller.words_changed()
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks)
        self.assertEqual(self.screen.view(), "album")

    def test_shuffle_and_repeat_are_remembered(self):
        tracks = self.album(4)
        self.open(Path(tracks[0]["path"]))
        self.list_album(tracks)
        saves = self.config.saves
        self.transport.shuffleRequested.emit()
        self.assertIs(self.config.audio_shuffle, True)
        self.assertEqual(self.transport.named("set_audio_state")[-1], (True, "off", True))
        self.assertTrue(self.controller._queue.shuffle)
        for expected in ("all", "one", "off", "all"):
            self.transport.repeatRequested.emit()
            self.assertEqual(self.config.audio_repeat, expected)
        self.assertEqual(self.config.saves, saves + 5)
        self.assertEqual(self.transport.named("set_audio_state")[-1], (True, "all", True))
        # A new album's queue starts as they were left.
        other = Path(tempfile.mkdtemp(dir=self.folder))
        others = [track(other, n, f"Other {n}") for n in (1, 2, 3)]
        self.open(Path(others[1]["path"]))
        self.list_album(others)
        queue = self.controller._queue
        self.assertEqual((queue.shuffle, queue.repeat), (True, "all"))
        self.assertEqual(queue.order()[0], 1)  # the song playing first
        # Repeat one: the song again at its end, from its start.
        self.transport.repeatRequested.emit()
        self.assertEqual(self.config.audio_repeat, "one")
        self.end()
        self.assertEqual(self.played()[-1], Path(others[1]["path"]))
        self.assertEqual(self.session.plays[-1][1]["start_ms"], 0)

    def test_repeat_plays_a_lone_song_again(self):
        self.config.audio_repeat = "all"
        self.open(self.folder / "Lone.mp3", size=99)
        self.list_album([])
        self.assertEqual(self.transport.named("set_audio_state")[-1], (False, "all", False))
        self.end()
        self.assertEqual(len(self.session.plays), 2)
        self.assertNotIn(FINISHED, self.toast.messages())


class ScreenApiTests(unittest.TestCase):
    def test_the_fakes_speak_the_real_screens_api(self):
        for name in (
            "show_song",
            "set_cover",
            "set_tracks",
            "set_view",
            "view",
            "shown_view",
            "set_words",
            "set_playing",
        ):
            real = inspect.signature(getattr(AudioScreen, name))
            fake = inspect.signature(getattr(FakeScreen, name))
            self.assertEqual(list(real.parameters), list(fake.parameters), name)
        for name in ("viewPicked", "trackPicked", "wordsRequested", "upNextRequested"):
            self.assertTrue(hasattr(AudioScreen, name), name)


class WindowWordsTests(base.WindowCase):
    """Words on the audio screen in a real window (no libVLC): the AI captions feed its Stage,
    and the captions over the picture give way to it there."""

    def test_the_stage_shows_the_words_and_the_captions_over_the_picture_give_way(self):
        window = self.window
        self.show()
        screen, bottom, captions = window.audio_screen, window.captions_overlay, window.captions
        lines = ("Kıyıda kimse kalmadı.", "Gece vapurları geçiyor.", "Night ferries pass by.")
        screen.show_song("Ferries at Night", "Mira Sol", "Harbour Lights")
        screen.set_view("stage")
        screen.show()
        captions._show(lines[2], lines)  # as a tick shows a translated line
        self.assertEqual(screen._lines, lines)
        self.assertTrue(bottom.isHidden())  # the Stage has them
        screen.set_view("cover")  # given back at once
        self.assertFalse(bottom.isHidden())
        self.assertEqual(bottom.text(), lines[2])
        screen.set_view("stage")
        self.assertTrue(bottom.isHidden())
        screen.hide()  # a film opens while it is on its Stage
        self.assertFalse(bottom.isHidden())
        self.assertEqual(bottom.text(), lines[2])
        captions._show("")
        self.assertEqual(screen._lines, ("", "", ""))
        self.assertTrue(bottom.isHidden())
        # Ruling P4-10: in Cover, with Words on, the screen leaves the captions their room.
        self.assertIs(screen._captions, bottom)
        screen.show()
        screen.set_view("cover")
        screen.set_words(True, "English")
        captions._show(lines[2], lines)
        self.assertFalse(bottom.isHidden())
        lay = screen._layout()
        self.assertFalse(bottom.geometry().intersects(lay.subtitle.toAlignedRect()))
        captions._show("")


def make_song(path: Path, *, title: str, album: str, number: int, seconds: float = 20) -> Path:
    """A tone with its tags and an embedded cover (ffmpeg). Through libVLC's dummy sound
    output the first ~2 s go at once: one under that fails as never played, and a 6 s one
    ends about 4 s after it starts. The album's name gets this run's suffix: libVLC keeps a
    song's art by its artist and album, and an earlier run's must never answer for it."""
    album = f"{album} {RUN}"
    cover = path.with_suffix(".png")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0xd8653f:s=64x64"]
        + ["-frames:v", "1", str(cover)],
        check=True,
    )
    args = ["-f", "lavfi", "-i", f"sine=frequency={330 + 40 * number}:duration={seconds}"]
    args += ["-i", str(cover), "-map", "0", "-map", "1"]
    if path.suffix == ".m4a":
        args += ["-c:a", "aac", "-c:v", "png", "-disposition:v", "attached_pic"]
    else:
        args += ["-c:a", "libmp3lame", "-c:v", "png", "-id3v2_version", "3"]
    tags = {"title": title, "album": album, "artist": "Wexlo Tide", "track": str(number)}
    for key, value in tags.items():
        args += ["-metadata", f"{key}={value}"]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args, str(path)], check=True)
    cover.unlink()
    return path


class MediaPanel:
    """The system's media panel (services.media_keys.System) as it hears what plays."""

    def __init__(self):
        self.shown: list[tuple] = []

    def listen(self):
        pass

    def now_playing(
        self, state, title, live, *, artist="", album="", art_path="", art_bytes=b"", music=False
    ):
        self.shown.append((state, title, live, artist, album, art_path, music))

    def last(self):
        return self.shown[-1] if self.shown else ("", "", False, "", "", "", False)

    def close(self):
        pass


@unittest.skipUnless(FFMPEG, "ffmpeg makes the songs")
class WindowSongTests(base.WindowCase):
    """A folder's song in a real window (libVLC with no picture or sound)."""

    vlc_instance = base.INSTANCE

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.song = make_song(self.folder / "Pell.mp3", title="Pell", album="Kesh", number=1)

    def play_song(self):
        window = self.window
        self.show()
        window.open_paths([str(self.song)], source="dialog")
        screen = window.audio_screen
        self.assertTrue(wait_until(lambda: screen.isVisible()), "the audio screen")
        return screen

    def test_the_systems_media_panel_hears_the_songs_artist_album_and_cover(self):
        # Task 8: a song's tags come at its first `playing` and its cover from a thread after
        # it: the system's media panel hears each as it comes, the cover as a small file of its
        # own, which goes when QiTV closes.
        window = self.window
        panel = MediaPanel()
        window.media_keys._system = panel  # as on macOS
        self.play_song()
        self.assertTrue(wait_until(lambda: bool(panel.last()[5])), panel.shown)  # the cover
        state, title, live, artist, album, art, music = panel.last()
        song = ("playing", "Pell", False, "Wexlo Tide", f"Kesh {RUN}", True)
        self.assertEqual((state, title, live, artist, album, music), song)
        self.assertTrue(os.path.isfile(art))
        self.assertEqual(QImage(art).size().width(), 64)  # the song's own (never made larger)
        folder = os.path.dirname(art)
        window.controller.stop()
        self.assertTrue(wait_until(lambda: panel.last() == ("", "", False, "", "", "", False)))
        self.close_window()
        self.assertFalse(os.path.exists(folder))

    def click(self, widget, rect):
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=rect.center())

    def test_back_and_more_library_open_the_library_for_a_folder_song(self):
        # Ruling P4-8 a: ‹ and ⋯'s Library open the Library (at its last tab) for any song,
        # one opened from a folder too, the song playing on in its corner.
        window = self.window
        screen = self.play_song()
        self.assertFalse(window.screens._can_return())  # a folder's: no Library it came from
        self.click(screen, screen.controls()["back"])
        self.assertTrue(wait_until(window.screens.library_shown))
        self.assertEqual(window.pages.mode(), PageStack.CORNER)
        self.assertEqual(window.controller.session.url, self.song.as_uri())
        window.screens.back_to_player()
        self.assertTrue(wait_until(lambda: window.pages.mode() == PageStack.PLAYER))
        transport, menu = window.overlays.transport, window.more_menu
        self.assertTrue(wait_until(transport.isVisible))  # the dock came back with the page
        self.assertTrue(transport.audio())
        self.click(transport, transport.button_rect("more").toAlignedRect())
        self.assertTrue(wait_until(menu.isVisible))
        self.click(menu, menu.controls()["library"])
        self.assertTrue(wait_until(window.screens.library_shown))
        window.screens.back_to_player()
        # Esc closes the menu first, and only it.
        self.assertTrue(wait_until(transport.isVisible))
        self.click(transport, transport.button_rect("more").toAlignedRect())
        self.assertTrue(wait_until(menu.isVisible))
        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.assertFalse(menu.isVisible())
        self.assertFalse(window.screens.library_shown())
        window.controller.stop()

    def test_in_the_corner_the_screen_stays_small_and_a_click_brings_the_player_back(self):
        # Review item 10: the corner player shows the song's cover and title, not black, and
        # a click on it is the corner's (the player again), never play or pause.
        window = self.window
        screen = self.play_song()
        window.screens.open_library("movies")
        self.assertEqual(window.pages.mode(), PageStack.CORNER)
        self.assertTrue(wait_until(screen.isVisible))
        self.assertTrue(screen.compact())
        self.assertEqual(screen.controls(), {})  # no top bar
        self.assertFalse(window.overlays.transport.isVisible())  # no dock
        self.click(screen, screen.rect())
        self.assertTrue(wait_until(lambda: window.pages.mode() == PageStack.PLAYER))
        self.assertFalse(screen.compact())
        self.assertIs(window.controller.session.state(), PlaybackState.PLAYING)
        window.controller.stop()

    def test_the_words_pill_turns_ai_captions_as_their_row_does(self):
        # The real pill and the real CaptionsController. Where no speech model can run (hel: no
        # runtime for its processor) it says so and changes nothing, even a moment later; where
        # one can, the Stage opens and their first question (or they) come.
        # It cleans up before it ends: no speed measure's answer can ask its question after
        # the window is gone (a slow runner).
        window = self.window
        screen = self.play_song()
        captions = window.captions

        def measuring():
            return any(t.name == "qitv-captions-tier" for t in threading.enumerate())

        self.click(screen, screen.controls()["words"])
        self.assertTrue(wait_until(lambda: not measuring(), 20_000))  # its speed, if measured
        pause(100)  # its answer, on the GUI thread
        if not captions.available():
            self.assertEqual(screen.view(), "cover")
            self.assertIn(("Not available for this stream", "info"), self.toasts)
        else:
            self.assertEqual(screen.view(), "stage")
            settled = (captions.asking, captions.enabled, captions.downloading)
            self.assertTrue(
                wait_until(lambda: any(f() for f in settled) or not captions.wanted(), 20_000)
            )
            if captions.asking():
                captions.answer(False)
            elif captions.enabled() or captions.downloading():
                captions.toggle()
        self.assertFalse(captions.wanted())
        self.assertFalse(measuring())
        window.controller.stop()

    def test_an_m4a_whose_cover_is_an_attached_picture_plays_on_the_screen(self):
        # Review item 14 and M10, on the real player: libVLC lists the .m4a's attached cover as
        # no video track, and no picture shows: the screen and the dock.
        window, controller = self.window, self.window.controller
        song = make_song(self.folder / "Arn.m4a", title="Arn", album="Kesh", number=2)
        self.show()
        window.open_paths([str(song)], source="dialog")
        self.assertTrue(wait_until(window.audio_screen.isVisible))
        session = controller.session
        self.assertEqual(session.url, song.as_uri())
        self.assertIs(session.has_video_track(), False)
        self.assertFalse(session.shows_picture())
        self.assertTrue(window.overlays.transport.audio())
        controller.stop()

    def test_qitvs_mark_is_left_out_only_over_a_songs_screen(self):
        # Review M4: a song opened over a song's screen keeps it (no mark over it); over a
        # radio channel's, which goes, QiTV's mark draws on the black as for any file.
        window, controller = self.window, self.window.controller
        self.show()
        marks = mock.patch.object(
            window.opening_overlay, "open_for", wraps=window.opening_overlay.open_for
        ).start()
        self.addCleanup(mock.patch.stopall)
        controller._sound_shown, controller._sound_kind = True, "radio"  # a radio's screen
        window.open_paths([str(self.song)], source="dialog")
        self.assertTrue(wait_until(lambda: marks.called))
        self.assertTrue(wait_until(window.audio_screen.isVisible))  # the song's, as it plays
        marks.reset_mock()
        other = make_song(self.folder / "Rook.mp3", title="Rook", album="Kesh", number=3)
        window.open_paths([str(other)], source="dialog")
        self.assertTrue(wait_until(lambda: controller.session.url == other.as_uri()))
        marks.assert_not_called()
        controller.stop()

    def test_the_pointer_hides_off_the_dock_and_never_while_its_menu_shows(self):
        # Review item 6: the dock never fades, so the pointer goes when it rests off its band.
        window = self.window
        self.play_song()
        transport = window.overlays.transport
        pointer = window.pointer
        off_band = transport.mapToGlobal(QPoint(transport.width() // 2, 10))
        on_band = transport.mapToGlobal(transport.button_rect("play").center().toPoint())

        def try_hide():
            pointer._idle.stop()  # its wait is over
            pointer._try_hide()

        with mock.patch("ui.pointer.hide_pointer_until_moved", return_value=True):
            with mock.patch.object(pointer, "_cursor_pos", return_value=on_band):
                try_hide()
                self.assertFalse(pointer.hidden())
                self.assertTrue(pointer._idle.isActive())  # it tries again later
            with mock.patch.object(pointer, "_cursor_pos", return_value=off_band):
                window.more_menu.open_at(transport.mapToGlobal(QPoint(40, transport.height())))
                self.assertTrue(window.more_menu.isVisible())
                try_hide()
                self.assertFalse(pointer.hidden())
                window.more_menu.dismiss()
                try_hide()
                self.assertTrue(pointer.hidden())
        window.controller.stop()


if __name__ == "__main__":
    unittest.main()
