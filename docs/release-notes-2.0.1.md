# QiTV 2.0.1

The first update to 2.0. It fixes how some live channels play, and puts a button on screen for
everything you could only do with a key, so QiTV works fully with a mouse or trackpad.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## Live TV plays smoother

- **Channels that arrive in bursts no longer stutter or say "Reconnecting…".** Some providers
  send a channel as about 10 seconds of video every 10 seconds, instead of a steady stream. QiTV
  now notices this within about one burst (10–15 seconds) and plays such a channel about one
  burst behind, without speeding up and running dry, and without false "Reconnecting…" messages.
  To build that room it may play 5% slower for a minute or two (the sound keeps its pitch).
- **Slow-starting channels get more time.** A channel whose provider starts it on demand now has
  up to 15 seconds to begin, as long as it is sending something, before QiTV falls back to playing
  it without time-shift.
- **A live channel that stops is reconnected.** If a live stream ends unexpectedly, QiTV tries it
  again twice before saying "The stream ended".

## Everything is on screen

- **Player controls:** new buttons for volume (opens the mixer), the guide, search, the previous
  channel, and a ★ to add or remove the channel from Favorites. Click the channel's name for its
  info. Point at a button to see its key.
- **Picture-in-picture:** the small picture's speaker now mutes and unmutes it, a new ⇄ button
  swaps the two pictures, and the buttons are bigger and easier to hit.
- **Channel list:** next to ↻ and ⚙, the top row now has ‹ to go back a level, ⌕ search, and
  clickable names: click your provider's name to see your providers and switch to another. The
  list of sources also has an **Open file** row.
- **Guide:** buttons for earlier and later, search, fullscreen and closing the guide.
- **Library:** a fullscreen button, and a **‹ Library** button on the player controls takes you
  back after a movie or an episode.

## Fixes

- **No more "Time-shift isn't available" pop-up when a channel loads.** A channel that can't be
  time-shifted just plays; the message shows only if you try to rewind it.
- **The channel banner waits for slow channels.** It stays up until the channel's picture starts,
  instead of fading while a slow channel is still opening.
- **macOS:** after a click on the picture, the arrow keys no longer make the "can't do that"
  sound.
- **Library:** hovering its top bar no longer fails to show the tooltips.

## Good to know

- A channel that arrives in bursts plays about one burst (often ~10 s) behind the provider. That
  is the nearest it can play without running out between bursts.
- On Windows, the independent PiP and main volumes are checked in code but not yet by ear on a
  real Windows machine.
- See the [2.0.0 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.0.md)
  for everything new in 2.0.
