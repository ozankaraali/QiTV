# QiTV 2.0.6

Software video decoding now really applies on Windows and Linux too.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## Fixes

- **Windows and Linux decode in software, as 2.0.5 intended.** 2.0.5 made software video
  decoding the default, so channels that stuttered or froze through the hardware decoder play
  at their full frame rate. On Windows and Linux every stream still asked VLC for hardware
  decoding, so the change only reached Macs. Now Settings → **Hardware video decoding
  (experimental)** decides on every platform, and it's off by default.

## Good to know

- If a channel's sound slowly drifts away from its picture over a long watch, check it in
  another player too: some providers' streams carry that drift themselves.
- See the [2.0.5 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.5.md)
  and the [2.0.0 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.0.md)
  for everything new in 2.0.
