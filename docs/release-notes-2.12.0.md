# QiTV 2.12.0

QiTV 2.12 moves Settings into the TV window. It opens where the channel list opens, beside the
picture, which keeps playing, and every change applies at once, with no Save. Your providers are
a list there, and a pasted link never turns one provider into another: a new address of another
kind, server or account asks first. File → Open Link… plays a single stream's link, and every
link that played is kept in Network streams. Deinterlacing is Auto or Off, in Settings ›
Playback. A large provider's lists load and save without holding the window still, and a
maintenance pass fixed parental controls in multiview, guides, playlists and more. 2.12 also
brings 2.11.2, a pre-release: AI captions hear the language the content speaks and remember a
translation for each spoken language, on top of 2.11's videos and music from your computer.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Settings beside the picture

- **Settings opens in the TV window,** where the channel list opens: its sections on the left,
  the chosen section's rows on the right, and the picture playing on beside them. Settings…
  (<kbd>Ctrl/⌘+,</kbd>), the channel list's ⚙ and its Settings row open it; from the Library,
  QiTV goes back to the TV first. The line at the top says where you are ("‹ Settings ›
  Playback").
- **The keys:** <kbd>↑</kbd>/<kbd>↓</kbd> move, and <kbd>→</kbd> or <kbd>Enter</kbd> opens a
  section. On a row with a value, <kbd>←</kbd>/<kbd>→</kbd> step it and <kbd>Enter</kbd> steps
  on; on any other row, <kbd>←</kbd> goes back to the sections. <kbd>Esc</kbd>, the mouse's Back
  and the phone remote's Back go one step up: from the rows to the sections, then closed. So the
  phone remote's arrows, OK and Back reach every setting.
- **Every row works the same way.** A choice steps with ‹ › as the Picture panel's rows do
  (`Live latency ‹ Auto (recommended) ›`), and a setting that is on or off steps between On and
  Off instead of being a checkbox. A value other than the default shows in the accent colour.
  Only the highlighted row shows its help, under it; with the mouse, hovering a row shows it too.
- **With the mouse,** a click on a row first highlights it, so you can read its help before
  anything changes; ‹ and › step it, and a click on the highlighted row steps on. A row that
  opens something or does something does it at the first click.
- **Changes apply at once; there is no Save.** Text you type applies on <kbd>Enter</kbd>, and
  <kbd>Esc</kbd> puts the old text back. As the picture stays in sight, a change to Live latency
  shows at once. Language, Hardware video decoding and the image cache's size say under them
  that they apply when QiTV starts again.
- **A value that wouldn't fit a row opens a card** over the dimmed picture: Disk cache size,
  with how far back a 1080p and a 4K channel can then be rewound; the guide's Check for updates
  every; a PIN; a channel's guide matching. A row that opens a list of its own ends in ›. One that
  deletes something is red, ends in "…" and asks first.
- **Typing finds a setting,** as typing in the channel list finds a channel: the sections whose
  name has the words, and the rows of every section whose name or help has them, capitals and
  accents aside, each saying where it is ("Providers › …"). <kbd>Enter</kbd> or a click takes
  you to it.
- **Long lists scroll,** with the channel list's thin scrollbar, and long names and values wrap
  onto a second line instead of being cut.
- **The Settings window is gone.** Every setting keeps its place in QiTV's settings file, so
  an older 2.x build reads all of them as you left them.

## Where each setting is now

- **Providers:** your providers, ＋ Add provider and Free channels › (iptv-org's channels), and
  Network streams once a link is kept (below).
- **General:** Language, Allow Check for Updates, Enable Channel Logos, Film and series details
  from TMDB and the TMDB token.
- **Playback:** Live latency, Hardware video decoding, Deinterlacing (below), 4K versions
  (Jellyfin, Emby), Start with the last channel, Mouse wheel, Play in and Autoplay › (the next
  episode, the next movie of the category); on a Mac, Picture in picture › (the two Float the
  picture settings). **Play in ‹ QiTV / VLC / MPV ›** takes the place of Always play in VLC and
  Always play in MPV.
- **Subtitles & AI captions:** the speech model, the room its downloaded files take and Delete
  downloaded files…, and your OpenSubtitles API key, username and password, with Sign out.
- **Time-shift & recordings:** Use disk for a longer time-shift buffer (turning it off asks
  first, as it removes what the disk kept), Memory per stream, Disk cache size and the
  Recordings folder, with Change…, Use the default and Open folder.
- **Guide:** where the guide comes from (no guide, the provider's own, a file or a web address),
  its address or file, Check for updates every, List window, Portal guide fetch period and
  Channel matching › (each channel's guide IDs, with ＋ Add, Import… and Export…).
- **Phone remote:** the remote on or off, Reach QiTV from any network, the home address and the
  relay, Paired phones › (each can be removed) and Show the pairing code on the TV.
- **Parental controls:** Set PIN…, Change PIN… and Remove PIN…, Unlock for an hour… and Lock
  now, Hide groups whose names look adult, and Hidden while locked › (each shown again with a
  press).
- **Files:** Open videos with QiTV and Open music with QiTV (below), and on Windows Default
  Apps….
- **Advanced:** Verify SSL certificates for every provider, Secure DNS, the detailed log, the
  image cache's size and Clear Image Cache….

## Providers

- **Settings › Providers lists your providers,** each with its kind under it ("Xtream · in use"
  for the one the TV shows). Choosing one opens its rows:
  - **Name**, changed in place;
  - **Kind**, shown only: another kind is another provider;
  - **Address and login ›**, a card with its address, its login and a portal's MAC, serial and
    device ID (below);
  - **Verify certificates**, **Streams at once** and **Guide time offset**, applied at once;
  - **Hidden groups ›**, each shown again with a press;
  - **Check the connection**, which asks the provider whether it answers, without opening a
    stream, and shows its answer;
  - **Remove provider…**, which asks first.

  While parental controls are locked, renaming or removing a provider asks for the PIN.
- **＋ Add provider is a card over the picture,** with one field, **Link or address**, and
  Paste. QiTV finds what a link is (an Xtream account, an M3U playlist or a portal), moves a
  login it holds into its own fields, and asks only for what is missing, such as a password or
  a portal's MAC. An address that is no such link is an Xtream server once you type a login,
  else a playlist; STB portal, Jellyfin and Emby under it pick another kind, and M3U file… and
  Free channels… are its other ways. The name is optional: by default, the server's.
- **Check and add** asks the provider first. A check that fails says why and offers **Add
  anyway**, except for Jellyfin and Emby, which need their sign-in, and for a playlist file that
  isn't there. Adding always makes a new provider.
- **Every Add provider opens that card:** the channel list's, the menu's, the welcome card's and
  the Library's. With no provider yet, the card shows over the empty screen, and Cancel brings
  the welcome card back.
- **A provider's link dropped onto the window** is added at once, as before; a portal link
  without its MAC opens the Add card filled in, waiting for the MAC.
- **Add provider no longer offers M3U Stream:** a single stream's link goes to Open Link
  (below). The M3U Stream providers you have keep working as they are.

## A provider never turns into another

- **Before, the Settings window's Providers tab was one form,** and a stream link pasted into
  its address could turn the provider it showed into another one: an Xtream account became a
  single stream.
- **Address and login › compares what you changed with what the provider was** when you press
  **Check and save**, whether you pasted it or typed it:
  - **the same server and account** (a new password, a port fixed): checked, then saved, with no
    question;
  - **another kind** ("This is another provider"): only **Add as new**, or Cancel;
  - **another server** ("This is another server"), or **another account** on the same server
    ("This is another account", a portal's other MAC too): **Add as new** first, or **Replace
    *name*'s address** (or login), or Cancel;
  - **a playlist at another address** on the same server, or another playlist file ("This is
    another address"): **Add as new** first, or **Replace *name*'s address**.
- **Nothing about who a provider is changes until the check answers,** and a check that fails
  keeps nothing. Add as new adds a new provider and leaves the one you edited as it was.
- **Jellyfin and Emby sign in again** in that card: the new access is kept, never the password,
  and their lists load again.

## Open a link

- **File → Open Link…** and the channel list's **Open a link** row, between Open file and Open a
  folder, open a card with one field and Paste. Under the link, QiTV says what it is:
  - **a live stream** plays as a channel does and goes into Recent;
  - **a film** (a file such as `.mp4` or `.mkv`, or an HLS stream that ends) plays as a film,
    and goes on where you stopped, also when its link comes back with a new token;
  - **a whole provider** (an Xtream account, an M3U playlist, a portal): "This is a whole
    provider, not one stream", and **Add as a provider…** opens the Add card filled in.
- **An Xtream stream's link** (one channel or film of an account) plays as that one stream, and
  the card also offers **Add the whole account**.
- **An `.m3u8` link is read once, when you press Play,** never while you type, and only its
  first 64 KB at most, to tell a live stream from a film or from a list of channels. A link to a
  provider that allows only so many streams is never read first: it plays at once, so QiTV never
  opens a stream more than that provider allows.
- **A link of one of your Xtream accounts** counts against that account's streams, as its
  channels do.
- **A stream's link dropped onto the window** opens the card filled in, and <kbd>Enter</kbd>
  plays it. Before, a dropped `.m3u8` or Xtream stream link became a provider.
- **With no provider yet,** a link plays as a file does: the welcome card waits until it ends.

## Network streams

- **Every link that played is kept in Network streams,** which the channel list shows after your
  providers once it has a link. Its rows say "Live", "Film · stopped at 41:12" or, when the link
  didn't play, why: "Didn't open last time (…)". The channel list's find finds them by name.
- **Network streams is not a provider** and never counts as one: with a single provider, QiTV
  stays as it was, with no All providers and no provider names on rows.
- **The same stream with a fresh token is the same link:** opening it again updates that link's
  address, with no second copy, and a film keeps where it stopped. A link that fails is kept with
  why; QiTV tries it again by itself once at most, and choosing it again opens it anew.
- **Settings › Providers lists Network streams after your providers** ("4 links"), then ＋ Open a
  link…. Each link has **Play**, **Name**, **Address**, **Kind**, **Last opened** and
  **Remove…**. A new address for the same stream is kept at once; another stream's asks first:
  **Keep as a new link**, or **Replace**.
- **Channel up and down step through Network streams** while one of its links plays (the keys,
  the media keys' next and previous, the phone's, the wheel over the picture): the next or
  previous link plays as its row plays it, round from the last to the first, in the order the
  list had when the steps began. A link that fails is stepped past, and in VLC or MPV only the
  link the steps stop on opens. A film link plays as a film: there <kbd>↑</kbd> and
  <kbd>↓</kbd> are the volume, as on any film, and the stepping stops. One link alone steps
  nowhere, and a provider's channel zaps as before.
- **Older 2.x builds** don't show Network streams.

## Deinterlacing

- **Deinterlacing is Auto or Off; On is gone.** On deinterlaced every picture, also on channels
  that aren't interlaced: on a progressive 1080p channel it tripled VLC's CPU use for nothing.
- **Auto deinterlaces only the pictures marked as interlaced,** so a progressive channel costs
  nothing more. On a Mac, and wherever hardware decoding is off, it uses VLC's yadif filter; with
  hardware decoding on Windows and Linux, the graphics chip's own deinterlacer does it, as
  before.
- **It moved from the Picture panel to Settings › Playback,** right after Hardware video
  decoding. Off applies at once; going back to Auto applies when QiTV starts again. The Picture
  panel (<kbd>A</kbd>) keeps the aspect ratio, the sleep timer and Statistics.
- **Everyone starts 2.12 on Auto,** once: whoever had On was deinterlacing progressive channels
  too. Off chosen after that stays.

## Parental controls in multiview

- **While QiTV is locked, a hidden channel plays in no multiview tile.** Before, one beside the
  focus played on after the hour ran out or after it was hidden, and Restore Last Multiview could
  bring it back. Now its tile closes to "+", the last grid forgets it, and neither a tile's
  retry nor a provider that gets ready later opens it. A hidden channel that records in a tile
  records on out of sight: the grid is left as Stop leaves it.

## Guide

- **A programme time the guide can't read leaves that programme out,** not the channel's whole
  guide, with its now and next, catch-up and series recordings.
- **XMLTV guides that write their times to the minute** show each programme at its minute
  (20:30, not 20:03).
- **The guide's day menu** lands on the same time of day across a clock change, not an hour off.

## Lists, recordings and subtitles

- **One malformed line in an M3U playlist** leaves out its channel, not the whole playlist.
- **An Xtream panel whose clock QiTV can't read** no longer fails the whole provider's load.
- **A change to the recording schedule made just as QiTV quits** is written, not lost.
- **A downloaded subtitle with a long non-Latin name** (Chinese, Cyrillic, Arabic…) is saved,
  instead of failing after the download had counted against the day's allowance, and keeps its
  own kind (`.ass`, `.vtt`).

## Faster

- **The window keeps answering while a provider's lists load and save:** they are read and
  written in pieces, so a very large provider no longer holds the window still for most of a
  second at start, on a switch or on Refresh. The first start of 2.12 writes each provider's
  lists anew once, in the background; an older 2.x build then loads them from the provider
  again once.
- **Opening a series, a season or a portal's group** no longer writes the whole list file
  again: what it opened is kept beside the lists.
- **QiTV's settings file drops the channel lists an early 1.x build left in it,** once. On a
  setup that began with 1.x they were most of the file, read at every start and written at
  every save.
- **The player bar** no longer lays itself out while it is hidden, and repaints only what
  changed, so watching takes less CPU.

## VLC and libraries

- **On macOS and Windows, QiTV comes with VideoLAN's own build of the current VLC 3.0
  release,** taken when the app is built and unchanged. **On Linux it uses the distribution's
  VLC:** the libVLC packages of the system the app is built on. QiTV's notices now say so.
- **Library updates:** certifi, pycryptodomex, tzlocal, pycares and multidict.

## AI captions and the language

- **AI captions hear the language the content speaks:** the one you pick in the Subtitles
  panel's Language row (for a title, that title's; for a song, its folder's), else the one the
  audio track names, else what they heard on that title while QiTV runs, else, on channels and
  providers' films, a clear language prefix in the title such as "TR| …" (detection still
  corrects it), else the speech model finds it by itself. **Settings › Subtitles & AI captions
  has no Default language any more;** older builds still read what it stored.
- **The translation is remembered for each spoken language.** Every language starts untranslated.
  Choosing a translation (or Off) while a language plays is remembered for that language: German
  to English, English with none, English to Türkçe. A language not heard yet isn't translated.
  The old single Translate to setting is not carried over.
- **The Subtitles panel's Translate to row** steps with ‹ › (and <kbd>←</kbd>/<kbd>→</kbd>)
  through Off and the languages you have used; a click opens the whole list, with the languages
  you used first. Until the language is known, the row says "Once the language is heard". The
  phone's Subtitles sheet shows Translate to once the language is known.
- **Live TV's player bar has a Library button** (the clapperboard, "Movies and series",
  <kbd>Ctrl/⌘+2</kbd>), where a film has its TV button. It opens the Library section you used
  last. The channel bar keeps its Library button.

## Your own videos

- **Double-click a video and it plays in QiTV,** once QiTV is your video player (Settings,
  below); on a Mac, Finder's Open With lists QiTV without any setting. A QiTV that already runs
  plays it in place of what was on: no second QiTV starts. Double-clicking the file that plays
  only brings QiTV to the front.
- **Drop a video or a song onto the window and it plays,** as File → Open File…
  (<kbd>Ctrl/⌘+O</kbd>) plays it. A subtitle file dropped with it loads for it.
- **AIFF and other audio files play:** AIFF (`.aif`, `.aiff`, `.aifc`) as every music type,
  and WMA, WavPack, Matroska audio (`.mka`), Core Audio (`.caf`), Musepack, TTA, AU, AMR, AC-3
  and DTS files when you open or drop them, or play their folder or a playlist of them: an
  album of them plays as one of MP3s does.
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
- **AI captions come back on for a video file** when they were on for the last one, with the
  translation remembered for the language they hear. They come on only when their speech model
  is already on your computer: nothing is asked for or downloaded, and they never come on while
  the video shows subtitles of its own. Choosing Off in the Subtitles panel during a video file
  keeps them off for the next one. Captions that came on by themselves go off again when you go
  back to a channel or a provider's film.
- **AI captions hear your own files' language** as above (a video's pick is that file's, a
  song's its whole folder's); **Auto** in the Language row has them hear the file anew. Turkish
  is written by QiTV's own Turkish model, as on a channel.
- **A file that's gone** says "Couldn't open …", and a file of a kind QiTV doesn't play says
  "QiTV can't play …"; what was playing goes on.
- **Several files opened at once** from the file manager: the first by name plays. Dropped
  together onto the window, they all play, as a list (below).

## The next episode from the folder

- **When an episode ends, the Next card offers the next one from the same folder.** A video
  named with its season and episode (`Show.S01E02.mkv`, `show s1e2.mp4`, `Show 1x02.avi`) gets
  the same show's next episode, or the next season's first after a season's last. It counts down
  as a provider's series does (Settings › Playback › Autoplay), and the player controls' ⏭
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
  Only the folder's own files play, never its subfolders', and an audio file named as a video
  beside it (`Show S01E01.mka`, `Show S01E01.eng.ac3`) is left out of the list.
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
  folders, newest first. Before you have opened one, it shows its Add folder tile and says what
  it is for. A single file, several files or a playlist never add their folder.
- **Each tile** shows the folder's name, what plays next or what it holds ("S2 · E5 next",
  "Album · 12 songs", "8 videos") and its picture: a `cover`, `folder` or `front` image in the
  folder, else a drawn one with its name.
- **Click a tile** and its folder plays from where you stopped; <kbd>Esc</kbd> comes back to the
  tab. **Add folder**, the last tile, picks another.
- **×** (or <kbd>Delete</kbd>) takes a tile off, and **Clear** takes them all off after asking
  (the tab stays, with Add folder): nothing is deleted from the disk. A folder that was moved or
  deleted, or is on an unplugged drive, says Missing, with Remove.
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
  translation under it. On Cover and Album the lines show at the bottom. They find the song's
  language by themselves ("Words · English" once they have); a language you pick in the
  Subtitles panel is its folder's. They are the AI captions themselves, so they stay on for what
  plays next until you turn them off. They never come on by themselves for music, and they catch
  less of a song than of speech: podcasts and talk do best.
- **The bars move with the music itself:** QiTV reads the file's sound a little ahead and draws
  them in the cover's colours. They stand still while it's paused. Only files have them.
- **Radio:** a channel that sends sound and no picture plays on the audio screen too, its logo
  as the cover, with the song its stream names (else the programme on now). It keeps live TV's
  controls, such as record, Go live and rewinding. Its Stage shows the words, without bars.
- **The system's media panel** on Windows, macOS and Linux shows the song's artist, album and
  cover (a radio channel's name and logo), and the media keys' previous and next go through the
  album's songs; previous on a lone song starts it again.

## QiTV as your video and music player

- **Settings › Files has two rows, Open videos with QiTV and Open music with QiTV,** so QiTV
  can open your videos, your music, or both. Each says at its end what opens them now ("QiTV
  opens them" or "Another app opens them"), and a press changes it. Playlists are offered in Open
  With with either, never made the default.
- **On Windows** QiTV registers for your user only, with no administrator rights, and appears in
  Open With. Windows' Default Apps settings then open (on QiTV's own page on Windows 11), where
  you confirm it; **Default Apps…**, the row after them, opens them again. While QiTV runs, a
  double-click on a file goes straight to it.
- **On a Mac** QiTV is in Finder's Open With for videos, music and playlists without any setting.
  The row makes it the default for MKV, MP4, MOV, AVI and WebM (music: MP3, M4A and FLAC);
  Finder's Get Info → Open with → Change All picks another app.
- **On Linux** QiTV adds its own entry for your user and becomes the default video (or music)
  player.
- **The music types** are MP3, M4A, AAC, FLAC, OGG, Opus, WAV and AIFF; the other audio formats
  above play without being registered.
- **`.ts` is in Open With only,** never made the default: program source files use the same
  extension.
- **Stopping removes exactly what QiTV wrote.** A registered QiTV that you move registers itself
  again from its new place when it starts. A QiTV run from a temporary folder or a zip, or on a
  Mac from its disk image or Downloads, asks to be moved first.

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
