# QiTV 2.0.7

Providers that turned QiTV away while other players signed in now work.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## Fixes

- **Xtream and M3U providers that refused QiTV now load.** Some providers block requests
  that don't look like they come from a player, so QiTV got "403 Forbidden" while other
  players signed in with the same account. QiTV now introduces itself the way its streams
  always did, for the channel list, series, playlists and the guide.
- **Verify in Settings really signs in to Xtream providers.** It used to check only that the
  server's address answered, which some servers never do, so a working account could fail
  and a wrong password could pass. Now it checks the username and password, and an expired
  or disabled account doesn't verify.

## Good to know

- See the [2.0.6 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.6.md)
  and the [2.0.0 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.0.md)
  for everything new in 2.0.
