# QiTV 2.8.2

QiTV 2.8.2 removes a long pause in the channel list's All providers, keeps the player's buttons
still, and gives you a log you can turn up and find in one click.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **All providers without the wait.** With parental controls on, choosing All providers in the
  channel list could freeze QiTV for many seconds: every channel of every list was checked one
  by one against the settings. Each list is now checked in one go; the pause is gone.
- **Continue shows only what your server still has.** When Jellyfin or Emby added a film again
  (it then gets a new id), Continue kept the old one beside it, and that one failed with "The
  provider sent an unexpected answer". A film its server no longer has leaves Continue.
- **The player's buttons stay put.** While Stay live reconnected or adjusted its delay, the
  buttons at the bottom moved back and forth with its text. Their places now depend on the
  window's width and the language only; the text gives way instead.
- **TMDB's logo in About** lines up with the text above it.

## New

- **A detailed log.** Settings → **Detailed log (for troubleshooting)** writes every step
  QiTV takes to its log file, still without addresses or passwords, for a report when
  something goes wrong. **Help → Show the Log File…** opens the folder it is in.
- **The log says more by itself.** Every message QiTV shows is in it, with why "This channel
  can't open right now. Playing what was kept." appeared, and what a media server answered
  when it refused a title.

## Mac

- **The microphone question explains itself.** macOS may ask whether QiTV can use the
  microphone when QiTV's sound starts while a Bluetooth speaker or headphones are connected.
  QiTV never records sound, and the question now says so; QiTV works the same if you don't
  allow it.
