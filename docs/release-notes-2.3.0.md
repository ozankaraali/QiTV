# QiTV 2.3.0

QiTV 2.3 gives the picture its own controls, gives every multiview tile its own buttons, keeps
the volume where you set it, finds the right subtitles for films with translated titles, and
plays on through channels whose clock jumps ahead.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## New

- **Picture.** A Picture button on the player controls (or A) sets the aspect ratio — Auto,
  16:9, 4:3 or Fill the window, with no black bars — and QiTV remembers it for each channel.
  It also turns deinterlacing on or off, and has a sleep timer that stops playback after 15 to
  90 minutes or at the end of the programme, then lets your computer sleep. Shift+I shows
  technical statistics over the picture.
- **Controls on each multiview tile.** Point at a tile to get its own play/pause, sound, LIVE
  (or how far behind it plays: a click goes live), a button that makes it the big picture,
  and ✕ to close it. They act on that tile without moving the focus. The layout chip beside
  the multiview button now looks like a button and opens the four layouts as small pictures
  to pick from (6 and 9 tiles need hardware decoding).
- **Your mix stays.** In multiview the sound follows the focused tile until you change another
  tile's sound (its level or mute, in the mixer or on the tile); from then on, clicking another
  tile moves only the controls. The mixer says which way it works, and Follow focus, or leaving
  the grid, goes back.
- **macOS downloads as a .dmg.** Open it and drag QiTV onto Applications. An update from inside
  QiTV downloads the new .dmg, opens it and closes QiTV, so updating is one drag and Replace.
- **Remove from Recents.** Right-click a channel in ⟲ Recent to remove it, or to clear Recents;
  Delete removes the highlighted one.
- **The speaker shows the volume:** one, two or three waves, and a cross when muted.

## Changed

- **The volume stays as you set it.** The big picture keeps its volume through a swap, a
  multiview tile taking the focus or a tile left as the big picture, and QiTV remembers it
  for the next start; the small picture keeps its own.
- **Subtitles for the right film.** The OpenSubtitles search now uses the film's or the
  series' id when your provider lists one, ignores tags such as "TR - " in a title, and never
  lists other films' subtitles as the one playing; when it finds subtitles only under other
  titles' names (a translated title), each row names its film. Refresh a provider once
  (Ctrl+R) after updating so its films carry their ids.
- **All providers lists** show long channel names in full, with the provider below.
- **The small picture** shows its own stream messages ("Reconnecting…") and reconnects when its
  stream ends, as the big picture does.

## Fixed

- **A frozen picture on channels whose clock jumps ahead.** Some channels' encoders lose a few
  seconds of their source and their clock leaps ahead without saying so; the picture stood
  still for as long as the leap (15 seconds on one public channel). QiTV now plays straight
  on.
- **A channel picked for a multiview tile plays there.** A click on a channel that moved the
  pointer a little dropped it on the tile behind the channel list (tile 1) instead.
- **The Library keeps its place, and the film you pick plays.** When its film lists refreshed,
  the Library went back to its first genre, and a film picked at that moment did not play.
  A provider not set up yet no longer says it "has no movies".
- **"Prefer HTTPS"** falls back to HTTP on portals without HTTPS.
- **An Xtream provider's last channel** comes back after you edit its server or password.
- **A channel played directly** respects the provider's stream limit while the small picture
  plays.
- **Subtitle files** keep their own names beside a stream's subtitle tracks.
