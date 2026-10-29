# QiTV 2.8.6

QiTV 2.8.6 shows the picture again on channels whose provider changes the video format
mid-stream but keeps announcing the old one.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **The picture doesn't freeze when a channel switches video format.** One provider's channel
  switched its video from HEVC to H.264 mid-stream and went on announcing HEVC. The player
  decodes what a stream announces, so the last picture stayed on screen while the sound went
  on, stuttering, until you tuned another channel; tuning back didn't help. QiTV now checks the
  pictures themselves and, once three in a row say otherwise, announces the format they really
  have, H.264 or HEVC, as it already did for sound in another format than the one announced.
  The picture comes back within a moment, and the time-shift buffer finds its keyframes again,
  so seeking back stays precise.
