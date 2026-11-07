# QiTV 2.9.10

QiTV 2.9.10 plays 4K channels smoothly with deinterlacing on, and keeps a channel's banner under
the channel list.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## 4K channels with deinterlacing on

- **With Picture → Deinterlacing set to On, a 4K channel showed about 5 of its 50 pictures a
  second, covered in coloured lines.** VLC deinterlaced each 4K picture on the processor, which
  could not keep up, and garbled them on the way. No broadcast taller than 1080i is interlaced,
  so On now leaves those pictures alone: the same channel shows all 50 pictures a second, none
  dropped, on a 2017 MacBook Pro. Every other channel is deinterlaced as before.

## Channel list

- **A channel's banner no longer covers the channel list.** Choose a channel, then press Enter to
  open the list again: while the channel was still opening, its banner came back over the list
  every few seconds until the list closed. It now stays under the list.
