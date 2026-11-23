# QiTV 2.14.0

QiTV 2.14.0 adds Plex: sign in with a code, and your server's films and series are in the
Library, played as they are. Live TV stays steady on channels that come in bursts or whose
provider pauses, and an ad break that switches source no longer freezes the picture. Both small
windows of picture in picture have the player's controls, the multiview keeps its grid when you
watch one tile big, the channel list always opens on the channel you watch, and radio pauses,
rewinds and has AI captions. 2.14.0 also brings 2.13.4, 2.13.3, 2.13.2, 2.13.1 and 2.13.0: a
round of fixes, a station's view to choose, translated captions sooner, nothing in a system
window any more, the phone's pictures at home, and free internet radio.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Plex

- **Add your Plex server.** In Settings → Providers → Add provider (or the Library's new ⚙),
  choose **Plex**. QiTV shows a code in large letters; enter it at **plex.tv/link** on any phone
  or computer. QiTV then adds your server, or lets you choose when your account has several.
- **The Library:** your server's films and series, with their seasons and episodes, posters,
  where you stopped, Continue watching and the next episode, as for Jellyfin and Emby.
- **Played as they are.** QiTV plays the server's own file. The ⚙ menu offers the title's
  versions.
- **Converted when you ask.** The ⚙ also offers your server's conversions (1080p, 720p, 480p),
  as for Jellyfin and Emby, for a slow connection. The first one asks first, since it uses the
  server's processor, and QiTV stops the conversion as soon as the title stops. A Plex,
  Jellyfin or Emby server that doesn't allow you conversions plays the file as it is, and QiTV
  says so.
- **Your place is kept on the server.** QiTV tells your server how far you watched; a title
  watched to the end is marked watched by the server.
- **At home and away.** QiTV keeps every address your server has and finds it again when one
  stops answering: at home it uses the home address, away the remote one.
- **Your sign-in stays here.** For a server you own, the access Plex gives QiTV is your account's
  own, so QiTV keeps it like a password: only on this computer, never in the log, and only sent
  to your server once that server has proved who it is. Your phone never receives it: a Plex
  title plays on the phone through QiTV on this computer. QiTV tells Plex only that it is QiTV,
  its version and a random id.
- **Posters** come in at the size QiTV shows them, so a large library loads quickly.

## Live TV that stays steady

- **No more jumps on channels that come in bursts.** Some channels arrive about ten seconds at a
  time for a few minutes, then steadily again. QiTV used to move how far behind live it keeps
  the picture by several seconds at each change, and then skip forward. Now, once a channel has
  shown its bursts, QiTV keeps room for them for as long as you watch it, and no longer skips
  when they stop or come back.
  The cost: on such a channel the picture stays a few seconds further behind live.
- **A provider that sends a backlog is caught up gently.** After a pause, a reconnect, or right
  after you tune, some providers send what they held all at once. QiTV now plays a little
  faster (1.03x, or 1.05x for a longer one) until it is back where it should be, instead of
  skipping. On the channel you watch, it skips only after a real freeze, or when the picture is
  more than half a minute past where it should be, and then once. (A channel that plays only at
  normal speed, and a multiview tile without the focus, still move closer at about 8 seconds.)
- **A tune shows the picture at once,** and if the provider's backlog started it far behind (on a
  channel sent as a continuous stream), it moves forward once, as soon as that backlog has
  arrived. Then it moves no more for that backlog.
- **Far fewer changes of speed.** Each change makes VLC's sound jump a moment (what you may
  have heard as a skip or a scratch), so QiTV now changes speed much less:
  - it leaves small drifts alone: on a steady channel, being a second or so off where it should
    be (up to 15% of that distance, at least 1.5 s) no longer starts a change;
  - on a channel that comes in bursts, it no longer slows down for a burst a second longer than
    before;
  - a change goes in one step and comes back in one step, instead of three each way.
  On a day like the one this was found on, that is about a quarter of the changes.
- **A pause instead of broken sound.** When the provider stops sending, QiTV now holds the
  picture before VLC runs out (on a channel that comes in bursts, also the first time), and keeps
  it held while the provider only
  trickles, until it sends at normal speed again. Before, VLC could play in fits right at the
  live edge and drop bits of sound each time.
- **No freeze at an ad break that switches source.** On channels whose ads come from another
  source, the picture could stand still for about 15 seconds at a switch and then open again.
  QiTV now joins the new source's clock to the channel's at once, whichever way it jumped. A
  skipped piece of a stream or a reconnect that lands a few seconds ahead now plays straight on,
  instead of showing a still picture for the gap.
- **The log says what QiTV waited for:** "The source sends far faster than real time", "…at
  about real time", and each pause and change of speed, with the time.

## Picture in picture

- **Both small windows have the player's controls.** The picture that floats when you leave a
  fullscreen QiTV for another desktop or app now shows the same controls as the mini player:
  they come on hover, even with another app in front. Its buttons act right there (play and
  pause, the jumps, live, mute, ★, record); the ones that open a panel (sound, subtitles, ⚙,
  the guide, search) bring the fullscreen window back first and open there. A click on its
  picture does nothing, a drag moves it, a double-click brings you back. It never takes the
  keyboard from the app in front.
- **On a Mac, the mini player** (Alt+P, or its button) is a small window on every desktop while
  the full window steps aside, with the player's controls on hover and the keys working as in
  the window. A double-click goes fullscreen; its button, Alt+P, the Library, Search and
  Settings bring the window back. Opening a file from the Finder brings it forward, and Quit
  works from it as from the window. A click opens the channel list, as in the window, and a
  click on its picture closes a panel that's open.
- **Both keep the picture's shape.** Dragging their edges keeps the picture's shape, also with
  macOS's own edge resize, between the smallest size and the screen. No black lines are left
  around the picture, and the size is remembered. The wheel over their picture does nothing,
  so a scroll can't change the volume or the channel by mistake.
- **⧉ with no channel before it** opens the channel list, to choose the small picture's channel.

## Multiview

- **Watch a tile big, and the grid waits.** A double-click on a tile (or its big-picture button)
  makes it fill the player while the other tiles play on, muted, behind it. Esc, V, the grid
  button or another double-click bring the grid back exactly as it was. Before, the other tiles
  were stopped and forgot their channels.
- **Choosing a tile's channel:** one click on a tile's name opens its list on its own channel
  and group. An open list follows when you move to another tile.
- **Tab and Shift+Tab** move between the tiles; Enter opens the tile's list.

## The channel list

- **It opens on the channel you watch.** It could open on the channel next to it after
  a playlist changed, or on the old channel during a zap. With the same channel twice in a list,
  it opens on the copy you picked.
- **Settings from the Library.** A ⚙ at the right of the Library's top bar opens Settings over
  the Library at Providers, to add Plex, Jellyfin or Emby without going back to live TV. Ctrl/⌘+,
  from the Library does the same.
- **Cards no longer cut off.** The Add provider card, the parental PIN and other cards opened from
  Settings could be cut off at the edge of the Settings panel on a wide window. They show whole
  now, in every language.

## Radio

- **Radio pauses, rewinds and has AI captions,** as channels do. A station now plays through
  QiTV's time-shift buffer: pause it, go back, record it, or turn on Words. The song now playing
  still shows under its name. A station in a format the buffer can't read (Ogg, FLAC and a few
  others) plays directly, as before.

## Buttons

- **The speaker closes the mixer.** A click opens it, the next click closes it.
- **A quick second click counts.** Clicking a button twice quickly to open and close its panel
  (sound, subtitles, Picture, quality, the layout, ⋯, casting, the outside player) used to lose
  the second click. Now it closes the panel.

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
- **The log says what stay-live does.** Each small change of speed stay-live makes to keep a
  channel near live, and the source's bursts it counts, are now in QiTV's log with the time,
  so a jump or a skip in the sound can be matched to what happened then.

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
- **Live, straight from the station.** In 2.13 VLC plays a station directly, without QiTV's
  time-shift buffer: it opens one connection, and there is no rewind, recording or AI captions.
  2.14 plays it through the buffer (Radio, above).
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
