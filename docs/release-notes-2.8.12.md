# QiTV 2.8.12

QiTV 2.8.12 speaks 47 languages, and brings a large round of fixes from a review of the whole
app: playback, recording, the phone remote, Settings, providers, the guide, accessibility and
privacy.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## 42 new languages

QiTV, its phone remote included, now speaks Svenska, Norsk bokmål, Dansk, Suomi, Nederlands,
Italiano, Português (Brasil), Català, Polski, Čeština, Slovenčina, Slovenščina, Hrvatski,
Srpski, Magyar, Română, Български, Македонски, Shqip, Ελληνικά, Русский, Українська, Lietuvių,
Latviešu, Eesti, Azərbaycan, 简体中文, 繁體中文, 日本語, 한국어, ไทย, Tiếng Việt, Bahasa
Indonesia, Bahasa Melayu, हिन्दी, বাংলা, Kiswahili, Filipino, العربية, עברית, فارسی and
اردو, beside English, Türkçe, Deutsch, Español and Français. It follows your computer's language,
or choose one in Settings. These translations are new: if a word reads oddly in yours, please
tell us. (Arabic, Hebrew, Persian and Urdu show their texts right to left; the screens
themselves are not mirrored yet.)

## Watching

- **A provider's "connection limit" recording is waited out for the full 7 minutes.** 2.8.11
  asked for the channel again every 15 seconds while the provider kept sending a recording,
  but gave up after about 2 minutes and then played the provider's stream directly, which was
  the recording. It now keeps asking for the 7 minutes it promised, and then says the provider
  refused for its limit.
- **Zapping back to a channel you paused finds your place.** Paused or rewound on a channel,
  then a look at another channel and back: the first channel's buffer was let go just before
  you came back to it, so it opened at live with no "Left off at…". It is kept now.
- **A channel that no longer exists says so at once.** A missing channel showed
  "Reconnecting…" three times over about 20 seconds before saying it couldn't be opened.
- **A live channel no longer stops for good when a reconnect gets an empty answer.** Some
  providers answer a reconnect with an empty page; QiTV took that for the end of the channel
  and stopped at the end of the buffer.
- **With "Prefer HTTPS", a provider's refusal no longer turns HTTPS off.** A refusal for the
  provider's limit, answered over HTTPS, made QiTV ask every later channel of that server over
  plain HTTP for the rest of the run. The refusal now stands, as it would over HTTP.
- **A short recording a provider sends instead of a channel no longer ends the channel
  later:** once the channel played, an empty answer to a reconnect stopped it for good.
- **After a server part or the archive, a one-stream provider never sees two connections.**
  Going back to live waited for nothing before reopening the live channel; it now waits for
  the archive's connection to close.
- **Radio channels keep their sound when the provider restarts their clock.** On a channel
  without pictures, each restart withheld the sound for up to a minute.
- **No grey picture after QiTV joins a restart up,** on channels that mark every picture as a
  possible start: QiTV now looks at the picture itself before starting there.
- Some smaller corrections: a channel's first hiccup is no longer held back because the
  previous channel had one, and a refusal of a new channel is no longer taken for an
  earlier one.
- **The player's buttons stay where they are.** Pausing used to put a different button under
  the pointer, so a second click jumped to the start of the buffer; the sound buttons and the
  multiview buttons moved too. Play now keeps its place in every mode, the buttons have larger
  click areas, the controls stay while the pointer rests on them, and the pointer itself hides
  reliably over the picture (it sometimes stayed after a menu, the list or a key). A hand
  resting on the mouse no longer keeps the controls up forever. The
  channel's name stays readable over a bright picture.
- **A channel that couldn't open no longer leaves a dead screen:** the controls still come
  with the mouse, and ▶ tries again.
- **The background REC sign no longer covers the channel's name.** It sits under the clock and
  steps aside for the list, the guide and the multiview.
- **⏮ and the 60-second jump name themselves** when you point at them.
- **Zapping huge lists is fast again:** a step in a list of hundreds of thousands of channels
  took up to half a second, opening the guide over such a list took up to three; both now take
  a fraction of that, and the first letter typed in the channel list no longer stalls.
- **Multiview, picture in picture and catch-up are sturdier.** A focused tile that fails says
  so and gives its stream back; leaving the multiview never shows an empty big picture; a
  channel opened right after closing the picture in picture no longer gets a false "allows 1
  stream"; "Watch from the start" on the picture in picture's channel opens it; an archive
  watched small says when it ends; ↑/↓ after Previous walk every channel; and an STB channel
  gets a fresh link again after playing a while.
- **Rewinding past the buffer while recording keeps the recording going,** as "Watch from
  the start" does, and an archive no longer opens a second stream on a one-stream provider.

## Recording

- **A recording is no longer cut short when you change channel.** A recording started far
  back in the buffer still had a lot to copy when you changed channel; after 2 seconds the
  buffer was deleted and the recording ended where it had got to. The buffer now stays until
  the recording has everything.
- **A recording goes on when the time-shift buffer's disk fails.** QiTV went on playing from
  memory, but the recording stopped unless the switch had already happened when it noticed.
  It now waits for the switch, and no longer skips a few seconds there. A recording whose
  last minutes can't be read when you change channel tries again, and if it still can't, it
  says the recording failed (it used to say it was saved whole).
- **A scheduled recording that fails says why in your language,** instead of a code such as
  "(link)".
- **Scheduled recordings are part of the "Stop the recording?" question.** Zapping, Go back
  or quitting while a scheduled recording held your provider's only stream gave a bare "Your
  provider allows 1 stream" or stopped it without a word; QiTV now asks, as it does for a
  recording you started. When no stream is free as a programme starts, QiTV says so at once
  (not only after it ended), and a programme recorded in part is no longer also listed as
  missed.
- **A scheduled recording in the Library shows its time and a Cancel button,** instead of a
  Play button that did nothing; "Recording saved" says which folder.

## Phone remote

- **Choosing a TV no longer starts an endless reconnect,** which kept reloading the lists
  and let go of held keys.
- **Swiping a question away answers it,** as tapping beside it does: "Stop the recording?"
  swiped down left QiTV waiting.
- **When QiTV is closed on the computer, the phone says so,** instead of "Reconnecting…" for
  two minutes, also away from home; and a phone whose computer went to sleep stops waiting
  after a few seconds and tries again.
- **A search says "Searching…" while QiTV still searches other providers,** not "Nothing
  found".
- **The Guide tab remembers where you were,** as the Library does; Done in a search shown on
  the TV closes it there too; "Refreshing films and series…" goes away; "Forget this TV" asks
  first.
- **The remote fits small phones and landscape,** "OK" included in every language.
- **Watching on the phone counts as one of your provider's streams,** as watching on the
  computer does: a scheduled recording or a picture in picture no longer opens a second
  stream on a provider that allows one. (Play in VLC counts nothing: QiTV can't see what VLC
  does.)

## Finding your way

- **Favourites from every provider.** With two providers or more, ★ Favorites lists the
  shown provider's stars first, then the other providers' stars under "Other providers",
  each row naming its provider.
- **The channel playing is marked** with a small ▶ in the channel list, the guide and the
  fast-zap list.
- **The guide's main button watches:** "▶ Watch" for the highlighted channel (or "▶ Watch
  from the start" for a past programme), as Enter does. Record keeps its red dot beside it.
- **Settings has a clear Save,** with Cancel beside it; Remove Provider stands apart, in red;
  the first tab is "General".
- **Groups sort in your language's alphabet,** upper and lower case together, and "Sport 2"
  comes before "Sport 10".
- **The Library's Play, Resume and ★ buttons sit under the title,** where the small picture
  no longer covers them; a search shows the section you're in first; a Library without films
  or series says where they come from, with an Add provider button.
- **A provider with only films and series opens on them,** without an empty Live TV, and
  File → Export → All Live Channels from such a Library exports the channel list's provider.
- The channel list's providers column is titled "Providers", with its Settings and Add
  provider rows set apart.

## Providers, guide and settings

- **An STB portal that forgot your session signs in again by itself** (after sleep, or the
  same box signing in elsewhere); before, every channel failed until you refreshed.
- **The guide shows past programmes for Xtream and playlist providers,** so catch-up can be
  started from the guide; a guide that failed once is asked again; a saved guide shows when
  its server is offline; an STB guide no longer goes blank between reloads; and Enter in the
  guide no longer starts catch-up after the clock passes the half hour.
- **A playlist refresh answered with an empty page no longer wipes your channels.**
- **The audio and subtitle tracks you choose stay,** through rewinds, reconnects and quality
  changes, and the next episode keeps their language.
- **Stopping a cast no longer reopens a stream you had handed to your phone or VLC.**
- **Jellyfin and Emby hear where you are** when you watch on the phone, in the small picture
  or until the sleep timer; a film you finished starts from the beginning instead of an old
  point; and an ended sign-in is said as such, with where to sign in again.
- **An interrupted save can no longer erase your providers.** If saving the provider list or
  the settings was cut off (a full disk, a crash), the next start replaced it with the
  defaults and deleted every provider. The damaged file is now kept aside, and nothing is
  lost. Both files are now readable by you alone.
- **Renaming a provider keeps everything stored under its name:** favourites, where you left
  off, hidden groups, scheduled recordings, and what QiTV learned about its channels. Before,
  a rename cut the provider off from all of it, and an empty Continue row, unhidden groups and
  failing timers followed. Two providers can no longer share a name, a provider without its
  address or sign-in can no longer be saved by mistake, removing one asks first (and cancels
  its scheduled recordings, saying so), and the last one can be removed too. With parental
  controls locked, renaming or removing a provider asks for the PIN.
- **QiTV opens once.** Starting QiTV again while it runs brings the open window to the
  front, instead of a second QiTV that recorded the same programme twice and saved its
  settings over the first one's.
- **Settings behaves as a form should.** Enter saves (it used to clear the image cache, or
  paste a link over the form); closing with unsaved changes asks; an empty field no longer
  makes Save do nothing; the Time-shift tab no longer overlaps itself; ticking "Check for
  updates" no longer checks before you save; and "Apply Change" says what it did.
- **"Couldn't load" can be clicked to try again** in the channel list and the Library, not
  only answered with Enter. The provider's name at the top of the channel list keeps its
  start, the Keyboard Shortcuts list shows long lines whole, and the welcome card names
  Jellyfin and Emby.
- **A running export can be cancelled with a button,** and its result says the file holds
  your sign-in.
- **A channel name cut in the middle of an emoji no longer stops QiTV from saving** your
  settings and scheduled recordings.
- **Saving settings no longer stutters the picture:** the file is written in the background.
  Turning the phone remote off no longer freezes the window for a moment.
- **Guides whose times have no time zone show their programmes.** Such a guide showed
  nothing at all.
- **Playlists are read as written.** A channel name with a comma ("News, Weather & Sport")
  kept only its last part; a backup address under a channel replaced the first; a plain list
  of addresses became one channel; and channels on udp://, rtsp:// or rtmp:// addresses were
  left out.
- **An exported playlist is safer.** Names with commas read back whole, a provider's text can
  no longer add lines to the file, and the file is readable by you alone, as it holds your
  account's sign-in.

## Windows and Linux

- **Updating from inside QiTV works when QiTV runs from Downloads,** and on Linux with a
  translated Downloads folder. A download is never written over the running QiTV, and on
  Windows the old copy is replaced as soon as it has closed; if it can't be, QiTV says where
  the new version is.
- **On Linux, opening the log, a folder or a web page works on every distribution,** and
  hardware decoding finds its drivers outside Debian and Ubuntu too.
- **Deleting a recording another program has open says so** on Windows, instead of nothing.
- **QiTV keeps one taskbar button on Windows across updates.**
- The README now says the Linux build needs a recent system (glibc 2.38: Ubuntu 24.04, Debian
  13, Fedora 39 or newer) and how to get past Windows' first-start warning.

## Privacy and safety

- **Your provider's sign-in stays out of the log,** in more of the forms it can take, and your
  settings and provider files are readable by you alone.
- **QiTV only hands a player a stream's address,** never a network share, a device or a file
  on another computer a provider might name.
- **Another web page in your browser can no longer use the phone remote's home connection,**
  and QiTV's developer port reads nothing before checking its key.
- **A provider can no longer make QiTV read an answer of any size:** lists, guides and logos
  have generous limits.
- A provider's names always show as text, never as formatted text.

## Accessibility

- **Screen readers can find QiTV's screens** (the player controls, the guide, the channel
  list, the mixer and more) and hear its messages as they appear.
- **Keyboard focus shows** in Settings and the other windows.
- **Faint text is easier to read,** and so is the channel's name over a bright picture.
- **One flick on a trackpad no longer skips several channels:** the glide after you lift your
  fingers is ignored, and a swipe has to go a little way to change channel.
- **The phone remote's buttons are read in your language** by the phone's screen reader, which
  also hears whether the sound is muted and which tab is open.
