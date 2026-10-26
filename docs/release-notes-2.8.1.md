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

## Changed

- **Picking one provider in the Library.** The provider menu stays open while you tick, and
  what you ticked shows when it closes. **All providers** ticks every provider; with every one
  already ticked it clears them all, so you can tick just the one you want. Closing the menu
  with nothing ticked keeps what was shown.
