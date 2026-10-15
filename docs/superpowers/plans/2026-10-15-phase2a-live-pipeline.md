# Phase 2A Live Pipeline Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure-Python live pipeline without any UI. It covers:
- MPEG-TS scanning and keyframe-aligned segmentation, with no remux;
- a continuous timeline;
- RAM and disk rings;
- raw-TS and HLS sources (AES-128);
- a token-protected progressive loopback server;
- one long-lived helper process hosting many sessions, supervised from the GUI process;
- the stay-live controller.

Everything is tested headless.

**Architecture:** `services/live/` holds pure modules (`ts`, `timeline`, `stay_live`), storage (`ring`), I/O (`source`, `server`), the helper process (`worker`) and the parent supervisor (`engine`). Wherever main's MPV-branch recorder already solved a problem, it is ported (see `.superpowers/sdd/phase2-port-map.md`, the binding reference for ported code and for the 21 requirements in its §8). PyAV/FFmpeg is **never** used.

**Tech Stack:** Python 3.14 stdlib (`multiprocessing` spawn, `http.server`, `threading`), `requests` (present), **new pure-Python and prebuilt-wheel dependencies `m3u8==6.0.0` and `pycryptodomex`** (allowed by spec D2), PySide6 QtCore (`QLockFile`, `QThread`, signals), `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md` §4.3, §6.1–6.5, §6.8, §9, §11. Port map: `.superpowers/sdd/phase2-port-map.md` (main line numbers, exact constants, protocol and requirements). Read `main` code with `git show main:<path>`, never by checking `main` out.

## Global Constraints

- **No PyAV, no FFmpeg, no native builds.** New dependencies are exactly `m3u8==6.0.0` and `pycryptodomex` (the latest version available in `uv`; import namespace `Cryptodome`). `cryptography` is **not** used: from v49 it ships no Intel macOS wheels, so it would build from source there. Add them to `pyproject.toml` and regenerate `uv.lock` and `requirements.txt` the way the repo does: `uv lock` then `uv export --format requirements-txt --no-dev --no-emit-project > requirements.txt` (check the header of `requirements.txt` for the exact command used and reuse it).
- **Spec values (exact):**
  - segments cut at a keyframe once at least 2.0 s long; forced cut at 10 s;
  - RAI fallback to NAL scanning after 6 s without RAI;
  - every segment starts with fresh PAT and PMT copies;
  - discontinuity when the PTS delta is negative, more than 5× the target, or missing;
  - RAM ring 128 MiB per session;
  - disk ring 256 MiB–16 GiB (default 2 GiB) with a 64 MiB free-space reserve;
  - server binds 127.0.0.1 on a random port with a numeric bind and a 128-bit token;
  - source timeouts `(60, 60)`; reconnect backoff 0.5 s doubling to 8 s, at most 6 tries;
  - stay-live targets Low 1.0 / Normal 2.5 / Stable 6 s; rate 1.05, or 1.10 when more than 3 s over target; back to 1.0 within 0.2 s of target; reopen at live when more than 20 s behind; never more than 1.10 in LIVE.
- **All 21 requirements in port map §8 bind this plan.** Each task lists the ones it owns.
- **Credential and log hygiene:**
  - credentials never appear in argv, logs or error text;
  - child fds 1 and 2 go to devnull;
  - all user-visible errors are fixed strings.
- **Concurrency:**
  - no blocking work on the GUI thread;
  - the supervisor runs on a QThread retired through `ThreadCleanup`;
  - no multiprocessing primitive is ever touched from the GUI thread; commands are queued to the supervisor.
- Tests use `unittest` with `QT_QPA_PLATFORM=offscreen`. Lint with `uv run flake8 --select=E9,F63,F7,F82 <files>`; format with black/isort (line length 100); every new module passes `uv run mypy <module>`.
- Commits end with these two lines; stage explicitly and never use `git add -A`:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_011UFrnH8jUNCzkFNLKJaoRD
  ```

## Review Focus

1. **Real IPTV oddities:**
   - PAT/PMT split across several packets;
   - streams whose RAI flag is never set (NAL fallback) or set on every PES (the 2 s minimum must still hold);
   - 33-bit PTS wrap;
   - a PMT change mid-stream (new video PID);
   - scrambled packets, which must be reported as unsupported.
   (Tasks 2 and 3.)
2. **A slow or paused reader:**
   - the loopback response blocks on a full socket while the ring keeps filling and evicts under it;
   - the reader must be cut off cleanly, never served a deleted or half-overwritten segment, and never deadlock the writer.
   (Tasks 4 and 6.)
3. **Stopping a session whose upstream is stalled** mid-header or mid-body must return in under 2 s without killing the helper or its sibling sessions (Tasks 5 and 7).
4. **Helper death:**
   - the parent must notice within 1 s, clean every session dir it handed out, restart once, and report a second death as fatal;
   - parent death must make the helper exit and close the upstream sockets.
   (Task 8.)
5. **HLS variety:**
   - a master playlist with only fMP4 variants (unsupported);
   - an external AUDIO group (unsupported);
   - AES-128 with an implicit IV;
   - live reload with a media-sequence gap;
   - `#EXT-X-DISCONTINUITY`;
   - relative URIs after a redirect;
   - gzip playlists.
   (Task 5.)

---

### Task 1: Dependencies and the TS fixture synthesizer

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `requirements.txt`
- Create: `services/live/__init__.py` (docstring only), `tests/ts_fixtures.py`
- Test: `tests/test_ts_fixtures.py`

**Interfaces:**
- Produces in `tests/ts_fixtures.py`:
  - `crc32_mpeg(data: bytes) -> int`;
  - `pat(pmt_pid=0x1000, cc=0) -> bytes`: one 188-byte packet;
  - `pmt(video_pid=0x100, video_type=0x1B, audio_pids=(0x101,), pcr_pid=None, cc=0) -> bytes`: one packet;
  - `pes_packets(pid, stream_id, payload: bytes, pts: int | None, *, rai=False, cc_start=0) -> tuple[list[bytes], int]`: returns the packets and the next continuity counter, splitting the payload over 184-byte packets with the adaptation-field RAI flag on the first packet when `rai`;
  - `h264_access_unit(idr: bool) -> bytes`: AUD (9) plus, when `idr`, SPS (7), PPS (8) and IDR (5); otherwise non-IDR (1); padded to about 400 bytes;
  - `make_stream(seconds: float, *, fps=25, gop_s=1.0, audio=True, rai=True, video_type=0x1B, pts_start=900000, resets: tuple[float, ...] = (), packet_size=188, scrambled=False) -> bytes`:
    - a valid TS with PAT and PMT every 0.5 s;
    - video PES per frame (PTS = start + frame × 3600), keyframes every `gop_s`;
    - AAC-ish audio PES every 1024/48000 s (PTS-aligned);
    - at each time in `resets`, PTS jumps back to `pts_start`;
    - `packet_size` 192 prefixes a 4-byte timecode, 204 appends 16 bytes of padding;
    - `scrambled` sets the transport scrambling bits on video packets.
- Dependencies: `m3u8==6.0.0` and `pycryptodomex` added.

- [ ] **Step 1:** Add the dependencies:
  1. `uv add "m3u8==6.0.0" pycryptodomex`, then regenerate `requirements.txt` with the command recorded in its header.
  2. Run `uv run python -c "import m3u8, Cryptodome; print('ok')"`.
- [ ] **Step 2:** Write `tests/test_ts_fixtures.py`, which asserts that `make_stream(3)`:
  - has a length divisible by 188;
  - has every packet starting with `0x47`;
  - has a first packet that is a PAT (PID 0) with a valid CRC (recompute it);
  - has video PUSI packets with RAI exactly at keyframes (count == 3 for a 1 s GOP over 3 s);
  - with `packet_size=192`, is divisible by 192 with sync at offset 4.
- [ ] **Step 3:** Implement `tests/ts_fixtures.py`:
  - PSI sections: `table_id`, `section_syntax_indicator=1`, `section_length` and `crc32_mpeg`, using the MPEG-2 CRC32 polynomial `0x04C11DB7` with init `0xFFFFFFFF`, no reflection and no final xor;
  - PES header: `00 00 01`, stream id (`0xE0` video, `0xC0` audio), length (0 for video), flags `0x80`/`0x80` with a 5-byte PTS;
  - PTS encoding: `0010 + pts[32..30] + 1`, `pts[29..15] + 1`, `pts[14..0] + 1`;
  - adaptation-field stuffing to fill the last packet of each PES.
- [ ] **Step 4:** Run the tests until they are green, then the suite. **Step 5:** Commit (`build: add m3u8 and pycryptodomex; add a TS fixture synthesizer`).

---

### Task 2: TS scanner and segmenter (`services/live/ts.py`)

**Owns requirements:** §8.17 (first segment starts on a keyframe; audio before it is discarded; every segment starts with PAT/PMT), plus Review Focus 1.

**Files:**
- Create: `services/live/ts.py`
- Test: `tests/test_ts_segmenter.py`

**Interfaces:**
- Produces:
  - `PACKET = 188`;
  - `class Unsupported(Exception)`, carrying a fixed reason string from `{"not MPEG-TS", "unsupported packet size", "no program tables", "scrambled"}`;
  - `pts_delta(a: int, b: int) -> int`: wrap-aware `b − a` in 90 kHz ticks, giving the shortest signed difference modulo 2^33;
  - `Opened(seq: int, first_pts: int | None, header: bytes, wall: float, discontinuity: bool)`: header = the PAT packet plus the PMT packets;
  - `Data(payload: bytes)`;
  - `Closed(seq: int, next_first_pts: int | None, wall: float)`;
  - `class Segmenter`:
    - `__init__(self, *, target_s=2.0, forced_s=10.0, rai_fallback_s=6.0, clock=time.monotonic)`;
    - `feed(chunk: bytes) -> list[Opened | Data | Closed]`, which raises `Unsupported`;
    - `finish() -> list[Closed]`, which closes the open segment at EOF;
    - `discontinuity()`, which marks the next cut as a discontinuity (used by HLS `#EXT-X-DISCONTINUITY`);
    - properties `video_pid`, `latest_pts`.

**Behaviour:**
- **Resync:** keep a byte buffer. While it does not start with a sync byte, find the first index `i` where `buf[i]`, `buf[i+188]` and `buf[i+376]` are all `0x47` and drop the bytes before it.
- **Detecting the input type** during the first 64 KiB:
  - sync with stride 192 or 204 but not 188 raises `Unsupported("unsupported packet size")`;
  - no sync at all raises `Unsupported("not MPEG-TS")`.
- **Program tables:**
  - **PAT (PID 0, PUSI):** pointer field, then the first program with a non-zero program number gives the PMT PID. Keep the last PAT packet bytes.
  - **PMT:** collect packets from a PUSI until the section is complete (`section_length`). Parse the ES loop:
    - video PID = the first stream type in `{0x1B, 0x24, 0x02, 0x01}`;
    - otherwise audio-only mode, using the first stream type in `{0x03, 0x04, 0x0F, 0x11, 0x81, 0x87}`.
    Keep the PMT packet bytes. A PMT change of the video PID updates `video_pid`.
  - No PAT/PMT within 3 MiB of input raises `Unsupported("no program tables")`.
- **Scrambling:** non-zero scrambling bits (`(b3 >> 6) & 3`) on a video packet raise `Unsupported("scrambled")`.
- **Keyframes** (on the cut PID, PUSI packets only):
  - RAI = adaptation field present with length > 0 and bit `0x40` set.
  - Until a RAI has been seen and while less than `rai_fallback_s` of PTS (or wall time when there is no PTS) has passed since the first video PUSI, only RAI counts. After that, the NAL scan of that packet's PES payload also counts:
    - H.264: start-code NAL type 5 or 7;
    - HEVC: `(b >> 1) & 0x3F` in 16..21 or 32;
    - MPEG-2: start code `0xB3`.
  - Audio-only mode: every audio PUSI with a PTS counts as a cut point.
- **Cutting:**
  - Before the first cut point, packets are dropped.
  - At a cut point, a segment is opened when none is open. Otherwise the open segment is closed and a new one opened when either:
    - `pts_delta(seg_first_pts, pts)` is at least `target_s × 90000`;
    - the delta is a discontinuity (negative, more than `5 × target_s × 90000`, PTS missing, or flagged by `discontinuity()`), in which case `Opened.discontinuity=True`.
  - **Forced cut:** when the open segment exceeds `forced_s` (by PTS, or by wall time when PTS is unusable), it is cut at the next PUSI on the cut PID even without a keyframe.
- **Output:**
  - `Opened.header` = the last PAT packet plus the PMT packets.
  - Every packet from the first cut point onward (any PID, PSI included) goes into `Data` events in input order, coalesced per `feed` call.

- [ ] **Step 1: Write failing tests** with `tests/ts_fixtures.make_stream`:
  1. A 10 s stream with a 1 s GOP gives 5 segments (each a multiple of 2 s by PTS). The first `Opened` comes before any `Data`. The concatenated `Data` payloads equal the input bytes from the first keyframe packet onward.
  2. Audio-only input cuts about every 2 s.
  3. Input with no RAI (`rai=False`) still cuts after the 6 s fallback, at IDR access units.
  4. RAI set on every PES still gives segments of at least 2 s.
  5. A reset at 5 s gives an `Opened` with `discontinuity=True`; durations never go negative.
  6. PTS starting at `2**33 − 90000 × 3` wraps, and segments stay 2 s long.
  7. The 192-byte variant raises `Unsupported("unsupported packet size")`.
  8. Random bytes raise `"not MPEG-TS"`.
  9. Scrambled input raises `"scrambled"`.
  10. A GOP longer than 10 s gives a forced cut at 10 s.
  11. Feeding the stream in 1-byte, 187-byte and 100 KiB chunks gives identical events.
  12. `finish()` closes the last segment.
  13. A PMT whose section spans two packets is parsed.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint and mypy, commit (`feat: add an MPEG-TS scanner and keyframe segmenter`).

---

### Task 3: Timeline (`services/live/timeline.py`) and stay-live (`services/live/stay_live.py`)

**Owns requirements:** §8.16 (backward jumps, B-frames and equal stamps are not aborts; monotone timeline), §8.19 (catch-up semantics), and spec §6.3 and §6.8.

**Files:**
- Create: `services/live/timeline.py`, `services/live/stay_live.py`
- Test: `tests/test_timeline.py`, `tests/test_stay_live.py`

**Interfaces:**
- `timeline.py`:
  - `Span(seq: int, start: float, end: float, discontinuity: bool)`.
  - `class Timeline`, constructed as `__init__(self, target_s=2.0)`, with:
    - `on_opened(seq, first_pts, wall, discontinuity)`;
    - `on_closed(seq, next_first_pts, wall) -> Span`. The duration is the PTS delta in seconds. When the delta is invalid (None, ≤ 0, or more than 5 × target), the wall delta is used instead, clamped to 0.04–(5 × target).
    - `on_evicted(seq)`, which drops spans `<= seq`;
    - `spans() -> list[Span]`;
    - `start`, `end`, `oldest_seq`, `newest_seq`;
    - `live_edge(latest_pts, now_wall) -> float`: `end` plus the open segment's progress from PTS (or wall), clamped to 0..forced (10 s);
    - `locate(t: float) -> tuple[int, float]`: the seq and in-segment offset for time `t`, clamped to the buffer;
    - `seq_after(t) -> int`.
  - The timeline is continuous: the first span starts at 0.0 and each span starts at the previous end.
- `stay_live.py`:
  - `class Mode(str, Enum)`: `LIVE`, `SHIFTED`, `CATCHING_UP`.
  - `TARGETS = {"low": 1.0, "normal": 2.5, "stable": 6.0}`.
  - `Decision(rate: float, reopen_live: bool)`.
  - `class StayLive`, constructed as `__init__(self, latency="normal")`, with:
    - `mode`;
    - `target`;
    - `set_latency(name)`;
    - `user_paused()` (enter SHIFTED);
    - `user_seeked_back()` (enter SHIFTED);
    - `user_go_live()` (enter LIVE);
    - `user_speed(rate)` (SHIFTED to CATCHING_UP when rate > 1);
    - `update(lag_s: float, buffering: bool) -> Decision`.
  - Rules in LIVE:
    - `lag > target + 20` reopens at live;
    - `lag > target + 3` gives rate 1.10;
    - `lag > target + 0.75` gives 1.05;
    - `lag <= target + 0.2` gives 1.0;
    - otherwise keep the previous rate (hysteresis);
    - while `buffering`, keep the previous rate and never reopen.
  - In CATCHING_UP, `lag <= target + 0.2` switches to LIVE at rate 1.0.
  - In SHIFTED, rate 1.0 unless the user chose a speed.

- [ ] **Step 1: Write failing tests:**
  - **Timeline:**
    - contiguous spans from PTS;
    - a reset (negative delta) falls back to wall time and stays monotone;
    - a 24 s stream with resets at GOPs 3, 6 and 9 ends within ±0.15 s of 24, with every span `0 < dur <= 2.2` (ported intent of main's `test_timeline` cases; port map §7);
    - eviction keeps `start` equal to the no-reset baseline within 0.08 s;
    - `locate` clamps below the start and above the end;
    - `live_edge` includes the open-segment progress.
  - **StayLive:** every threshold boundary as a table-driven test, hysteresis, buffering behaviour, the mode transitions, and `set_latency`.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint and mypy, commit (`feat: add the live timeline and stay-live controller`).

---

### Task 4: Rings (`services/live/ring.py`): RAM and disk

**Owns requirements:** §8.2 (CRLF marker), §8.6 and §8.7 (stale-session safety), §8.12 and §8.13 (cap accounting, reserve, leases, per-chunk file handling).

**Files:**
- Create: `services/live/ring.py`
- Test: `tests/test_ring.py`

**Interfaces:**
- `class RingError(Exception)` carries fixed messages (port the `_Failure` texts from main exactly: "The temporary buffer directory is busy.", "Not enough free disk space for the temporary buffer.", "Recording stopped to preserve free disk space.", "The disk buffer limit cannot fit one keyframe interval or an active read.", "Cannot lock the temporary playback buffer.", "The temporary buffer size must be positive.").
- `Segment`: `seq`, `header_len`, `size`, `complete: bool`, `retired: bool`, `readers: int`.
- `class Ring` (abstract base). Shared logic: eviction of the oldest *completed* segments, leases, a `threading.Condition` named `changed`, and `closed`. Methods:
  - `open_segment(seq, header: bytes)`;
  - `append(data: bytes)` (may evict; raises `RingError`);
  - `complete(seq)`;
  - `lease(seq) -> Segment | None` (completed or open, not retired);
  - `release(segment)`;
  - `read(segment, offset, size) -> bytes`;
  - `wait_for(segment, offset, timeout) -> bool`: True when `segment.size > offset`, the segment completed, or the ring closed;
  - `close()`, which wakes all waiters and frees storage;
  - properties `oldest_seq`, `newest_seq`, `open_seq`, `bytes` (logical) and `charged` (cap accounting).
- `class RamRing(Ring)`: `__init__(self, max_bytes=128 * 1024 * 1024)`; segments are `bytearray`s; the charge is the byte length.
- `class DiskRing(Ring)`: `__init__(self, root: Path, session_dir_name: str, max_bytes: int)`. Ported from main `services/timeshift.py`, keeping each piece's exact semantics and constants:
  - `_allocation_unit` (173–192);
  - `_owned_files` (195–216);
  - `_remove_stale_sessions` (219–242);
  - `_prepare_directory` (467–492), including the manager lock and the 64 MiB reserve check;
  - `_delete_locked` (494–502);
  - `_retire_oldest_locked` (504–510);
  - `_reserve_write` (512–528);
  - `_lease` / `_release` (530–544), extended to the open segment;
  - teardown (`run`'s `finally`, 740–762);
  - session-dir naming `.qitv-timeshift-<32 hex>`, the owner marker `_OWNER` and the lock files;
  - writes into `segment-<seq>.ts` with `open("xb", buffering=0)`;
  - every read chunk opens, seeks, reads and closes under the disk lock.
- Module function `cleanup_session_dir(root: Path, name: str) -> None`, ported from main `_cleanup_session` (880–901), for the parent to call after a helper death.

- [ ] **Step 1: Write failing tests**, porting main's `test_cap_counts_open_segment_disk_blocks_and_evicted_reader_lease` and `test_stale_owned_sessions_only_and_active_sibling_lock_survives` (LF and CRLF markers, symlink, unowned dir, active sibling lock) **verbatim, adapting only the class names and method calls**, plus new tests:
  - **RAM ring eviction:** charged bytes never exceed the cap after any append; the oldest completed segments go first; the open segment is never evicted; a single segment larger than the cap raises `RingError`.
  - **Leased segments:** a leased retired segment survives until release.
  - **`wait_for`:** returns on append, complete and close; it times out otherwise.
  - **Reads:** `read` across append boundaries returns the exact bytes.
  - **Disk ring:**
    - the reserve check (patch `shutil.disk_usage`);
    - teardown leaves nothing behind;
    - `cleanup_session_dir` removes a dead helper's dir;
    - `cleanup_session_dir` leaves alone a dir whose lock is held by a live process: spawn a child with `multiprocessing` that holds the `QLockFile`.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint and mypy, commit (`feat: add RAM and disk rings for the live pipeline`).

---

### Task 5: Sources (`services/live/source.py`): raw TS and HLS

**Owns requirements:** §8.3 (drive letters), §8.14 (60 s timeouts, sniff by `#EXTM3U`, one request, redirect base), §8.15 (redirect bodies, verify on every hop, cross-origin auth, no file:// from remote playlists), and §8.18 (HLS variant rules).

**Files:**
- Create: `services/live/source.py`
- Test: `tests/test_live_source.py`

**Interfaces:**
- `class SourceError(Exception)`, with `.kind`, one of `"unsupported"`, `"network"`, `"tls"`, `"timeout"` or `"http"`, and fixed URL-free messages. Port the texts from main `open_source` (348–423).
- `class Source` (protocol), with:
  - `chunks() -> Iterator[bytes | Discontinuity]`;
  - `close()`, which is thread-safe and aborts any blocking read within 1 s (it closes the active `requests` Response and Session);
  - `user_agent`.
- `open_source(url: str, *, headers: dict[str, str], verify_ssl: bool, stop: threading.Event) -> Source`.
  - Scheme detection is ported from main, including the drive-letter check.
  - **HTTP(S):** a `requests.Session` with the `_close_redirect_body` hook (ported), `timeout=(60, 60)` and the provider headers (UA default `"VLC/3.0.20"`; never `"libmpv"`).
  - **Sniffing:** read the first 10 bytes. `#EXTM3U` after a BOM or whitespace means `HlsSource`; otherwise `TsSource`, which replays the prefix.
  - `file://` and local paths (tests only) give `TsSource` over the file.
- `TsSource` yields the response body through `raw.read1(65536, decode_content=True)`.
- `HlsSource`:
  - **Variant choice:** `_select_manifest` and `_video_compatible` are ported verbatim (main 95–146). Reject as `unsupported`:
    - fMP4 (`init_section` / `segment_map`);
    - `EXT-X-BYTERANGE`;
    - an AUDIO group with its own URI on the selected variant;
    - key methods other than NONE / AES-128.
  - **Live reload:** reload the media playlist every `max(1.0, target_duration / 2)` seconds, until ENDLIST.
    - Fetch segments in media-sequence order, skipping those already fetched.
    - On a sequence gap or `#EXT-X-DISCONTINUITY`, yield a `Discontinuity()` marker.
  - **AES-128:**
    - keys are cached by URI and fetched through the same Session;
    - the IV is the explicit IV or the big-endian 16-byte media sequence;
    - decryption uses `Cryptodome.Cipher.AES` in CBC mode as a stream, with PKCS7 unpadding at the segment end.
  - **Segment check:** reject a segment whose first decrypted bytes are not TS (`0x47` at offsets 0 and 188) as `unsupported`.
  - **URIs:** every URI is resolved against the **final redirected** playlist URL, and must be http(s) (anything else raises `unsupported`).
  - `close()` closes the active response.
- **Reconnects (raw TS):** on a network error after data has flowed, retry with backoff 0.5, 1, 2, 4, 8, 8 s (6 tries), then yield `Discontinuity()` and continue. When retries are exhausted, raise `SourceError("network")`.

- [ ] **Step 1: Write failing tests**, porting main `tests/test_timeshift_source.py` helpers `leaf`, `LocalSource` and `tls_context` **verbatim**, and the source servers using `ThreadingLoopbackHTTPServer` (Task 6 adds it; put an early copy in `tests/http_fixtures.py` that Task 6 then imports). Generate fixtures with `tests/ts_fixtures.make_stream`. Port these tests from main, adapting only the decode assertions to byte assertions:
  - direct TS uses one request with the provider UA;
  - gzip media and nested playlists;
  - redirect bodies that never end;
  - cross-origin Authorization is dropped while cookies are kept;
  - fMP4/BYTERANGE is now `unsupported` (inverted);
  - a delayed startup longer than 5 s still works;
  - the master playlist selects the highest compatible variant and never fetches the lower one;
  - a live leaf reloads the redirected URL and tolerates a 2.2 s segment gap;
  - TLS verification: rejection by default, opt-out works, and the setting is obeyed on redirect hops and nested playlists;
  - a remote playlist cannot read local media;
  - an encrypted leaf with an explicit IV and a relative key, using `Cryptodome.Cipher.AES` to encrypt the fixture;
  - failed opens redact credentials.

  Add new tests:
  - AES-128 with an implicit IV;
  - `#EXT-X-DISCONTINUITY` yields the marker;
  - an external audio group raises `unsupported`;
  - `close()` from another thread aborts a stalled body read in under 1 s;
  - a raw TS reconnect after the server drops the connection mid-stream.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint and mypy, commit (`feat: add raw TS and HLS sources for the live pipeline`).

---

### Task 6: Progressive loopback server (`services/live/server.py`)

**Owns requirements:** §8.4 (DNS-independent bind), §8.5 (Windows reset/abort semantics, silent handlers), §8.10 (stop under 2 s with half-sent headers), and Review Focus 2.

**Files:**
- Create: `services/live/server.py`
- Modify: `tests/http_fixtures.py`, so it imports `ThreadingLoopbackHTTPServer` from here
- Test: `tests/test_live_server.py`

**Interfaces:**
- `LoopbackHTTPServer` and `ThreadingLoopbackHTTPServer` ported **verbatim** from main `services/timeshift_source.py:27-37`, plus `daemon_threads = True` and `block_on_close = False`.
- `class StreamServer`:
  - `__init__(self)` generates `token = secrets.token_hex(16)`.
  - `start() -> str` returns the base URL `http://127.0.0.1:<port>/<token>`.
  - `add_session(session_id: str, ring: Ring, is_ended: Callable[[], bool])` and `remove_session(session_id)`.
  - `stream_url(session_id, seq) -> str`.
  - `stop()`.
- **Route:** `GET /<token>/<session>/stream/<seq>.ts`.
  - The token is compared with `secrets.compare_digest`. A bad token, an unknown session, or a seq evicted (older than the oldest) or not yet opened returns 404 with no body.
  - On success: status 200, `Content-Type: video/mp2t`, `Cache-Control: no-store`, `Connection: close`, no Content-Length. The body is streamed across segments `seq`, `seq+1`, …:
    - lease segment `n`;
    - for `n == seq`, send from offset 0 (with its PAT/PMT header); for later segments, send from `header_len`, so the byte stream stays the original;
    - read 64 KiB chunks through `ring.read`, writing outside any lock;
    - when caught up with an open segment, `ring.wait_for(segment, offset, 0.5)` and loop;
    - release, then move to `n+1` once `n` is complete;
    - if `n+1` is gone (evicted under a slow reader), end the response;
    - if the session ended and the last segment is fully sent, end the response.
  - Stop and session removal wake waiters and end responses.
- **Sockets:**
  - client sockets are tracked and shut down (`SHUT_RDWR`) before `server.shutdown()` (ported pattern);
  - header reads use a 2 s timeout, and body writes block without a timeout;
  - `BrokenPipeError`, `ConnectionResetError`, `ConnectionAbortedError` and `TimeoutError` are swallowed;
  - `log_message` and `handle_error` are no-ops.

- [ ] **Step 1: Write failing tests:**
  - **Bind:** binding with `socket.getfqdn` patched to raise `OSError` still works.
  - **Routes:** a wrong token returns 404; an evicted seq returns 404.
  - **Live edge:** a client reading from the oldest seq receives `header(seq) + data…` and later segments without their headers, so the bytes equal the input from the first keyframe plus one header. Appends made while the client waits arrive within 0.6 s.
  - **Ended session:** the response closes after the last segment.
  - **Slow reader:** a client that stops reading while the ring evicts its next segment has its response ended. The writer never blocks: appends keep their speed, measured as 50 appends taking under 0.5 s.
  - **Stop:** with a half-sent request header, it returns in under 2 s (port of main's `test_stop_interrupts_partial_local_http_headers…`).
  - **Concurrency:** two sessions on one server are served concurrently.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint and mypy, commit (`feat: add the progressive loopback stream server`).

---

### Task 7: Helper process (`services/live/worker.py`)

**Owns requirements:** §8.8 (credential hygiene, devnull fds), §8.9 (owner-death watchdog), and §8.10 (bounded per-session stop without killing the helper).

**Files:**
- Create: `services/live/worker.py`
- Test: `tests/test_live_worker.py`

**Interfaces:**
- `helper_main(conn: Connection, root: str) -> None` is the spawn target. It:
  1. points fds 1 and 2 at devnull (ported);
  2. starts the `_exit_with_owner` watchdog thread (ported verbatim, main 765–771);
  3. starts `StreamServer`;
  4. sends `("hello", base_url)`;
  5. loops on `conn.poll(0.1)`.
- **Commands (parent to helper):**
  - `("open", sid: str, url: str, headers: dict, verify_ssl: bool, ring: dict)`, where `ring` is `{"kind": "ram", "max_bytes": int}` or `{"kind": "disk", "root": str, "dir": str, "max_bytes": int}`;
  - `("close", sid)`;
  - `("shutdown",)`.
- **Messages (helper to parent):**
  - `("hello", base_url)`;
  - `("ready", sid)`: the first segment is open, so playback can start;
  - `("state", sid, snapshot: dict, first: int | None, additions: list[tuple[int, float, float, bool]])`. The snapshot contains `{"oldest", "newest", "open", "start", "end", "live_edge", "bytes", "ended"}`. `first` and `additions` are ported from main's delta encoding (448–465) and throttled to 0.25 s unless forced.
  - `("error", sid, kind, message)`;
  - `("ended", sid)`;
  - `("closed", sid)`.
  - `conn.send` is guarded by one lock.
- **Each session** runs in a thread: `open_source`, then `Segmenter`, then the ring and the `Timeline`.
  - A `Discontinuity` marker calls `segmenter.discontinuity()`.
  - `Unsupported` and `SourceError("unsupported")` produce `error kind="unsupported"`.
  - A `RingError` on the disk ring switches the session to a fresh `RamRing` and reports `("error", sid, "disk", msg)`, but keeps running (spec §9).
  - `close` sets the session stop event and calls `source.close()`. The session thread ends within 1 s and sends `closed`; its ring is torn down; the server removes the session.
  - A catch-all `BaseException` handler sends the fixed `"internal"` error.
- **On `shutdown`:** close every session, stop the server and exit.

- [ ] **Step 1: Write failing tests.** Run `helper_main` in a real `multiprocessing.get_context("spawn")` process against `tests/http_fixtures` sources:
  - The hello URL serves `/stream/<oldest>.ts` bytes for an opened session.
  - Two sessions run concurrently; closing one leaves the other streaming (ported intent of main `test_stopping_stalled_child_preserves_playing_sibling`).
  - Closing a session whose upstream is stalled returns `closed` in under 2 s, the helper pid is still alive, and `ps -o command= -p <pid>` (POSIX only; skip on Windows) contains no URL or "secret".
  - Unsupported input yields exactly one `error` with kind `unsupported`.
  - A finite source yields `ended`.
  - Owner death (port main `test_owner_death_interrupts_stalled_native_source` with the helper) makes the upstream socket see EOF or a reset.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint and mypy, commit (`feat: add the live pipeline helper process`).

---

### Task 8: Parent supervisor (`services/live/engine.py`)

**Owns requirements:** §8.1 (Windows pipe EOF from poll/recv, no duplicate error), §8.10 and §8.11 (bounded stop, no multiprocessing primitives from Qt), §8.21 (spawn, daemon, restart once, then fall back), and Review Focus 4.

**Files:**
- Create: `services/live/engine.py`
- Test: `tests/test_live_engine.py`

**Interfaces:**
- `class LiveEngine(QObject)`, constructed as `__init__(self, root_dir: str, parent=None)`. It has:
  - signals `available(bool)`, `sessionReady(str)`, `sessionUpdated(str, dict)`, `sessionFailed(str, str, str)` (sid, kind, message), `sessionEnded(str)`, `sessionClosed(str)`;
  - `start()`, which spawns the helper in the background; `available(True)` fires on `hello`;
  - `is_available() -> bool`;
  - `open_session(url, *, headers, verify_ssl, disk: bool, max_bytes: int) -> str`. The sid is `secrets.token_hex(8)`; for disk sessions the parent picks `dir = ".qitv-timeshift-" + token_hex(16)` and remembers it.
  - `close_session(sid)`;
  - `spans(sid) -> list[Span]`, reconstructed from the deltas;
  - `snapshot(sid) -> dict`;
  - `stream_url(sid, seq) -> str`;
  - `shutdown()`.
- **Supervisor:**
  - a `QThread` subclass runs the receive loop, ported from main `TimeshiftRecorder.run` (903–985), with `EOFError, BrokenPipeError` handled on **both** `poll()` and `recv()`, and a `failed` flag preventing a duplicate "stopped unexpectedly" error;
  - commands from the GUI go into a `queue.Queue` and are sent by the supervisor thread;
  - messages turn into Qt signals, delivered queued to the GUI thread;
  - it uses `multiprocessing.get_context("spawn")` with `context.Pipe(duplex=True)` (via the context object, so the ported Windows-EOF test can patch it) and `daemon=True`.
- **Helper death:**
  - emit `sessionFailed(sid, "internal", "The live buffer stopped unexpectedly.")` for each open session;
  - run `cleanup_session_dir` for each disk session;
  - restart once, and on the restart's `hello` reopen the sessions with the same arguments;
  - a second death in the same app run emits `available(False)`, which is permanent for this run.
- **Shutdown:** send `("shutdown",)`, join for 0.5 s, then terminate and join 0.5 s, then kill and join 0.5 s (ported escalation); clean every disk session dir; retire the QThread through `ThreadCleanup`.

- [ ] **Step 1: Write failing tests**, using a real QApplication, real spawn and fixture sources:
  - `start` emits `available(True)`; `open_session`, then `sessionReady`, then `sessionUpdated` with growing spans; `stream_url` serves bytes.
  - Killing the helper (`os.kill(pid, SIGKILL)`, or `terminate` on Windows) restarts it and reopens the session; a second kill emits `available(False)`.
  - Port main `test_closed_recording_pipe_does_not_repeat_reported_source_failure` **verbatim in spirit**: patch `context.Pipe` so `poll()` raises `BrokenPipeError` after the child reported an error; exactly one `sessionFailed` is emitted.
  - `shutdown` returns within 2 s and leaves no helper process (`multiprocessing.active_children()` empty) and no session dir.
  - Credentials are not in the helper's argv.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint and mypy, commit (`feat: supervise the live helper from the GUI process`).
