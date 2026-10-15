import unittest

from services.vlc_profile import DEFAULT_USER_AGENT, PlayKind, classify, media_options


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

    def test_common_options(self):
        for kind in PlayKind:
            options = media_options(kind)
            reconnect = "false" if kind is PlayKind.LIVE_PIPELINE else "true"
            self.assertIn(f":http-reconnect={reconnect}", options)
            self.assertIn(":avcodec-hw=any", options)


if __name__ == "__main__":
    unittest.main()
