# QiTV 2.8.8

QiTV 2.8.8 keeps playing more smoothly on channels whose provider keeps restarting the stream,
and brings the sound back when the player loses it.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **Fewer, cleaner pauses on channels that keep restarting.** One provider's channel restarted
  its stream every minute or two and sent the last few seconds again each time. QiTV skips
  those repeats so nothing plays twice, which left the player with nothing new for 4 to 11
  seconds. It ran out each time, froze, and dropped a little picture and sound when the stream
  came back. Now, once the player has run out on a channel, QiTV pauses it just before it
  would run out again and plays on once a few seconds have built up. On a recording of that
  channel this halved the number of interruptions, and none of them drops picture or sound. A
  one-off hiccup on a healthy channel plays as before.
- **The sound comes back if the player loses it.** On that same channel the sound went silent
  for good while the picture played on, though the stream still carried it. When a
  time-shifted channel's picture plays without sound for 3 seconds, QiTV now restarts the
  player's sound, and if that doesn't help, opens the stream again where you are.
- **Stay live keeps adjusting the speed after a jump in the stream's clock.** When a
  channel's timestamps jumped ahead, the player waited the jump out, and QiTV took that for a
  player that can't change speed on that channel. It then kept the channel at normal speed
  for a day, so it could no longer build up a buffer and froze more often. A jump in the
  stream's clock no longer counts.
