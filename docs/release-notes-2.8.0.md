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
  server, which converts nothing unless you ask it to in Settings (Server conversion). When a
  title has a 4K version and another one, the 4K one plays on a 4K screen, the other below it;
  a title only in 4K plays anywhere, as it is (Settings → 4K versions). Where you stop is told
  to the server, so its own Continue watching and watched marks follow, and a title started
  only in the server's apps starts here where you left it there. QiTV keeps the sign-in the
  server gives it, not your password. Older Jellyfin servers work too.
- **Cast to a TV.** The new cast button in the player controls sends what plays to a
  Chromecast, or a TV with Chromecast built in, on your network: a channel goes on from where
  it was in its time-shift buffer, a film from where it is. **Playing on …** says where the
  picture went, and **Stop casting** brings it back. The phone remote's More has **Cast to a
  TV** too.
- **Scheduled recordings.** In the guide, **Record** (or R) records a programme on now or still
  to come at its time, and **Record series** every airing of it on that channel; a red dot marks
  them, and Cancel recording takes one off. QiTV records in the background, from a minute
  before to three minutes after, without opening anything on screen, as long as it is open.
  It needs one of the provider's streams: without one free, it tries again every minute
  while the programme airs (it never stops what you are watching for it). Back-to-back
  programmes on one channel share the stream, and a recording cut short goes on in another
  file. The REC sign shows what records and stops it. The phone remote's timetable has the
  same Record and Series.
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
  What you hid leaves Recent and Continue, locked or not.
- **New for Xtream films and series.** The Library's New shelf and Recently added sort now
  know when an Xtream provider added each film and series.
- **Film and series details from TMDB.** Where your provider leaves a film or a series without
  a poster (or with one that doesn't load), a story, a year, a running time, a genre, a rating
  or a cast, the Library shows TMDB's, in QiTV's language; the strip now names the cast too.
  QiTV finds the title by the TMDB or IMDb number the provider lists, else by its name and
  year, and shows nothing when it can't be sure. Only the title and its year are sent. Turn it
  off in Settings.

## Fixed

- **QiTV's log says why it quit.** It now tells QiTV's own Quit from a quit asked from outside
  (the Dock, another app, logging out), to explain quits that looked like crashes.
