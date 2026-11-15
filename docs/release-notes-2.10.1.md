# QiTV 2.10.1

QiTV 2.10.1 no longer leaves a live channel far behind when you turn AI captions on.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## AI captions

- **Turning AI captions on no longer leaves the picture far behind live.** The picture waits
  only until it is the captions' distance behind live (about 8 seconds, or 13 while
  translating), and never for the speech model to load. "Starting captions…" shows until the
  first line is written, and the first seconds may go without captions. In 2.10.0 the picture
  also waited while the model loaded and until its first lines were written (up to half a
  minute), and then stayed that far behind until you pressed Go live.
- **A picture you paused or moved back further than that** no longer waits there too when you
  turn captions on: it stays where you put it, with the bar and Go live showing, and Go live
  takes it to the captions' distance.
