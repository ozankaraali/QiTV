# QiTV 2.1.0

QiTV 2.1 puts all your providers in one place. Channels, films and series from every account
share one channel list, one Library and one history, each one plays from its own provider, and
what you watch keeps playing while you browse, search or pick a film.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## New

- **All providers.** With two or more providers, the channel list's sources column starts with
  **All providers**. It shows ★ Favorites, ⟲ Recent and All with every provider's channels
  together, each with its provider's name beside it, and then each provider's own groups under
  its name, named as that provider names them. The Library has the same choice behind its
  provider button at the top right: one provider's films and series, or all of them, with each
  provider's genres under its name and Continue, New and My list mixing them. The menu bar's
  Providers menu has All providers too and ticks the one the channel list shows.
- **Every stream from its own provider.** A channel, film or episode always plays from the
  provider it came from, in the big picture and in picture-in-picture. Watch a channel from one
  account, open a channel from another account in picture-in-picture, swap them and change
  channels on either. Stream limits count per provider: two accounts that each allow one stream
  can each play one picture.
- **Browsing is only browsing.** Picking another provider, or All providers, in the channel list
  or the Library only changes what that list shows. Nothing that plays stops, picture-in-picture
  stays open, and ↑/↓ keep going through the list the channel was picked from.
- **The channel list opens at what you watch.** Enter or a click on the picture opens the list
  where the big picture's channel was picked: its provider (or All providers), its group, its
  row. After a swap, that is the new big picture's channel.
- **One history.** ⟲ Recent in the channel list lists the channels you watched at every
  provider, newest first, each with its provider's name. Backspace (the channel before) goes back
  across providers too, and "Start with the last channel" tunes the last channel from its own
  provider.
- **Stars per provider.** A star now belongs to one provider: starring a channel called "News"
  at one account doesn't star a "News" at another. A provider's ★ Favorites and My list show its
  own stars; All providers shows them all.
- **Search plays from every provider.** A search hit from another provider plays from its own
  provider, without switching provider first.
- **Keep watching while you browse.** Opening the Library or the search no longer stops what
  plays: the TV shrinks into a corner of the Library, picture-in-picture and sound included, and
  goes on playing. Its buttons mute it, give it the whole window back (⤢) or stop it (×); drag
  it to another corner if it is in the way. A click on it, ⤢, **Live TV** or Esc from the genres
  brings the TV back exactly as it was.
- **A film keeps the channel small.** Playing a film or an episode from the Library puts it in
  the big picture and keeps the channel you were watching in the small one. If the film's
  provider has no stream to spare, QiTV closes the small picture from that provider and says so.
- **Each provider's guide when you need it.** QiTV loads a provider's guide while its channels
  are on screen or playing and lets it go afterwards, so All providers doesn't download every
  guide at once.
- **Quick with big accounts.** QiTV stays quick with four providers and over 200,000 channels
  between them. On a test catalog of 214,000 channels and 40,000 films, the channel list opens
  on All in about a tenth of a second, the Library's genres show in a quarter of a second, and
  search shows its first results in about half a second. The lists come from each provider's
  saved copy, never from the network while you browse.
- **Steadier live TV.** Some HLS channels deliver their pieces unevenly, and QiTV used to answer
  that by speeding up and slowing down every few seconds. Now it looks at about a minute of the
  stream before it acts and keeps normal speed while the channel plays about where it should.
  Only when it has really drifted for a while does QiTV change the speed, by a few percent, eased
  in and out in three gentle steps, at most about once every 5 minutes. Far behind, it still
  catches up faster, and after the stream runs dry it slows down a little at once to build room
  again.
- **A calm distance display.** At the live edge the player controls read, for example,
  "≈ 7 s behind", in half seconds, and change only once the distance has stayed different for a
  few seconds. The dot beside it is amber whenever the speed isn't normal: "catching up" while
  QiTV plays faster, "adding buffer" while it plays slower.
- **The distance shown is the real one.** On channels whose clock only moves every 2 seconds,
  QiTV used to think it was about 11 seconds further behind than it was, stopped changing the
  speed and soon gave up on the time-shift buffer. It now follows the player's own clock on every
  channel, so the distance, Go live and the speed work there as on any other channel.
- **Channels start where they should.** A channel now starts at the distance QiTV aims for, not
  at the oldest part of what the provider sends. On a provider that sends 20 seconds at once, a
  channel started about 19 seconds behind and then sped up for a minute; it now starts a few
  seconds behind at normal speed. HLS channels fetch enough at the start to keep playing: on
  channels that send 10-second pieces, the picture used to freeze and the sound drop out in the
  first half minute.
- **Clean jumps.** Go live, ← and →, and QiTV's own jumps start where the picture is complete
  (a keyframe), so a jump no longer shows broken blocks or loses the sound for a moment. When a
  channel has fallen far behind, QiTV jumps closer once instead of playing faster for a minute or
  more, at most once a minute; a few seconds off, it still eases the speed gently as above.
- **Auto learns each channel.** Under Auto, QiTV now learns from how a channel's pieces arrive
  how far behind live it can play without the picture running dry, and starts there the next
  time: about 2.5-3.5 seconds for a steady stream, more for one that arrives in uneven pieces or
  in bursts, never less than one burst. A pause the buffer covered no longer pushes a channel
  further back; only running dry does, and quiet minutes bring it closer again. "adjusted" beside
  the distance means trouble is holding the channel further back for now.
- **A late backlog doesn't leave you far behind.** Some providers send 20 seconds of backlog a
  moment after a channel starts. QiTV used to stay that far behind for a minute; now, once the
  stream flows on normally, it picks the start again within seconds.
- **More channels with time-shift.** A channel whose server answers at once but sends its first
  picture seconds later now gets up to 15 seconds ("Still loading…") instead of playing without
  rewind after 6, and a first request a server turns away in passing is tried again (never one
  that says the account's connections are used up). An HLS channel that lists its sound
  separately but also carries it with the picture now time-shifts, and a playlist entry that
  names the page its stream expects to be opened from sends it, to the player and the time-shift
  buffer.

## Fixes

- **No more speed lock for a day.** A channel whose clock changed rhythm while QiTV was adjusting
  the speed could be held at normal speed for a day. It isn't any more.
- **Picture-in-picture renews an expired portal link.** An STB channel in the small picture whose
  link expired now gets a fresh one once, as the big picture does, instead of an error.
- **Guides from a web address are kept.** A guide whose server sends no date, or a date QiTV
  couldn't read, wasn't saved. It is now.
- **Live TV comes back after a lost buffer.** When the time-shift buffer was lost and QiTV
  switched to playing the channel directly, a provider that allows one stream could still count
  the old connection for a few seconds and refuse the new one, which left the picture black.
  QiTV now tries again up to three times (after 3, 5 and 10 seconds), showing "Reconnecting…",
  before it reports an error.

## Good to know

- **Some channels still play without rewind.** A few HLS channels send their sound as a separate
  stream, or use the newer fragmented MP4 format. QiTV plays them directly, as before, so they
  have no time-shift yet.
- **Hardware video decoding is safer to try.** It is still off by default and experimental
  (Settings). With it on, when a channel's picture freezes while its sound goes on, QiTV
  switches that channel back to software decoding by itself within a few seconds, and keeps it
  there for a day.
- **With one provider, nothing changes.** All providers appears once you have two or more
  providers. With one, the lists, channel numbers, stars, the guide and Recent work as in 2.0.
- **Your stars come along.** A star from 2.0 stays on every provider that has a channel, film or
  series of that name; un-star it at one provider and the others keep theirs. Within one
  provider, items with the same name still share one star. If you go back to 2.0, it still finds
  your stars by name.
- **Channel numbers in mixed lists.** In All providers, and in ⟲ Recent with several
  providers, two providers can use the same number. Typing it tunes the first one, in
  the order of your providers in Settings.
- **Groups are never merged.** Each provider keeps its own groups and genres, even when two use
  the same name, and a channel that two providers carry is listed for each.
- **One account's trouble stays with it.** A provider that can't sign in or refresh keeps its
  saved lists, and the other providers keep working. A channel you play from it says which
  provider failed and why.
- **Each list remembers where you were.** The channel list and the Library each remember the
  provider (or All providers) you browsed last, also after a restart.
- See the [2.0.8 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.8.md)
  and the [2.0.0 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.0.md)
  for everything new in 2.0.
