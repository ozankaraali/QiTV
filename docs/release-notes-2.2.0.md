# QiTV 2.2.0

QiTV 2.2 watches several channels at once, speaks your language, finds any genre or channel
as you type, keeps sound and subtitles in step, and copes with providers whose streams pause,
restart or repeat themselves.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## New

- **Multiview.** Watch 2, 4, 6 or 9 live channels at once (V, View → Multiview, or the grid
  button). The channel you were watching becomes the first tile and picture-in-picture the
  second; click "+" on an empty tile to choose a channel, or drag one from the channel list onto
  a tile. The tile you click gets the sound and the player controls; each tile keeps its own
  rewind and shows LIVE or how far behind it plays. QiTV never opens more streams than a
  provider allows: a tile that can't get one says "No stream to spare" and tries again when you
  click it. Leaving the grid (V, Esc or a double-click) keeps the focused tile as the big picture;
  "Last grid" brings your grid back. 6 and 9 tiles need hardware decoding.
- **Find genres and channels.** Type in the channel list, or click ⌕ Find at its top: QiTV looks
  through the genres and channels of every provider at once. Genres come first, then channels,
  then a row that searches films and series. Whole-word matches and the providers you watch come
  first, so your own channels aren't buried under free lists.
- **Sound in step.** ♫ opens a Sound panel: pick an audio track and move the sound earlier or
  later in 50 ms steps (also J and K). QiTV remembers it for each channel.
- **Subtitles.** The subtitles button opens a panel: the stream's subtitles, Load a file… (or
  drop a subtitle file on the window), and a sync row (also Shift+J and Shift+K).
- **Subtitles from OpenSubtitles.** For films and episodes, "Search OpenSubtitles…" lists the
  subtitles found, languages on the left and files on the right, best first; click one and it
  loads. QiTV remembers your pick for each title. Without signing in you get 5 downloads a day;
  signing in to your OpenSubtitles account in Settings → Subtitles gives you more.
- **Catch-up.** Many providers keep an archive of the last days for some channels. On such a
  channel the rewind bar goes on past QiTV's own buffer (the dashed part): go there and QiTV plays
  the provider's archive from that moment, and Go live brings you back. In the guide, go back to a
  past programme and choose "▶ Watch from the start". On an account that allows one stream, QiTV
  closes live before it opens the archive. Refresh a provider once (Ctrl+R) after updating so its
  channels show their archive.
- **Recording.** Press R, or the red dot after the channel's star, to record the live channel you
  watch; press again to stop. QiTV records from its own rewind buffer, so it needs no second
  connection to your provider, and if you rewound first it records from there. Recordings are
  saved in a QiTV folder in your Movies (Videos) folder; Settings → Time-shift changes it.
- **Languages.** QiTV speaks Türkçe, Deutsch, Español and Français as well as English (Settings →
  Language; it follows your system by default and applies at the next start).
- **Plain reasons.** When a channel or a provider fails, QiTV says why: the account expired, the
  account's only stream is in use, the password is wrong, the server refused QiTV, and so on. It
  asks the provider's account page, never opening a stream to find out.
- **Add a provider faster.** Paste a provider's link into the provider form (or click Paste link)
  and QiTV fills in the type, address and login. Drop an .m3u file or a link on the window to add
  it as a provider.
- **Guide time offset.** Some providers' guides are off by whole hours: Settings → Providers sets
  an offset per provider, and the guide, the banner and the player's title line move together.
- **Rewind on more channels.** HLS channels that send their sound as a separate stream now get
  rewind and stay-live too, instead of playing without them.

## Changed

- **Everyone tries Auto once.** Live latency's Auto learns the right distance for each channel.
  With this update every live latency is set to Auto once; a latency you choose afterwards stays.
- **A much lighter guide.** A large XMLTV guide used to need several gigabytes of memory while it
  loaded: a 350 MB guide now loads with about a tenth of that and faster, and its saved copy is a
  third smaller. Local .xml.gz and .zip guides work too.
- **Pauses are waited out.** When a channel stops sending for a while, QiTV now waits 20 seconds
  before it reconnects, because many providers pause and carry on where they stopped. The player
  says "Waiting for the channel…" meanwhile, and "Reconnecting…" only when it really reconnects.

## Fixed

- **Lists keep their place.** Choosing a genre far down the channel list no longer jumps the list
  to the top; going back up lands on the genre you came from, and the list opens at the channel
  that plays, however far down.
- **The programme on now matches the guide.** Some guides list a day-long placeholder ("Program",
  "Movie") beside the real programmes; the player's title line, the banner and the channel list
  showed the placeholder. They show the real programme now.
- **No more repeated seconds.** Some providers restart their stream every minute or so and send
  the last seconds again, or resend them after a reconnect. QiTV now drops what it already played
  and keeps the picture's clock smooth across the restart, so you no longer see the same seconds
  twice or a jump ahead to catch up.
- **Sound that went silent.** Some providers switch a channel's sound to another format midway
  but keep describing the old one, which left the sound silent. QiTV now notices and keeps the
  sound playing.
- **A rare freeze.** Under load QiTV could stop responding for good when memory was being cleaned
  up on a background thread at the moment the window was changing. Cleaning up now happens only
  on the window's own thread.
- **A click right after a key.** A click within a moment of pressing a key or picking a menu entry
  that changed the screen was sometimes ignored.
