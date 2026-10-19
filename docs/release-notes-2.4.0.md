# QiTV 2.4.0

QiTV 2.4 keeps the picture with you when you leave a fullscreen desktop on a Mac, brings
catch-up to STB portals, and finds every subtitle of a film you watch under its translated
title.

**Please always download QiTV from [Releases](https://github.com/ozankaraali/QiTV/releases)
instead of using others' distributions, which may have malware.**

## New

- **The picture follows you across desktops (Mac).** In fullscreen, switch to another desktop or
  app and the picture keeps playing in a small window on every desktop, like a browser's
  picture in picture on a tab change; come back to the player's desktop, or click the small
  window, and it fills the screen again. Drag the small window anywhere. Settings → Float the
  picture when you leave a fullscreen desktop turns it off.
- **Catch-up on STB portals.** On a portal that marks its archived channels, the time-shift
  bar reaches into the portal's archive, and the guide's **▶ Watch from the start** plays past
  programmes the portal lists as recorded; at a programme's end the next one follows. It uses
  the guide from the portal itself. Portals differ in what they keep and how they answer, so
  when a portal has no recording of a programme QiTV says so and the channel plays on live.

## Fixed

- **Every subtitle of a film with a translated title.** Searching OpenSubtitles for a film your
  provider lists under a translated title without its id found a subtitle or two under that
  title and set the rest, filed under the film's original title, apart as other films'. They
  are all listed as the film's now, and a word that two titles share never brings another
  film's subtitles along.
