# QiTV 2.7.1

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

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
- **Updates on a Mac without the Downloads folder.** An update now downloads to QiTV's own
  cache folder, so macOS no longer asks for access to Downloads, and the update no longer fails
  with "Operation not permitted" where that access was refused.

## Good to know

- After updating, each provider's lists load once more, so lists saved with HTTPS addresses
  that never answered are replaced.
