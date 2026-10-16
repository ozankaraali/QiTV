# QiTV 2.0.2

This update makes live TV adapt to each channel. A new **Auto** latency, the default for new
installs, finds how far behind live each channel plays best, and QiTV no longer speeds up or slows
down a channel that can't take it.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## Auto live latency

- **Auto adjusts each channel.** Settings → **Live latency** has a new first choice, **Auto
  (recommended)**. It starts a channel about 2.5 seconds behind live, like Normal. Each time the
  channel runs dry or stalls although it had that much room, Auto plays it 1.5 seconds further
  behind, up to about 10 seconds (for a channel that arrives in bursts, up to a burst plus 2
  seconds). Running dry right after you switch to a channel, before it had the room, doesn't
  count. After every 5 minutes without trouble it comes half a second closer again, back to where
  it started.
- **No jumps.** When Auto moves a channel further behind, QiTV doesn't jump back: it plays 5%
  slower for a while (the sound keeps its pitch), or simply lets the room grow if the channel
  can't change speed.
- **Channels are remembered.** What Auto learns is kept per channel, so the next time you watch
  one, it starts where it left off.
- **You can see it.** At the live edge the player controls read, for example, "Stay live ● 7.2 s
  behind · adjusted": how far behind live the channel plays, and that Auto has moved it further
  back for this channel.
- **Your setting stays.** If you used 2.0.0 or 2.0.1, your latency stays as it was (Normal, unless
  you changed it). Choose **Auto (recommended)** in Settings to use it. Low, Normal and Stable keep
  the same distance on every channel, as before.

## Smoother live playback

- **HLS channels whose segments are packaged separately are joined cleanly.** The player no
  longer sees a broken stream at every segment join.
- **A few channels whose streams can't be time-shifted smoothly play without rewind.** Some
  channels still crawl through the time-shift buffer while the player plays them smoothly on its
  own. QiTV notices this by itself after about 20 seconds, plays the channel without rewind ("Plays
  without rewind for smoother playback", which a rewind there says too), and tries the
  time-shift again a day later.
- **Channels that can't change speed play at normal speed.** To stay near live, QiTV plays a
  little faster or slower (1.05×, 1.1× or 0.95×). On some channels the player can't do that
  smoothly: the picture crawled, and QiTV then jumped back to live every half minute or so. QiTV
  now notices this within a few seconds, plays such a channel at normal speed only, and
  remembers that for the channel for a day, whatever latency you chose. After a day it tries
  other speeds again, and goes back to normal speed only if they still don't play smoothly.

## Good to know

- A channel that plays at normal speed only can't catch up by playing faster. It stays a little
  further behind instead; if it ever falls more than 20 seconds behind its target, QiTV goes back
  to live once, as before.
- What QiTV learns is kept in `live_tuning.json` beside your settings, for at most 500 channels.
  It holds a code for each channel, not its address or your login.
- See the [2.0.1 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.1.md)
  and the [2.0.0 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.0.md)
  for the earlier changes.

## Fixes

- **Security and upkeep:** `aiohttp` 3.14.5 and `multidict` 7.1.0 (a reported vulnerability in 6.9.0), plus `orjson` and other small library updates. Qt stays at 6.11 for now.
- **Picture-in-picture buttons work while the player controls show.** The controls were drawn above the
  small picture's label and took the clicks on its speaker, ⇄ and ×; a dark band behind the label is gone too.
