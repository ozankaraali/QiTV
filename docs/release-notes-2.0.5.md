# QiTV 2.0.5

Smoother picture on channels that stuttered or froze while their sound kept playing.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## Fixes

- **No more slideshow on some TV channels.** QiTV now decodes video in software, as mpv does.
  Some channels, often interlaced broadcast TV, showed only about half their pictures, or
  froze for seconds, through the computer's hardware video decoder (on Macs, VideoToolbox).
  They now play at their full frame rate.
- **Hardware decoding is still there.** Settings → **Hardware video decoding (experimental)**
  turns it back on. It uses less CPU, but those channels may stutter again. It applies after
  QiTV restarts.

## Good to know

- See the [2.0.4 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.4.md)
  and the [2.0.0 notes](https://github.com/ozankaraali/QiTV/blob/main/docs/release-notes-2.0.0.md)
  for everything new in 2.0.
