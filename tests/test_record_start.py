"""The recorded start's analysis (scripts/record_start.py): the verdicts on synthetic window and
frame timelines, and the window-tree reading. The recording itself needs a Linux box with Xvfb
and runs by hand."""

import time
import unittest
from unittest import mock

from scripts import record_start
from scripts.record_start import (
    Events,
    Sample,
    analyse,
    classify,
    parse_tree,
)

T0 = 1000.0


def smp(t, splash=False, still=False, mains=(), opacity=None, pos=(520, 280)):
    return Sample(
        t=T0 + t,
        splash=splash,
        still=still,
        mains=tuple(mains),
        opacity=opacity,
        splash_pos=pos if splash else None,
    )


def good_start():
    """Splash from 0.5 s, the window from 1.1 s fading to 2.1 s, the splash gone with it."""
    out = [smp(i / 10) for i in range(5)]
    out += [smp(i / 10, splash=True) for i in range(5, 11)]
    out += [smp(i / 10, splash=True, mains=["a"], opacity=(i - 10) / 10) for i in range(11, 21)]
    out += [smp(i / 10, mains=["a"]) for i in range(21, 40)]
    return out


def by(report):
    return {c.name: c for c in report.checks}


class AnalyseTest(unittest.TestCase):
    def test_a_good_start_passes(self):
        report = analyse(good_start(), Events(t_exec=T0))
        self.assertTrue(report.ok, [c for c in report.checks if not c.ok])
        self.assertEqual(report.timeline["splash_first"], 0.5)

    def test_no_splash_fails(self):
        samples = [s for s in good_start() if not s.splash]
        self.assertFalse(by(analyse(samples, Events(t_exec=T0)))["splash_shown"].ok)

    def test_a_late_splash_fails(self):
        samples = [smp(i / 10) for i in range(20)]
        samples += [smp(i / 10, splash=True, mains=["a"]) for i in range(20, 30)]
        self.assertFalse(by(analyse(samples, Events(t_exec=T0)))["splash_shown"].ok)

    def test_the_still_counts_as_the_splash(self):
        samples = [smp(i / 10, still=True) for i in range(1, 11)] + good_start()[10:]
        report = analyse(samples, Events(t_exec=T0, onefile=True))
        self.assertTrue(by(report)["splash_shown"].ok)

    def test_a_window_that_closes_and_opens_fails(self):
        samples = good_start()
        for s in samples:
            if 1.5 <= s.t - T0 < 1.7:
                s.mains = ()
                s.splash = False
        report = analyse(samples, Events(t_exec=T0))
        self.assertFalse(by(report)["main_once"].ok)
        self.assertFalse(by(report)["count_no_dip"].ok)

    def test_a_listing_blip_while_the_window_is_transparent_is_not_an_appearance(self):
        samples = good_start()
        for s in samples:
            if s.mains and s.t - T0 < 1.4:
                s.opacity = 0.0  # on the list but not on screen
            if 1.2 <= s.t - T0 < 1.3:
                s.mains = ()  # one sample missed it
        self.assertTrue(by(analyse(samples, Events(t_exec=T0)))["main_once"].ok)

    def test_a_second_window_id_fails(self):
        samples = good_start()
        for s in samples:
            if s.t - T0 > 3:
                s.mains = ("b",)
        self.assertFalse(by(analyse(samples, Events(t_exec=T0)))["main_once"].ok)

    def test_the_overlay_is_not_a_second_window(self):
        samples = good_start()
        for s in samples:
            if s.mains:
                s.overlays = ("o",)
        self.assertTrue(analyse(samples, Events(t_exec=T0)).ok)

    def test_a_splash_that_stays_fails(self):
        samples = good_start()
        for s in samples:
            if 2.1 <= s.t - T0 < 3.0:
                s.splash = True
        self.assertFalse(by(analyse(samples, Events(t_exec=T0)))["splash_gone"].ok)

    def test_an_empty_frame_between_the_mark_and_the_window_fails(self):
        times = [T0 + i / 30 for i in range(90)]
        diffs = [0.0 if t < T0 + 0.5 else 5.0 for t in times]
        for i, t in enumerate(times):  # one empty frame at 1.0 s
            if abs(t - (T0 + 1.0)) < 0.01:
                diffs[i] = 0.0
        ev = Events(t_exec=T0, frame_times=times, frame_diffs=diffs)
        report = analyse(good_start(), ev)
        self.assertFalse(by(report)["frames_no_gap"].ok)
        ev.frame_diffs = [0.0 if t < T0 + 0.5 else 5.0 for t in times]
        self.assertTrue(by(analyse(good_start(), ev))["frames_no_gap"].ok)

    def test_reduced_motion_splash_must_not_glide(self):
        samples = good_start()
        report = analyse(samples, Events(t_exec=T0, reduced=True))
        self.assertTrue(by(report)["no_glide"].ok)
        for s in samples:
            if s.splash and s.t - T0 > 0.8:
                s.splash_pos = (520, 300)
        report = analyse(samples, Events(t_exec=T0, reduced=True))
        self.assertFalse(by(report)["no_glide"].ok)


class GlyphTest(unittest.TestCase):
    def levels(self, out_from=None, out_to=None):
        times = [T0 + i / 30 for i in range(150)]
        levels = [60.0 if not (out_from and out_from <= t - T0 < out_to) else 28.0 for t in times]
        return times, levels

    def samples(self):
        # the still from 0.1 s, the window at opacity > 0 from 3.0 s
        out = [smp(i / 10, still=True) for i in range(1, 30)]
        out += [smp(i / 10, still=True, mains=["a"], opacity=0.1 * (i - 29)) for i in range(30, 40)]
        out += [smp(i / 10, mains=["a"]) for i in range(40, 50)]
        return out

    def ev(self, **levels):
        times, lv = self.levels(**levels)
        return Events(t_exec=T0, onefile=True, frame_times=times, glyph_levels=lv)

    def test_a_glyph_lit_to_the_open_passes(self):
        report = analyse(self.samples(), self.ev())
        self.assertTrue(by(report)["glyph_held"].ok)

    def test_a_glyph_that_goes_out_and_comes_back_fails(self):
        report = analyse(self.samples(), self.ev(out_from=1.5, out_to=2.2))
        self.assertFalse(by(report)["glyph_held"].ok)

    def test_the_open_may_dim_it(self):
        ev = self.ev(out_from=3.0, out_to=4.0)  # from the open on: not judged
        self.assertTrue(by(analyse(self.samples(), ev))["glyph_held"].ok)

    def test_a_one_file_start_that_could_not_be_measured_fails_with_its_reason(self):
        ev = self.ev()
        ev.glyph_levels = []  # no frames
        check = by(analyse(self.samples(), ev))["glyph_held"]
        self.assertFalse(check.ok)
        self.assertIn("no frames", check.detail)
        ev = self.ev()  # no still seen
        none = [s for s in self.samples() if not s.still and not s.splash]
        check = by(analyse(none, ev))["glyph_held"]
        self.assertFalse(check.ok)
        self.assertIn("not measured", check.detail)
        ev = self.ev()  # too few frames between the still and the open
        ev.frame_times, ev.glyph_levels = ev.frame_times[:16], ev.glyph_levels[:16]
        check = by(analyse(self.samples(), ev))["glyph_held"]
        self.assertFalse(check.ok)
        self.assertIn("frame(s)", check.detail)

    def test_a_source_start_is_not_judged(self):
        ev = self.ev(out_from=1.5, out_to=2.2)
        ev.onefile = False
        self.assertNotIn("glyph_held", by(analyse(self.samples(), ev)))


class SamplerTest(unittest.TestCase):
    """The sampler thread, with a fake `xwininfo` (no X server)."""

    def run_sampler(self, answers):
        calls = []

        def fake(cmd, env, timeout=2.0):
            calls.append(cmd[0])
            if cmd[0] == "xwininfo":
                return answers.pop(0) if answers else TREE
            if cmd[0] == "xdotool":
                return "8388615 6291463"
            return ""

        sampler = record_start.Sampler({})
        with mock.patch.object(record_start, "_run", fake):
            sampler.start()
            deadline = time.time() + 5
            while len(sampler.samples) < 2 and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(sampler.is_alive())
            sampler.stop()
        self.assertFalse(sampler.is_alive())
        return sampler

    def test_an_empty_tree_does_not_end_the_sampling(self):
        sampler = self.run_sampler(["", "", ""])  # xwininfo failed three times, then answers
        self.assertGreaterEqual(len(sampler.samples), 2)
        self.assertTrue(all(s.splash for s in sampler.samples))  # nothing recorded from a blank

    def test_it_stops_cleanly_and_does_not_shadow_the_threads_own_stop(self):
        sampler = self.run_sampler([])
        self.assertNotIn("_stop", vars(sampler))


class FramesTest(unittest.TestCase):
    def test_no_frames_is_the_failure(self):
        with self.assertRaises(SystemExit) as caught:
            record_start.require_frames([])
        self.assertIn("no frames", str(caught.exception))
        record_start.require_frames([(0.0, b"")])


def handover_timeline(splash_until, new_main=False):
    out = [smp(i / 10, mains=["a"]) for i in range(0, 50)]  # the running window
    t2 = 5.0
    for i in range(50, 80):
        t = i / 10
        s = smp(t, splash=t2 + 0.3 <= t < splash_until, mains=["a"])
        if new_main and t > 6:
            s.mains = ("a", "b")
        out.append(s)
    return out


class HandoverTest(unittest.TestCase):
    def ev(self):
        return Events(t_exec=T0, t_second=T0 + 5.0, t_second_exit=T0 + 5.8)

    def test_a_splash_that_goes_with_the_answer_passes(self):
        report = analyse(handover_timeline(5.9) + [], self.ev(), "handover")
        self.assertTrue(report.ok, [c for c in report.checks if not c.ok])

    def test_no_splash_at_all_passes(self):
        self.assertTrue(analyse(handover_timeline(0), self.ev(), "handover").ok)

    def test_a_splash_left_behind_fails(self):
        report = analyse(handover_timeline(7.5), self.ev(), "handover")
        self.assertFalse(by(report)["splash_gone"].ok)

    def test_a_second_window_fails(self):
        report = analyse(handover_timeline(5.9, new_main=True), self.ev(), "handover")
        self.assertFalse(by(report)["main_once"].ok)


TREE = """
  Root window id: 0x1bd (the root window) "x"
     0x800007 "qitv-splash": ("qitv-splash" "qitv-splash")  240x240+0+0  +520+280
     0x200176 (has no name): ()  240x240+520+280  +520+280
        0x600007 "QiTV": ("main.py" "main.py")  1280x720+1+20  +0+80
        0x600019 "main.py": ("main.py" "main.py")  1280x693+0+0  +0+107
           0x60000e "main.py": ("main.py" "main.py")  1280x693+0+27  +0+107
        0x700001 (has no name): ()  100x100+0+0  +5+5
"""


class TreeTest(unittest.TestCase):
    def test_it_reads_the_windows(self):
        tree = parse_tree(TREE)
        names = [w["name"] for w in tree]
        self.assertIn("qitv-splash", names)
        self.assertIn("", names)
        splash = next(w for w in tree if w["name"] == "qitv-splash")
        self.assertEqual((splash["w"], splash["x"], splash["y"]), (240, 520, 280))

    def test_it_tells_the_windows_apart(self):
        tree = parse_tree(TREE)
        ids = {w["id"] for w in tree}
        s = classify(tree, ids, 0.5)
        self.assertTrue(s.splash)
        self.assertEqual(s.mains, ("0x600007",))
        self.assertEqual(s.overlays, ("0x600019",))  # not the deeper child
        self.assertTrue(s.still)
        self.assertEqual(s.splash_pos, (520, 280))
        self.assertFalse(classify(tree, {"0x600007"}, None).splash)


if __name__ == "__main__":
    unittest.main()
