# QiTV 2.13.4

QiTV 2.13.4 is a round of fixes: a radio station's ‹ opens the channel list, and about fifteen
smaller things found by a careful read of QiTV's code are put right, from the guide and the
captions to time-shift and the phone remote. 2.13.4 also brings 2.13.3, a pre-release, and
2.13.2, 2.13.1 and 2.13.0: a station's view to choose, translated captions sooner, nothing in a
system window any more, the phone's pictures at home, and free internet radio.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Fixes in 2.13.4

### Radio
- **A station's ‹ opens the channel list,** at the station playing, as Back does on live TV.
  It went to the Library. A song from your Library still goes back to the Library.

### Live TV and time-shift
- **One connection when a channel starts afresh.** If the time-shift helper stopped while a
  channel was picking up where it left, QiTV could open the channel anew while the old
  connection was still open, two at once. It now closes the old one first.
- **No new start on a provider that just said no.** After the helper came back, QiTV could
  take the provider's refusal for the channel picking up again, and start the channel afresh
  on that provider at once. It now treats the refusal as one.
- **Playing directly holds its connection at once.** On a provider QiTV found keeps fewer
  streams than it says, a channel, film or episode that plays without time-shift now counts its
  connection the moment it starts, so another stream of QiTV's can't take it while it starts.
- **No sound downloads for nothing.** A channel whose sound comes as a separate track that its
  picture can't carry no longer downloads that sound for every few seconds of picture. Such a
  channel still plays without sound in time-shift, as before.

### The guide
- **Changing the guide's mapping in Settings keeps every other channel's guide.** Before, the
  channels you didn't map lost their guide until the next guide refresh, and the mapped ones
  lost the other names the guide knows them by.

### AI captions
- **After a rewind, the captions come back.** Going back more than 2 seconds could leave that
  stretch without captions.
- **Choosing a subtitle track takes down the question about downloading the captions model,**
  on the TV and on the phone.
- **Another language mid-film** stops the Turkish model at once. It could go on reading the new
  language as Turkish for the rest of the film, with wrong captions or none.

### Watching while recording
- **A channel waiting for a recording's connection never jumps in late.** When you answer that
  the recording may stop, the channel waits a few seconds for its connection. If you play
  something else meanwhile, in either picture (another channel, a film, a song, a file, a cast),
  or press Stop, the waiting channel is dropped instead of opening over what you chose.

### Series
- **The next season from another provider** no longer stays at "Loading next season…". If that
  provider is no longer in your list, QiTV says it couldn't load the next season at once.

### Settings
- **A settings file QiTV can't open** (another program holds it, or its permissions) is never
  written over. QiTV tries again for a moment; if it still can't, it starts with its defaults,
  says it can't save, and leaves your file as it was for the next start. A damaged file is
  still set aside beside the new one, as before.

### The phone remote
- **A phone remote that couldn't start** (its port taken, say) now tries again when you change
  its settings or open **Phone Remote…**, and a phone can be removed meanwhile.
- **With several films open on phones,** the one read least recently closes first, not the
  first one opened.

### Recordings
- **The keys on the Recordings shelf no longer crash it** when the corner player, or a smaller
  window, leaves room for fewer tiles.

### Windows and Linux
- **Windows: clearing the image cache with a picture in use** skips that one file instead of
  stopping, and the cache size it shows stays right.
- **Linux: the installer writes its menu entry right** for an install folder whose path has
  spaces, quotes or symbols such as `&`.

## Radio and the audio screen

- **A station's view can be chosen again.** On a radio station, live TV's controls lay over the
  audio screen and took its clicks, so the view at the top right (the cover, the list, the bars)
  and Words couldn't be clicked. They can now, and the screen's top row no longer jumps when the
  controls come and go.
- **The keys and the phone choose the view too.** While the audio screen shows, the Picture
  panel (<kbd>A</kbd>, or the phone's Picture button) has a **View** row: ←/→ steps through the
  views, for a station and for a song from your Library.

## Left off at

- **The pill goes after 15 seconds,** not a minute. It says where you left a channel you come
  back to, with **Resume**.
- **Close it any time** with its **✕**, <kbd>Esc</kbd>, or the remote's or phone's Back. Enter or
  a click on Resume still plays on from where you left.

## A faster translation model

- **The same translations, sooner.** Before it writes a line's translation, the model reads
  the prompt: the line and the words around it, in small pieces. Its last step, which picks the
  next word, ran for every piece of that prompt, and only the last one was used. Now it runs
  once when the prompt is read, and once for each word it writes, as before.
- **Word for word the same.** QiTV compared 120 lines in six language pairs, Chinese among
  them, on its own and with the line before, and every translation matched.
- **How much sooner.** On a fast desktop processor, a medium line (7 to 9 words, Turkish into
  English) took 0.36 s on 2 cores and now takes 0.33 s. Translated with the line before (for
  "he" and "she"), it took 0.54 s and now takes 0.46 s. On 4 cores: 0.28 s to 0.27 s, and
  0.37 s to 0.33 s.
- **A 0.3 MB download, if you have the translation model.** QiTV fetches the model's new part
  by itself the next time it translates, without asking, and translates with the old one until
  the new one is in place. The model's 693 MB of weights stay as they are. A first download of
  the translation model is the same size as before.
- **Going back to 2.13.1 or earlier** after that, the older version asks once to download its
  own 0.3 MB part of the translation model, and translates as before.

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

## Pictures on your phone

- **At home, the phone shows pictures.** When the phone is on the same network as QiTV, its
  Library shows the films' and series' posters, its channel lists show the channels' logos
  (while **Enable Channel Logos** is on in Settings), and what plays shows its artwork.
- **They come from QiTV.** QiTV takes each picture from its own cache and sends it only to
  your paired phone. A picture that doesn't load tries again a little later.
- **Away from home, nothing changes.** Through the relay the phone shows its coloured cards as
  before, and the relay never sees what you watch.

## Translated captions sooner

- **The picture waits only as long as your computer needs.** With a translation on, the
  picture used to wait 5 seconds for everyone. Now it waits as long as this computer takes to
  translate a line: about 2 to 2.5 seconds on a fast computer or a Mac with Apple silicon, and
  4 to 4.5 seconds on an older computer with 4 cores.
- **QiTV measures it once and remembers.** It times its translations as they start and keeps
  that. If a line still comes late, the picture waits a second longer, and it stays that way
  for as long as QiTV runs.
- **More of the processor, where there's room.** On a computer with cores to spare,
  translations use more of them.
- **The line before, for "he" and "she".** Translations into English, German, French and the
  other languages that say "he" or "she" now read the line before, so they get who is
  speaking right more often.

## Free radio

- **Where it is.** **＋ Free radio…** sits right under **＋ Free channels…** wherever that is: the
  channel list's sources column, the **Providers** menu, the Add provider card's other ways,
  and the welcome card (a third button). In Settings → Providers it is **＋ Free radio**. The
  keys reach it as any row: ↑/↓ and OK on the keyboard, the remote or the phone.
- **The same countries as Free channels.** They are a list in Settings: **All countries**, your
  region (from your computer's settings), then every country from A to Z in your language.
  Type to find one. The **Providers** menu offers the first two and **Another country…**, which
  opens the list; its tooltips say where the stations come from.
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
