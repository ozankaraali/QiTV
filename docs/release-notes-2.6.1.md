# QiTV 2.6.1

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Security

- **A flaw in the Brotli decoder is fixed.** Servers can send their answers Brotli-compressed:
  your providers' sign-ins and lists, guides, logos, subtitles and the update check. A
  malformed answer could make the decoder write past its memory (GHSA-9v8h-qqgm-4m35). QiTV now
  ships Brotli 1.2.1, which fixes it. Nothing else changed in this release.

What 2.6 brings (kept time-shift buffers, Record as a recorder, Stop) is in
[2.6.0's notes](https://github.com/ozankaraali/QiTV/releases/tag/v2.6.0).
