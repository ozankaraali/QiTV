# QiTV 2.6.1

QiTV 2.6 keeps a channel's time-shift buffer when you leave the channel, and turns Record into a
recorder that goes on recording whatever you watch meanwhile.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## New

- **Leaving a channel keeps its buffer.** Paused a film on a live channel and a scroll of the
  wheel zapped away? What the buffer held is still there. A zap, another channel, a film or
  quitting QiTV keeps it; come back to the channel by any route and it plays live, with
  **Left off at 20:10 · Resume** at the top for a minute if you had paused or rewound (on the
  phone too). Resume goes on from there. The time away shows on the bar as a gap that playback
  and seeks skip, and the bar's times and the guide's marks stay right on both sides of it.
- **For a day, and after a restart.** With the disk buffer on, kept buffers last 24 hours and
  are still there after quitting QiTV. The size in Settings → Time-shift is now the total for
  every channel's buffer: when it's used up, the oldest parts of the channels you left go
  first. With the memory buffer, the channel you left last keeps its buffer until QiTV quits.
  A channel that can't open when you come back plays what was kept all the same, and Go live
  tries the channel again.
- **Record keeps recording.** A recording now keeps the channel's stream for itself and records
  on through pauses, rewinds, other channels, films and the Library, for up to 24 hours (it was
  4), and several channels can record at once as far as your provider's streams go. Back on the
  channel, you can rewind to the start of the recording while it still records, and
  picture-in-picture of that channel shows the recording without a new stream. **● REC** at
  the top left of the picture (bottom left in the Library) names what records: click it to go
  back to the channel or stop the recording. The phone shows the same REC line.
- **The recording comes first.** Watching something else from the same provider needs another
  stream. When the provider has none left, QiTV asks whether to stop the recording, with **Keep
  recording** chosen first; a question that comes from something started on the phone shows on
  the phone. Quitting QiTV while it records asks too. When a provider that doesn't say its
  limit drops the older stream as another opens, QiTV stops the newer one and says so, and the
  recording goes on.
- **Stop.** **■** beside play, <kbd>X</kbd>, the keyboard's media Stop key, Playback → Stop and
  the phone's More → Stop stop every picture: nothing plays and nothing streams until you play
  again (<kbd>Space</kbd>, ▶ or the phone's ⏯). The channel's buffer is kept, and recordings
  go on.

## Security

- **A flaw in the Brotli decoder is fixed.** Servers can send their answers Brotli-compressed:
  your providers' sign-ins and lists, guides, logos, subtitles and the update check. A
  malformed answer could make the decoder write past its memory (GHSA-9v8h-qqgm-4m35). QiTV now
  ships Brotli 1.2.1, which fixes it.
