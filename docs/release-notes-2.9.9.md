# QiTV 2.9.9

QiTV 2.9.9 fixes the phone remote and channel logos on Macs without Homebrew, and five things
in recording.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Phone remote and logos on every Mac

- **On a Mac without Homebrew, the phone remote never reached remote.qitv.app and no channel
  logo loaded.** Part of QiTV looked for the list of trusted certificates only where Homebrew
  keeps it, found none, and so trusted no website. The pairing screen said "Starting the phone
  remote…" and then showed a code only a phone on the same network could use. QiTV now uses the
  list it ships when the computer has none there. A computer with its own list keeps it.
- **The pairing screen says when remote.qitv.app can't be reached:** "Not connected to
  remote.qitv.app: only a phone on this network can pair with this code". **Settings → Phone
  remote** says "Can't connect; trying again" instead of "Connecting…" for good.

## Recording

- **A multiview tile onto a channel recording on schedule** asks again whether to use the
  recording's stream, so a provider that allows one stream no longer says there is none to spare.
- **▶ Watch on a recording** that records on the small picture brings it large; on a multiview
  tile, that tile takes the focus. Both did nothing.
- **A small picture that fails to open** on a recording's stream gives the stream back, as the
  large picture already did.
- **A player kept in the background for the next programme** that then didn't record on it
  lets go of its stream once the schedule changes, instead of holding it for nothing.
- **A deleted recording's picture is deleted with it**, and quitting while a picture is being
  taken stops it, waiting at most 2 seconds.
