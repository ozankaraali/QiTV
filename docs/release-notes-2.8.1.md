# QiTV 2.8.1

QiTV 2.8.1 plays Jellyfin and Emby titles again and makes the Library's provider ticks easier to
use.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **Jellyfin and Emby titles play.** In 2.8.0 a film or episode from a media server stopped at
  once with "'TVWindow' object has no attribute 'config'". It opens again, and the server hears
  where you are in it, as 2.8.0 meant to.
- **Continue shows only the providers ticked.** With some providers ticked in the Library,
  Continue listed titles from every provider. It now lists only those of the providers shown.
- **Another mix shows its own titles.** Ticking a different set of the same number of
  providers could keep showing the titles of the set before.
- **A set-top-box portal without HTTPS has its channels asked over HTTP.** When a portal
  answers only over HTTP, its channels are no longer tried over HTTPS first, also when their
  links name another server than the portal's. Xtream panels already worked this way.
- **The log says why a channel doesn't open.** When a channel fails, QiTV's log now says
  whether its server refused the connection, closed it without answering, or broke it off.
  For a set-top-box portal it also describes the link the portal gave: whether it is on the
  portal's own server and what it asks for, never its address or values. The time-shift
  buffer's own notes, which used to be lost in a downloaded QiTV, are in the log too.

## Changed

- **Picking one provider in the Library.** The provider menu stays open while you tick, and
  what you ticked shows when it closes. **All providers** ticks every provider; with every one
  already ticked it clears them all, so you can tick just the one you want. Closing the menu
  with nothing ticked keeps what was shown.
