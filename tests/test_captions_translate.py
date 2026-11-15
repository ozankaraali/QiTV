"""The caption translator (services.captions.translate) with a fake session and tokenizer: the
prompt, the greedy loop's stops, the clean-up of what the model wrote, the language names."""

import ctypes
from pathlib import Path
import sys
from typing import Any
import unittest
from unittest import mock

from services.captions import onnx_c, translate
from services.captions.mt_languages import MT_LANGUAGES, app_default, own_name, source_for
from services.captions.translate import EOS_IDS, MAX_NEW_TOKENS, Translator, clean, prompt

LAYERS = 2
PASTS = [f"past_key_values.{i}.{kv}" for i in range(LAYERS) for kv in ("key", "value")]
PRESENTS = [n.replace("past_key_values", "present") for n in PASTS]


class FakeValue:
    def __init__(self, data: Any = None) -> None:
        self.data = data
        self.released = False
        self._cell = ctypes.c_int64(0)

    @property
    def shape(self) -> tuple[int, ...]:
        return (1, 1, 0, 256)

    def address(self) -> int:
        return ctypes.addressof(self._cell)

    def release(self) -> None:
        self.released = True


class FakeApi:
    def tensor(self, data: Any, shape: Any, element_type: int) -> FakeValue:
        return FakeValue(data)


class FakeSession:
    """Answers each run with the next scripted token; keeps what it was fed."""

    def __init__(self, script: list[int]) -> None:
        self.script = list(script)
        self.inputs = ["input_ids", "attention_mask", *PASTS]
        self.calls: list[dict[str, Any]] = []
        self.made: list[FakeValue] = []

    def select(self, names: Any) -> list[str]:
        return list(names)

    def run(self, values: list[FakeValue], selection: Any) -> list[FakeValue]:
        fed = dict(zip(self.inputs, values))
        self.calls.append(
            {
                "ids": list(fed["input_ids"].data),
                "mask": len(fed["attention_mask"].data),
                "released": [v.released for v in values],
            }
        )
        token = FakeValue()
        token._cell.value = self.script.pop(0)
        outs = [token] + [FakeValue() for _ in PRESENTS]
        self.made.extend(outs)
        return outs


class FakeTokenizer:
    """Ids 10..: words; 7 is a newline; 1 and 106 end it."""

    WORDS = {10: " So", 11: " —", 12: " okay", 13: " the", 7: "\n", 14: " Привет", 15: "x" * 200}

    def encode(self, text: str, add_special_tokens: bool = True) -> Any:
        self.special = add_special_tokens
        self.encoded = text
        return mock.Mock(ids=[2, 20, 21])

    def decode(self, ids: list[int], skip_special_tokens: bool = True) -> str:
        return "".join(self.WORDS.get(i, "") for i in ids)


class Fake(Translator):
    def __init__(self, script: list[int]) -> None:
        super().__init__(Path("."))
        self.session = FakeSession(script)
        self.fake_tokenizer = FakeTokenizer()

    def _load(self) -> None:
        self._api = FakeApi()  # type: ignore[assignment]
        self._session = self.session  # type: ignore[assignment]
        self._tokenizer = self.fake_tokenizer
        self._past_names = PASTS
        self._present_names = PRESENTS
        self._selection = self.session.select(["next_token", *PRESENTS])
        self._empty = FakeValue()


class TranslatorTest(unittest.TestCase):
    def test_prompt_is_the_model_cards(self) -> None:
        self.assertEqual(
            prompt("Merhaba", "Turkish", "English"),
            "Translate this from Turkish to English:\nTurkish: Merhaba\nEnglish:",
        )

    def test_it_stops_at_eos_or_newline_and_cleans(self) -> None:
        t = Fake([10, 11, 12, 1])
        self.assertEqual(t.translate("Pekala tamam", "Turkish", "English"), "So, okay")
        self.assertEqual(
            t.fake_tokenizer.encoded,
            "Translate this from Turkish to English:\nTurkish: Pekala tamam\nEnglish:",
        )
        self.assertFalse(t.fake_tokenizer.special)  # no <bos>, as the model card
        calls = t.session.calls
        self.assertEqual(len(calls), 4)  # the prefill, then a step after each text token
        self.assertEqual(calls[0]["ids"], [2, 20, 21])
        self.assertEqual(calls[0]["mask"], 3)
        self.assertEqual(calls[1]["ids"], [10])
        self.assertEqual(calls[1]["mask"], 4)
        self.assertEqual(calls[2]["mask"], 5)
        # a step's pasts are the last call's presents; the earlier ones are released
        self.assertTrue(all(v.released for v in t.session.made))
        # newline and EOS ids 106 as well
        self.assertEqual(Fake([10, 7]).translate("Pekala", "Turkish", "English"), "So")
        self.assertEqual(Fake([10, 106]).translate("Pekala", "Turkish", "English"), "So")

    def test_the_cap(self) -> None:
        t = Fake([10] * (MAX_NEW_TOKENS + 5))
        t.translate("Pekala tamam", "Turkish", "English")
        self.assertEqual(len(t.session.calls), MAX_NEW_TOKENS)  # prefill + cap - 1 steps
        self.assertEqual(EOS_IDS, frozenset({1, 106}))

    def test_a_deadline_passed_between_tokens_gives_none(self) -> None:
        t = Fake([10, 12, 12, 1])
        with mock.patch.object(translate.time, "monotonic", side_effect=[1.0, 1.0, 9.0, 9.0]):
            self.assertIsNone(t.translate("Pekala tamam", "Turkish", "English", deadline=5.0))
        self.assertTrue(all(v.released for v in t.session.made))

    def test_garbage_gives_none(self) -> None:
        for out in ("", "the the the the the the the the", "x" * 400, "Привет мир"):
            self.assertIsNone(clean("Merhaba dünya", out, "English"))
        self.assertEqual(clean("Merhaba dünya", " Hello — world \n", "English"), "Hello, world")

    def test_a_cjk_source_may_have_a_longer_translation(self) -> None:
        chinese = "我们今天晚上一起去吃饭吧好吗"[:12]
        english = "We are all going out to eat together tonight, aren't we? Yes. " * 2
        self.assertEqual(len(chinese), 12)
        self.assertEqual(clean(chinese, english[:90], "English"), english[:90].strip())
        self.assertIsNone(clean(chinese, "x" * 400, "English"))
        self.assertIsNone(clean("Merhaba dünya", english[:90], "English"))

    def test_a_failing_run_gives_none_and_releases_everything(self) -> None:
        t = Fake([10, 12, 1])
        original = t.session.run
        calls = []

        def run(values: Any, selection: Any) -> Any:
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return original(values, selection)

        t.session.run = run  # type: ignore[method-assign]
        with self.assertLogs(translate.logger, "ERROR"):
            self.assertIsNone(t.translate("Pekala tamam", "Turkish", "English"))
        self.assertTrue(t.session.made)
        self.assertTrue(all(v.released for v in t.session.made))
        t2 = Fake([])  # run raises at once (no scripted token): warm logs, no raise
        with self.assertLogs(translate.logger, "ERROR"):
            t2.warm()

    def test_a_failed_load_closes_what_it_made_and_is_not_retried(self) -> None:
        closed: list[str] = []
        made: list[str] = []

        class Api:
            version = "x"

            def close(self) -> None:
                closed.append("api")

        class Session:
            inputs = ["input_ids", "attention_mask", *PASTS]
            outputs = ["next_token", *PRESENTS]

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                made.append("session")

            def select(self, names: Any) -> list[str]:
                return list(names)

            def close(self) -> None:
                closed.append("session")

        class Tokenizer:
            @staticmethod
            def from_file(path: str) -> Any:
                made.append("tokenizer")
                raise OSError("no tokenizer.json")

        t = Translator(Path("."))
        fake_module = mock.Mock(Tokenizer=Tokenizer)
        with (
            mock.patch.dict(sys.modules, {"tokenizers": fake_module}),
            mock.patch.object(onnx_c, "Api", return_value=Api()),
            mock.patch.object(onnx_c, "Session", Session),
            mock.patch.object(onnx_c, "find_library", return_value=Path("lib")),
        ):
            with self.assertLogs(translate.logger, "ERROR") as logged:
                self.assertIsNone(t.translate("Merhaba", "Turkish", "English"))
            self.assertIsNone(t.translate("Merhaba", "Turkish", "English"))
            t.warm()
        self.assertEqual(closed, ["session", "api"])
        self.assertEqual(made, ["session", "tokenizer"])  # loaded once, never retried
        self.assertEqual(len(logged.records), 1)

    def test_names(self) -> None:
        self.assertEqual(len(MT_LANGUAGES), 46)
        self.assertEqual(source_for("Chinese"), "Chinese (Simplified)")
        self.assertEqual(source_for("Filipino"), "Tagalog")
        self.assertEqual(source_for("Turkish"), "Turkish")
        self.assertIsNone(source_for("Ukrainian"))
        self.assertEqual(app_default("zh_TW"), "Chinese (Traditional)")
        self.assertEqual(app_default("nb"), "Norwegian")
        self.assertIsNone(
            app_default("sr")
        )  # Serbian isn't one of MiLMMT's: the list starts at Off
        self.assertEqual(own_name("Turkish"), "Türkçe")
        self.assertEqual(own_name("Chinese (Simplified)"), "简体中文")
        for name in MT_LANGUAGES:
            self.assertTrue(own_name(name))


if __name__ == "__main__":
    unittest.main()
