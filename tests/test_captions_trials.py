"""The Turkish test models a run from source lists (services.captions.trial_models): found by
their folders' names and files, or a manifest, in the list's order; offered from source only."""

from array import array
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.captions import trial_models
from services.captions.trial_models import BEAM_LM, MOONSHINE, STREAM, UFAK

MOON_FILES = ("encoder_model_quantized.onnx", "decoder_model_merged_quantized.onnx", "tokens.txt")


class TrialModelsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def folder(self, name, files, manifest=None):
        path = self.root / name
        path.mkdir()
        for file in files:
            (path / file).write_bytes(b"x")
        if manifest is not None:
            (path / trial_models.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def found(self):
        return [(m.name, m.kind, m.label) for m in trial_models.discover(self.root)]

    def test_each_export_by_its_folder_s_name_and_files_in_the_list_s_order(self):
        self.folder("tr-moonm-avg", MOON_FILES)
        self.folder("tr-fc-r2", trial_models.TRANSDUCER_FILES)
        self.folder("tr-moon4-avg", MOON_FILES)
        self.folder("ufakzeka-kulak", trial_models.TRANSDUCER_FILES)
        self.folder("qwen3-asr-0.6b", ("encoder.int8.onnx", "decoder.int8.onnx"))  # not one
        self.folder(
            "tr-moon5s-avg", ("encoder_model.onnx", "decoder_model_merged.onnx", "tokens.txt")
        )
        self.folder("tr-moonm2-avg", MOON_FILES)
        (self.root / "silero_vad.onnx").write_bytes(b"x")
        self.assertEqual(
            self.found(),
            [
                ("ufakzeka-kulak", UFAK, "ufak"),
                ("tr-moon4-avg", MOONSHINE, "Moonshine small · tr-moon4-avg"),
                ("tr-moon5s-avg", MOONSHINE, "Moonshine small · tr-moon5s-avg"),
                ("tr-moonm-avg", MOONSHINE, "Moonshine medium · tr-moonm-avg"),
                ("tr-moonm2-avg", MOONSHINE, "Moonshine medium · tr-moonm2-avg"),
                ("tr-fc-r2", STREAM, "FastConformer · tr-fc-r2"),
            ],
        )

    def test_a_folder_whose_files_are_not_all_there_is_not_offered(self):
        self.folder("tr-moon6-avg", MOON_FILES[:2])  # still copying: no tokens yet
        self.folder("tr-fc5", trial_models.TRANSDUCER_FILES[:3])
        self.assertEqual(self.found(), [])

    def test_a_manifest_names_its_kind_and_label_whatever_the_folder_s_name(self):
        manifest = {"kind": "fastconformer-stream", "label": "FastConformer r2 (1040 ms)"}
        self.folder("tr-fc-r2", trial_models.TRANSDUCER_FILES, manifest)
        self.folder("night-run", MOON_FILES, {"kind": "moonshine", "size": "medium"})
        self.folder("other", trial_models.TRANSDUCER_FILES, {"kind": "something else"})
        self.assertEqual(
            self.found(),
            [
                ("night-run", MOONSHINE, "Moonshine medium · night-run"),
                ("tr-fc-r2", STREAM, "FastConformer r2 (1040 ms) · tr-fc-r2"),
            ],
        )

    def test_a_manifest_that_can_t_be_read_leaves_the_name_to_say(self):
        path = self.folder("tr-fc5", trial_models.TRANSDUCER_FILES)
        (path / trial_models.MANIFEST).write_text("{not json", encoding="utf-8")
        with self.assertLogs("services.captions.trial_models", "WARNING"):
            self.assertEqual(self.found(), [("tr-fc5", STREAM, "FastConformer · tr-fc5")])

    def test_moonshine_s_quantized_files_where_they_are(self):
        path = self.folder("tr-moon4-avg", MOON_FILES)
        encoder, decoder, tokens = trial_models.moonshine_files(path)
        self.assertEqual(
            (encoder.name, decoder.name, tokens.name),
            ("encoder_model_quantized.onnx", "decoder_model_merged_quantized.onnx", "tokens.txt"),
        )

    def test_no_folder_no_models(self):
        self.assertEqual(trial_models.discover(self.root / "missing"), [])

    def test_with_an_n_gram_each_moonshine_is_offered_again_with_the_beam(self):
        self.folder("tr-moonm-avg", MOON_FILES)
        self.folder("tr-moon4-avg", MOON_FILES)
        self.folder("tr-fc-r2", trial_models.TRANSDUCER_FILES)
        self.folder("ufakzeka-kulak", trial_models.TRANSDUCER_FILES)
        lm = self.folder("tr-lm-4g", ("p4.qng",))
        self.assertEqual(
            self.found(),
            [
                ("ufakzeka-kulak", UFAK, "ufak"),
                ("tr-moon4-avg", MOONSHINE, "Moonshine small · tr-moon4-avg"),
                (
                    "tr-moon4-avg+beam-lm",
                    MOONSHINE,
                    "Moonshine small · beam + LM · tr-moon4-avg",
                ),
                ("tr-moonm-avg", MOONSHINE, "Moonshine medium · tr-moonm-avg"),
                (
                    "tr-moonm-avg+beam-lm",
                    MOONSHINE,
                    "Moonshine medium · beam + LM · tr-moonm-avg",
                ),
                ("tr-fc-r2", STREAM, "FastConformer · tr-fc-r2"),
            ],
        )
        beam = trial_models.discover(self.root)[4]
        self.assertEqual((beam.decoding, beam.lm), (BEAM_LM, lm / "p4.qng"))
        self.assertEqual(beam.folder, self.root / "tr-moonm-avg")  # the export itself

    def test_a_beam_entry_s_name_says_its_export_beside_the_n_gram(self):
        self.folder("tr-moonm-avg", MOON_FILES)
        described = trial_models.describe(self.root / "tr-moonm-avg+beam-lm")
        self.assertIsNone(described)  # no n-gram here
        self.folder("night-lm", ("lm.qng", "other.qng"), {"kind": "lm", "file": "other.qng"})
        described = trial_models.describe(self.root / "tr-moonm-avg+beam-lm")
        assert described is not None
        self.assertEqual(described.name, "tr-moonm-avg+beam-lm")
        self.assertEqual(described.folder, self.root / "tr-moonm-avg")
        self.assertEqual(described.lm, self.root / "night-lm" / "other.qng")
        self.assertIsNone(trial_models.describe(self.root / "tr-moon9-avg+beam-lm"))
        self.folder("tr-fc5", trial_models.TRANSDUCER_FILES)
        self.assertIsNone(trial_models.describe(self.root / "tr-fc5+beam-lm"))  # Moonshine only
        self.assertEqual([m.name for m in trial_models.discover(self.root)][-1], "tr-fc5")

    def test_an_n_gram_built_on_other_pieces_is_offered_beside_none(self):
        import hashlib

        self.folder("tr-moonm-avg", MOON_FILES)  # its tokens.txt holds b"x"
        theirs = hashlib.sha256(b"x").hexdigest()
        lm = self.folder("tr-lm-4g", ("lm.qng",), {"kind": "lm", "tokens_sha256": "0" * 64})
        self.assertEqual([m.name for m in trial_models.discover(self.root)], ["tr-moonm-avg"])
        manifest = {"kind": "lm", "tokens_sha256": theirs.upper()}
        (lm / trial_models.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(
            [m.name for m in trial_models.discover(self.root)],
            ["tr-moonm-avg", "tr-moonm-avg+beam-lm"],
        )

    def test_an_n_gram_folder_without_its_file_offers_nothing(self):
        self.folder("tr-moonm-avg", MOON_FILES)
        self.folder("tr-lm-4g", ("README",))
        self.assertEqual([m.name for m in trial_models.discover(self.root)], ["tr-moonm-avg"])

    def test_offered_from_source_unless_turned_off_and_a_packaged_build_only_when_asked(self):
        enabled = trial_models.enabled
        self.assertTrue(enabled({}, frozen=False))
        self.assertFalse(enabled({trial_models.ENV: "0"}, frozen=False))
        self.assertFalse(enabled({}, frozen=True))
        self.assertTrue(enabled({trial_models.ENV: "1"}, frozen=True))

    def test_it_needs_no_qt(self):  # the captions process imports it
        import subprocess
        import sys

        probe = "import sys, services.captions.trial_models; print('PySide6' in sys.modules)"
        root = Path(__file__).resolve().parents[1]
        done = subprocess.run(
            [sys.executable, "-c", probe], cwd=root, capture_output=True, text=True
        )
        self.assertEqual(done.stdout.strip(), "False", done.stderr)


def setUpModule():
    """AI captions' files in a folder of this module's own (the controller's speed check)."""
    if not os.environ.get("QITV_CAPTIONS_DIR"):
        folder = tempfile.mkdtemp(prefix="qitv-captions-")
        patch = mock.patch.dict(os.environ, {"QITV_CAPTIONS_DIR": folder})
        patch.start()
        unittest.addModuleCleanup(patch.stop)
        unittest.addModuleCleanup(shutil.rmtree, folder, True)


class _Recognizer:
    def __init__(self, kind, options):
        self.kind, self.options = kind, options

    def create_stream(self):
        return SimpleNamespace(
            accept_waveform=lambda rate, samples: None,
            set_option=lambda key, value: None,
            result=SimpleNamespace(text=""),
        )

    def decode_stream(self, stream):
        pass


def _fake_sherpa(loaded):
    """sherpa-onnx's face as the captions process loads models: each load is noted."""

    def loader(kind):
        def load(**options):
            loaded.append(kind)
            return _Recognizer(kind, options)

        return staticmethod(load)

    class OfflineRecognizer:
        from_qwen3_asr = loader("qwen")
        from_moonshine_v2 = loader("moonshine")
        from_transducer = loader("offline transducer")
        from_cohere_transcribe = loader("cohere")

    class OnlineRecognizer:
        from_transducer = loader("streaming transducer")

    class VadModelConfig:
        def __init__(self):
            self.silero_vad = SimpleNamespace()

    class VadModel:
        @staticmethod
        def create(config):
            return SimpleNamespace(is_speech=lambda window: False)

    return SimpleNamespace(
        OfflineRecognizer=OfflineRecognizer,
        OnlineRecognizer=OnlineRecognizer,
        VadModelConfig=VadModelConfig,
        VadModel=VadModel,
    )


class TrialProcessTests(unittest.TestCase):
    """A test model picked runs as it is in the captions process: it alone, on Turkish."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name, files in (
            ("tr-moonm-avg", MOON_FILES),
            ("tr-fc-r2", trial_models.TRANSDUCER_FILES),
            ("ufakzeka-kulak", trial_models.TRANSDUCER_FILES),
            ("tr-lm-4g", ("p4.qng",)),
        ):
            (self.root / name).mkdir()
            for file in files:
                (self.root / name / file).write_bytes(b"x")
        self.loaded: list[str] = []
        sherpa = mock.patch.dict(sys.modules, {"sherpa_onnx": _fake_sherpa(self.loaded)})
        sherpa.start()
        self.addCleanup(sherpa.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def model(self, trial=None, env=None):
        from services.captions.worker import _Model

        spec = {
            "runtime": None,
            "model": {"conv_frontend": "c", "encoder": "e", "decoder": "d", "tokenizer": "t"},
            "vad": "vad.onnx",
            "turkish": str(self.root / "ufakzeka-kulak"),
            "european": None,
            "cohere": str(self.root / "cohere"),
            "trial": str(self.root / trial) if trial else None,
        }
        with mock.patch.dict(os.environ, env or {}):
            return _Model(spec)

    def test_a_moonshine_picked_loads_alone_and_the_environment_s_trials_step_aside(self):
        env = {"QITV_TR_STREAM": str(self.root / "tr-fc-r2")}
        model = self.model("tr-moonm-avg", env)
        self.assertEqual(self.loaded, ["moonshine"])
        self.assertTrue(model.trial and model.moonshine)
        self.assertIsNone(model.turkish_stream)
        self.assertFalse(model.has_qwen())
        model.prepare_qwen()
        self.assertIsNone(model.qwen())
        self.assertEqual(self.loaded, ["moonshine"])  # Qwen never loads beside it
        self.assertEqual(model._folders, {})  # nor a specialist

    def test_a_beam_pick_reads_with_our_beam_and_the_n_gram_not_sherpa_s_greedy(self):
        from services.captions.worker import TURKISH

        made = []

        class Reader:
            def __init__(self, folder, lm, threads):
                made.append((Path(folder).name, Path(lm).name, threads))
                self.warmed = 0

            def warm(self):
                self.warmed += 1

            def transcribe(self, samples, width=None):
                return " Deniz'i gördüm i şte "

        with mock.patch("services.captions.moon_beam.BeamReader", Reader):
            model = self.model("tr-moonm-avg+beam-lm")
        self.assertEqual(made, [("tr-moonm-avg", "p4.qng", 2)])
        self.assertEqual(self.loaded, [])  # sherpa-onnx loads no model: not Qwen, not greedy
        self.assertTrue(model.trial and model.moonshine)
        self.assertIs(model.turkish, model.beam)
        self.assertEqual(model.beam.warmed, 1)
        # as it wrote it: ufak's lone-i join is not for our own models (2026-11-08, the owner)
        self.assertEqual(model.decode(array("f", bytes(64)), TURKISH), "Deniz'i gördüm i şte")
        label = "Moonshine medium · beam + LM · tr-moonm-avg"
        self.assertEqual((model.wrote(), model.writes(TURKISH)), (label, label))  # for the log
        self.assertFalse(model.has_qwen())

    @staticmethod
    def said(model, text):
        """The Turkish model's next reading is `text`."""
        model.turkish.create_stream = lambda: SimpleNamespace(
            accept_waveform=lambda rate, samples: None, result=SimpleNamespace(text=text)
        )

    def test_only_ufak_has_its_lone_i_joined(self):
        # 2026-11-08, the owner: "Deniz'igördüm bu arada." from a Moonshine pick; "model
        # specific": the join is for ufak's "i stanbul" alone
        from services.captions.worker import TURKISH

        moonshine = self.model("tr-moonm-avg")
        self.said(moonshine, "Deniz'i gördüm i şte")
        self.assertEqual(moonshine.decode(array("f", bytes(64)), TURKISH), "Deniz'i gördüm i şte")
        self.assertEqual(moonshine.wrote(), "Moonshine medium · tr-moonm-avg")
        ufak = self.model("ufakzeka-kulak")
        self.said(ufak, "i stanbul'a gittik Deniz'i gördüm")
        self.assertEqual(
            ufak.decode(array("f", bytes(64)), TURKISH), "istanbul'a gittik Deniz'i gördüm"
        )
        self.assertEqual(ufak.wrote(), "ufak")

    def test_the_log_names_the_model_each_language_is_written_by(self):
        from services.captions.worker import TURKISH

        self.assertEqual(self.model("tr-fc-r2").writes(TURKISH), "FastConformer · tr-fc-r2")
        usual = self.model()  # ufak for Turkish, Qwen for the rest until a specialist loads
        self.assertEqual(usual.writes(TURKISH), "ufak")
        self.assertEqual(usual.writes("English"), "Qwen3-ASR")
        self.assertEqual(usual.writes(""), "Qwen3-ASR")
        usual.cohere = object()
        self.assertEqual(usual.writes("English"), "cohere")  # its folder's name

    def test_a_streaming_fastconformer_picked_is_read_as_a_stream(self):
        model = self.model("tr-fc-r2")
        self.assertEqual(self.loaded, ["streaming transducer"])
        self.assertIsNone(model.turkish)
        self.assertFalse(model.moonshine)
        self.assertEqual(
            Path(model.turkish_stream.options["encoder"]),
            self.root / "tr-fc-r2" / "encoder.int8.onnx",
        )

    def test_ufak_picked_writes_alone(self):
        model = self.model("ufakzeka-kulak")
        self.assertEqual(self.loaded, ["offline transducer"])
        self.assertEqual(model.turkish.options["model_type"], "nemo_transducer")
        self.assertTrue(model.trial)

    def test_a_pick_whose_folder_went_loads_the_usual_models(self):
        with self.assertLogs("services.captions.worker", "WARNING"):
            model = self.model("tr-moon9-avg")
        self.assertFalse(model.trial)
        self.assertEqual(self.loaded, ["qwen", "offline transducer"])  # Qwen and ufak

    def test_without_a_pick_the_environment_s_trial_still_takes_turkish(self):
        model = self.model(env={"QITV_TR_MOONSHINE": str(self.root / "tr-moonm-avg")})
        self.assertFalse(model.trial)
        self.assertTrue(model.moonshine)
        self.assertEqual(self.loaded, ["moonshine"])  # Qwen waits until a run needs it

    def test_a_test_model_s_run_is_turkish_whatever_it_was_told(self):
        from services.captions.worker import TURKISH, _Run

        model = self.model("tr-fc-r2")
        link: Any = SimpleNamespace(send=lambda message: None)
        with mock.patch("services.captions.decoder.AudioDecoder"):
            for language, guessed in (("", False), ("English", True), ("Turkish", False)):
                run = _Run(link, model, 1, "http://x", 0.0, None, language, [0.0], guessed)
                self.assertEqual((run.language, run.guessed), (TURKISH, False), language)
                self.assertFalse(run.qwen_writes())  # no step down for it
        self.assertEqual(self.loaded, ["streaming transducer"])


class TrialControllerTests(unittest.TestCase):
    """The Test model list's pick, as the controller keeps it and has the process load it."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication, QWidget
        from test_captions_pipeline import _Live, _Overlay

        from ui.captions_controller import CaptionsController

        self.app = QApplication.instance() or QApplication([])
        self.window = QWidget()
        self.window.live_session = _Live()
        self.window.live_session.position_s = 100.0
        self.captions = CaptionsController(self.window, overlay=_Overlay())
        process: Any = self.captions._process
        self.sent: list[tuple] = []
        self.loaded: list[dict] = []
        self.stopped: list[bool] = []
        process.send = lambda *m: self.sent.append(m)
        process.running = lambda: bool(self.loaded)
        process.stop = lambda: self.stopped.append(True)
        process.start = lambda load, detailed_log=False: self.loaded.append(load)
        self.config: dict[str, Any] = {}
        captions: Any = self.captions  # (its methods replaced: the config, the files here)
        captions._config_get = lambda name, default: self.config.get(name, default)
        captions._config_set = self.config.__setitem__
        captions._here = lambda name: True
        self.captions._on_trials_found(
            [
                trial_models.TrialModel("ufakzeka-kulak", UFAK, "ufak", Path("u")),
                trial_models.TrialModel("tr-moonm-avg", MOONSHINE, "Moonshine medium", Path("m")),
            ]
        )
        self.captions._trials_scanned = float("inf")  # no scan of the developer's folder
        self.patches = [
            mock.patch.object(trial_models, "enabled", return_value=True),
            mock.patch("services.captions.models.installed", return_value=True),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_list_is_automatic_then_each_model_and_none_without_models(self):
        self.assertEqual(
            self.captions.trial_choices(),
            [("", "Automatic"), ("ufakzeka-kulak", "ufak"), ("tr-moonm-avg", "Moonshine medium")],
        )
        self.captions._on_trials_found([])
        self.assertEqual(self.captions.trial_choices(), [])
        with mock.patch.object(trial_models, "enabled", return_value=False):
            self.captions._on_trials_found(
                [trial_models.TrialModel("tr-fc5", STREAM, "FastConformer", Path("f"))]
            )
            self.assertEqual(self.captions.trial_choices(), [])  # a packaged build
            self.config["ai_captions_trial_model"] = "tr-fc5"
            self.assertEqual(self.captions.trial_model(), "")

    def test_picked_while_off_they_come_on_with_it_as_turkish(self):
        self.captions.set_trial_model("tr-moonm-avg")
        self.assertEqual(self.config["ai_captions_trial_model"], "tr-moonm-avg")
        self.assertTrue(self.captions.enabled())
        [load] = self.loaded
        self.assertTrue(load["trial"].endswith("tr-moonm-avg"))
        self.captions._on_ready()
        start = next(m for m in self.sent if m[0] == "start")
        self.assertEqual((start[5], start[6]), ("Turkish", False))  # no detection, no guess
        self.assertEqual(self.captions.language()[0], "Turkish")

    def test_picked_while_on_the_process_loads_it_and_automatic_goes_back(self):
        self.captions.toggle()
        [load] = self.loaded
        self.assertIsNone(load["trial"])  # the usual models
        self.captions._on_ready()
        self.captions._on_caption(self.captions._run, 99.0, 101.0, "written")
        self.captions.set_trial_model("ufakzeka-kulak")
        self.assertEqual(self.stopped, [True])
        self.assertTrue(self.loaded[-1]["trial"].endswith("ufakzeka-kulak"))
        self.assertEqual(self.captions._cues, [])  # the new model's captions only
        self.assertEqual(self.captions.status_text(), "Preparing…")
        self.captions.set_trial_model("ufakzeka-kulak")  # the same again: nothing new
        self.assertEqual(len(self.loaded), 2)
        self.captions.set_trial_model("")
        self.assertEqual((len(self.stopped), self.loaded[-1]["trial"]), (2, None))
        self.assertTrue(self.captions.enabled())

    def test_a_model_not_listed_is_not_picked_and_a_pick_that_went_reads_as_none(self):
        self.captions.set_trial_model("tr-moon9-avg")
        self.assertEqual((self.config, self.loaded), ({}, []))
        self.config["ai_captions_trial_model"] = "tr-fc-old"
        self.assertEqual(self.captions.trial_model(), "")

    def test_a_beam_entry_is_listed_and_picked_by_its_own_name(self):
        beam = trial_models.TrialModel(
            "tr-moonm-avg+beam-lm",
            MOONSHINE,
            "Moonshine medium · beam + LM",
            Path("m"),
            BEAM_LM,
            Path("lm.qng"),
        )
        self.captions._on_trials_found(self.captions._trials + [beam])
        self.assertEqual(
            self.captions.trial_choices()[-1],
            ("tr-moonm-avg+beam-lm", "Moonshine medium · beam + LM"),
        )
        self.captions.set_trial_model("tr-moonm-avg+beam-lm")
        [load] = self.loaded
        self.assertTrue(load["trial"].endswith("tr-moonm-avg+beam-lm"))

    def test_automatic_picked_while_off_changes_only_the_pick(self):
        self.config["ai_captions_trial_model"] = "tr-moonm-avg"
        self.captions.set_trial_model("")
        self.assertEqual((self.config["ai_captions_trial_model"], self.loaded), ("", []))
        self.assertFalse(self.captions.enabled())

    def test_the_scan_runs_off_the_gui_thread_and_the_list_follows(self):
        import threading

        from timer_fixtures import wait_until

        root = Path(tempfile.mkdtemp(prefix="qitv-trials-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "tr-fc5").mkdir()
        for file in trial_models.TRANSDUCER_FILES:
            (root / "tr-fc5" / file).write_bytes(b"x")
        threads = []
        real = trial_models.discover

        def discover(folder):
            threads.append(threading.current_thread() is threading.main_thread())
            return real(folder)

        self.captions._trials_scanned = None
        changed = []
        self.captions.changed.connect(lambda: changed.append(True))
        with (
            mock.patch("services.captions.models.captions_root", return_value=root),
            mock.patch.object(trial_models, "discover", side_effect=discover),
        ):
            self.captions.trial_models()  # the list as it was; the scan goes on in a thread
            self.assertTrue(wait_until(lambda: bool(changed), 5000))
        self.assertEqual(threads, [False])
        self.assertEqual(self.captions.trial_models(), [("tr-fc5", "FastConformer · tr-fc5")])
        self.captions.trial_models()  # asked again at once: no new scan
        self.assertEqual(len(threads), 1)


if __name__ == "__main__":
    unittest.main()
