# QiTV 2.8.4

QiTV 2.8.4 plays the channels of portals that list each channel with its own ready-made link.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **Portals that list ready-made links play.** Some portals give each channel in their list a
  finished link to play. QiTV asked such a portal to make that link again, and got one that
  named no channel: every channel stopped with "The portal returned an empty link" (before
  2.8.3, it reconnected again and again). QiTV now plays the link the portal listed. If
  channels stop opening after a long while, View → Refresh Channels and Guide (Ctrl+R) gets
  fresh links.
- **The log counts a portal's channel numbers right.** 2.8.3's log said that such a portal's
  channels had no number; it now finds the number in their links too.
