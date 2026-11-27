# QiTV 2.15.1

QiTV 2.15.1 is a round of small things: <kbd>[</kbd> and <kbd>]</kbd> step the playback speed, a
speed you chose holds through a pause, AI captions work when you rewind past the buffer into a
Kick or Twitch recording or a channel's archive, Network streams' rows have a right-click menu,
and the small windows of picture in picture are easier to resize by their edges. 2.15.1 also
brings 2.15.0: Kick and Twitch channels, their videos, clips and rewind, typing and opening links
from the phone, and 1.75× and 3×.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## New in 2.15.1

### Playback speed
- **<kbd>[</kbd> slower, <kbd>]</kbd> faster.** The keys step the speed as the speed chip's
  **−** and **+** do, from 0.5× to 3×, for a film or a live channel you've rewound, even in a
  window too small to show **−** and **+**. At the live edge they do nothing, as the buttons.
  On a Mac's Turkish keyboard ⌥8 and ⌥9 type the brackets and work, and so do brackets typed
  with AltGr on Windows and Linux; ⌘[ and ⌘] stay free for what they mean. The buttons'
  tooltips name the keys.
- **A speed you chose holds through a pause.** Play after a pause came back at 1×. It now goes
  on at your speed, in the buffer and in a recording you rewound into, and reaching live still
  eases you into it and ends the speed, as before.
- **The log says what set the speed:** each speed you choose and the control you chose it with,
  and each pause and play with the speed kept.

### AI captions past the buffer
- **Captions on a Kick or Twitch recording, a channel's server window and a provider's
  archive.** Rewound past QiTV's own buffer, captions said "Not available for this stream".
  They now read what plays there, over the connection already open (never a second one to the
  site or the provider), and go on when the picture comes back into the buffer, after a short
  gap.
- **A Kick or Twitch recording is fetched further ahead:** up to three minutes instead of one,
  as far as the buffer's memory allows, so the captions have its sound in time, even at 3× with
  translation.

### Network streams
- **A right-click menu on Network streams' rows,** as on Recent's: a live link offers **Open
  in picture-in-picture**, **Hide** (once parental controls have a PIN), **Play in VLC** /
  **MPV** and **Copy stream link**; a film the same without picture in picture. The menu key and
  Shift+F10 open it too, and the find's rows have it.
- **A live link drags onto a multiview tile,** and Ctrl/⌘+P opens it in picture in picture. A
  film the find shows no longer opens small as a live stream.

### Picture in picture
- **Edges easier to catch.** With the controls showing, they took the clicks near the edges:
  the mini player's top and bottom edges and its corners never resized it, and neither small
  window showed the resize pointer over its controls. Near an edge a press now always resizes,
  whatever is under it, and the controls stay up while you resize. The band follows the
  window's width, up to 14 points, and a corner reaches 24 points along each of its edges, so
  the buttons stay clickable.

### In your language
- The new words are in all 46 languages.

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
  it runs on into QiTV's buffer and up to live as one stream, with nothing reloading at the join:
  QiTV joins the two by their own timestamps, so the next picture follows. A faster speed eases
  back to normal over the last few seconds, so the picture arrives at live without a stop. **Go live**
  (<kbd>L</kbd>) comes back to live as usual. A channel that keeps no past broadcasts rewinds as
  far as QiTV's buffer, as before.
- **Right after you tune in.** Kick's recording can trail live by up to a minute or so. QiTV
  fetches what the channel still lists from just before your buffer to bridge part of that gap.
  If playing forward reaches a stretch the recording doesn't hold yet, the picture waits there
  a moment, as when it buffers, and goes on as soon as the recording grows.

## On the phone

- **Type into QiTV's text boxes from the phone.** When a text box on the TV has the keys (Open
  Link's field, a provider's address and login, a setting edited in place, the Library's search
  and genre filter, the OpenSubtitles search), the phone remote shows a field with the box's
  name and its text. What you type or paste goes into the box as you type, the keyboard's Go is
  Enter on the TV, and **Done** closes it. A password box stays masked on the phone, and its
  text never goes to the phone. Where typing on the TV would start a find (the channel list,
  Settings, the Library), a keyboard button at the top of the phone opens the field, so it never
  covers the remote's buttons; it also opens a box again after **Done**.
- **Open a link from the phone.** **More → Open a link** opens QiTV's Open Link card with the
  phone's keyboard up: paste a Kick or Twitch channel, a video, a clip or any stream's link,
  press Go, and it plays on the TV.
- **Watch on this phone waits for a channel that isn't live.** A Kick or Twitch channel that is
  off says "This channel isn't live now · It plays as soon as it's live". QiTV asks the site
  again about every half a minute (never while the phone is away) and hands the phone the
  stream once the channel is live; the phone holds no stream meanwhile. Two phones waiting for
  the same channel share its asks, and it plays on the one that asked last. Choosing another
  channel, switching back to the TV, leaving, or the TV playing something else ends the wait.

## Also in 2.15.0

- **Ctrl/⌘+N opens File → Open Link…**, the card that plays a stream's link or a channel's
  page.
- **The picture's right-click menu stays open on Windows.** It closed again as soon as it
  opened.
- **1.75× and 3× playback speeds.** The speed's **−** and **+** now step 0.5×, 0.75×, 1×,
  1.25×, 1.5×, 1.75×, 2× and 3×, for live channels you've rewound and for films alike.
