# QiTV 2.10.0

QiTV 2.10 translates AI captions into the language you choose, on your computer, and writes
Turkish captions with QiTV's own Turkish speech model. A live channel whose stream drops now
comes back by itself. It also brings the fixes of 2.9.9 and 2.9.10, which were never released:
the phone remote and channel logos on Macs without Homebrew, recording, and 4K channels with
deinterlacing on.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## AI captions in your language

- **Translate to:** a new row under AI captions in the Subtitles panel shows the captions in
  another language. Its list has Off first, then your QiTV language when the model covers it,
  then 46 languages, each by its own name. Like the Language row above it, it shows while AI
  captions are on.
- **Translated on your computer:** each caption line is translated by Xiaomi's MiLMMT-46 1B
  model, on your computer. No text or sound leaves it, and it costs nothing. The first time you
  pick a language, QiTV asks before a one-time download of about 730 MB.
- **On time, translated or as said:** while translating, the picture waits about 3 seconds more,
  so each line has time to be translated before it shows. A line that isn't translated in time
  shows as it was said, never late. A line already in the language you chose shows as it is.
- **On a slower computer,** Automatic uses the faster speech model while translating, so the
  captions and their translation both keep up.
- **On the phone remote,** the Subtitles sheet has the same Translate to row and list.

## Turkish captions by QiTV's own model

- **Turkish AI captions are now written by QiTV's own Turkish speech model,**
  [qitv-asr-tr-moonshine-mid](https://huggingface.co/ozankaraali/qitv-asr-tr-moonshine-mid),
  released openly on Hugging Face (CC BY 4.0). QiTV reads it with a beam search, which weighs
  several readings of what was said before writing one. It gets fewer words wrong than the model
  2.9.1 to 2.9.10 used, especially in TV series and talk. It downloads once (278 MB), the first
  time Turkish captions are wanted, as before.
- **Numbers as subtitles write them:** "86,5 milyon", and a time after a day as a clock time
  ("pazartesi 21.30'da"), not as a range.

## AI captions

- **A channel whose title starts with its language** ("TR: …", "DE - …") starts its captions in
  that language at once, without first listening for it. What is said can still correct it.
- **At 1.5× and 2×,** each caption stays on screen about as long for you as at normal speed, and
  Turkish captions keep up with the picture.
- **When a channel's clock jumps,** captions no longer start over and over, the next channel's
  captions no longer stay silent, and "Can't keep up" no longer shows by mistake.
- **Your answer to the first download question is kept:** the question about the speech models
  for the languages you watch came back after every restart.

## Live channels

- **A playing channel whose stream drops comes back by itself,** as a TV's does. Until now QiTV
  tried twice and then said "The stream ended" until you changed channel. While the channel stays
  chosen, QiTV now tries again after a second, then after longer and longer waits, up to every
  5 minutes. The picture says "The stream ended · Trying again in 0:25", counting down, and
  **Try now** (a click, <kbd>Enter</kbd> or ▶) tries at once. The small picture and multiview
  tiles do the same.

## Picture in picture on a Mac

- **The picture can float when another window covers QiTV:** turn on **Settings → Float the
  picture when another window covers QiTV** (off at first). When QiTV isn't fullscreen and
  another app's window covers it whole, or you minimize it, the picture goes on in a small
  window above the others; it comes back as soon as QiTV's window shows again, or when you
  double-click the small window. Not with Keep on Top.

## Multiview

- **Picking a tile's channel no longer clicks the grid:** the second click of the double-click
  that picks it could leave multiview, move the focus or go fullscreen.
- **A "+" tile starts empty:** leaving the grid, or going to a smaller layout, forgets each
  stopped tile's channel, as its close button does. A press on a tile's picture no longer keeps
  the channel list from opening.

## Player controls

- **The speaker's mixer keeps the player's controls up until it closes.** Before, they hid while
  the pointer was over the mixer, and the mixer went down with them.

## Phone remote and logos on every Mac

- **On a Mac without Homebrew, the phone remote never reached remote.qitv.app and no channel
  logo loaded.** Part of QiTV looked for the computer's trusted certificates only where Homebrew
  keeps them. QiTV now uses the list it ships when the computer has none there.
- **The pairing screen says when remote.qitv.app can't be reached** (then only a phone on the
  same network can pair), and **Settings → Phone remote** says "Can't connect; trying again"
  instead of "Connecting…" for good.

## Recording

- **A multiview tile onto a channel recording on schedule** asks again whether to use the
  recording's stream, so a provider that allows one stream no longer says there is none to spare.
- **▶ Watch on a recording** that records on the small picture brings it large; on a multiview
  tile, that tile takes the focus. Both did nothing.
- **A small picture that fails to open** on a recording's stream gives the stream back, as the
  large picture already did.
- **A player kept in the background for the next programme** lets go of its stream once the
  schedule changes and it no longer records.
- **A deleted recording's picture is deleted with it**, and quitting while a picture is being
  taken waits at most 2 seconds.

## 4K channels with deinterlacing on

- **With Picture → Deinterlacing set to On, a 4K channel showed about 5 of its 50 pictures a
  second, covered in coloured lines.** No broadcast taller than 1080i is interlaced, so On now
  leaves those pictures alone: the channel shows all 50 pictures a second, even on a 2017
  laptop. Every other channel is deinterlaced as before.

## Channel list

- **A channel's banner no longer covers the channel list** when you open the list again while
  the channel is still opening.
