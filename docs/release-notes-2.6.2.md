# QiTV 2.6.2

QiTV 2.6.2 rewinds a live channel as far back as the channel's own server keeps it, and plays
your provider's archive through the time-shift buffer.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## New

- **As far back as the channel's server keeps it.** Some live channels' own servers keep hours
  of the channel, a day for some, while QiTV's buffer begins when you tune in. On such a
  channel the bar now reaches half an hour further back than the buffer (dashed), and ←, the
  bar and ⏮ go on back as far as the server keeps it. The guide's **▶ Watch from the start**
  plays a past programme from there too, on the channel you watch or one you watched earlier.
- **Your buffer first.** What the buffer holds always plays from the buffer. Only what lies
  before it comes from the server, a minute ahead of what plays and never twice, and at the
  buffer's start playback goes on into the buffer without a break. What came from the server
  plays again at once until you go live, leave the channel or go back further. With a provider
  that limits its streams, the live stream pauses meanwhile and opens again when you are back
  in the buffer or go live.
- **The provider's archive, through the buffer too.** Rewinding past the buffer into your
  provider's archive (catch-up) plays the archive the same way when it comes as a stream QiTV
  can buffer, as most do: it stays on the channel's own bar, what came of it replays at once,
  and when it reaches the buffer's start, playback goes on into the buffer. So does the
  guide's Watch from the start on the channel you are watching. Other archives, an STB
  portal's recordings and Watch from the start on another channel play as before.

## Fixed

- **Channels served from two places no longer jump.** Some broadcasters serve one live
  playlist from two groups of servers, each numbering its own copy of the channel. QiTV took
  each switch between them for a break: the picture jumped back, and the sound dropped out and
  came back, sometimes louder. It now waits a moment for its own group's copy, and follows the
  other only when that one stays.
