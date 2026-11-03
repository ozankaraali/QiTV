# QiTV 2.9.0

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## AI captions

- **Captions for live TV, made on your computer:** the Subtitles panel has a new row, AI
  captions. Turn it on and QiTV writes what is said in the channel's own language, for anyone
  who follows speech better by reading it. It understands 30 languages, among them English,
  Turkish, German, Spanish, French, Italian, Portuguese, Dutch, Polish, Russian and Arabic.
- **In step with the picture:** QiTV reads the channel a few seconds ahead of what it shows, so
  each caption appears when its words are said, not after them. While captions are on, live TV
  plays about 8 seconds behind the broadcast: QiTV slows the picture very slightly (3%) until it
  has those seconds, so nothing stops or jumps. Turn captions off and it catches up the same
  way, or press Go live.
- **One download, sized for your computer:** the first time, QiTV asks before downloading the
  speech model once: 0.9 GB on most computers, or a more accurate 2.4 GB one where the
  computer is fast enough (Settings → AI captions can choose either). Nothing of it ships with
  QiTV, nothing you watch leaves your computer, and Settings shows the space it takes and
  deletes it.
- **The language:** QiTV takes it from the channel's audio track when the channel names it;
  otherwise the model hears it. You can choose another in the panel, and QiTV remembers your
  choice for that channel.
- **Nothing made up:** what speech models write over music and silence (credits, repeated
  lines) is kept off the screen.

## Sound

- **The Sound panel names each audio track's language** ("Türkçe", "English"…), with its
  format when two would read alike, and says so when a channel doesn't name it.
