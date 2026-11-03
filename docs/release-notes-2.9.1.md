# QiTV 2.9.1

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Films and series

- **The sound and subtitles you choose are kept:** pick another audio track or subtitles for a
  film, and it opens with them the next time, another day too. A series keeps your choice for
  all its episodes (by language), and a live channel keeps its own. Subtitle files you load are
  not kept this way (OpenSubtitles' are offered again as before).
- **Where you stopped follows the film:** some providers number their films anew. A film's
  place is now kept with its TMDB id too, so it comes back under its new number, and a number
  that now belongs to another film starts that one from its beginning, never where you left the
  first. The Library's Continue shows the film under its new number.

## Lists

- **Hide a group without parental controls:** a channel group's menu has **Hide group**, a
  Library genre's **Hide genre**: it leaves the lists of groups (here and on the phone) at once,
  with no PIN. Nothing is locked: its channels still come up in All channels, the searches and
  your history. **Settings → Providers → Hidden groups** shows it again.

## Connections

- **Secure DNS:** if your internet provider blocks your IPTV provider's address, turn on
  **Settings → General → Secure DNS**. QiTV then looks up its providers' names over HTTPS, with
  Cloudflare's or Google's resolver (the names of the providers you use are sent to them), and
  checks each provider's own certificate as before. It covers the lists, the guide, the logos and
  every channel QiTV time-shifts; a channel VLC plays directly still uses your computer's DNS.
  Off by default. When a provider's address can't be found, QiTV now says so, and suggests it.

## AI captions

- **The faster model when the more accurate one can't be downloaded:** if the more accurate
  speech model's download fails, Automatic offers the faster one instead.
