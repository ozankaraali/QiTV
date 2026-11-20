# QiTV 2.13.0

QiTV 2.13 plays free internet radio. **＋ Free radio…**, beside **＋ Free channels…**, adds the
stations of [radio-browser.info](https://www.radio-browser.info), a free directory run by its
community: the most played stations of the whole world, or of one country, with their logos,
all of them in All stations and each in its genre. A station plays live on the audio screen,
with the song now playing when the station sends it.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Free radio

- **Where it is.** **＋ Free radio…** sits right under **＋ Free channels…** wherever that is: the
  channel list's sources column, the **Providers** menu, Settings → Providers, the Add provider
  card's other ways, and the welcome card (a third button). The keys reach it as any row:
  ↑/↓ and OK on the keyboard, the remote or the phone.
- **The same menu as Free channels.** **All countries**, your region (from your computer's
  settings), or **Another country…**, a list of every country in your language. The menu's
  tooltips say where the stations come from.
- **All stations first.** A list's first group holds every station, the most played first.
- **One country:** then its stations by genre: **News & talk**, **Classical**, **Jazz &
  blues**, **Electronic & dance**, **Rock**, **Pop**, **Folk & traditional**, **Religious**,
  **Kids**, and **Other** for stations whose tags say none of these (many have no tags at all).
  The genre comes from the tags the directory gives each station; a station tagged with two
  genres goes to the one first in that list ("pop rock" is Rock). Turkish tags are known too:
  türkü, halk, arabesk and fantezi are Folk & traditional, Türk sanat müziği is Classical.
- **All countries:** the 2,000 most played stations in the world, then by country, the
  countries named in your language.
- **Up to 2,000 stations a country,** the most played, so a large country's list stays quick
  to load. The directory's own check of each station is used: stations it found broken are left
  out.
- **A provider like any other.** Each list is added as Free channels adds one: it is selected
  and loads at once, and adding the same list again only switches to it. It counts as a
  provider (two of them, or a radio list beside your provider, give you All providers), it is
  refreshed like the others, and you remove it in Settings → Providers. Its **Check the
  connection** works too.
- **Live, straight from the station.** VLC plays a station directly, without QiTV's
  time-shift buffer: it opens one connection, and there is no rewind or recording, as for any
  channel that plays directly.
- **On the audio screen.** A station has no picture, so it plays on the audio screen with its
  logo, as a radio channel in your provider's list does: the station's name, and under it the
  song now playing when the station sends it. Live TV's controls stay: ↑/↓ go to the next
  station.
- **Nothing of yours is sent.** QiTV asks the directory only for its lists, naming itself as
  QiTV, as the directory asks apps to. It doesn't report which station you play.

## Older versions

- **A list added in 2.13 shows an error in an older 2.x.** It is saved as an ordinary playlist
  provider, so an older QiTV reads your settings as before, but it can't read the directory's
  list and says its answer is no playlist. Your other providers are not affected.
