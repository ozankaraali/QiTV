# QiTV 2.9.0

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## AI captions

- **Captions for live TV and films, made on your computer:** the Subtitles panel has a new row,
  AI captions. Turn it on and QiTV writes what is said in its own language, for anyone who
  follows speech better by reading it: on live channels, and on films and episodes from your
  provider, your Jellyfin or Emby server or your disk (recordings too). A provider's film still
  takes one stream from the provider: the picture and the captions share it. Turned on during
  one, the film opens again where it is (a moment's pause). It understands 30 languages, among
  them English, Turkish, German, Spanish, French, Italian, Portuguese, Dutch, Polish, Russian
  and Arabic.
- **In step with the picture:** QiTV reads a few seconds ahead of what it shows, so each
  caption appears when its words are said, not after them. While captions are on, live TV plays
  8 to 10 seconds behind the broadcast: when you turn them on, or change channel, the picture
  waits a few seconds once ("Starting captions…") and then plays on normally. Go live keeps
  that distance while captions are on. Turn them off and the picture stays where it is, as
  after a pause: the bar and Go live take you back to live. A film needs no delay: after a
  seek, captions start a few seconds later.
- **One download, sized for your computer:** the first time, QiTV asks before downloading the
  speech model once: the more accurate one (2.4 GB) where the computer is fast enough and
  has 12 GB of memory, as most from the last few years are, else a faster one (0.9 GB).
  You can choose yourself, before the download too, as a game's quality setting: the AI
  captions row's ‹ › step through Automatic, Faster (0.6B) and More accurate (1.7B) (Settings →
  AI captions too). While it downloads, the picture says how far it is. Nothing of it ships
  with QiTV, nothing you watch leaves your computer, and Settings shows the space it takes and
  deletes it.
- **The language:** QiTV takes it from the channel's audio track when the channel names it;
  otherwise the model hears it. You can choose another in the panel, and QiTV remembers your
  choice for that channel.
- **Nothing made up:** what speech models write over music and silence (credits, repeated
  lines) is kept off the screen.

## Sound

- **A channel whose sound is stamped with a clock that runs slow plays its sound again:** one
  provider's channel stamped its sound 2% slower than it plays, and after a few hours the
  stamps sat minutes behind the picture. VLC then lost the sound in a long play, and after a
  pause or Go live it played either the sound with a frozen picture or the picture without
  sound. QiTV now notices such a sound (its stamps against its own length, and against the
  picture's) and stamps it again from its length, in step with the picture; any other channel
  passes as it comes.

- **The Sound panel names each audio track's language** ("Türkçe", "English"…), with its
  format when two would read alike, and says so when a channel doesn't name it.

## Phone remote

- **Subtitles and Sound from the phone:** the remote's **More** has **Subtitles** and
  **Sound**, the TV's two panels: Off, the stream's subtitles and **AI captions** (on or off,
  what they are waiting for, such as "Downloading 40%", their speech model's ‹ › and the
  language they hear, which you can change), and the sound tracks by their language, named as
  on the TV. They follow what
  the TV does while they are open. The first time AI captions are turned on from the phone, the
  download question shows on the phone and on the TV: answer either one.
