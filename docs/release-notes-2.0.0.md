# QiTV 2.0.0

QiTV 2.0 is a new QiTV: a player that works like a TV, built on VLC. The channel fills the
window, the arrow keys change channels, and the channel list, the guide and the controls slide in
over the picture when you need them.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## Highlights

- **One window, like a TV.** ↑/↓ change the channel, numbers jump to one, Backspace goes back to
  the channel before. Each change shows a banner with what is on now and next; pressing quickly
  opens a fast list of the channels around you. Enter (or a click) opens the channel list over
  the picture, with your groups, Favorites and Recent channels.
- **Pause and rewind live TV (time-shift).** Every live channel keeps its last few minutes in
  memory (128, 256 or 512 MiB per stream, about 2, 4 or 9 minutes of HD). Space pauses, ←/→ jump
  10 seconds, L goes back to live, and you can play faster to catch up. For a longer history,
  QiTV can use the disk, but only once you allow it in Settings → Time-shift.
- **Live latency.** Choose how close to the live broadcast QiTV plays (Low, Normal, Stable); it
  gently adjusts the speed to stay there.
- **Picture-in-picture.** A second channel in a small picture in the bottom-right corner. Click it
  or press S to swap the two; the sound follows the big picture, and a mixer sets each picture's
  volume.
- **The Library for movies and series.** Posters by genre, with Continue, New and My list at the
  top, a detail strip with Resume and Start over, seasons and episodes with what you have
  watched, the next episode after a countdown, A–Z jumps, and one search across all your
  providers.
- **The programme guide.** G shows the channels and what is on, from now to 24 hours ahead.
- **VLC comes inside QiTV.** You no longer need to install VLC to use the app.
- **Settings without the menu bar.** The channel list's left-most column has ⚙ Settings and
  ＋ Add provider, so you can reach everything with the keyboard or a remote, also in
  fullscreen.
- **Faster.** A last performance pass made 2.0 lighter than its own earlier builds: the window
  shows its first picture in 1.45 s instead of 1.80 s, the channel list opens on 2,500 channels
  in 53 ms instead of 118 ms, and the guide opens in 56 ms instead of 106 ms.

## Keys

All keys are in the [README](https://github.com/ozankaraali/QiTV/blob/main/README.md#keys) and in the app under Help → Keyboard Shortcuts.
<kbd>Ctrl/⌘</kbd> means Ctrl on Windows and Linux and ⌘ (Command) on a Mac.

| Keys | What they do |
| --- | --- |
| <kbd>↑</kbd> / <kbd>↓</kbd> | Previous / next channel (volume during a movie) |
| <kbd>0–9</kbd> | Type a channel number |
| <kbd>Backspace</kbd> | Previous channel |
| <kbd>Enter</kbd> | Channel list |
| <kbd>←</kbd> / <kbd>→</kbd> | Back / forward 10 seconds |
| <kbd>Space</kbd> | Play / pause |
| <kbd>L</kbd> | Go live |
| <kbd>G</kbd> | Guide |
| <kbd>I</kbd> | Channel info |
| <kbd>+</kbd> / <kbd>-</kbd> | Volume up / down (the mixer) |
| <kbd>M</kbd> | Mute |
| <kbd>F</kbd> / <kbd>F11</kbd> / <kbd>⌃⌘F (Mac)</kbd> | Fullscreen |
| <kbd>Ctrl/⌘+F</kbd> | Search |
| <kbd>Ctrl/⌘+D</kbd> | Add or remove a favorite channel |
| <kbd>P</kbd> / <kbd>Ctrl/⌘+P</kbd> | Picture-in-picture with the previous channel, or close it |
| <kbd>S</kbd> | Swap the large and the small picture |
| <kbd>Alt/⌥+P</kbd> | Mini player |
| <kbd>Ctrl/⌘+1</kbd> / <kbd>Ctrl/⌘+2</kbd> / <kbd>Ctrl/⌘+3</kbd> | Live TV / Movies / Series |
| <kbd>Esc</kbd> | Close what is open, leave fullscreen; after a movie, back to the Library |

In the channel list, picture-in-picture is <kbd>Ctrl/⌘+P</kbd>: there, a plain P types into the
filter.

## Good to know

- **2.0 replaces the 1.14 line.** QiTV 1.14 played with MPV, which it bundled (GPLv3); 2.0 plays
  with VLC again, which now comes inside the app, and bundles no MPV. 2.0 uses the same settings
  folder as 1.13 and 1.14.
- **Channel logos and the guide are on.** 2.0 turns both on once, also if you had turned them off
  in 1.x, because the banner and the channel list show them. To stop loading logos, untick
  **Enable Channel Logos** in Settings; to stop the guide downloads, set **EPG Source** to
  **No Source** in Settings → EPG.
- **Resume points of episodes do not carry over.** 2.0 tells episodes apart in a new way, so an
  episode you were in the middle of in 1.x starts from the beginning once.
- **The minimum system is unchanged from 1.13.8** (on a Mac, macOS 13 or newer).
- **Time-shift on disk asks first.** Without your consent, time-shift uses only memory. With it,
  one stream at a time keeps its history on the disk, up to the size you choose (256 MiB to
  16 GiB); QiTV deletes it as it goes and when you change channel or quit.
- **"Keyboard/Remote Mode" is gone,** because its behaviour (the arrow keys move and play) is now
  how QiTV always works.
- **Other audio languages on time-shifted channels.** Some HLS channels offer other languages as
  separate audio streams. While such a channel plays with time-shift, only its main audio is
  available.
- **Favorites are still a list of names,** as in 1.13.8, so your favorites carry over. Two items
  with the same name (in different groups, or a channel and a film) share one star. A change is
  planned for 2.1.
- **Picture-in-picture in the channel list is Ctrl/⌘+P.** Plain P types into the list's filter.

## Known issues

- After you change an Xtream provider's address, HTTPS setting or password, QiTV may not start
  on the last channel you watched; pick it again once.
- An STB portal that works only over plain HTTP cannot be set up while "Prefer HTTPS" is on
  (as in 1.13). Turn it off for that provider.
- On Windows, an HTTPS stream through an HTTP proxy that stops answering can take up to a minute
  to let go.
- On Linux and Windows, libVLC checks all its plugins each time QiTV starts, which adds about a
  second to startup.

## Licensing

QiTV is MIT-licensed. The downloads contain VLC (libVLC, LGPL-2.1-or-later; some plugins
GPL-2.0-or-later), Qt and PySide6 (LGPLv3), the Python runtime and Python libraries under their
own licenses: see
[THIRD-PARTY-NOTICES.md](https://github.com/ozankaraali/QiTV/blob/main/THIRD-PARTY-NOTICES.md),
which ships inside every download with the license texts.

*The screenshots in the README show made-up demo content (`scripts/make_demo_content.py`).*
