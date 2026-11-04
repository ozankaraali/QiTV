# QiTV 2.9.3

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## AI captions

- **More words right, in every language:** QiTV cut speech into short scraps before the speech
  model heard it (a pause as short as a comma ended a piece, and a piece was at most 6 seconds),
  and the models got many more words wrong than they do on whole sentences. Pieces now end at
  real pauses and may be up to 10 seconds long. Measured on spoken sentences: Turkish went from
  15.5% of words wrong to 8.5%, Russian from 37% to 14.5%, French from 26.5% to 10%.
- **Better speech models for most languages, chosen on real broadcasts:** QiTV now picks the
  model that writes each language best, measured on spoken sentences and on news, talk shows and
  dramas from many countries:
  - English, French, German, Italian, Spanish, Portuguese, Greek, Dutch, Polish, Chinese,
    Japanese, Korean, Vietnamese and Arabic: Cohere Labs' Cohere Transcribe (French went from
    28% of words wrong to under 4%, Greek from 63% to 21%, Japanese from 12% to 5%, and names on
    the news come out right);
  - the other European languages (Danish, Swedish, Finnish, Czech, Hungarian, Romanian, Russian
    and more): NVIDIA's Parakeet, more accurate than before and five times faster; Ukrainian,
    Bulgarian, Croatian, Slovak, Slovenian, Estonian, Latvian, Lithuanian and Maltese can now be
    captioned too (choose one in the subtitles panel or in Settings, or play a sound track that
    names it);
  - Turkish: ufak AI's model, as in 2.9.2; every other language, and telling which one is
    spoken: Qwen3-ASR, as before.
  Only the model a language needs is loaded, once that language is known (Qwen writes the first
  half minute or so meanwhile). They come with the speech model (2.3 GB more to download); if
  you already have AI captions, the next time you turn them on QiTV asks once to download them.
- **A language with English words in it stays in its language:** Parakeet can't be told the
  language, and on a broadcast with some English it wrote whole lines in English; such a line is
  now written by Qwen, told the language.
- **Hindi, Arabic and Greek captions no longer cut short:** the model could only write so much
  for each piece, too little for scripts whose words take many letters' worth of its tokens.
- **No more flash of the controls or of old captions:** as a caption went at a silence, the
  player's controls, or older captions, showed for a moment.

## Find and search

- **Text fields work as text fields:** in the channel list's find, the Library's search and
  genre filter and the subtitles search, a click puts the caret where you click, a drag or a
  double click selects, and ⌘A (Ctrl+A), ⌘C, ⌘X and ⌘V work. The arrow keys still move between
  the lists until you click into the text; then they move the caret (with Shift they select,
  with ⌥ on a Mac or Ctrl elsewhere by words).
- **The Library's search is where you look first:** a wide field in the middle of the
  Library's top bar instead of a small "Search" button, so the genre filter no longer looks like
  the place to search. Typing in the top bar searches, and the results say what was found
  ("“salt” 3 results"). Letters typed anywhere in the Library go to the search, unless you
  chose the genre filter, and a ✕ clears it.

## Easier with the mouse

- **A film's picture:** a click plays or pauses it instead of opening the channel list, the
  controls have a TV button that brings back live TV, and a right-click offers what plays (its
  players, its link; a channel's own menu on live TV).
- **A wider window shows more, not the same bigger:** past a 1600-pixel-wide window, lists show
  more rows, the Library more posters and the guide more hours.
- **Series:** a click on a season shows it, a click on "Start over" starts the episode over, and
  a ‹ at the series' title goes back.
- **The guide:** a click on a channel's name tunes it, a right-click opens its menu, "Now" (or
  Home) comes back to now, and "Today" opens a menu of the days to go to.
- **Lists:** the row under the pointer is lit, a thin scrollbar shows while you scroll, a name
  cut short shows whole in its tooltip, and the Library's sort reads as buttons.
- **The channel list's names read in full** ("All providers › Live TV"), and its Library search
  button no longer looks like the Find box.
- **The volume panel stays while the pointer is on it,** and the wheel over it changes the
  volume.
- **With two or more providers, Movies and Series find the provider that has them** instead of
  saying that the one shown has none.
- **Settings:** every tab scrolls (it fits a small screen), the guide source "STB" is now called
  the provider's own guide (it is Xtream's too), Apply Change is gone (Save keeps everything), and
  Clear Image Cache asks first.
