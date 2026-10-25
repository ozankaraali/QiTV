# QiTV 2.7.2

QiTV 2.7.2 picks HTTPS by itself wherever your provider offers it, gives the phone remote its own
Record and LIVE keys, and keeps a log you can read when something goes wrong.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## New

- **Record and LIVE on the phone remote.** Record is a key of its own now, a red dot as on the
  computer, lit while the TV records; it is no longer under More. Go live, whose red dot looked
  like Record, is a LIVE key, lit while the channel plays behind its live point.
- **The phone remote on your Home Screen.** The remote's menu (⋯) offers Add to Home Screen: an
  Android phone installs it from there, and an iPhone gets the steps (in Safari, Share, Add to
  Home Screen, then scan the TV's code once more).
- **A log to read afterwards.** QiTV now keeps its log in a file as well, with addresses and
  passwords left out: `~/Library/Logs/QiTV` on a Mac, `%LOCALAPPDATA%\QiTV\Logs` on Windows,
  `~/.local/state/qitv` on Linux. It says how QiTV ended each time, and a crash leaves a
  `crash.log` beside it.

## Changed

- **HTTPS is chosen for you.** QiTV asks every provider over HTTPS first and uses HTTPS
  wherever the provider really answers there: its lists, links, guide and streams, through the
  time-shift buffer too. Where HTTPS doesn't answer, QiTV uses HTTP, at once for the rest of
  the session. The "Prefer HTTPS" settings are gone, the general one and each provider's.
- **Verify Provider says when there is no HTTPS.** Verifying a provider in Settings notes when
  it answers only over HTTP. Nothing warns about it while you watch.

## Fixed

- **Films, series and the phone with HTTPS.** With "Prefer HTTPS" on, an Xtream provider that
  advertises HTTPS without serving it lost its Films and Series, and its streams played
  nowhere, the phone included. QiTV now uses a provider's HTTPS only once the provider has
  answered there, and a stream that doesn't open over HTTPS plays over HTTP.
- **STB portals without HTTPS.** A portal whose sign-in fell back to HTTP had its lists, links
  and guide asked over HTTPS all the same. They now follow the sign-in.
- **The phone remote fits the screen.** On some phones, and inside the browser bars an iPhone
  shows at home, the volume and channel buttons ran into the tabs. The round pad now takes the
  room that is left.
- **Updates on a Mac without the Downloads folder.** An update now downloads to QiTV's own
  cache folder, so macOS no longer asks for access to Downloads, and the update no longer fails
  with "Operation not permitted" where that access was refused.

## Good to know

- After updating, each provider's lists load once more, so lists saved with HTTPS addresses
  that never answered are replaced.
- 2.7.1 was never published: everything it had is in 2.7.2.
