# QiTV 2.15.0

QiTV 2.15.0 plays Kick and Twitch channels: paste a channel's page link and it plays live, at its
best quality, about a second behind the broadcaster, with Twitch's ad breaks skipped. Pause,
rewind, recordings and AI captions work on them as on any live channel. Ctrl/⌘+N opens Open
Link…, and on Windows the picture's right-click menu stays open.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Kick and Twitch

- **Paste a channel's page.** In **File → Open Link…** (or the channel list's **Open a link**),
  paste `kick.com/<channel>` or `twitch.tv/<channel>`, with or without `https://` or `www.`.
  QiTV says "A live stream" and plays it. No stream link to hunt for in the page.
- **The best quality the channel offers.** QiTV picks the tallest picture, then the higher frame
  rate, and a kind of video it can play; it passes over AV1, which the player inside QiTV
  cannot play from these streams.
- **About a second behind the broadcaster.** With **Live latency** at Auto, a Kick or Twitch
  channel usually plays about a second behind; Low takes it closer, under a second, and Stable keeps
  about six seconds in hand for a picture that never waits. A channel and **Go live** start
  there, timed to the channel's newest keyframe, so they rarely need to catch up afterwards. Twitch's pieces play as they arrive, not once they are complete.
- **How far behind, in the statistics.** <kbd>Shift+I</kbd> (or the Picture panel's
  **Statistics**) shows **Behind the broadcaster**: the seconds between the moment the site
  stamped on the picture and now.
- **Twitch's ad breaks are skipped.** When a Twitch channel's ads come, QiTV leaves them out and
  shows "Ad break skipped"; the picture waits for the channel, then plays from its newest
  moment. A break that goes on much longer than usual is waited for instead of freezing.
- **Back at the newest moment.** When the stream comes back after a drop, a restart or an ad
  break, the channel plays from its newest moment, not from where the picture stopped.
- **A channel that isn't live waits for it.** Opened while the streamer is off, or when the
  stream ends while you watch, the channel waits on screen: "channel isn't live now · It plays
  as soon as it's live". QiTV asks the site again about every half a minute, and the channel
  plays as soon as it's live. **Try now** asks at once.
- **Asked afresh each time.** A page's stream address changes, so QiTV asks the site again
  whenever you play the channel, and again each time the stream drops or its address runs out,
  waiting a little longer each time it keeps failing, in picture-in-picture and multiview too.
  When QiTV asks again after a drop, the pause and rewind buffer starts over. Nothing the site
  gives QiTV is kept or written to the log: only the site, the channel and what happened.
- **In Network streams** the page is kept, named "channel · Kick" or "channel · Twitch", and
  channel up and down step through it like any link. A channel saved more than once, written
  differently (with `www.`, other capitals, or a `?` part), is one row again.
- **When it doesn't play, it says why:** "No channel named channel on Kick", or "Kick refused
  QiTV's request: try again later, or paste the stream link".
- **In your language.** The new words are in all 46 languages.

## Also in 2.15.0

- **Ctrl/⌘+N opens File → Open Link…**, the card that plays a stream's link or a channel's
  page.
- **The picture's right-click menu stays open on Windows.** It closed again as soon as it
  opened.
