"""The caption translator (services.captions.translate) with a fake session and tokenizer: the
prompt, the greedy loop's stops, the clean-up of what the model wrote, the language names."""

import ctypes
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

from services.captions import onnx_c, translate
from services.captions.mt_languages import MT_LANGUAGES, app_default, own_name, source_for
from services.captions.translate import EOS_IDS, MAX_NEW_TOKENS, Translator, clean, prompt

LAYERS = 2
PASTS = [f"past_key_values.{i}.{kv}" for i in range(LAYERS) for kv in ("key", "value")]
PRESENTS = [n.replace("past_key_values", "present") for n in PASTS]

# "I am going home today" (or near it) in each of MiLMMT's 46 languages, in its own script
SAMPLES = {
    "Arabic": "أنا ذاهب إلى البيت اليوم",
    "Azerbaijani": "Bu gün evə gedirəm",
    "Bulgarian": "Днес се прибирам вкъщи",
    "Bengali": "আমি আজ বাড়ি যাচ্ছি",
    "Catalan": "Avui me'n vaig a casa",
    "Czech": "Dnes jdu domů",
    "Danish": "Jeg går hjem i dag",
    "German": "Ich gehe heute nach Hause",
    "Greek": "Σήμερα πηγαίνω σπίτι",
    "English": "I am going home today",
    "Spanish": "Hoy me voy a casa",
    "Persian": "امروز به خانه می‌روم",
    "Finnish": "Menen tänään kotiin",
    "French": "Je rentre à la maison aujourd'hui",
    "Hebrew": "אני הולך הביתה היום",
    "Hindi": "मैं आज घर जा रहा हूँ",
    "Croatian": "Danas idem kući",
    "Hungarian": "Ma hazamegyek",
    "Indonesian": "Hari ini saya pulang ke rumah",
    "Italian": "Oggi vado a casa",
    "Japanese": "今日は家に帰ります",
    "Kazakh": "Мен бүгін үйге барамын",
    "Khmer": "ថ្ងៃនេះខ្ញុំទៅផ្ទះ",
    "Korean": "오늘 집에 갑니다",
    "Lao": "ມື້ນີ້ຂ້ອຍກັບບ້ານ",
    "Malay": "Hari ini saya balik ke rumah",
    "Burmese": "ဒီနေ့ ကျွန်တော် အိမ်ပြန်မယ်",
    "Norwegian": "Jeg går hjem i dag",
    "Dutch": "Ik ga vandaag naar huis",
    "Polish": "Dzisiaj idę do domu",
    "Portuguese": "Hoje vou para casa",
    "Romanian": "Astăzi merg acasă",
    "Russian": "Сегодня я иду домой",
    "Slovak": "Dnes idem domov",
    "Slovenian": "Danes grem domov",
    "Swedish": "Jag går hem idag",
    "Tamil": "நான் இன்று வீட்டுக்குப் போகிறேன்",
    "Thai": "วันนี้ฉันจะกลับบ้าน",
    "Tagalog": "Uuwi ako ngayon",
    "Turkish": "Bugün eve gidiyorum",
    "Urdu": "میں آج گھر جا رہا ہوں",
    "Uzbek": "Bugun uyga boraman",
    "Vietnamese": "Hôm nay tôi về nhà",
    "Cantonese": "我今日返屋企",
    "Chinese (Simplified)": "我今天回家",
    "Chinese (Traditional)": "我們今天回家",
}
# the targets a Latin line is not written in
NOT_LATIN = frozenset(
    {
        "Arabic",
        "Bulgarian",
        "Bengali",
        "Greek",
        "Persian",
        "Hebrew",
        "Hindi",
        "Japanese",
        "Kazakh",
        "Khmer",
        "Korean",
        "Lao",
        "Burmese",
        "Russian",
        "Tamil",
        "Thai",
        "Urdu",
        "Cantonese",
        "Chinese (Simplified)",
        "Chinese (Traditional)",
    }
)


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
        long = "Pekala tamam " * 60  # (room for a long translation)
        t = Fake([10, 12, 13] * 34)  # 102 tokens, no end
        # cut off at the cap, not ended: not the whole translation, the original shows
        self.assertIsNone(t.translate(long, "Turkish", "English"))
        self.assertEqual(len(t.session.calls), MAX_NEW_TOKENS)  # prefill + cap - 1 steps
        self.assertTrue(all(v.released for v in t.session.made))
        short = Fake([10, 12, 13] * 31 + [10, 12, 1])  # ended a token under the cap: shown
        self.assertEqual(len(short.session.script), MAX_NEW_TOKENS)
        self.assertIsNotNone(short.translate(long, "Turkish", "English"))
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
        self.assertTrue(Fake([1]).warm())  # loaded, and its call ran
        t2 = Fake([])  # run raises at once (no scripted token): warm logs, no raise
        with self.assertLogs(translate.logger, "ERROR"):
            self.assertFalse(t2.warm())  # of no use: the captions show as written

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
            self.assertFalse(t.warm())
        self.assertEqual(closed, ["session", "api"])
        self.assertEqual(made, ["session", "tokenizer"])  # loaded once, never retried
        self.assertEqual(len(logged.records), 1)
        # the owner's log gets a record's message alone (worker._LogToOwner): the cause in it
        self.assertIn("OSError: no tokenizer.json", logged.records[0].getMessage())
        self.assertEqual(t.error, "OSError: no tokenizer.json")  # for the worker's warning

    def test_the_site_folder_beside_the_model_is_put_first_on_sys_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qitv-mt-") as name:
            folder = Path(name)
            package = folder / "site" / "tokenizers"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("class Tokenizer:\n    pass\n")
            site = str(folder / "site")
            t = Translator(folder)
            with (
                mock.patch.object(sys, "path", list(sys.path)),
                mock.patch.dict(sys.modules),
                mock.patch.object(onnx_c, "find_library", side_effect=OSError("no library")),
            ):
                sys.modules.pop("tokenizers", None)
                with self.assertRaises(OSError):
                    t._load()
                self.assertEqual(sys.path[0], site)
                self.assertEqual(sys.modules["tokenizers"].__file__, str(package / "__init__.py"))
                with self.assertRaises(OSError):
                    t._load()
                self.assertEqual(sys.path.count(site), 1)
            # no site folder: nothing is added
            with tempfile.TemporaryDirectory(prefix="qitv-mt-") as bare:
                with (
                    mock.patch.object(sys, "path", list(sys.path)),
                    mock.patch.dict(sys.modules, {"tokenizers": mock.Mock()}),
                    mock.patch.object(onnx_c, "find_library", side_effect=OSError("x")),
                ):
                    before = list(sys.path)
                    with self.assertRaises(OSError):
                        Translator(Path(bare))._load()
                    self.assertEqual(sys.path, before)

    def test_a_line_in_each_targets_own_script_passes_and_a_latin_one_fails_where_it_should(
        self,
    ) -> None:
        # the final review: Urdu and Kazakh lines were taken for Latin ones and never shown, and
        # Hebrew, Bengali, Tamil, Khmer, Lao and Burmese letters counted as Latin
        source = "Bugün eve gidiyorum."
        for target in MT_LANGUAGES:
            with self.subTest(target=target):
                line = SAMPLES[target]
                self.assertEqual(clean(source, line, target), line)
                latin = clean(source, "I am going home today.", target)
                if target in NOT_LATIN:
                    self.assertIsNone(latin)
                else:
                    self.assertEqual(latin, "I am going home today.")
        self.assertEqual(set(SAMPLES), set(MT_LANGUAGES))
        self.assertIsNone(clean("Merhaba", "Hola", "Hebrew"))
        self.assertIsNone(clean(source, SAMPLES["Russian"], "Hebrew"))  # another script
        self.assertIsNone(clean(source, SAMPLES["Hebrew"], "Turkish"))  # nor Latin's

    def test_the_speech_side_still_tells_only_its_own_languages(self) -> None:
        from services.captions.text_language import guess, written_in

        for target in ("Hebrew", "Bengali", "Tamil", "Khmer", "Lao", "Burmese"):
            self.assertIsNone(guess(SAMPLES[target]), target)  # none of Qwen's
        self.assertTrue(written_in(SAMPLES["Turkish"], "Turkish"))

    def test_dashes_become_commas_only_between_words(self) -> None:
        for out, shown in (
            ("— Hello", "Hello"),
            ("Hello —", "Hello"),
            ("— Hello — world —", "Hello, world"),
            ("Hello—world", "Hello, world"),
            ("Hello — world", "Hello, world"),
        ):
            self.assertEqual(clean("Merhaba dünya", out, "English"), shown, out)

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
