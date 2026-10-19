# Multiview: several live channels at once (design)

Status: design for review by the owner. Stages 0–1 may be built as an experiment on a branch;
nothing merges into tv/vlc before the owner has seen it.

## What it is and why

A grid of live channels on one screen: 2, 4, 6 or 9 tiles, each its own stream, from any
provider. One tile has the focus: its sound plays, and the player controls, the channel list,
zapping, rewind and Go live act on it. The owner: "we already have the foundation ... a
straightforward layouting of multiple spawned pips". QiTV already plays two pictures (the big
one and picture-in-picture) with their own players, sound levels, stream budgets per provider,
time-shift and stay-live; the grid generalises two to N.

Goals: watch several matches or news channels side by side; sound follows the focused tile;
never open more streams than a provider allows (a tile that can't get a stream says so and keeps
its channel); leaving the grid returns to normal TV with the focused tile as the big picture.

Non-goals for the first version: per-tile rewind (stage 2), saved layouts (stage 4), dragging
channels onto tiles (stage 4), films/series in tiles (live only).

Anti-goals: freezing or stuttering the focused tile because of the others; opening more streams
than allowed or retrying refused streams by itself; a grid that only works with a keyboard.

## How it looks and works (stage 1)

- Open it from View → Multiview, the V key, or a grid button on the player controls. The channel
  playing now becomes tile 1, an open picture-in-picture tile 2; the other tiles show "+".
- A layout chip on the controls switches 2 / 4 / 6 / 9 (1×2, 2×2, 3×2, 3×3).
- Click an empty "+" tile: it takes the focus and the channel list opens; the channel picked
  plays there. Click a playing tile: it takes the focus (its sound plays, the others go quiet).
  A click on the focused tile opens the channel list, as on the big picture today.
- Zapping (↑/↓, numbers, the channel list, the find) changes the focused tile's channel. Zapping
  onto a channel another tile shows just moves the focus there.
- Each tile has a small label: channel name and LIVE (or the time behind, later).
- A provider with no stream to spare: the tile shows "No stream to spare at <provider>" and keeps
  its channel; a click tries again. Tiles never queue or retry by themselves.
- Leave with Esc (when nothing else is open), V, or a double-click on a tile: the focused tile
  becomes the big picture; one other tile may stay as picture-in-picture; the rest stop.
- Library, films, the mini player and external players leave the grid first.

## How it's built (from the code study)

- Stage 0 (no visible change): SessionManager holds named slots ("main", "pip", "t2"…"t8")
  instead of two; roles become names (a general set_large(name) with swap/demote as wrappers);
  LiveTuner peers become a list; PipFrame takes surfaces by name; TVWindow/TVController loop over
  slots. Budget purposes become slot names so "everyone but me" counts right. All tests green.
- Stage 1: a grid layout mode of the player page (the native video views never move between
  pages, so the grid is a layout, not a new page: ui/grid_frame.py), tiles created lazily,
  ui/grid_controls.py modelled on PipControls (one link request at a time on the side lane;
  HeldSlot.try_take for direct plays so the limit is respected), focus = SessionManager's large
  role, so the existing controls act on the focused tile for free; the mixer lists the focused
  tile first. Tiles play direct (no time-shift) in stage 1.
- Stage 2: per-tile time-shift with small RAM rings (64 MiB; the focused tile grows to the full
  ring via a helper "resize" command), plus a muted 4-tile smoke run that logs CPU.
- Stage 3: stay-live per tile (already per session); unfocused tiles stay at 1x and only jump.
- Stage 4: save/restore the grid (an optional config key, versioned), drag from the channel list.

## Constraints and risks

- Decoding load: four 1080p tiles with hardware decoding is about 0.4–1 core on a 2017 laptop;
  nine is 1–2.3 cores; software decoding several times that. Offer 6 and 9 only with hardware
  decoding; limit decoder threads on unfocused tiles; measure dropped frames per tile.
- One time-shift helper process serves every tile (one Python GIL); stage 2 measures it.
- Memory: 64 MiB rings per tile (a 4K tile needs ≥48 MiB for one keyframe interval).
- Providers keep closed connections counted for minutes: changing channels on tiles will meet
  refusals within the budget; those show the same card and never retry by themselves.
- With a single provider nothing else changes; the grid is a new mode, not a change to the TV.

## Left to the builder

Tile label styling, the focus ring, the layout chip's exact place, the "+" tile's look (reuse the
app's overlay paint code and the PiP label), and the mixer's row names ("Tile 3 · <channel>").
