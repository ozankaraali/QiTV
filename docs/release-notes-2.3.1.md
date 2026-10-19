# QiTV 2.3.1

QiTV 2.3.1 keeps the sound on channels that change their sound format, keeps very long channel
lists on the channel you watch, and gives the macOS download a window that shows where to drag
QiTV.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## Fixed

- **Sound that went silent and came back.** Some channels switch their sound format whenever
  their encoder restarts and announce it, but without marking the announcement as new; the
  player kept decoding the old format and stayed silent until the format switched back. QiTV
  now marks each change, so the sound plays through.
- **Long channel lists stay on your channel.** In a list of more than 5,000 channels the
  channel list and the quick list jumped back to the channels around 1,000, again and again,
  as logos and the guide loaded. They now stay on the channel you watch, however far down,
  and on where you scrolled.
- **Five-digit channel numbers** can be typed, and the guide shows them whole.
- **A multiview tile** the focus leaves while it waits to reconnect says its stream ended
  instead of showing LIVE over a still picture.
- **The live lag** no longer reads short after a channel's pictures stop for a few seconds
  while its sound goes on, and bursty HLS channels with a few stray scrambled packets no longer
  give up rewind.

## Changed

- **The macOS download opens as a window** with QiTV on the left, Applications on the right
  and an arrow between them, and it is about a tenth smaller.
- **Smaller apps:** Qt's PDF library, which QiTV never uses, is left out.
