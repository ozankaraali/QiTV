# QiTV 2.9.6

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## AI captions

- **Only the speech models your languages need:** the first time you turn AI captions on, one
  question says what it comes to: about 880 MB to start (2.4 GB with the more accurate model),
  up to 3.3 GB (4.8 GB) with every language's own model. The models that write particular
  languages best then download by themselves, once, when a channel or film in one of their
  languages first plays; the captions row shows how far:
  - ufak AI's model for Turkish (43 MB);
  - Cohere Transcribe for English and 13 more languages (1.7 GB);
  - NVIDIA's Parakeet for the other European languages (670 MB).
  Until now all of them came at once, whatever you watched. If you already have them, nothing
  changes; if you have some, QiTV asks before fetching the rest.
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
