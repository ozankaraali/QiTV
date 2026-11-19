"""Dev build (2026-11-19): how our Turkish Moonshine reads (services.captions.moon_reading), with
a model and a beam that only record what they were asked; no onnxruntime."""

from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from services.captions import moon_reading
from services.captions.lstm_lm import Fused
from services.captions.moon_beam import Settings
from services.captions.moon_ort import Decoded


class FakeEncoded:
    def __init__(self, silent=False):
        self.silent = silent
        self.frames = 50
        self.value = SimpleNamespace(shape=(1, 50, 8))
        self.released = False

    def release(self):
        self.released = True


class FakeModel:
    def __init__(self):
        self._api = object()
        self.encoded = []
        self.decoded = []

    def encode(self, samples):
        encoded = FakeEncoded()
        self.encoded.append(encoded)
        return encoded

    def decode(self, encoded):
        self.decoded.append(encoded)
        return Decoded(tokens=[5], eos=True)

    @contextmanager
    def steps(self, encoded):
        yield "steps"

    def text(self, tokens):
        return " ".join(str(token) for token in tokens)


class FakeBeam:
    def __init__(self):
        self.model = FakeModel()
        self.warmed = 0
        self.closed = False

    def warm(self):
        self.warmed += 1

    def close(self):
        self.closed = True


class FakeIlm:
    made = []

    def __init__(self, model, frames, width):
        self.shape = (frames, width)
        self.closed = False
        FakeIlm.made.append(self)

    def close(self):
        self.closed = True


def folder_with(*names):
    folder = Path(tempfile.mkdtemp())
    for name in names:
        (folder / name).write_bytes(b"x")
    return folder


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.searched = []

        def search(steps, cap, settings, lm):
            self.searched.append((settings, lm))
            return Decoded(tokens=[9], eos=True)

        patcher = mock.patch.object(moon_reading, "search", side_effect=search)
        self.search = patcher.start()
        self.addCleanup(patcher.stop)
        FakeIlm.made = []
        patcher = mock.patch.object(moon_reading, "DecoderIlm", FakeIlm)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.loaded = []

    def factory(self, path, api):
        self.loaded.append(Path(path).name)
        return SimpleNamespace(name=Path(path).name, close=lambda: None)

    def reader(self, reading, folder="both", factory=None):
        if folder == "both":
            folder = folder_with("lm.u8mm.onnx", "lm.int8.onnx")
        return moon_reading.TurkishReader(
            FakeBeam(), folder, reading, lm_factory=factory or self.factory
        )

    def test_available_lists_readings_whose_file_is_there(self):
        self.assertEqual(
            moon_reading.available(folder_with("lm.int8.onnx")), ["greedy", "beam", "lm_int8"]
        )
        self.assertEqual(moon_reading.available(None), ["greedy", "beam"])
        self.assertEqual(
            moon_reading.available(folder_with("lm.u8mm.onnx", "lm.int8.onnx")),
            list(moon_reading.READINGS),
        )

    def test_stepped_wraps_and_starts_from_default_when_unknown(self):
        self.assertEqual(moon_reading.stepped("ilme", 1, ["greedy", "beam"]), "greedy")
        self.assertEqual(moon_reading.stepped("beam", -1, ["greedy", "beam"]), "greedy")
        self.assertEqual(moon_reading.stepped("greedy", -1, ["greedy", "beam", "lm"]), "lm")
        self.assertEqual(moon_reading.stepped("beam", 1, ["greedy", "beam", "lm"]), "lm")

    def test_greedy_uses_the_model_decode_not_the_search(self):
        reader = self.reader("greedy")
        self.assertEqual(reader.transcribe([0.0] * 16000), ("5", "greedy"))
        self.assertEqual(len(reader.model.decoded), 1)
        self.assertEqual(self.searched, [])
        self.assertTrue(reader.model.encoded[0].released)

    def test_beam_searches_with_2_10_settings_and_no_lm(self):
        reader = self.reader("beam")
        self.assertEqual(reader.transcribe([0.0] * 16000), ("9", "beam"))
        self.assertEqual(self.searched, [(Settings(), None)])
        self.assertEqual(self.loaded, [])

    def test_lm_searches_with_the_u8mm_lm_at_its_tuned_weights(self):
        reader = self.reader("lm")
        self.assertEqual(reader.transcribe([0.0] * 16000), ("9", "lm"))
        self.assertEqual(self.loaded, ["lm.u8mm.onnx"])
        settings, lm = self.searched[0]
        self.assertEqual(settings, Settings(lm_weight=0.1, length_bonus=0.41))
        self.assertEqual(lm.name, "lm.u8mm.onnx")

    def test_lm_int8_loads_the_int8_file(self):
        reader = self.reader("lm_int8")
        self.assertEqual(reader.transcribe([0.0] * 16000)[1], "lm_int8")
        self.assertEqual(self.loaded, ["lm.int8.onnx"])
        self.assertEqual(self.searched[0][0], Settings(lm_weight=0.1, length_bonus=0.41))

    def test_ilme_fuses_with_ratio_half_and_closes_the_ilm_even_when_search_raises(self):
        reader = self.reader("ilme")
        self.assertEqual(reader.transcribe([0.0] * 16000), ("9", "ilme"))
        settings, fused = self.searched[0]
        self.assertEqual(settings, Settings(lm_weight=0.2, length_bonus=0.62))
        self.assertIsInstance(fused, Fused)
        self.assertAlmostEqual(fused.ratio, 0.5)
        self.assertEqual(fused.lm.name, "lm.u8mm.onnx")
        self.assertEqual(FakeIlm.made[0].shape, (50, 8))
        self.assertTrue(FakeIlm.made[0].closed)
        self.search.side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            reader.transcribe([0.0] * 16000)
        self.assertTrue(FakeIlm.made[1].closed)
        self.assertTrue(reader.model.encoded[1].released)

    def test_fast_reads_the_best_path_alone_without_lm(self):
        reader = self.reader("lm")
        self.assertEqual(reader.transcribe([0.0] * 16000, fast=True), ("9", "lm (fast)"))
        self.assertEqual(self.searched, [(Settings(width=1), None)])

    def test_an_lm_that_fails_to_load_falls_back_to_beam_once(self):
        calls = []

        def broken(path, api):
            calls.append(path)
            raise OSError("gone")

        reader = self.reader("lm", factory=broken)
        with self.assertLogs("services.captions.moon_reading", "WARNING") as logs:
            self.assertEqual(reader.transcribe([0.0] * 16000), ("9", "beam"))
            self.assertEqual(reader.transcribe([0.0] * 16000), ("9", "beam"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(reader.reading, "beam")
        self.assertEqual(self.searched[0], (Settings(), None))
        reader.set_reading("lm")
        self.assertEqual(reader.reading, "beam")  # its file failed: not offered again

    def test_a_reading_without_its_file_starts_as_beam(self):
        reader = self.reader("ilme", folder=folder_with("lm.int8.onnx"))
        self.assertEqual(reader.reading, "beam")
        reader = self.reader("lm", folder=None)
        self.assertEqual(reader.reading, "beam")
        reader.set_reading("nonsense")
        self.assertEqual(reader.reading, "beam")

    def test_set_reading_switches_the_next_reading(self):
        reader = self.reader("beam")
        reader.set_reading("lm_int8")
        self.assertEqual(reader.transcribe([0.0] * 16000)[1], "lm_int8")

    def test_the_lm_is_loaded_once_across_threads(self):
        def slow(path, api):
            time.sleep(0.05)
            return self.factory(path, api)

        reader = self.reader("lm", factory=slow)
        threads = [threading.Thread(target=reader.transcribe, args=([0.0],)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.loaded, ["lm.u8mm.onnx"])

    def test_warm_loads_the_chosen_lm_and_close_closes_it(self):
        closed = []

        def factory(path, api):
            return SimpleNamespace(close=lambda: closed.append(Path(path).name))

        reader = self.reader("lm_int8", factory=factory)
        reader.warm()
        self.assertEqual(reader.beam.warmed, 1)
        reader.close()
        self.assertEqual(closed, ["lm.int8.onnx"])
        self.assertTrue(reader.beam.closed)

    def test_fused_subtracts_ratio_times_the_ilm(self):
        lm = SimpleNamespace(start=lambda: "a", score=lambda state, token: (-1.0, "a2"))
        ilm = SimpleNamespace(start=lambda: "b", score=lambda state, token: (-2.0, "b2"))
        fused = Fused(lm, ilm, 0.5)
        value, state = fused.score(fused.start(), 7)
        self.assertEqual(value, 0.0)
        self.assertEqual(state, ("a2", "b2"))


if __name__ == "__main__":
    unittest.main()
