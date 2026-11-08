"""The Test model list's beam + LM reading of our Turkish Moonshine: the n-gram reader
(services.captions.ngram), the best tokens of a row of logits and the beam search's rules
(services.captions.moon_beam). With QITV_TR_MOONSHINE naming a model folder and sherpa_onnx
importable, the model itself: a beam of one is the greedy decode, and a beam + LM reading
(QITV_TR_LM naming a .qng file) gives text. QITV_TR_MOONSHINE_WAV may name 16 kHz WAV clips
(os.pathsep between them); else a made-up sound is used."""

from array import array
import math
import os
from pathlib import Path
import random
import tempfile
import unittest

from services.captions import ngram
from services.captions.moon_beam import (
    BANNED,
    LN10,
    LmScores,
    Settings,
    best_tokens,
    search,
)
from services.captions.moon_ort import EOS

MODEL = os.environ.get("QITV_TR_MOONSHINE", "")
LM_FILE = os.environ.get("QITV_TR_LM", "")
CLIPS = os.environ.get("QITV_TR_MOONSHINE_WAV", "")

# A tiny ARPA over piece ids: <s> 1, </s> 2, <unk>; words 10, 11, 12.
ARPA = """\\data\\
ngram 1=6
ngram 2=4
ngram 3=2

\\1-grams:
-99\t<s>\t-0.5
-1.0\t</s>
-2.0\t<unk>
-0.7\t10\t-0.25
-0.9\t11\t-0.4
-1.3\t12\t-0.1

\\2-grams:
-0.3\t<s> 10\t-0.2
-0.6\t10 11\t-0.15
-0.45\t11 </s>
-0.8\t11 12

\\3-grams:
-0.1\t<s> 10 11
-0.2\t10 11 12

\\end\\
"""


def reference(grams, context, token):
    """A back-off n-gram's log10 p(token | context), straight from the ARPA's numbers."""
    probs = {ids: p for n in grams for ids, p, _ in grams[n]}
    backs = {ids: b for n in grams for ids, _, b in grams[n]}
    if (token,) not in probs:
        token = ngram.UNK
    order = max(grams)
    context = tuple(context[-(order - 1) :])
    total = 0.0
    for n in range(len(context), -1, -1):
        gram = context[len(context) - n :] + (token,)
        if gram in probs:
            return total + probs[gram]
        total += backs.get(gram[:-1], 0.0) if gram[:-1] else 0.0
    return total


class NgramTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        arpa = self.folder / "lm.arpa"
        arpa.write_text(ARPA, encoding="utf-8")
        self.path = self.folder / "lm.qng"
        ngram.write_arpa(arpa, str(self.path))
        self.grams = ngram.read_arpa(ARPA.splitlines(keepends=True))
        self.lm = ngram.NgramLM(self.path)

    def tearDown(self):
        self.lm.close()
        self._tmp.cleanup()

    def test_scores_back_off_as_the_arpa_says(self):
        cases = [
            ((1,), 10),  # a bigram
            ((1, 10), 11),  # a trigram
            ((10, 11), 12),  # a trigram
            ((1, 10), 12),  # backs off twice: <s> 10's weight, then 10's
            ((11,), 2),  # the end, as a bigram
            ((12,), 2),  # the end after 12: its back-off and the unigram
            ((1, 12), 10),  # a context the LM never saw: unigram with 12's back-off
            ((10, 11), 99),  # an unknown id: <unk>
            ((), 11),  # no context
        ]
        for context, token in cases:
            score, _after = self.lm.score(context, token)
            # 255 steps over each order's range: a code is off by at most half a step
            self.assertAlmostEqual(score, reference(self.grams, context, token), delta=0.02)

    def test_the_context_after_keeps_the_last_ids_and_unknowns_become_unk(self):
        self.assertEqual(self.lm.order, 3)
        self.assertEqual(self.lm.score((1, 10), 11)[1], (10, 11))
        self.assertEqual(self.lm.score((1,), 10)[1], (1, 10))
        self.assertEqual(self.lm.score((10, 11), 99)[1], (11, ngram.UNK))
        self.assertTrue(self.lm.known(12))
        self.assertFalse(self.lm.known(99))

    def test_keys_pack_ids_and_mix_a_fifth(self):
        self.assertEqual(ngram.key([5]), 5)
        self.assertEqual(ngram.key([1, 2]), (1 << 14) | 2)
        five = ngram.key([1, 2, 3, 4, 5])
        self.assertNotEqual(five, ngram.key([1, 2, 3, 4, 6]))
        self.assertLess(five, 1 << 64)

    def test_a_file_that_is_not_one_is_refused(self):
        bad = self.folder / "bad.qng"
        bad.write_bytes(b"QNG1" + (3).to_bytes(4, "little") + b"\x00" * 16)
        with self.assertRaises(ValueError):
            ngram.NgramLM(bad)
        other = self.folder / "other.qng"
        other.write_bytes(b"ARPA" + b"\x00" * 100)
        with self.assertRaises(ValueError):
            ngram.NgramLM(other)

    def test_scores_in_nats_are_remembered(self):
        scores = LmScores(self.lm, limit=2)
        nats, after = scores.score(LmScores.start(), 10)
        self.assertAlmostEqual(nats, self.lm.score((1,), 10)[0] * LN10)
        self.assertEqual(after, (1, 10))
        self.assertIs(scores.score((1,), 10), scores.score((1,), 10))
        scores.score((1,), 11)
        scores.score((1,), 12)  # past the limit: it starts again
        self.assertLessEqual(len(scores._cache), 2)


def log_softmax(row):
    top = max(row)
    norm = top + math.log(sum(math.exp(v - top) for v in row))
    return [v - norm for v in row]


class BestTokensTests(unittest.TestCase):
    def test_the_best_by_log_softmax_over_the_whole_row_never_a_banned_one(self):
        rng = random.Random(7)
        for width in (5, 40, 128, 129, 1000, 10623):
            values = [rng.gauss(0.0, 3.0) for _ in range(width)]
            values[0] = values[3] = 50.0  # padding and the unknown piece: the best, banned
            expected_lp = log_softmax(values)
            got = best_tokens(array("f", values), 8)
            stored = list(array("f", values))  # (float32, as the row)
            allowed = [t for t in range(width) if t not in BANNED]
            want = sorted(allowed, key=lambda t: (-stored[t], t))[:8]
            self.assertEqual([t for t, _ in got], want, width)
            for token, logprob in got:
                self.assertAlmostEqual(logprob, expected_lp[token], delta=1e-4)

    def test_of_equals_the_lower_id_and_a_peaked_row(self):
        row = array("f", [0.0] * 600)
        row[300] = row[200] = 5.0
        row[599] = 30.0
        got = best_tokens(row, 3)
        self.assertEqual([t for t, _ in got], [599, 200, 300])
        self.assertAlmostEqual(math.exp(got[0][1]), 1.0, delta=1e-6)

    def test_more_than_the_row_has(self):
        got = best_tokens(array("f", [1.0, 2.0, 3.0, 4.0, 5.0]), 8)
        self.assertEqual([t for t, _ in got], [4, 2])  # 0, 1 and 3 are banned


class FakeSteps:
    """A model as a table: the logits after a history of tokens (a function of it), each
    state the history itself; what is made and released is counted."""

    def __init__(self, logits, width=20, fail_at=None):
        self.logits = logits
        self.width = width
        self.fail_at = fail_at  # the call that fails (onnxruntime's error)
        self.alive = set()
        self.calls = 0
        self.first = self._row(())
        self.start = self._keep(())

    def _row(self, history):
        row = array("f", [-10.0] * self.width)
        for token, value in self.logits(history).items():
            row[token] = value
        return row

    def _keep(self, history):
        state = (len(self.alive), history, object())
        self.alive.add(state)
        return state

    def step(self, state, token):
        assert state in self.alive, "a released state was stepped from"
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("onnxruntime")
        history = state[1] + (token,)
        return self._keep(history), self._row(history)

    def release(self, state):
        self.alive.discard(state)


def table(rows):
    """Logits by history; a history not in `rows` ends (EOS far ahead)."""
    return lambda history: rows.get(history, {EOS: 10.0})


class SearchTests(unittest.TestCase):
    def test_a_close_second_the_lm_prefers_wins_and_without_it_the_first(self):
        # the model: 10 a little before 11, then the end; the LM: 11 far likelier after <s>
        steps = lambda: FakeSteps(table({(): {10: 2.0, 11: 1.8}}))  # noqa: E731

        class Lm:
            order = 2

            def score(self, context, token):
                return {10: -3.0, 11: -0.2}.get(token, -0.5), (token,)

        alone = search(steps(), 10, Settings())
        fused = search(steps(), 10, Settings(), LmScores(Lm()))
        self.assertEqual((alone.tokens, fused.tokens), ([10], [11]))
        self.assertTrue(alone.eos and fused.eos)
        self.assertEqual(len(alone.logprobs), 1)

    def test_only_the_model_s_8_best_reach_the_lm(self):
        logits = {10 + i: 5.0 - 0.1 * i for i in range(9)}  # token 18 is the 9th

        class Lm:
            order = 2

            def score(self, context, token):
                return (0.0 if token == 18 else -20.0), (token,)

        found = search(FakeSteps(table({(): logits}), width=30), 5, Settings(), LmScores(Lm()))
        self.assertNotIn(18, found.tokens)

    def test_an_end_far_below_the_best_token_is_no_end(self):
        # after 10 the end is among the 8 best, 2.0 below the best: no end there, though it would
        # score best (after 12 the model is unsure of everything: the end and 15 others alike)
        rows = {
            (): {10: 5.0},
            (10,): {12: 4.0, EOS: 2.0},
            (10, 12): {token: 3.0 for token in [EOS, *range(13, 28)]},
        }
        found = search(FakeSteps(table(rows), width=30), 10, Settings(length_bonus=0.0))
        self.assertEqual(found.tokens, [10, 12])
        early = Settings(length_bonus=0.0, eos_gap=3.0)
        self.assertEqual(search(FakeSteps(table(rows), width=30), 10, early).tokens, [10])

    def test_a_looping_hypothesis_is_dropped_not_kept(self):
        # the model repeats 10 for ever, with 11 then the end a little behind at each step
        def logits(history):
            if history and history[-1] == 11:
                return {EOS: 9.0}
            return {10: 3.0, 11: 2.5}

        found = search(FakeSteps(logits), 40, Settings(length_bonus=0.0))
        self.assertTrue(found.eos)
        self.assertEqual(found.tokens[-1], 11)
        self.assertLess(found.tokens.count(10), 4)  # four 10s in a row are a loop

    def test_the_cap_ends_it_and_each_state_is_released(self):
        steps = FakeSteps(lambda history: {10 + len(history) % 5: 3.0, 20: 1.0}, width=30)
        found = search(steps, 6, Settings())
        self.assertEqual(len(found.tokens), 6)
        self.assertFalse(found.eos)
        self.assertEqual(steps.alive, set())

    def test_a_failing_step_releases_what_it_made(self):
        steps = FakeSteps(lambda history: {10: 3.0, 11: 2.9, 12: 2.8}, fail_at=7)
        with self.assertRaises(RuntimeError):
            search(steps, 20, Settings())
        self.assertEqual(steps.alive, set())

    def test_four_hypotheses_go_on_each_with_its_own_call(self):
        steps = FakeSteps(lambda history: {10 + i: 3.0 - i * 0.01 for i in range(6)})
        search(steps, 3, Settings())
        self.assertEqual(steps.calls, 4 + 4)  # two steps of four (the third reaches the cap)


@unittest.skipUnless(MODEL and os.path.isdir(MODEL), "QITV_TR_MOONSHINE names no model folder")
class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import sherpa_onnx  # noqa: F401  (its onnxruntime library is the one used)
        except ImportError:
            raise unittest.SkipTest("sherpa_onnx (and its onnxruntime library) not importable")
        from test_captions_moon_ort import made_up_sound, read_clip

        from services.captions.moon_beam import BeamReader

        cls.reader = BeamReader(MODEL, LM_FILE or None, threads=2)
        paths = [p for p in CLIPS.split(os.pathsep) if p]
        cls.clips = [read_clip(p) for p in paths] or [made_up_sound()]

    def test_a_beam_of_one_alone_is_the_greedy_decode(self):
        model = self.reader.model
        one_alone = BeamReaderSettings(self.reader, Settings(width=1, length_bonus=0.0))
        for samples in self.clips:
            encoded = model.encode(samples)
            try:
                greedy = model.decode(encoded)
                with one_alone:
                    one = self.reader.decode(encoded)
            finally:
                encoded.release()
            if not greedy.looped:  # (a loop is dropped by the beam, kept by greedy)
                self.assertEqual(one.tokens, greedy.tokens)
                for mine, theirs in zip(one.logprobs, greedy.logprobs):
                    self.assertAlmostEqual(mine, theirs, delta=1e-4)

    def test_a_reading_gives_text_and_silence_none(self):
        self.reader.warm()
        for samples in self.clips:
            self.assertIsInstance(self.reader.transcribe(samples), str)
        self.assertEqual(self.reader.transcribe(array("f", bytes(4 * 16000))), "")


class BeamReaderSettings:
    """A reader's settings for a while, with no n-gram."""

    def __init__(self, reader, settings):
        self.reader, self.settings = reader, settings

    def __enter__(self):
        self.saved = (self.reader.settings, self.reader.scores)
        self.reader.settings, self.reader.scores = self.settings, None

    def __exit__(self, *exc):
        self.reader.settings, self.reader.scores = self.saved


if __name__ == "__main__":
    unittest.main()
