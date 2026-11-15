"""The caption translator (services.captions.translate) with a fake session and tokenizer: the
prompt, the greedy loop's stops, the clean-up of what the model wrote, the language names."""

import ctypes
from pathlib import Path
from typing import Any
import unittest
from unittest import mock

from services.captions import translate
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

    def encode(self, text: str) -> Any:
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
