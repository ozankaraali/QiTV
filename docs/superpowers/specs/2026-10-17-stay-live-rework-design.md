# Stay-live rework: learn each stream, steer gently (design)

Status: agreed direction from the owner on 2026-10-17; evidence measured the same day.
Branch: `tv/vlc` (part of 2.1). Builds on 2.0.1/2.0.2's stay-live, bursts, Auto latency and rate health.

## Why (the owner's words)
- "current stay live is a bit annoying my hls is around 8 sec, sometimes green sometimes yellow, and
  therefore 1x or 1.05x and then it scratches audio when changing speed"
- "it was unbearable on iptvorg trt1 hls with 7 sec stay alive on border of 1.05 and 1x so i switched
  to vlc.app so fix it please"
- "if the stay live says 20 sec and somehow we fasten or jump, 3-4 sec in trt 1 4k in strong vendor,
  frame freezes, or frame runs audio is missing"
- "not only hls, as i told, you need to find a way, idk maybe bayesian stat to understand, like you
  need to sense things that audio and video does not break, drift and such"

## Evidence
The measured root causes, with the recorder, runs and numbers, are in the session's
`scratchpad/staylive/FINDINGS.md` (copied into this branch's ledger folder,
`.superpowers/sdd/2026-10-17-qitv-2.1-mixed-providers/staylive-FINDINGS.md`). In short: a dead band
narrower than the stream's own arrival sawtooth (the public HLS channel HLS: 18 rate changes / 5 min; the 1-stream provider raw TS: 40,
some 0.25 s apart); a burst detector that flips on/off and moves the target; a start at the oldest
segment (the 1-stream provider: 20 s backlog → 1.10x for 177 s; the sparse-PCR channels: starved start → 4 s freeze and 2 s of
dropped audio/video); a position estimate that misreads sparse-PCR clocks (a sparse-PCR channel: -11 s error →
false "crawl" lock and direct-play fallback); an unsmoothed lag text (90 changes/min). Fullscreen
toggles are not a cause.

## Goals
1. **Steady by default.** On a stream that arrives on time, stay-live plays at 1.0x and changes
   nothing: no rate change because of the stream's own arrival rhythm (segments, bursts).
2. **Learn each stream.** Per channel, learn how the data arrives (gaps, batch sizes, lateness) and
   the smallest distance behind live with a low risk of running dry (the owner's "Bayesian" idea: a
   prior per channel, updated by starvations and near-misses, decaying when quiet, remembered in
   live_tuning.json).
3. **Start where it should be.** A tune starts at the learned distance (choose the start point in the
   ring), never from an old backlog that then needs minutes of catch-up, and never so close to the
   edge that VLC runs dry.
4. **Correct only real drift, gently.** Act only on a persistent drift outside a dead band at least as
   wide as the stream's own sawtooth; one gentle, ramped nudge (about 1.02-1.03x), sized to end at
   the band's centre; a budget of rate changes (at most 2 per 5 minutes; otherwise widen the band);
   1.10x only when far behind.
5. **One-off steps are absorbed**, not chased: a stall of the clock, a reconnect, a window change is
   taken into the estimate once.
6. **An honest clock.** The position estimate follows VLC's real clock (a software clock corrected
   by get_time, its quantisation learned); the rate-health and crawl checks run on it, so a sparse-PCR
   channel isn't mistaken for a crawling one.
7. **A steady display.** The lag shows as a settled value ("≈ 7 s", 0.5 s steps) that changes only on
   a real change; green when steady at 1.0x, amber whenever the rate isn't 1.0.
8. **Safe jumps.** A jump to live only when a nudge would take over a minute; it lands on a real
   keyframe segment start, never twice within a minute, merged with a user's Go live.

## Acceptance (measured with the recorder, offscreen and muted, 5 minutes per run)
- iptv-org the public HLS channel (HLS), Auto: after the first 30 s, ≤ 2 rate changes, none within 15 s of another,
  rate ≠ 1.0 for ≤ 10% of the time, ≤ 2 colour flips, lag text changes ≤ 1 per 10 s on average.
- iptv-org a sparse-PCR channel and a sparse-PCR channel: |mean position error| ≤ 1 s; no crawl lock or direct-play fallback caused
  by the clock; no "buffer too late … dropped" incident after the start.
- The owner's the 1-stream provider the 4K raw-TS channel (run by the controller): ≤ 2 rate changes after 30 s; the start lag
  within 1 s of the target (no long catch-up); with jumps (back30/golive actions) no A/V loss.
- One provider and every existing stay-live behaviour that isn't named above keep their tests green;
  2.0.1's bursty-source handling and 2.0.2's HLS direct fallback still work where they're really needed.

## Stages (each shippable, measured before/after)
1. Steady control and display: wide dead band from the measured sawtooth, persistence (CUSUM) before
   acting, single ramped nudges, a rate-change budget, burst classification with hysteresis (no target
   flip-flop), steps absorbed, the settled display and colour rule. (Goals 1, 4, 5, 7.)
2. Honest clock: a complementary filter on get_time with learned quantisation; drop the `_JUMP_S`
   rule; rate health and the crawl check on that clock. (Goal 6.)
3. Start policy: choose the start point for the target lag; hold a big-batch source until it has the
   target plus half its sawtooth; no open-ended 0.95x build. (Goal 3.)
4. The learned target per channel (arrival model and starvation prior, persisted). (Goal 2.)
5. Safe jumps. (Goal 8.)

## Constraints
- AGENTS.md rules: no blocking on the GUI thread; never Qt objects or timers from worker threads.
- The live helper process and libVLC stay as they are (no native builds).
- Anything visible or audible on the owner's Mac: muted, brief, closed by the agent; the owner's
  provider credentials only through a throwaway config, never in the repo, docs, commits or prompts.
