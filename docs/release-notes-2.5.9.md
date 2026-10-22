# QiTV 2.5.9

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixed

- **Films play on the phone.** Most providers' films are MKV files, which a phone's browser
  can't play, so a film picked to watch on the phone didn't start. QiTV now hands the phone
  such a film in a form it plays (HLS), reading it from the provider as the phone plays it.
  Nothing is re-encoded, and seeking and going on where you stopped work as before. The film
  comes from QiTV itself, so the phone must be at home on QiTV's network: through the relay,
  the phone says the relay can't carry films. A film whose sound a phone can't play (DTS, for
  one) says so under the player: tap TV to watch it there.
