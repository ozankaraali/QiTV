"""Makes the fragmented MP4 (CMAF) HLS fixtures of tests/test_fmp4.py from ffmpeg's test
sources (a test pattern and a beeping sine; nothing recorded). Local only: needs ffmpeg with
libx264 and libx265, and macOS's AudioToolbox for HE-AAC.

    uv run python tests/fixtures/fmp4/make_fixtures.py
    QT_QPA_PLATFORM=offscreen PYTHONPATH=.:tests uv run python -m tests.test_fmp4 --golden

The second command rewrites golden.json once the remuxed fixtures decode as ffmpeg decodes
the fMP4 (`FfmpegTests`).
"""

import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO = ["-f", "lavfi", "-i", "testsrc2=size=128x72:rate=25"]
AUDIO = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:beep_factor=4"]
HLS = [
    "-f", "hls", "-hls_time", "2", "-hls_segment_type", "fmp4", "-hls_playlist_type", "vod",
    "-hls_flags", "independent_segments",
]  # fmt: skip
SINGLE = ["-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "seg%d.m4s"]
X264 = [
    "-c:v", "libx264", "-preset", "veryfast", "-bf", "2", "-g", "25", "-keyint_min", "25",
    "-sc_threshold", "0", "-crf", "38", "-pix_fmt", "yuv420p",
]  # fmt: skip


def make(name: str, args: list[str]) -> None:
    folder = os.path.join(HERE, name)
    os.makedirs(folder, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], cwd=folder, check=True)


# H.264 with B-frames (ffmpeg writes an edit list) and AAC LC, in one fMP4.
make("avc-aac", [
    *VIDEO, *AUDIO, "-t", "6", *X264, "-c:a", "aac", "-b:a", "24k", "-ac", "2",
    *HLS, *SINGLE, "index.m3u8",
])  # fmt: skip
# HEVC (hvc1) with negative composition offsets (trun v1) and E-AC-3, in one fMP4.
make("hevc-eac3", [
    *VIDEO, *AUDIO, "-t", "6", "-c:v", "libx265", "-tag:v", "hvc1",
    "-x265-params", "bframes=2:keyint=25:min-keyint=25:scenecut=0:log-level=error",
    "-crf", "38", "-pix_fmt", "yuv420p", "-c:a", "eac3", "-b:a", "48k", "-ac", "2",
    *HLS, "-hls_segment_options", "movflags=+negative_cts_offsets", *SINGLE, "index.m3u8",
])  # fmt: skip
# H.264 and AC-3, in one fMP4.
make("avc-ac3", [
    *VIDEO, *AUDIO, "-t", "4", *X264, "-c:a", "ac3", "-b:a", "64k", "-ac", "2",
    *HLS, *SINGLE, "index.m3u8",
])  # fmt: skip
# Separate renditions under a master: H.264 (avc3: parameter sets in band) and HE-AAC.
make("split", [
    *VIDEO, *AUDIO, "-t", "6", *X264, "-tag:v", "avc3", "-c:a", "aac_at", "-profile:a", "4",
    "-b:a", "32k", "-ac", "2", "-map", "0:v", "-map", "1:a", *HLS,
    "-var_stream_map", "v:0,agroup:aud a:0,agroup:aud,default:yes",
    "-master_pl_name", "master.m3u8", "-hls_fmp4_init_filename", "init.mp4",
    "-hls_segment_filename", "%v/seg%d.m4s", "%v/index.m3u8",
])  # fmt: skip
