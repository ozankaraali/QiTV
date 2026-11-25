# QiTV 2.15.0

QiTV 2.15.0 plays Kick and Twitch channels: paste a channel's page link and it plays live, at its
best quality, about a second behind the broadcaster, with Twitch's ad breaks skipped. Pause,
rewind, recordings and AI captions work on them as on any live channel, and rewind reaches back
to the start of the broadcast. Their videos and clips play as films. Ctrl/⌘+N opens Open
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
- **Twitch's ad breaks are skipped,** by the markers Twitch puts in the stream: QiTV leaves the
  marked pieces out and shows "Ad break skipped", then plays the channel from its newest moment.
- **Back at the newest moment.** When the stream comes back after a drop, a restart or an ad
  break, the channel plays from its newest moment, not from where the picture stopped.
- **A channel that isn't live waits for it.** Opened while the streamer is off, or when the
  stream ends while you watch, the channel waits on screen: "channel isn't live now · It plays
  as soon as it's live". QiTV asks the site again about every half a minute, and the channel
  plays as soon as it's live. **Try now** asks at once. A multiview tile waits the same way, and
  so does picture-in-picture (it says so once and opens when the channel is live).
- **Asked afresh each time.** A page's stream address changes, so QiTV asks the site again
  whenever you play the channel, and again each time the stream drops or its address runs out,
  without a message, waiting a little longer each time it keeps failing, in picture-in-picture
  and multiview too.
  When QiTV asks again after a drop, the pause and rewind buffer starts over. Nothing the site
  gives QiTV is kept or written to the log: only the site, the channel and what happened.
- **In Network streams** the page is kept, named "channel · Kick" or "channel · Twitch", and
  channel up and down step through it like any link. A channel saved more than once, written
  differently (with `www.`, other capitals, or a `?` part), is one row again.
- **When it doesn't play, it says why:** "No channel named channel on Kick", or "Kick refused
  QiTV's request: try again later, or paste the stream link".
- **In your language.** The new words are in all 46 languages.

## Videos, clips and rewind

- **A video or clip plays as a film.** Paste a video's link (`twitch.tv/videos/…`,
  `kick.com/<channel>/videos/…`, `kick.com/video/…`) or a clip's (`clips.twitch.tv/…`,
  `twitch.tv/<channel>/clip/…`, `kick.com/<channel>/clips/…`, or a channel page with
  `?clip=…`): QiTV says "A film" and plays it at its source quality. You can seek anywhere,
  and it resumes where you stopped. It's kept in Network streams under its title, as
  "title · Twitch". A video still being recorded plays as far as the site lists it when you open
  it. A video that's gone says "This video is gone from Twitch"; one that's private or for
  subscribers only says so too.
- **Rewind back to the start of the broadcast.** When a Kick or Twitch channel keeps its past
  broadcasts, the time-shift bar reaches back to the start of the one running now. Rewind past
  what QiTV buffered and you watch the site's own recording of it. Play forward (at 2× too) and
  it runs on into QiTV's buffer and up to live as one stream, with nothing reloading at the join.
  **Go live** (<kbd>L</kbd>) comes back to live as usual. A channel that keeps no past
  broadcasts rewinds as far as QiTV's buffer, as before.

## On the phone

- **Type into QiTV's text boxes from the phone.** When a text box on the TV has the keys (Open
  Link's field, a provider's address and login, a setting edited in place, the channel list's
  Find box once it shows a caret), the phone remote shows a field with the box's name and its
  text. What you type or paste goes into the box as you type, the keyboard's Go is Enter on
  the TV, and **Done** closes it. A password box stays masked on the phone, and its text never
  goes to the phone.
- **Open a link from the phone.** **More → Open a link** opens QiTV's Open Link card with the
  phone's keyboard up: paste a Kick or Twitch channel, a video, a clip or any stream's link,
  press Go, and it plays on the TV.
- **Watch on this phone waits for a channel that isn't live.** A Kick or Twitch channel that is
  off says "This channel isn't live now · It plays as soon as it's live". QiTV asks the site
  again about every half a minute (never while the phone is away) and hands the phone the
  stream once the channel is live; the phone holds no stream meanwhile. Choosing another
  channel, switching back to the TV or leaving ends the wait.

## Also in 2.15.0

- **Ctrl/⌘+N opens File → Open Link…**, the card that plays a stream's link or a channel's
  page.
- **The picture's right-click menu stays open on Windows.** It closed again as soon as it
  opened.
