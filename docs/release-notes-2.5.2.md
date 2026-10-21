# QiTV 2.5.2

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Security

- **An untrusted certificate authority is gone.** QiTV checks secure connections (your
  providers' sign-ins and lists, guides, logos, subtitles and the update check) against an
  updated certificate list (certifi 2026.10.19), which no longer trusts eMudhra's
  "emSign ECC Root CA - G3". Nothing else changed in this release.
