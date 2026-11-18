# QiTV 2.11.0

QiTV 2.11 plays the videos and music on your computer too. Double-click an episode or a song in
your file manager, or drop it, or a whole folder, onto QiTV's window: it plays from where you
stopped, the next episode follows from the same folder, and AI captions come back on if you had
them on. Songs play on a screen of their own, with their cover, their album and bars that move
with the music, and the folders you open wait on the Library's new Folders tab. QiTV's mark now
draws itself while QiTV starts.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Your own videos

- **Double-click a video and it plays in QiTV,** once QiTV is your video player (Settings,
  below); on a Mac, Finder's Open With lists QiTV without any setting. A QiTV that already runs
  plays it in place of what was on: no second QiTV starts. Double-clicking the file that plays
  only brings QiTV to the front.
- **Drop a video or a song onto the window and it plays,** as File → Open File…
  (<kbd>Ctrl/⌘+O</kbd>) plays it. A subtitle file dropped with it loads for it.
- **A file plays alone.** A QiTV started with a file tunes no channel and opens nothing over
  it: the channel list doesn't open by itself, and the welcome card and the question about an
  update wait until the file ends or you stop it. Your providers load behind it, and their
  "Loading …" waits too.
- **From where you stopped.** A video goes on where you left it, as a provider's film does.
  QiTV knows it by its name and size, so moving it to another folder keeps its place. A song
  shorter than 20 minutes always starts at its beginning; anything longer, such as a podcast or
  an audiobook (`.m4b` too), goes on where you stopped.
- **Nothing goes into your history.** Files don't appear in Continue, Recent or the Library. Only
  the folders you open as folders are kept, on the Folders tab (below).
- **AI captions come back on for a video file** when they were on for the last one, in the
  language and with the Translate to you chose. They come on only when their speech model is
  already on your computer: nothing is asked for or downloaded, and they never come on while the
  video shows subtitles of its own. Choosing Off in the Subtitles panel during a video file
  keeps them off for the next one. Captions that came on by themselves go off again when you go
  back to a channel or a provider's film.
- **A file that's gone** says "Couldn't open …", and a file of a kind QiTV doesn't play says
  "QiTV can't play …"; what was playing goes on.
- **Several files opened at once** from the file manager: the first by name plays. Dropped
  together onto the window, they all play, as a list (below).

## The next episode from the folder

- **When an episode ends, the Next card offers the next one from the same folder.** A video
  named with its season and episode (`Show.S01E02.mkv`, `show s1e2.mp4`, `Show 1x02.avi`) gets
  the same show's next episode, or the next season's first after a season's last. It counts down
  as a provider's series does (Settings → Auto-play next episode), and the player controls' ⏭
  plays it at once, also with auto-play off.
- **The same show however its name is written:** dots, spaces, dashes and capitals don't
  matter, and a file named only `S01E02.mkv` in a `Show/Season 1` folder takes its folder's
  name.
- **Only the next episode is ever named.** Any other video ends on "Finished", with no card and
  no other file's name on screen. Only the video's own folder is looked at, never a "Season 2"
  folder beside it, and when an episode is missing (E03 gone, E04 there) nothing is offered
  rather than skipping one. Picture sizes such as `1920x1080` never count as an episode.

## Folders and playlists

- **A folder plays in order:** drop it onto the window, or choose File → Open Folder…
  (<kbd>Ctrl/⌘+Shift+O</kbd>) or the channel list's new **Open a folder** row. A series'
  episodes go by season and episode, an album's songs by disc and track, anything else by name.
  Only the folder's own files play, never its subfolders'.
- **It starts where you stopped:** at the video you stopped part-way into, else after the last
  one you watched to its end. An album starts at its first song (with shuffle on, at a song of
  its own picking). The player controls' ⏭ goes to the next video, and with Auto-play next
  episode on, the Next card offers it.
- **Several files dropped together** play as such a list too.
- **A playlist (`.m3u`, `.m3u8`) of files on your computer plays as a list,** in its order: a
  list of songs as an album named after it, a list of videos one after another (⏭ and the Next
  card follow it), a mixed one each file on its own screen. Files it names that aren't there are
  skipped, and QiTV says how many. A playlist with a web link in it becomes a provider, as
  before, and an HLS stream's `.m3u8` plays as one stream.
- **A playlist that isn't UTF-8** (an older one, saved in the system's code page) is read in the
  code page under which most of its files are found, on Windows the system's own first, so names
  with letters such as ş, ı, ж or λ find their files.

## The Folders tab

- **The Library's new Folders tab,** after Recordings, keeps the last 30 folders you opened as
  folders, newest first. It shows once you have opened one. A single file, several files or a
  playlist never add their folder.
- **Each tile** shows the folder's name, what plays next or what it holds ("S2 · E5 next",
  "Album · 12 songs", "8 videos") and its picture: a `cover`, `folder` or `front` image in the
  folder, else a drawn one with its name.
- **Click a tile** and its folder plays from where you stopped; <kbd>Esc</kbd> comes back to the
  tab. **Add folder**, the last tile, picks another.
- **×** (or <kbd>Delete</kbd>) takes a tile off, and **Clear** takes them all off after asking:
  nothing is deleted from the disk. A folder that was moved or deleted, or is on an unplugged
  drive, says Missing, with Remove.
- **Nothing is scanned:** QiTV reads only these folders' own files, fetches nothing for them,
  and the list stays on your computer.

## Music on a screen of its own

- **Songs play on the audio screen,** in place of the black picture, and so does a video file
  that turns out to have no picture. Its three views are switched at its top right:
  - **Cover:** the cover large, the title, then the artist and the album; **Up next** shows the
    album's songs;
  - **Album:** the cover beside the album's songs, the one playing marked by small moving bars;
    click one to play it;
  - **Stage:** a small cover and the title at the top, the words large in the middle, and bars
    that move with the music.
- **A song with an album opens on Album,** and a lone song on Cover; once you pick a view, it
  opens on that one.
- **The cover's colours light the screen** around it and breathe slowly; when your system asks
  for reduced motion they hold still. The cover is the song's own picture, else a `cover`,
  `folder` or `front` picture in its folder, else a drawn one with its name.
- **The album plays on:** the other songs of its album in the song's own folder (the same album
  tag, or numbered files with no tags) follow it in disc and track order, with no card between
  them, and stop after the last unless repeat is on. A lone song plays nothing after itself. A
  song that can't be read is skipped; two in a row stop the album.
- **The controls** at the bottom: the time bar, shuffle, ⏮, play, ⏭, repeat (the album, then
  the song) and the volume, whose bar you drag (the speaker opens the mixer). **⋯** holds Stop,
  Sound, Subtitles, Picture (the sleep timer), Fullscreen, the mini player, the Library and
  Search. Shuffle and repeat are kept for next time, and every key works as on a film.
- **Words** at the top turns AI captions on, and the screen goes to Stage (unless you picked a
  view): the line said shows large, the one before it dimmed above and, with Translate to, its
  translation under it. On Cover and Album the lines show at the bottom. They are the AI captions
  themselves, so they stay on for what plays next until you turn them off. They never come on by
  themselves for music, and they catch less of a song than of speech: podcasts and talk do best.
- **The bars move with the music itself:** QiTV reads the file's sound a little ahead and draws
  them in the cover's colours. They stand still while it's paused. Only files have them.
- **Radio:** a channel that sends sound and no picture plays on the audio screen too, its logo
  as the cover, with the song its stream names (else the programme on now). It keeps live TV's
  controls, such as record, Go live and rewinding. Its Stage shows the words, without bars.
- **The system's media panel** on Windows, macOS and Linux shows the song's artist, album and
  cover (a radio channel's name and logo), and the media keys' previous and next go through the
  album's songs; previous on a lone song starts it again.

## QiTV as your video and music player

- **Settings → General has two new rows, Open videos with QiTV and Open music with QiTV,** each
  with its own button (**Use QiTV for videos**, then **Stop using QiTV for videos**), so QiTV can
  open your videos, your music, or both. Playlists are offered in Open With with either, never
  made the default.
- **On Windows** QiTV registers for your user only, with no administrator rights, and appears in
  Open With. Windows' Default Apps settings then open (on QiTV's own page on Windows 11), where
  you confirm it; **Default Apps…** beside the button opens them again. While QiTV runs, a
  double-click on a file goes straight to it.
- **On a Mac** QiTV is in Finder's Open With for videos, music and playlists without any setting.
  The button makes it the default for MKV, MP4, MOV, AVI and WebM (music: MP3, M4A and FLAC);
  Finder's Get Info → Open with → Change All picks another app.
- **On Linux** QiTV adds its own entry for your user and becomes the default video (or music)
  player.
- **`.ts` is in Open With only,** never made the default: program source files use the same
  extension.
- **Stop using… removes exactly what QiTV wrote.** A registered QiTV that you move registers
  itself again from its new place when it starts. A QiTV run from a temporary folder or a zip, or
  on a Mac from its disk image or Downloads, asks to be moved first.

## Starting QiTV

- **QiTV's mark draws itself while QiTV starts,** in the middle of the screen, from the moment
  you start it until its window is ready; then it opens into the window as the window fades in.
  On Windows and Linux the mark's outline shows while QiTV unpacks, then it fills and lights up
  as QiTV starts. When your system asks for reduced motion, the mark holds still and the window
  just fades in. On Windows, the update to 2.11 shows the outline while the new version unpacks
  too; later updates start without it.
- **While a file you open starts,** the mark draws in the middle of the black player until the
  file's picture, or the audio screen, shows.
- **Starts at the same moment leave one QiTV** (a few quick double-clicks, or several files
  opened at once). Before, two QiTVs could start on the same settings.
- **Starting QiTV while it is still closing** waits until it has closed, then starts (with the
  file you opened). Before, such a start could end with no QiTV open.

## Updating on Windows

- **A second update in the same run updates the QiTV you installed.** After an update, QiTV runs
  on from the downloaded file until you quit it; an update found before then replaced that
  download and left the installed QiTV as it was.
