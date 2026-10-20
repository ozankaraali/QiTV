# QiTV 2.4.3

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## Fixed

- **Live TV no longer drifts hours behind.** When a channel's connection dropped and came back
  with its clock started over, the picture played on but QiTV lost track of where it was: the
  Stay live indicator counted further behind every second (thousands of seconds, its dot
  still green) and never caught up. QiTV now keeps the clock running across such a
  reconnect, and if it ever loses track of the player's clock again, it opens the stream
  again where you watch within seconds.
- **A long pause resumes with room to spare.** After a pause longer than the time-shift buffer
  holds, playback now resumes 30 seconds after the oldest moment still kept (half way in on a
  short buffer), instead of 5 seconds after it, where it could fall off the end again and
  rewind once more.
