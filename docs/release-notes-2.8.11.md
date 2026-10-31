# QiTV 2.8.11

QiTV 2.8.11 no longer plays a provider's "connection limit" recording when you switch
channels.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **Switching channels no longer lands on a black recording "573 s behind".** 2.8.10 dropped
  the recording a provider sent instead of the channel when QiTV reconnected. The same
  recording also answered channel switches: switching quickly on a provider that allows one
  stream, the provider still counted the previous channel and sent ten minutes of black
  pictures in two seconds. QiTV showed them as "573 s behind". QiTV now checks the first
  seconds of every channel you switch to as well. When they come far faster than live TV can,
  it drops them and says "The provider sent a recording, not the channel. Trying again…". It
  then asks for the channel again every 15 seconds, for up to 7 minutes, until the provider
  sends the channel itself. It doesn't play the provider's stream directly meanwhile, which
  would only bring the same recording. QiTV already waited for a channel's first seconds before
  playing it, and the check happens during that wait, so channels start about as quickly as
  before.
- **The check measures video correctly on more channels.** On channels whose pictures arrive
  out of order, it counted up to twice the video that actually came. A channel that sent a
  large buffer when QiTV connected could then have been taken for a recording.
