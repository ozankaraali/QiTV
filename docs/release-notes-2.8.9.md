# QiTV 2.8.9

QiTV 2.8.9 plays straight on where a channel's stream skipped a few seconds, instead of
holding a still picture for as long as the gap.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **No still picture where a channel skipped a few seconds.** Some providers' streams go quiet
  for a few seconds and come back further on, with their clock having run on meanwhile. QiTV
  plays a few seconds behind live, so the player reached that spot with plenty buffered and
  still held the last picture, without sound, for as long as the gap: 6 seconds on one
  channel. QiTV already joined up a clock that leapt ahead on its own. It now does the same
  for these gaps, so the picture plays straight on. What the provider skipped is still
  missing.
