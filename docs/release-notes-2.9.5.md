# QiTV 2.9.5

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Sound

- **No more silence after a channel stalls:** after a pause, some channels send ten seconds of
  sound in one go, ahead of the picture it belongs to, and the player threw most of it away:
  seconds of silence every ten, until QiTV opened the stream again. Sound that comes early now
  waits for its picture and plays with it.
- **No more drop when a channel switches its sound format:** a channel that alternates between
  two sound formats, both timed minutes off its picture, lost its sound for a few seconds at
  each switch while QiTV measured the new format's timing again. The new format is now timed
  to the picture from its first moment.

## AI captions

- **Time to read a long line:** a caption stayed only until the next one began, so a long
  sentence was gone before it could be read. The next caption now waits until the one before
  has been read, by at most a second, and the captions catch up at the next pause.
- **A sentence's last words no longer come alone:** when long speech had to be cut, its last
  words often became a caption of their own. Such an ending now joins its sentence when the two
  still fit on two lines.
- **Cuts between words, not inside them:** QiTV's voice detector hid the short pauses between
  words, so long speech was cut at its quietest moment, often in the middle of a phrase. QiTV
  now finds those pauses, cuts at the longest one, and ends a long stretch at a phrase's pause.
  Measured on continuous speech, fewer words came out wrong in Turkish (13.9% → 12.4%), English
  (9.0% → 7.1%) and Russian (17.2% → 14.5%); French was the same in one test and a little worse
  in the other.
