# QiTV 2.8.0

QiTV 2.8.0 plays your Jellyfin and Emby servers' films and series, casts to a TV, records
programmes at their time while you are away, and adds parental controls.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## New

- **Jellyfin and Emby.** Add your server as a provider (Settings → Providers → Jellyfin or
  Emby, then Verify Provider) and its films and series join the Library: its libraries are the
  genres, with Continue watching first, the server's posters and stories, and the search
  finding them beside your other providers'. A title plays as its file is, straight from the
  server, which converts nothing unless you ask it to: the player's ⚙ lists the title's
  versions and the server's conversions (1080p, 720p, 480p), and asks before the first
  conversion on a server, since it uses that server's processor. When a
  title has a 4K version and another one, the 4K one plays on a 4K screen, the other below it;
  a title only in 4K plays anywhere, as it is (Settings → 4K versions). Where you stop is told
  to the server, so its own Continue watching and watched marks follow, and a title started
  only in the server's apps starts here where you left it there. QiTV keeps the sign-in the
  server gives it, not your password. Older Jellyfin servers work too.
- **A channel's picture size.** A channel whose stream comes in several sizes shows the ⚙ in
  the player controls too: Auto (its best) or a smaller size, for a slow or busy connection.
  The channel goes on from where it was, its time-shift buffer kept (and the one of the
  channel you watched before it), and keeps that size for next time until you choose Auto,
  also when it plays without the time-shift buffer.
- **Cast to a TV.** The new cast button in the player controls sends what plays to a
  Chromecast, or a TV with Chromecast built in, on your network: a channel goes on from where
  it was in its time-shift buffer, a film from where it is. **Playing on …** says where the
  picture went, and **Stop casting** brings it back. The phone remote's More has **Cast to a
  TV** too. A picture in picture opened while casting plays on the computer, and swapping the
  pictures sends the new large one to the TV.
- **Scheduled recordings.** In the guide, **Record** (or R) records a programme on now or still
  to come at its time, and **Record series** every airing of it on that channel; a red dot marks
  them, and Cancel recording takes one off. QiTV records in the background, from a minute
  before to three minutes after, without opening anything on screen, as long as it is open.
  It needs one of the provider's streams: without one free, it tries again every minute
  while the programme airs (it never stops what you are watching for it). Back-to-back
  programmes on one channel share the stream, and a recording cut short goes on in another
  file. The REC sign shows what records and stops it. The phone remote's timetable has the
  same Record and Series.
- **Record at a time.** A channel without a guide records too: right-click it (or use the
  guide's **Record at a time…**) and choose from when to when, once, every day, Monday to
  Friday or on the days you tick. The phone remote's timetable offers the same.
- **Recordings in the Library.** A **Recordings** shelf in Movies lists your recordings, newest
  first, named after their programme when they were scheduled, with the recordings still to
  come above them. A recording plays from where you stopped and seeks like a film; right-click
  shows its folder, deletes it, or cancels one to come. The phone remote lists them too and
  plays them on the TV, or at home on the phone itself, from QiTV, with nothing re-encoded.
- **Parental controls.** Settings → Parental controls sets a PIN. Then right-click a group or
  a channel to hide it, or hide every group whose name looks adult, also the ones a provider
  adds later. While QiTV is locked, what you hid is gone from the channel list, the guide,
  changing channels, the Library, the search and the phone, and a hidden channel that plays
  stops; unlocking with the PIN lasts an hour, and only then can anything be shown again.
  What you hid leaves Recent and Continue, locked or not. A Library genre hides the same way
  (right-click it).
- **New for Xtream films and series.** The Library's New shelf and Recently added sort now
  know when an Xtream provider added each film and series.
- **The Library in one click, and without waiting.** The channel list's top row has a Library
  button (a clapperboard, as Ctrl/⌘+2) that opens Movies, or the section you opened last, and a
  provider's Movies and Series rows open the Library on that provider. The Library's provider
  menu ticks several providers to mix their films and series (All providers ticks them all).
  A Jellyfin or Emby server chosen in the channel list shows its Movies and Series rows, not a
  Live TV error. An Xtream provider's and a media server's film and series lists load ahead
  once the provider is ready, and a list past its six hours shows at once while the new one
  loads behind it.
- **Refresh films and series.** The Library's ↻ (beside its provider menu, also in that menu
  and as Ctrl/⌘+Shift+R) loads the films and series of the providers it shows again, while
  the saved lists stay on screen. A Jellyfin or Emby server is asked only for what changed
  since the last look (titles added, removed, changed, or watched elsewhere), not its whole
  library again. The phone remote's Library has **Refresh** too.
- **The channel list's find starts where you are.** Typing in the channel list finds the open
  group's channels first, then the provider's, then every provider's, as the phone's find
  does.
- **A large Library without waits.** With a provider of tens of thousands of films, the
  shelves, a genre's sorting and a find are worked out in the background: the window no longer
  stops for them.
- **Film and series details from TMDB.** Where your provider leaves a film or a series without
  a poster (or with one that doesn't load), a story, a year, a running time, a genre, a rating
  or a cast, the Library shows TMDB's, in QiTV's language; the strip now names the cast too.
  QiTV finds the title by the TMDB or IMDb number the provider lists, else by its name and
  year, and shows nothing when it can't be sure. Only the title and its year are sent. Turn it
  off in Settings.

## Fixed

- **STB portals' channels and films open again where their stream servers want a box.** QiTV
  asks for an STB portal's streams as a MAG box does (its user agent), as QiTV 1.x did: some
  portals' stream servers drop any other client, and their channels failed to open.
- **STB portals' lists are asked more patiently.** Film and series pages go four at a time,
  wait 15 s and are tried three times; the whole channel list waits 30 s and is asked again
  after a timeout. VLC or MPV chosen as the player gets the box's user agent too, and an M3U
  channel's own agent and referrer.
- **Casting says when it can't.** Without VLC's casting on a computer, the cast menu (and the
  phone's) says so instead of looking for devices forever.
- **Recordings.** A recording still being written can't be deleted; quitting while a programme
  records keeps it named in the Library, and reopening QiTV while it airs records the rest; a
  programme that ended is not scheduled (a phone's old timetable); a series is found among
  very short programmes; the schedule file no longer keeps an Xtream account inside channel
  addresses.
- **Time-shift.** After a stream's clock leapt ahead, a reconnect onto a clock a little behind
  no longer steps the picture back; a long server window's playlist is not parsed again when
  it hasn't changed. Kept buffers on disk keep what is whole after a crash (a damaged last
  segment no longer loses the whole run), a lowered disk amount applies at once, Go live
  pressed while a kept channel comes back goes live on its new stream, and updating QiTV while
  a channel records keeps its buffer.
- **Phone remote.** Behind a VPN on the computer, the phone finds QiTV at home on the Wi-Fi
  (not the VPN's address); a find with more than a hundred groups or genres pages on; two
  phones searching at once each get their own results; where a phone stopped a film is never
  credited to the next title; and a recording question answered on the TV or another phone
  goes from both screens.
- **QiTV's log says why it quit.** It now tells QiTV's own Quit from a quit asked from outside
  (the Dock, another app, logging out), to explain quits that looked like crashes.
