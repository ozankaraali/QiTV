"""2.12.1: our Turkish Moonshine read with its LSTM language model (services.captions.moon_reading
and lstm_lm): the beam with the LM when it is here, the beam alone (as 2.12.0) when it isn't,
won't load or fails; the LM's states kept by their tokens. A model, a beam and an LM that only
record what they were asked; no onnxruntime."""

from array import array
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_captions_beam import FakeSteps, table

from services.captions import lstm_lm, moon_reading, onnx_c
from services.captions.moon_beam import BeamReader, Settings

# The model: 10 a little before 11, then the end; the LM: 11 far likelier at the start. The beam
# alone writes 10, the beam with the LM 11 (moon_beam's own test of shallow fusion).
CLOSE_SECOND = {(): {10: 2.0, 11: 1.8}}


class FakeEncoded:
    def __init__(self, silent=False):
        self.silent = silent
        self.frames = 50
        self.released = False

    def release(self):
        self.released = True


class FakeModel:
    """moon_ort.MoonshineOrt: each reading's sound encoded, its steps a FakeSteps over `rows`."""

    def __init__(self, rows=CLOSE_SECOND, fail_at=None):
        self._api = object()
        self.rows = rows
        self.fail_at = fail_at
        self.encoded = []
        self.steps_made = []
        self.silent = False

    def encode(self, samples):
        encoded = FakeEncoded(self.silent)
        self.encoded.append(encoded)
        return encoded

    @contextmanager
    def steps(self, encoded):
        steps = FakeSteps(table(self.rows), fail_at=self.fail_at)
        self.steps_made.append(steps)
        yield steps

    def text(self, tokens):
        return " ".join(str(token) for token in tokens)


def beam_of(model):
    """moon_beam.BeamReader over `model`, as the captions process makes it (no n-gram)."""
    reader = object.__new__(BeamReader)
    reader.model, reader.lm, reader.scores, reader.settings = model, None, None, Settings()
    return reader


class PrefersEleven:
    """An LM (start, score): 11 likely at the start, 10 not; each score asked recorded."""

    def __init__(self, fail=False):
        self.asked = []
        self.fail = fail
        self.closed = False

    def start(self):
        return ()

    def score(self, state, token):
        self.asked.append((state, token))
        if self.fail:
            raise RuntimeError("onnxruntime: the LM's run")
        return {10: -3.0, 11: -0.2}.get(token, -0.5), state + (token,)

    def close(self):
        self.closed = True


def lm_file():
    folder = Path(tempfile.mkdtemp())
    path = folder / "lm" / "lm.int8.onnx"
    path.parent.mkdir()
    path.write_bytes(b"x")
    return path


class TurkishReaderTests(unittest.TestCase):
    def setUp(self):
        self.made = []
        self.lm = PrefersEleven()

    def factory(self, path, api):
        self.made.append((Path(path).name, api))
        return self.lm

    def reader(self, lm="file", model=None, factory=None):
        model = model or FakeModel()
        path = lm_file() if lm == "file" else lm
        return moon_reading.TurkishReader(beam_of(model), path, lm_factory=factory or self.factory)

    def test_with_the_lm_the_beam_writes_what_the_lm_prefers(self):
        reader = self.reader()
        self.assertEqual(reader.transcribe([0.0] * 16000), "11")
        self.assertEqual(self.made, [("lm.int8.onnx", reader.beam.model._api)])  # its session
        self.assertTrue(self.lm.asked)
        self.assertTrue(reader.beam.model.encoded[0].released)
        self.assertEqual(reader.beam.model.steps_made[0].alive, set())

    def test_its_weights_are_the_measured_ones(self):
        self.assertEqual(moon_reading.LM_SETTINGS, Settings(lm_weight=0.1, length_bonus=0.41))
        self.assertEqual(Settings(), Settings(lm_weight=0.1, length_bonus=0.5))  # the beam alone
        searched = []
        real = moon_reading.search

        def search(steps, cap, settings, lm):
            searched.append((settings, lm))
            return real(steps, cap, settings, lm)

        with mock.patch.object(moon_reading, "search", search):
            self.reader().transcribe([0.0] * 16000)
        self.assertEqual(searched, [(moon_reading.LM_SETTINGS, self.lm)])

    def test_without_the_lm_file_it_reads_as_2_12_0(self):
        reader = self.reader(lm=None)
        self.assertEqual(reader.transcribe([0.0] * 16000), "10")  # the beam alone's choice
        self.assertEqual(self.made, [])
        self.assertEqual(reader.transcribe([0.0] * 16000), beam_of(FakeModel()).transcribe([0.0]))

    def test_an_lm_that_does_not_load_is_said_once_and_the_beam_reads_on(self):
        tries = []

        def broken(path, api):
            tries.append(path)
            raise onnx_c.OrtError("Protobuf parsing failed")

        reader = self.reader(factory=broken)
        with self.assertLogs("services.captions.moon_reading", "WARNING") as logs:
            self.assertEqual(reader.transcribe([0.0] * 16000), "10")
            self.assertEqual(reader.transcribe([0.0] * 16000), "10")
            reader.warm()
        self.assertEqual(len(tries), 1)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("did not load", logs.records[0].getMessage())

    def test_an_lm_that_fails_in_a_reading_leaves_the_beam_alone_for_good(self):
        self.lm = PrefersEleven(fail=True)
        model = FakeModel()
        reader = self.reader(model=model)
        with self.assertLogs("services.captions.moon_reading", "WARNING") as logs:
            self.assertEqual(reader.transcribe([0.0] * 16000), "10")  # read again by the beam
            asked = len(self.lm.asked)
            self.assertEqual(reader.transcribe([0.0] * 16000), "10")
        self.assertEqual(len(self.lm.asked), asked)  # never asked again
        self.assertEqual(len(logs.records), 1)
        self.assertIn("failed", logs.records[0].getMessage())
        self.assertTrue(all(encoded.released for encoded in model.encoded))
        self.assertTrue(all(steps.alive == set() for steps in model.steps_made))

    def test_a_model_that_fails_with_the_beam_alone_too_is_the_worker_s_to_handle(self):
        # the Turkish model itself failing: as in 2.12.0, the worker's (drop_turkish)
        reader = self.reader(model=FakeModel(fail_at=1))
        with self.assertLogs("services.captions.moon_reading", "WARNING"):
            with self.assertRaises(RuntimeError):
                reader.transcribe([0.0] * 16000)

    def test_a_reading_narrowed_to_its_best_path_has_no_lm(self):
        widths = []
        reader = self.reader()
        beam = reader.beam
        plain = beam.transcribe
        beam.transcribe = lambda samples, width=None: widths.append(width) or plain(samples, width)
        self.assertEqual(reader.transcribe([0.0] * 16000, width=1), "10")
        self.assertEqual(widths, [1])  # 2.10's fast reading: its best path alone
        self.assertEqual((self.made, self.lm.asked), ([], []))

    def test_silence_is_nothing(self):
        model = FakeModel()
        model.silent = True
        self.assertEqual(self.reader(model=model).transcribe([0.0] * 16000), "")
        self.assertEqual(model.steps_made, [])
        self.assertTrue(model.encoded[0].released)

    def test_warm_loads_the_lm_and_close_closes_it(self):
        warmed = []
        reader = self.reader()
        reader.beam.warm = lambda: warmed.append(True)
        reader.beam.close = lambda: warmed.append("closed")
        reader.warm()
        self.assertEqual((warmed, len(self.made)), ([True], 1))
        reader.transcribe([0.0] * 16000)
        self.assertEqual(len(self.made), 1)  # loaded once
        reader.close()
        self.assertTrue(self.lm.closed)
        self.assertEqual(warmed, [True, "closed"])

    def test_the_lm_is_loaded_once_across_threads(self):
        def slow(path, api):
            time.sleep(0.05)
            return self.factory(path, api)

        reader = self.reader(factory=slow)
        threads = [
            threading.Thread(target=reader.transcribe, args=([0.0] * 1600,)) for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.made), 1)

    def test_an_lm_that_arrives_later_is_read_with_from_the_next_piece(self):
        # an install from before 2.12.1: the LM downloaded while the captions process runs
        reader = self.reader(lm=None)
        self.assertEqual(reader.transcribe([0.0] * 16000), "10")
        reader.use_lm(lm_file())
        self.assertEqual(reader.transcribe([0.0] * 16000), "11")
        reader.use_lm(Path("/elsewhere/lm.int8.onnx"))  # one already given stays
        self.assertEqual([name for name, _api in self.made], ["lm.int8.onnx"])


class FakeValue:
    """onnx_c.Value: over a Python buffer (a tensor), or what a run gave."""

    def __init__(self, data=None, history=None):
        self.data = data
        self.history = history  # an LM state: the tokens fed so far
        self.released = False

    def floats(self):
        return array("f", self.data)

    def release(self):
        self.released = True


class FakeApi:
    def tensor(self, data, shape, element_type):
        return FakeValue(data)  # (x's buffer is written before each run)


class FakeSession:
    """An exported LM with its state: after the tokens fed so far (from SOS), log-probabilities by
    `logprobs(history)` over a vocabulary of 16; each run recorded."""

    vocabulary = 16
    made: list = []

    def __init__(self, api, path, threads):
        self.inputs = ["x", "h0", "c0"]
        self.runs: list = []
        self.fail = False
        FakeSession.made.append(self)

    @staticmethod
    def logprobs(history):
        return [-1.0 - ((sum(history) + 3 * token) % 7) / 2 for token in range(16)]

    def select(self, names):
        return list(names)

    def input_shape(self, index):
        return (2, 1, 4), onnx_c.FLOAT

    def run(self, feed, outputs):
        if self.fail:
            raise onnx_c.OrtError("the LM's run")
        token = feed[0].data[0]
        history = (feed[1].history or ()) + (token,)
        self.runs.append(history)
        return [
            FakeValue(self.logprobs(history)),
            FakeValue(history=history),
            FakeValue(history=history),
        ]


class LstmLmTests(unittest.TestCase):
    def setUp(self):
        FakeSession.made = []
        patcher = mock.patch.object(lstm_lm.onnx_c, "Session", FakeSession)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lm(self, **options):
        return lstm_lm.LstmLM("lm.int8.onnx", FakeApi(), **options)

    @staticmethod
    def read(lm, tokens):
        """`tokens` scored one after another from the start, as a hypothesis grows."""
        state, scores = lm.start(), []
        for token in tokens:
            value, state = lm.score(state, token)
            scores.append(value)
        return scores

    def test_scores_are_the_lm_s_after_the_tokens_before(self):
        lm = self.lm()
        sos = lm.sos
        scores = self.read(lm, [5, 7, 9])
        expected = [
            FakeSession.logprobs((sos,))[5],
            FakeSession.logprobs((sos, 5))[7],
            FakeSession.logprobs((sos, 5, 7))[9],
        ]
        self.assertEqual(scores, [array("f", [value])[0] for value in expected])

    def test_a_state_is_run_only_once_it_is_scored_from(self):
        lm = self.lm()
        runs = FakeSession.made[0].runs
        self.assertEqual(len(runs), 1)  # the start, at load
        state = lm.start()
        for token in range(8):  # a hypothesis's 8 candidates: no run yet
            lm.score(state, token)
        self.assertEqual(len(runs), 1)

    def test_a_growing_utterance_read_again_runs_only_its_new_tokens(self):
        lm = self.lm()
        runs = FakeSession.made[0].runs
        first = self.read(lm, [5, 7, 9])
        made = len(runs)  # the start, then after 5 and after 7
        self.assertEqual(made, 3)
        again = self.read(lm, [5, 7, 9, 11, 2])
        self.assertEqual(again[:3], first)
        self.assertEqual(len(runs), made + 2)  # after 9 and after 11: the rest was kept
        self.assertEqual(runs[made:], [(lm.sos, 5, 7, 9), (lm.sos, 5, 7, 9, 11)])

    def test_kept_scores_are_the_ones_it_would_run(self):
        kept, fresh = self.lm(), self.lm(cache=0)
        for tokens in ([5, 7, 9], [5, 7, 3, 1], [5, 7, 9, 11, 2], [4]):
            self.assertEqual(self.read(kept, tokens), self.read(fresh, tokens))
        self.assertLess(len(FakeSession.made[0].runs), len(FakeSession.made[1].runs))

    def test_it_keeps_so_many_states_the_oldest_go_first(self):
        lm = self.lm(cache=2)
        runs = FakeSession.made[0].runs
        self.read(lm, [1, 2, 3])  # after 1 and after 2 run, and are kept
        self.read(lm, [4, 5])  # after 4: the oldest kept, after 1, goes
        made = len(runs)
        self.read(lm, [4, 5])
        self.assertEqual(len(runs), made)  # kept
        self.read(lm, [1, 2])
        self.assertEqual(runs[made:], [(lm.sos, 1)])  # it had gone

    def test_an_lm_without_its_state_is_refused(self):
        class Plain(FakeSession):
            def __init__(self, api, path, threads):
                super().__init__(api, path, threads)
                self.inputs = ["x"]

        with mock.patch.object(lstm_lm.onnx_c, "Session", Plain):
            with self.assertRaises(ValueError):
                self.lm()


if __name__ == "__main__":
    unittest.main()
