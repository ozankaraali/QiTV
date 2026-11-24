# QiTV 2.15.0

QiTV 2.15.0 plays Kick and Twitch channels: paste a channel's page link and it plays live, at its
best quality, about as close to live as the site's own player, with Twitch's ad breaks skipped.
Pause, rewind, recordings and AI captions work on them as on any live channel.

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
- **Close to live.** QiTV starts at the newest pieces the site has announced, so the picture is
  about as close to live as the site's own player.
- **Twitch's ad breaks are skipped.** When a Twitch channel's ads come, QiTV leaves them out and
  shows "Ad break skipped"; the picture waits for the channel and goes on. A break that goes
  on much longer than usual is waited for instead of freezing.
- **Asked afresh each time.** A page's stream address changes, so QiTV asks the site again
  whenever you play the channel, and again if the stream drops, in picture-in-picture and
  multiview too. When QiTV asks again after a drop, the pause and rewind buffer starts over.
  Nothing the site gives QiTV is kept or written to the log: only the site, the channel and
  what happened.
- **In Network streams** the page is kept, named "channel · Kick" or "channel · Twitch", and
  channel up and down step through it like any link.
- **When it doesn't play, it says why:** "channel isn't live now", "No channel named channel on
  Kick", or "Kick refused QiTV's request: try again later, or paste the stream link".
- **In your language.** The new words are in all 46 languages.
