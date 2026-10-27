# QiTV 2.8.3

QiTV 2.8.3 makes the channel list's search work with parental controls on, says when an Xtream
subscription has expired, and stops a portal's channel from reconnecting when its link can't
play.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **Search finds channels with parental controls on.** Since 2.8.0, with a PIN set and
  something hidden, the channel list's search found no channels at all. It finds them again,
  still without what you hid.
- **An expired subscription says so.** An Xtream provider whose subscription had expired, or
  whose account the provider disabled, loaded as an empty list without a word. It now says
  "Your subscription has expired", or that the provider has disabled the account.
- **A portal's link that names no channel stops at once.** Some portals answer with a link
  that has no channel in it, which their server closes straight away; QiTV kept reconnecting
  for about 25 seconds. It now says "The portal returned an empty link" at once.
- **The detailed log takes effect at once.** Turning it on in Settings now also reaches the
  time-shift buffer that is running, without restarting QiTV.

## The log

- **What a portal was asked.** When a portal's channels don't open, the log now says how many
  of its channels come with a channel number, and what QiTV asked for a link that named none,
  still without addresses, MACs or numbers.

## For developers

- **A control port when QiTV runs from its source code.** `scripts/qitv_control.py` shows what
  plays, reads the log, tunes channels, presses keys and restarts QiTV, through a port on this
  computer only that answers only with a token only your user can read. Downloaded builds open
  it only when started with `QITV_CONTROL=1`, and never run code sent to it.
