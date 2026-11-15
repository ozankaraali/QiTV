"""The Turkish Moonshine trial on onnxruntime's C API (services.captions.moon_ort): its text from
tokens, its words, and the loop that stops a decode. With QITV_TR_MOONSHINE naming the model's
folder and sherpa_onnx importable (its onnxruntime library is the one used), the model itself:
a free decode writes what sherpa-onnx writes, and a decode told the first tokens of a free decode
continues it exactly. QITV_TR_MOONSHINE_WAV may name 16 kHz WAV clips (os.pathsep between them);
else a made-up sound is used."""

from array import array
import base64
import math
import os
from pathlib import Path
import random
import tempfile
from typing import Any
import unittest
import wave

from services.captions.moon_ort import (
    FRAME,
    Pieces,
    encoder_frames,
    loop_tail,
    padded,
    silent,
    token_cap,
)

MODEL = os.environ.get("QITV_TR_MOONSHINE", "")
CLIPS = os.environ.get("QITV_TR_MOONSHINE_WAV", "")


def tokens_file(folder: str, pieces: list[bytes]) -> Path:
    path = Path(folder) / "tokens.txt"
    lines = [f"{base64.b64encode(p).decode()} {i}" for i, p in enumerate(pieces)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class PiecesTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="qitv-moon-")
        self.addCleanup(folder.cleanup)
        control = [b"\x00"] * 4
        # 4 " Bu", 5 "gün", 6 ",", 7 " ı", 8 the first byte of "ş", 9 its second, 10 "ık", 11 "."
        word = "ş".encode()
        pieces = control + [b" Bu", "gün".encode(), b",", " ı".encode(), word[:1], word[1:]]
        pieces += ["ık".encode(), b"."]
        self.pieces = Pieces.read(tokens_file(folder.name, pieces))

    def test_text_joins_the_bytes_and_skips_control_tokens(self) -> None:
        self.assertEqual(len(self.pieces), 12)
        self.assertEqual(self.pieces.text([1, 4, 5, 6, 0, 7, 8, 9, 10, 11, 2]), "Bugün, ışık.")
        self.assertEqual(self.pieces.text([5, 7]), "gün ı")
        self.assertEqual(self.pieces.text([8, 10]), "ık")  # half a character: dropped
        self.assertEqual(self.pieces.text([]), "")
        self.assertEqual(self.pieces.text([99]), "")  # unknown: nothing

    def test_a_character_split_over_tokens_is_whole_in_its_word(self) -> None:
        words = self.pieces.words([1, 4, 5, 6, 7, 8, 9, 10, 11])
        self.assertEqual(words, [("Bugün,", [4, 5, 6]), ("ışık.", [7, 8, 9, 10, 11])])

    def test_the_first_token_starts_a_word_without_a_space(self) -> None:
        self.assertEqual(self.pieces.words([5, 6, 4]), [("gün,", [5, 6]), ("Bu", [4])])
        self.assertEqual(self.pieces.words([0, 3]), [])

    def test_a_bad_line_is_an_error(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="qitv-moon-")
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "tokens.txt"
        path.write_text("QQ== 0 extra\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            Pieces.read(path)


class LoopTest(unittest.TestCase):
    def test_one_or_two_tokens_loop_at_four(self) -> None:
        self.assertEqual(loop_tail([5, 9, 9, 9]), 0)
        self.assertEqual(loop_tail([5, 9, 9, 9, 9]), 1)
        self.assertEqual(loop_tail([7, 8] * 3), 0)
        self.assertEqual(loop_tail([1, 7, 8] + [7, 8] * 3), 2)  # four times, one before them

    def test_longer_ones_loop_at_three(self) -> None:
        self.assertEqual(loop_tail([4, 5, 6] * 2), 0)
        self.assertEqual(loop_tail([4, 5, 6] * 3), 3)
        gram = [10, 11, 12, 13, 14, 15, 16, 17]
        self.assertEqual(loop_tail([3] + gram * 3), 8)
        self.assertEqual(loop_tail(gram * 2 + gram[:7]), 0)
        self.assertEqual(loop_tail(list(range(9)) * 3), 0)  # nine tokens: no loop by this rule

    def test_only_the_tail_counts(self) -> None:
        self.assertEqual(loop_tail([9, 9, 9, 9, 4]), 0)
        self.assertEqual(loop_tail([]), 0)

    def test_a_piece_is_padded_to_whole_frames(self) -> None:
        sound = padded([0.5] * 81)
        self.assertEqual(len(sound), 2 * FRAME)
        self.assertEqual((sound[80], sound[81], sound[-1]), (0.5, 0.0, 0.0))
        whole = array("f", [0.25] * FRAME)
        self.assertIs(padded(whole), whole)
        self.assertEqual(len(padded(array("f", [0.25] * 79))), FRAME)

    def test_silence_is_sherpas(self) -> None:
        quiet = array("f", [1e-7, -1e-6] * 9000)
        self.assertTrue(silent(quiet))
        self.assertTrue(silent(array("f")))
        quiet[17001] = 2e-6  # one sample past the first quarter second
        self.assertFalse(silent(quiet))

    def test_the_cap_and_frames_are_sherpas(self) -> None:
        self.assertEqual(encoder_frames(16000 * 8), 400)  # 20 ms frames
        self.assertEqual(encoder_frames(FRAME), 1)
        self.assertEqual(encoder_frames(0), 0)
        self.assertEqual(token_cap(400), 144)  # 18 a second
        self.assertEqual(token_cap(1), 0)


def read_clip(path: str) -> array:
    """A 16-bit PCM WAV at 16 kHz as float samples (channels averaged)."""
    with wave.open(path, "rb") as clip:
        if clip.getframerate() != 16000 or clip.getsampwidth() != 2:
            raise unittest.SkipTest(f"{path}: not 16-bit 16 kHz")
        channels = clip.getnchannels()
        ints = array("h", clip.readframes(clip.getnframes()))
    if channels > 1:
        return array(
            "f",
            (
                sum(ints[i : i + channels]) / channels / 32768.0
                for i in range(0, len(ints), channels)
            ),
        )
    return array("f", (v / 32768.0 for v in ints))


def made_up_sound(seconds: float = 4.0) -> array:
    """Tones and noise: no speech, but something to decode the same way twice."""
    rng = random.Random(7)
    count = int(16000 * seconds)
    return array(
        "f",
        (
            0.2 * math.sin(2 * math.pi * (180 + 40 * (i // 4000)) * i / 16000)
            + 0.05 * rng.uniform(-1.0, 1.0)
            for i in range(count)
        ),
    )


@unittest.skipUnless(MODEL and os.path.isdir(MODEL), "QITV_TR_MOONSHINE names no model folder")
class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            import sherpa_onnx
        except ImportError:
            raise unittest.SkipTest("sherpa_onnx (and its onnxruntime library) not importable")
        from services.captions import onnx_c
        from services.captions.moon_ort import MoonshineOrt

        try:
            onnx_c.find_library()
        except FileNotFoundError as error:
            raise unittest.SkipTest(str(error))
        folder = Path(MODEL)
        cls.model = MoonshineOrt(folder, threads=2)

        def onnx(stem: str) -> str:
            quantized = folder / f"{stem}_quantized.onnx"
            return str(quantized if quantized.exists() else folder / f"{stem}.onnx")

        cls.sherpa = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
            encoder=onnx("encoder_model"),
            decoder=onnx("decoder_model_merged"),
            tokens=str(folder / "tokens.txt"),
            num_threads=2,
        )
        paths = [p for p in CLIPS.split(os.pathsep) if p]
        cls.clips = [read_clip(p) for p in paths] or [made_up_sound()]

    def sherpa_text(self, samples: array) -> str:
        stream = self.sherpa.create_stream()
        stream.accept_waveform(16000, padded(array("f", samples)))
        self.sherpa.decode_stream(stream)
        return str(stream.result.text)

    def test_a_free_decode_writes_what_sherpa_writes(self) -> None:
        for samples in self.clips:
            decoded = self.model.decode(self.model.encode(samples))
            expected = self.sherpa_text(samples)
            mine = self.model.text(decoded.tokens)
            if decoded.looped:  # sherpa-onnx goes on to its cap
                self.assertTrue(expected.startswith(mine), (mine, expected))
            else:
                self.assertEqual(mine, expected)
            self.assertEqual(len(decoded.logprobs), len(decoded.tokens))
            self.assertTrue(all(p <= 0.0 for p in decoded.logprobs))

    def cases(self) -> list[tuple[Any, Any, int]]:
        """(encoded piece, its free decode, k) for k of 1, half and all but one of its tokens."""
        found = []
        for samples in self.clips:
            encoded = self.model.encode(samples)
            free = self.model.decode(encoded)
            count = len(free.tokens)
            found += [
                (encoded, free, k) for k in sorted({1, count // 2, count - 1}) if 0 < k < count
            ]
        return found

    def test_a_stepped_prefix_continues_the_free_decode_exactly(self) -> None:
        for encoded, free, k in self.cases():
            forced = self.model.decode(encoded, prefix=free.tokens[:k], batched=False)
            self.assertEqual(forced.tokens, free.tokens[k:], k)
            self.assertEqual((forced.eos, forced.looped), (free.eos, free.looped))
            self.assertEqual(forced.logprobs, free.logprobs[k:])

    def test_a_prefix_fed_at_once_mostly_continues_it(self) -> None:
        # One call quantizes all its rows together (DynamicQuantizeLinear), so the numbers differ
        # a little from stepping: the next token is the free decode's nearly always, not always.
        cases = self.cases()
        parted = 0
        for encoded, free, k in cases:
            forced = self.model.decode(encoded, prefix=free.tokens[:k])
            if forced.tokens[:1] != free.tokens[k : k + 1]:
                parted += 1
            else:
                self.assertAlmostEqual(forced.logprobs[0], free.logprobs[k], delta=1.5)
        self.assertLessEqual(parted, max(1, len(cases) // 5), f"{parted} of {len(cases)}")

    def test_silence_is_nothing(self) -> None:
        encoded = self.model.encode(array("f", bytes(4 * 16000)))
        self.assertTrue(encoded.silent)
        decoded = self.model.decode(encoded)
        self.assertEqual((decoded.tokens, decoded.eos), ([], True))


if __name__ == "__main__":
    unittest.main()
