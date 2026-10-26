import unittest

from services.vlc_profile import (
    DEFAULT_USER_AGENT,
    STB_USER_AGENT,
    PlayKind,
    classify,
    media_options,
    user_agent_for,
)


def option(options, name):
    matches = [o for o in options if o.startswith(f":{name}=") or o == f":{name}"]
    return (
        matches[0].split("=", 1)[1]
        if matches and "=" in matches[0]
        else (matches[0] if matches else None)
    )


class ClassifyTests(unittest.TestCase):
    def test_containers_are_vod_files(self):
        for url in ("http://h/movie/u/p/1.mkv", "http://h/a.MP4?token=1", "http://h/x.webm"):
            self.assertIs(classify(url, True), PlayKind.VOD_FILE, url)

    def test_hints(self):
        self.assertIs(classify("http://h/live/u/p/1.ts", True), PlayKind.LIVE)
        self.assertIs(classify("http://h/series/1.m3u8", False), PlayKind.VOD_STREAM)
        self.assertIs(classify("http://h/stream", None), PlayKind.UNKNOWN)

    def test_a_recording_on_the_disk_seeks_like_a_film(self):
        self.assertIs(
            classify("file:///rec/News%202026-10-19%2021.30.ts", False), PlayKind.VOD_FILE
        )
        self.assertIs(classify("http://h/movie/u/p/1.ts", False), PlayKind.VOD_STREAM)


class PipelineProfileTests(unittest.TestCase):
    def test_loopback_pipeline_options(self):
        options = media_options(PlayKind.LIVE_PIPELINE)
        for expected in (
            ":network-caching=300",
            ":live-caching=300",
            ":clock-jitter=0",
            ":ts-trust-pcr=0",
            ":http-reconnect=false",
        ):
            self.assertIn(expected, options)
        self.assertNotIn(":http-reconnect=true", options)
        self.assertNotIn(":no-audio-time-stretch", options)
        self.assertIsNone(option(options, "adaptive-livedelay"))

    def test_latency_setting_does_not_change_the_pipeline_profile(self):
        self.assertEqual(
            media_options(PlayKind.LIVE_PIPELINE, latency="stable"),
            media_options(PlayKind.LIVE_PIPELINE, latency="low"),
        )

    def test_direct_live_keeps_reconnect(self):
        self.assertIn(":http-reconnect=true", media_options(PlayKind.LIVE))


class MediaOptionsTests(unittest.TestCase):
    def test_removed_v1_options(self):
        for kind in PlayKind:
            options = media_options(kind)
            self.assertNotIn(":adaptive-logic=lowest", options)
            self.assertNotIn(":no-audio-time-stretch", options)

    def test_auto_latency_plays_a_direct_channel_as_normal_does(self):
        self.assertEqual(
            media_options(PlayKind.LIVE, latency="auto"),
            media_options(PlayKind.LIVE, latency="normal"),
        )

    def test_live_latency_drives_caching(self):
        low = media_options(PlayKind.LIVE, latency="low")
        stable = media_options(PlayKind.LIVE, latency="stable")
        self.assertEqual(option(low, "network-caching"), "300")
        self.assertEqual(option(stable, "network-caching"), "3000")
        self.assertEqual(option(low, "adaptive-livedelay"), "3000")
        self.assertEqual(option(media_options(PlayKind.LIVE), "network-caching"), "1000")
        self.assertEqual(
            option(media_options(PlayKind.LIVE, latency="??"), "network-caching"), "1000"
        )

    def test_unknown_is_live_like_but_never_below_1500ms(self):
        options = media_options(PlayKind.UNKNOWN, latency="low")
        self.assertEqual(option(options, "network-caching"), "1500")
        self.assertIn(":clock-jitter=0", options)

    def test_vod_file_profile(self):
        options = media_options(PlayKind.VOD_FILE)
        self.assertEqual(option(options, "network-caching"), "4000")
        self.assertIn(":input-fast-seek", options)
        self.assertNotIn(":clock-jitter=0", options)

    def test_user_agent(self):
        self.assertIn(f":http-user-agent={DEFAULT_USER_AGENT}", media_options(PlayKind.LIVE))
        self.assertIn(
            ":http-user-agent=Custom/1", media_options(PlayKind.LIVE, user_agent="Custom/1")
        )

    def test_an_stb_portals_streams_are_asked_for_as_its_box(self):  # 2.8
        self.assertIn("MAG200 stbapp", STB_USER_AGENT)
        self.assertEqual(user_agent_for({}, "STB"), STB_USER_AGENT)
        self.assertEqual(user_agent_for({}, "stb"), STB_USER_AGENT)
        self.assertEqual(user_agent_for({"user_agent": "Box/2"}, "STB"), "Box/2")  # its own
        for kind in ("XTREAM", "M3UPLAYLIST", "JELLYFIN", ""):
            self.assertEqual(user_agent_for({}, kind), DEFAULT_USER_AGENT)

    def test_a_size_cap_reaches_vlcs_own_adaptive_streams(self):  # 2.8, the live ⚙
        for kind in (PlayKind.LIVE, PlayKind.UNKNOWN):
            self.assertIn(":adaptive-maxheight=720", media_options(kind, max_height=720))
            self.assertNotIn(
                "adaptive-maxheight",
                " ".join(media_options(kind) + media_options(kind, max_height=0)),
            )
        # The loopback stream is one transport stream: nothing to pick from.
        pipeline = media_options(PlayKind.LIVE_PIPELINE, max_height=720)
        self.assertNotIn("adaptive-maxheight", " ".join(pipeline))

    def test_referrer(self):
        # A channel's M3U http-referrer goes with VLC's own requests; without one, none.
        for kind in PlayKind:
            self.assertFalse(any(o.startswith(":http-referrer") for o in media_options(kind)))
        options = media_options(PlayKind.LIVE, referrer="https://site.test/live/")
        self.assertIn(":http-referrer=https://site.test/live/", options)
        self.assertEqual(
            [o for o in options if not o.startswith(":http-referrer")], media_options(PlayKind.LIVE)
        )

    def test_common_options(self):
        for kind in PlayKind:
            options = media_options(kind)
            reconnect = "false" if kind is PlayKind.LIVE_PIPELINE else "true"
            self.assertIn(f":http-reconnect={reconnect}", options)
            # The decoder is the instance's (Settings): a media option would override it.
            self.assertFalse([o for o in options if o.startswith(":avcodec-hw")])


if __name__ == "__main__":
    unittest.main()
