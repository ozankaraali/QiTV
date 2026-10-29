# QiTV 2.8.7

QiTV 2.8.7 keeps the sound smooth on channels whose provider changes the sound's sample rate
mid-stream.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **The sound doesn't stutter when a channel changes its sample rate.** The channel that
  switched its video format (fixed in 2.8.6) switched its sound from 44.1 to 48 kHz at the
  same moment. The player went on at the old rate and dropped a little of the sound twice a
  second, a stutter that lasted until you tuned another channel. QiTV now notices when the
  sound's sample rate or number of channels changes, and the player starts afresh with the new
  one: a few hundredths of a second of sound go missing once, at the switch, and it plays
  smoothly after.
