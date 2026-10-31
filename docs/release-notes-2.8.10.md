# QiTV 2.8.10

QiTV 2.8.10 keeps a channel watchable when its provider answers a reconnect with a recording
instead of the channel, and the phone remote's sheets close with a swipe.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **A provider's "connection limit" clip is no longer taken for the channel.** One provider
  closed a channel's stream, and when QiTV reconnected a second later it answered with a
  recording: ten minutes of black pictures, sent in two seconds. QiTV took that for the
  channel, showed it was "490 s behind" and played the black clip. Live TV can't arrive
  hundreds of times faster than it plays. So when a reconnect brings more than a minute and a
  half of video at once, QiTV now drops it. Meanwhile you keep watching what's already
  buffered, the player says "Reconnecting…", and QiTV asks for the channel again every
  15 seconds for up to 7 minutes.
- **The phone remote's sheets close with a swipe, and the remote stays still under them.**
  Swiping down on More (or any other sheet) moved the remote page underneath instead of closing
  the sheet. Now the sheet follows your finger and closes when you swipe it down, and the page
  under it holds still while it's open. A swipe that starts on a number key doesn't close it.
