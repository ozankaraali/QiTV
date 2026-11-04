# QiTV 2.9.6

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## AI captions

- **Only the speech models your languages need:** turning AI captions on now downloads one
  speech model, about 880 MB (2.4 GB for the more accurate one). The models that write
  particular languages best come only when a channel or film in one of their languages plays,
  after a question that names the language and the size:
  - ufak AI's model for Turkish (43 MB);
  - Cohere Transcribe for English and 13 more languages (1.7 GB);
  - NVIDIA's Parakeet for the other European languages (670 MB).
  Until now all of them came at once: 3.3 to 4.8 GB, whatever you watched. If you already
  have them, nothing changes.
- **English captions start sooner on a busy computer:** QiTV switched to its faster model while
  the English model was still loading, and the switch made the English model load again: the
  first caption could take over a minute and a half. QiTV now switches only when the faster
  model would actually help.
- **A caption that has gone no longer comes back:** when the next caption came late, the one
  before could show again for a moment.
- **No more re-wrapping while you read:** the last words of a sentence that arrive late no
  longer join a caption already on screen; when they come in time, they join their sentence
  even after a long caption shown in two parts.
- **Turkish: İstanbul, not "i stanbul":** words that begin with a capital İ came apart.
