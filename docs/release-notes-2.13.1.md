# QiTV 2.13.1

QiTV 2.13.1 opens nothing in a system window any more. Every menu, question and card is drawn
in QiTV's own look, the same as the channel list and Settings, and the keys, the remote and the
phone reach all of them. The channel list also loads while QiTV starts, and picture in picture
shows no bars. 2.13.1 also brings 2.13.0, a pre-release, which plays free internet radio.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Everything in QiTV's own look

- **Free channels and Free radio: the countries are a list in Settings.** **All countries**
  comes first, then your region, then every country from A to Z in your language. Type to find
  one. The channel list, Settings, the welcome card and the Add provider card all open it.
- **Right-click menus are QiTV's own.** They look like the channel list, and ↑/↓, Enter and Esc
  work in them. The keyboard's menu key and <kbd>Shift+F10</kbd> open the highlighted item's
  menu, the same menu a right-click opens, in the channel list, the guide, the Library and
  Recordings. A group's menu (Hide group, Hide with a PIN) now opens on a right-click too.
- **Questions are cards.** These are cards over the picture or the Library now, never a separate
  window:
  - the parental PIN (a wrong PIN is said inside the card);
  - "an update is ready", with its download's progress;
  - "Quit while recording?";
  - the Library's questions.

  Esc or Back always gives the safe answer: "Not now" for an update, never "Skip this version".
- **Record at a time… is a card** with the channel, a name, how often, the day, the start and
  the end.
- **Keys and About QiTV are sections of Settings.** The Help menu opens them.
- Only the system's own windows for opening and saving files, and the menu bar, stay as they
  are.

## Starts with the channels there

- **The channel list loads while QiTV starts,** behind the opening mark, instead of after the
  window opens. On a normal start the channels are there when the window appears, and nothing
  says "loading…". The window still opens as soon as it's ready: it never waits for the list.
  A list that takes longer shows its banner as before.
- This covers the TV page set to one provider. All providers comes in a later version.

## Picture in picture

- **No more thin bars.** A picture in picture, the floating window on macOS or the small
  picture in the window, fills its box to the last pixel. Before, a box within 1% of the
  picture's shape kept its own shape, and VLC filled the difference with a few black rows. A
  stretch of up to about 2% doesn't show.
- **Your own Picture setting comes back** when the picture returns to the large view, where
  bars are right, for example a 16:9 picture on a 16:10 screen.
- **After a zap in picture in picture,** a thin bar may show for half a second until the new
  channel's shape is known, so a 4:3 channel is never stretched into a 16:9 box.

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
  **Kids**, and last **Other**, for stations whose tags say none of these (many have no tags).
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
  channel that plays directly (Record at a time… isn't offered for a station). AI captions
  don't read radio stations either: they listen through the time-shift buffer.
- **On the audio screen.** A station has no picture, so it plays on the audio screen with its
  logo, as a radio channel in your provider's list does: the station's name, and under it the
  song now playing when the station sends it. Live TV's controls stay: ↑/↓ go to the next
  station.
- **Nothing of yours is sent.** QiTV asks the directory only for its lists, naming itself as
  QiTV, as the directory asks apps to. It doesn't report which station you play.

## Older versions

- **2.12 plays a radio list saved by 2.13** from its saved copy; only its refresh fails, quietly,
  so the stations stay as they were until you go back to 2.13. **Versions before 2.12** can't
  read the directory's list and say its answer is no playlist. A list is saved as an ordinary
  playlist provider, so every 2.x reads your settings as before, and your other providers are
  not affected.
