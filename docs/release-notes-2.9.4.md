# QiTV 2.9.4

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## AI captions

- **No full stop in the middle of a sentence:** QiTV cuts long speech into pieces for the
  speech model, and the model ended every piece with a full stop, so a caption split
  mid-sentence ended with one too (news, where nobody pauses, the most). A piece cut in the
  middle of speech now ends without it; a question mark, an exclamation mark or "…" stays, and
  a caption that ended at a real pause keeps its full stop.
