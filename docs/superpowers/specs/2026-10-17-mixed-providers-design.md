# QiTV 2.1: mixed providers and uninterrupted playback (design)

Status: agreed in conversation with the owner on 2026-10-17, ready for a plan.
Branch: `tv/vlc`, shipping as 2.1. Builds on the 2.0 TV shell
(`2026-10-15-tv-on-vlc-design.md`); it replaces that spec's rule that changing provider,
opening the Library or searching stops what plays (R5).

## Why

The owner has more than one IPTV account and found they cannot be combined: picture-in-picture
from a second provider "did not do", and there is no single place to find something in
whichever account has it. In their words:

- "just append vendor ids to items, therefore we should be able to mix all the channels"
- "you can ask mapped stream provider id token without assuming"
- "you can keep both tokens its not that hard"
- on swap: "you'd find where the channel is fetched and enter shows that one"; "list can open
  the big screen stream's position"
- on the list: each provider keeps its own groups, "because every vendor has its own groups"
- "changing a vendor never would stop what's playing. user's just navigating." The point:
  "so people can zap navigate comfortable".
- "even the history should keep altogether, like: you watched vendor 1 channel 2 before and
  vendor 3 channel 5 before"
- on the Library: "library already has all channel search, it shouldnt be hard to implement
  all there either" (confirmed: the Library gets "All providers" too)
- on stars: "your faves also kept mapped"
- on scale: "it can be more than 2 vendors, so effectively you may need to store/navigate
  200+k channels for 4 vendors for example, that's why we download and cache"
- on playback: "I expect to have noninterrupted video play. so if someone watches tv and
  decides to search, or go watch movies, we take playing video as inner pip(s)." (in this
  release, not a later one)

Success: with two accounts set up, the owner watches a channel from one in the big picture
and a channel from the other in the PiP, swaps them, zaps on either, browses any provider (or
all of them), searches and goes through the Library without the video ever stopping, and
finds channels, films and series from every account (four or more of them) in one list, one
Library and one history.

## Goals

1. **Every item knows its provider.** Each live channel, film, series and episode carries the
   id of the provider it came from (the provider's name is today's identity in config, history
   and caches).
2. **Each stream comes from its item's own provider.** Opening anything, in the big picture or
   the PiP, asks that item's provider for its link (STB `create_link` with that provider's own
   sign-in and token; Xtream/M3U links are already in the item). Nothing assumes "the provider
   QiTV is on" when a stream is opened.
3. **Several sign-ins at once.** QiTV keeps a session (token, headers) per provider side by side
   and signs in to a provider the first time a request to it needs one, not all at startup.
4. **Changing provider is navigation only.** Picking a provider (or "All providers") in the
   channel list's sources column, or in the Library, only changes what that page lists.
   Nothing that plays stops, closes or loses its zap list; link requests already under way
   still arrive. Today a provider change tears down (`ProviderFlow._on_provider_ready` →
   `TVController._settle_links(provider_changed=True)`: zap list reset, guide closed, PiP
   closed, Library moved; the catalog drops in-flight link results); none of that happens on a
   provider change any more.
5. **"All providers" in the channel list.** One more entry at the top of the sources column
   (above the providers, which stay as they are). Picking it shows:
   - ★ Favorites, ⟲ Recent and All, mixing every provider's channels; in these mixed lists a
     channel shows its provider's name beside it;
   - then each provider's own groups, under a header with the provider's name, named as that
     provider names them (layout A from the owner's mockup review; groups are never merged by
     name).
6. **"All providers" in the Library.** The Library's movies and series offer the same choice:
   one provider, or all of them, with each provider's genres under its name (as in 5) and the
   pinned rows (Continue, New, My list) mixing every provider; a title shows its provider in
   the mixed rows.
7. **The channel list opens at the big picture's channel.** Enter, a click on the picture, or
   ‹ opens the list where the big picture's channel was picked: its source (a provider, or All
   providers), its group, its row highlighted. After a swap that is the new big picture's
   channel. Zapping (Up/Down) goes through that list.
8. **PiP from anything, any provider.** The channel rows' right-click "Open in PiP"
   (`ui/context_actions.py`, already there) works on a channel of any provider, in any list.
9. **One history for every provider.** Recent (the channel list) and Continue (the Library)
   always list what was watched from every provider together, newest first, each entry with
   its provider's name ("vendor 1 channel 2, then vendor 3 channel 5"), whichever source is
   being browsed. ⟲ (the previous channel, Backspace) goes back to the channel before, from
   whichever provider it was.
10. **Search covers every provider.** The ⌕ (channel list, player controls, guide) stays the
    Library's quick search, which already covers every provider's channels, films and series
    (`TVPages.open_search`). A hit from another provider now plays from its own provider, with
    no provider switch (today `LibraryController` switches first: "Switching to …"). Typing in
    the drawer keeps filtering the focused column, mixed lists included.
12. **Playback never stops for navigation.** Opening the search or the Library no longer stops
    what plays (today `TVPages._enter_library` → `stop_for_library`). The picture(s) that were
    playing come along as small pictures inside the Library page ("inner PiP(s)"): the big
    picture, and the PiP too when there was one. They keep playing, with the sound that was
    on. See "Inner pictures" below for the details.
11. **Stream limits stay per provider.** The connection budget already counts per provider
    (`services/connection_budget.py`): two pictures from two providers that each allow one
    stream both play.

## Non-goals

- No merging of groups or genres across providers by name, and no de-duplication of items
  that several providers carry.
- No new STB keep-alive: QiTV sends none today (checked), and a second STB sign-in adds none.

## Anti-goals (a failure even if the feature works)

- **A one-provider setup changes.** With a single provider configured, every list, zap,
  number, star, guide, history entry and stream behaves exactly as now (apart from a provider
  name beside history entries, if shown at all with one provider: the builder's call). Both
  setups are tested.
- **The UI waits on the network.** Opening "All providers" uses each provider's saved lists at
  once and refreshes them in the background as a provider does now; no blocking calls on the
  GUI thread (AGENTS.md).
- **One account's trouble breaks the others.** A provider that fails to sign in or refresh
  keeps its saved items listed; playing one says that provider couldn't be reached, and the
  other providers keep working.
- **It gets slow with big accounts.** One of the owner's accounts has 57,359 live channels,
  and the owner expects four or more providers: 200,000+ channels in "All", in search and in
  the drawer, plus their films and series. Lists come from each provider's downloaded cache
  (as now), never from the network while browsing. The 2.0 performance pass measured 2,500
  channels / 20,000 films; the drawer, the Library, search and the guide stay usable at
  200,000+ channels across 4 providers, measured before and after.

## Calls made in the conversation (agent's, not contradicted by the owner)

- **Zapping follows the list the channel was picked from.** In a provider's own group it goes
  through that provider's channels; in Favorites, Recent or All it goes through the mixed list.
- **Typed channel numbers search the list you are in**, as today. In the mixed lists numbers
  from different providers can repeat: the first match wins, in the order the providers are
  listed in Settings.
- **Stars become per provider, still by name.** Today a favorite is a bare name
  (`services/favorites.py`, `favorite_key`); it covers every item with that name in the
  provider (a channel listed in two groups, and a film or series of that name: My list uses
  the same list). A star becomes (provider, name), for channels, films and series alike, so a
  one-provider user sees exactly today's stars. Migration: an old bare-name star becomes a
  (provider, name) star for every provider that has an item of that name (nothing lost), saved
  once. Un-starring at one provider removes only that provider's star. (The 2.1 backlog's
  provider+section+item_key idea, one star per individual item, is not done here: it would
  split today's shared stars.)
- **History needs no migration.** Watch history already stores each entry's `provider_name`
  (`services/watch_state.py`); Recent filters on the current provider today
  (`services/navigation.py`, `_compute_recent`), and stops filtering.
- **The guide loads per provider, on demand.** Today one provider's guide is loaded at a time
  (`EpgManager`, `_epg_source`). A provider's guide is loaded while its channels are on screen
  or playing, and guides no longer in use are released, so "All providers" doesn't download
  and hold every provider's guide at once.
- **Each page remembers its own source.** The channel list and the Library each keep the
  source last browsed (a provider or All providers), and a launch reopens them there.
  "Start with last channel" tunes the last channel from its own provider.

## Inner pictures (goal 12)

The owner chose "the TV shrinks" (layout B in the mockup review): "it is basically a native
resize trick feel, like we put your mainscreen here while you navigate". The player as it
was, the big picture with its PiP inside it, is resized into a corner of the Library page,
about a quarter of the window; nothing is rearranged or reopened.

Agent's calls, not contradicted by the owner:

- **One frame, one set of buttons**: sound, ⤢ back to the full TV, × stop. Draggable to another
  corner (the PiP's snapping rules). The PiP inside it keeps its place within the picture,
  scaled with it.
- **Back to TV.** ⤢, a click on the frame, Esc or ‹ (as today's way back) returns to the
  player page with both pictures where they were; nothing restarts.
- **Playing a movie or an episode from the Library.** It takes the big picture. What was the
  big picture becomes the small picture (the PiP) and keeps playing; a previous small picture
  closes, since QiTV has two players. If the movie's provider has no free stream, the small
  picture holding that provider's stream closes to make room, and a notice says so.
- **Search the same.** The search lives in the Library page, so the same corner player shows
  there; picking a channel hit tunes it in the big picture as a channel pick does.
- **Stream limits and the time-shift ring** are those of the pictures that keep playing:
  nothing reopens, and a live picture keeps its ring (going back shows it where it was).
- **Mouse rule.** The corner player's buttons are at least 28 px (the mouse audit).

## Where the code assumes one provider today

Reading aids for the plan, not a design (counts of `current_provider`/`current_provider_name`
uses): `services/provider_manager.py` (29; one `current_provider`, `token`, `headers`),
`services/catalog.py` (25; `request_link` uses the current provider; a provider setup bumps a
generation that drops in-flight link results), `services/epg_manager.py` (16),
`ui/library_controller.py` (14; a title from another provider switches provider first),
`services/navigation.py` (5; the sources column lists `catalog.provider_names()`, Recent
filters by the current provider), `services/library.py` (4), `ui/live_controls.py` (3),
`services/workers.py` (3), `ui/pip_controls.py` (2; the PiP's stream-limit check reads the
current provider; `links_dropped` closes the PiP on a provider setup), `ui/tv_controller.py`
(`_settle_links`), `ui/provider_flow.py` (the provider setup flow and its teardown),
`ui/tv_menus.py`, `ui/export_flow.py`, `ui/settings_dialog.py` (2 each). `StbHandshake` in
`services/provider_manager.py` already signs in to a portal on its own (Settings' Verify uses
it in isolation). Streams are opened with the VLC user agent only (`ui/live_tuner.py`), so no
provider headers travel with a stream. For goal 12: the video surfaces are native child widgets
of the player page (`ui/video_surface.py`, `ui/pip.py`'s frame, the overlay host glued over
them), and the Library is another page of the window's stack (`ui/tv_pages.py`); on macOS a
native view must stay attached while it moves over the Library page (the mini player mode,
`MINI_WIDTH_FRACTION`, already resizes the whole window around a playing picture).

## Constraints

- AGENTS.md and the 2.0 rules: no network or heavy I/O on the GUI thread; every key action has
  an on-screen control (the mouse audit test); targets of at least 28 px at the default window;
  muted, brief, agent-closed windows when anything visible is launched on the owner's Mac.
- Provider credentials are local only: never in the repo, docs, commits, CI, memory or
  subagent prompts. Real checks pass them through environment variables to a throwaway config,
  deleted afterwards.
- Never exceed a provider's stream limit: the owner's two accounts allow 2 and 1 streams.

## Verification

- Automated: two fictional providers served from 127.0.0.1 (as `scripts/make_demo_content.py`
  does for one) cover the mixed lists in the channel list and the Library, PiP from the other
  provider, browsing another provider while both pictures keep playing, swap then Enter
  (the list opens at the big picture's channel), zapping and numbers in mixed and own lists,
  the mixed history and ⟲ across providers, the star migration, a provider that fails to sign
  in, the one-provider behaviour unchanged, and the inner pictures (opening the search and the
  Library with one and two pictures playing, back to TV, a movie taking the big picture,
  stream limits honoured, nothing restarted).
- Performance: a synthetic catalog with 4 providers and 200,000+ channels in all (and their
  films and series): the drawer, the "All" group, the Library's "All providers", search and
  the guide, before and after; memory at start and after browsing.
- Real: the owner's two accounts (2 streams and 1 stream): big picture from one, PiP from the
  other, browse the other provider, search and the Library meanwhile (the pictures keep
  playing inside), play a film, swap, zap on both, quit, with never more streams open than
  each allows.

## Left to the builder

How provider ids are attached to items (and whether the provider's name stays the id), the
session store's shape, how the catalog, navigation and Library hold several providers' lists,
the guide's loading and release rules beyond "on demand", the star storage format, how the
provider name is drawn beside an item in the mixed lists, and the order of the work (a plan
may land it in stages, each one shippable, as long as the one-provider setup never regresses).
