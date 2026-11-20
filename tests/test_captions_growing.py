from array import array
from typing import Any
import unittest

from services.captions import growing
from services.captions.growing import (
    GrowingWriter,
    agreed,
    continues,
    ends_sentence,
    joined_rest,
    place,
    words_of,
)
from services.captions.pieces import RATE, WINDOW
from services.captions.streaming import TRANSLATE_LEAD_S

STEP = 0.1  # the test's blocks of sound


class Script:
    """What is said, word by word on the timeline, and a model that reads it: a stretch of sound
    is written as the words inside it; a word the stretch's end cuts comes out cut ("gü"); a word
    its start cuts is not heard at all (the model needs a word's beginning). Each sample of the
    test's sound holds its own timeline second, so the model and the detector know where they
    are."""

    def __init__(
        self,
        words: list[tuple[str, float, float]],
        cost: float = 0.0,
        stop: bool = False,
        hushed: tuple[tuple[float, float], ...] = (),
        unsure: bool = False,
        joins: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.words = words
        self.cost = cost  # seconds a reading takes per second of sound
        self.stop = stop  # each reading ends with a full stop, as Moonshine's do
        self.hushed = hushed  # stretches inside words the detector hears no voice in
        self.unsure = unsure  # each reading writes its later words a little differently
        self.joins = joins  # two words a reading writes run together when it has both whole
        self.readings: list[tuple[float, float]] = []
        self.clock = [0.0]

    def voiced(self, t: float) -> bool:
        if any(a <= t < b for a, b in self.hushed):
            return False
        return any(start <= t < end + 0.05 for _, start, end in self.words)

    def decode(self, samples: array) -> str:
        first, last = float(samples[0]), float(samples[-1]) + 1.0 / RATE
        self.readings.append((first, last))
        self.clock[0] += self.cost * (last - first)
        out: list[str] = []
        for text, start, end in self.words:
            if start >= first and end <= last:
                if out and (out[-1], text) in self.joins:
                    out[-1] += text  # "kara kedi" heard as "karakedi"
                else:
                    out.append(text)
            elif start >= first and start < last:
                out.append(text[: max(1, len(text) // 2)])  # cut by the end: garbled
        if self.unsure:  # a shouted scene: two readings agree on little
            out = out[:2] + [w + "ı" * (len(self.readings) % 3) for w in out[2:]]
        if self.stop and out and not out[-1].endswith((".", "?", "!")):
            out[-1] += "."
        return " ".join(out)


class Run:
    """A writer fed the script's sound in real time, the picture `lead` seconds behind it (or
    waiting where it is, `held`)."""

    def __init__(self, script: Script, lead: float, held: bool = False, late: float = 0.4):
        self.script = script
        self.lead = lead
        self.held = held
        self.viewer = [0.0]
        self.late = late  # the detector says speech this long after the voice starts
        self.writer = GrowingWriter(
            script.decode,
            lambda window: script.voiced(float(window[0]) - self.late),
            lambda window: script.voiced(float(window[0])),
            self.viewer,
            clock=lambda: script.clock[0],
        )
        self.lines: list[tuple[str, float, float, float]] = []  # text, start, end, when out
        self.t = 0.0

    def play(self, until: float) -> "Run":
        clock = self.script.clock
        while self.t < until - 1e-9:
            n = round(STEP * RATE)
            samples = array("f", (self.t + i / RATE for i in range(n)))
            clock[0] = max(clock[0], self.t + STEP)  # the block has come (readings take time)
            if not self.held:
                self.viewer[0] = max(0.0, clock[0] - self.lead)
            for text, start, end in self.writer.feed(self.t, samples):
                self.lines.append((text, start, end, clock[0]))
            self.t += STEP
        return self

    def end(self) -> "Run":
        for text, start, end in self.writer.flush():
            self.lines.append((text, start, end, self.script.clock[0]))
        return self

    def words(self) -> list[str]:
        return [growing.norm(w) for text, *_ in self.lines for w in text.split()]

    def full_stops(self) -> list[str]:
        """The words a line ends with a full stop after."""
        return [text.split()[-1] for text, *_ in self.lines if text.endswith(".")]

    def on_time(self) -> bool:
        """Every line was out before the picture got to it (the picture `lead` behind)."""
        return all(out <= start + self.lead for _, start, _, out in self.lines)


def sentence(text: str, start: float, word_s: float = 0.4, gap: float = 0.12):
    words, t = [], start
    for w in text.split():
        words.append((w, t, t + word_s))
        t += word_s + gap
    return words


# distinct words for long synthetic speech (a model's words are seldom alike side by side)
LEXICON = (
    "araba bahçe çiçek deniz elma fener gemi halı ışık iğne jilet kalem lamba masa nehir "
    "orman pencere radyo saat tahta uçak vapur yastık zeytin anahtar bulut cadde dolap ekmek "
    "fincan gazete harita insan kapı limon mektup nokta okul pazar resim sokak tabak ufuk "
    "yaprak zaman ayna balık cami davul eldiven fırın gitar hamak ipek kilit"
).split()


class HelpersTest(unittest.TestCase):
    def test_a_word_said_again_that_the_written_words_hold_once_writes_nothing_twice(self) -> None:
        # An eval drama clip at an 8 s lead: a partial reading wrote "... barış da yeter. Kardeşim"
        # (it heard "yeter" once); the next reading heard "yeter yeter. Kardeşim çok severdi ...".
        # After "yeter" and after "Kardeşim" were equally near; the earlier was taken and the line
        # read "... yeter. Kardeşim yeter. Kardeşim çok severdi ...".
        held = (
            "yapabildim ama yeter umarım. Yani yeter bence de ya. Bir sürü barış da yeter. Kardeşim"
        ).split()
        heard = (
            "Bir sürü barıştı, yeter yeter. Kardeşim çok severdi bunları, özellikle çikolatalısını."
        ).split()
        self.assertEqual(heard[continues(held, heard, 5) :][0], "çok")

    def test_a_reading_goes_on_past_the_words_written_from_it(self) -> None:
        self.assertEqual(continues(["Burada", "her"], ["Burada", "her", "gün", "yeni"]), 2)
        # written a little differently the second time: still past them
        self.assertEqual(continues(["Burada", "her"], ["burada", "her,", "gün"]), 2)
        self.assertEqual(continues(["Burada", "hep"], ["Burada", "her", "gün"]), 2)
        # a word the reading added before them
        self.assertEqual(continues(["her", "gün"], ["Burada", "her", "gün", "yeni"]), 3)
        self.assertEqual(continues([], ["Burada"]), 0)
        # a reading that begins before the words it was expected to hold (the words' times are
        # guesses): the written ones it has again are not new (2026-11-07, the owner: "Sen de
        # iletişime geç, riske atmayalım." and then "de iletişime geç, riske atmayalım.")
        written = ["Sen", "de", "iletişime", "geç,", "riske", "atmayalım."]
        heard = ["de", "iletişime", "geç,", "riske", "atmayalım.", "Tabii", "Doktor"]
        self.assertEqual(continues(written, heard), 5)
        self.assertEqual(continues(written, ["Tabii", "Doktor"]), 0)
        self.assertEqual(continues(written, ["atmayalım.", "Tabii"]), 1)
        # a written word the reading left out costs it no new word...
        self.assertEqual(continues(["bir", "hafta", "sonu"], ["bir", "hafta", "geçirmenizi"]), 2)
        # ... and one it read a little differently is still that word
        self.assertEqual(continues(["bir", "hafta", "sonu"], ["bir", "hafta", "sonun", "iyi"]), 3)
        # a written word read again in two, or two read again run together (2026-11-08)
        self.assertEqual(continues(["oraya", "Karakedi"], ["oraya", "Kara", "kedi", "yolunu"]), 3)
        written = ["oraya", "Karakedi", "yolunu"]
        self.assertEqual(continues(written, ["oraya", "Kara", "kedi", "yolunu", "kesti"]), 4)
        self.assertEqual(continues(["Kara", "kedi"], ["Karakedi", "yolunu"]), 1)

    def test_a_word_read_again_run_together_with_the_next_gives_the_next_back(self) -> None:
        # the owner, 2026-11-08: "Kara" written, a later reading had "Karakedi"
        self.assertEqual(joined_rest("Kara", "Karakedi,", ["kedi"]), "kedi,")
        self.assertEqual(joined_rest("Kara", "Karakedi", ["ke"]), "kedi")  # its end cut
        self.assertEqual(joined_rest("ben", "Bende", ["de"]), "de")
        # the same word read a little differently: nothing new in it
        self.assertIsNone(joined_rest("seviyor", "seviyorum", ["çünkü"]))
        self.assertIsNone(joined_rest("bahanemiz", "bahanemizi", ["istiyoruz"]))
        self.assertIsNone(joined_rest("gel", "geliyorum", ["iyi"]))
        self.assertIsNone(joined_rest("Kara", "Karakedi", []))  # nothing heard after it
        self.assertIsNone(joined_rest("Kara", "Karam", ["kedi"]))

    def test_two_readings_agree_without_case_or_marks(self) -> None:
        self.assertEqual(agreed(["Burada", "her", "gü"], ["burada", "her,", "gün"]), 2)
        self.assertEqual(agreed(["İyi", "akşamlar."], ["iyi", "akşamlar"]), 2)

    def test_a_mark_on_its_own_goes_with_the_word_before(self) -> None:
        self.assertEqual(words_of("geldik . ''Evet'' ,"), ["geldik.", "''Evet'',"])
        self.assertEqual(words_of(" . "), [])

    def test_a_sentence_ends_before_its_closing_quotes(self) -> None:
        self.assertTrue(ends_sentence("''Köşesindesin.''"))
        self.assertFalse(ends_sentence("köşesindesin"))

    def test_words_spread_over_the_voiced_windows_by_their_letters(self) -> None:
        step = WINDOW / RATE
        voiced = [False] * 10 + [True] * 20 + [False] * 5 + [True] * 10
        words = place(["Bir", "mücadele"], 100.0, voiced)
        self.assertAlmostEqual(words[0].start, 100.0 + 10 * step)  # not in the silence before
        self.assertLess(words[0].end, words[1].start + 1e-9)
        self.assertAlmostEqual(words[1].end, 100.0 + 45 * step)
        # nothing voiced: over all of it
        self.assertEqual(place(["a"], 5.0, [False] * 4)[0].start, 5.0)


class GrowingWriterTest(unittest.TestCase):
    def test_an_utterance_the_lead_covers_is_read_once_whole(self) -> None:
        script = Script(sentence("Tek ihtimalin kazanmak olduğu bir mücadele", 2.0))
        run = Run(script, lead=8.0).play(10.0).end()
        self.assertEqual(len(script.readings), 1)  # at its pause, never cut
        self.assertEqual(
            [t for t, *_ in run.lines], ["Tek ihtimalin kazanmak olduğu bir mücadele."]
        )
        start = run.lines[0][1]
        self.assertAlmostEqual(start, 2.0 - growing.FIRST_WORD_S, delta=0.1)  # at its first word
        self.assertTrue(run.on_time())

    def test_with_the_picture_close_no_word_is_lost_or_written_twice(self) -> None:
        # one long utterance, the picture 3 s behind: it can't wait for the pause
        text = (
            "Burada her gün yeni bir mücadele var tek ihtimalin kazanmak olduğu bir mücadele "
            "ağırlığı ölçü birimleriyle ifade edilemeyecek sorumluluklar"
        )
        script = Script(sentence(text, 1.0), cost=0.05)
        run = Run(script, lead=3.0).play(16.0).end()
        self.assertGreater(len(script.readings), 2)  # read again as it grew
        self.assertEqual(run.words(), [growing.norm(w) for w in text.split()])
        self.assertTrue(run.on_time())

    def test_a_waiting_picture_lets_the_utterance_end_first(self) -> None:
        # right after a tune: the picture waits until it is the captions' distance behind
        script = Script(sentence("İyi bir hafta sonu geçirmenizi diliyorum", 1.0))
        run = Run(script, lead=0.0, held=True).play(8.0).end()
        self.assertEqual(len(script.readings), 1)
        self.assertEqual(run.lines[0][0], "İyi bir hafta sonu geçirmenizi diliyorum.")

    def test_the_first_word_after_a_silence_is_heard(self) -> None:
        # the detector says speech 0.4 s late: the utterance's sound still begins before it
        script = Script([("İhtiyar", 12.0, 12.5)] + sentence("hayırlı akşamlar", 12.6))
        run = Run(script, lead=8.0, late=0.4).play(18.0).end()
        self.assertEqual(run.lines[0][0], "İhtiyar hayırlı akşamlar.")

    def test_a_very_long_utterance_ends_at_a_pause_after_its_written_words(self) -> None:
        # some 21 s without a pause that ends an utterance; a short one after every 6th word
        words, t = [], 1.0
        for i, word in enumerate(LEXICON[:40]):
            words.append((word, t, t + 0.4))
            t += 0.4 + (0.3 if i % 6 == 5 else 0.05)
        for lead in (4.0, 30.0):
            script = Script(words, cost=0.02, stop=True)
            run = Run(script, lead=lead).play(t + 2.0).end()
            self.assertEqual(run.words(), LEXICON[:40], lead)
            longest = max(last - first for first, last in script.readings)
            self.assertLessEqual(longest, growing.LONG_S + 0.5, lead)

    def test_while_translating_a_line_is_out_the_translators_time_sooner(self) -> None:
        # 2.10: a line's translation takes its time before the picture gets to it; the picture
        # is held that much further back (ui.captions_controller), which then buys it back
        words = LEXICON[:40]

        def played(lead: float, reserve: float) -> Run:
            # (said once the picture plays at either lead: the test's picture waits at 0 till then)
            script = Script(sentence(" ".join(words), 12.0, gap=0.0), cost=0.02)
            run = Run(script, lead=lead)
            run.writer.reserve = reserve
            return run.play(36.0).end()

        def before(run: Run, seconds: float) -> bool:
            return all(out <= start + seconds for _, start, _, out in run.lines)

        plain, translating = played(8.0, 0.0), played(8.0, TRANSLATE_LEAD_S)
        self.assertEqual(translating.words(), words)
        self.assertTrue(before(translating, 8.0 - TRANSLATE_LEAD_S))  # the translator's time
        self.assertFalse(before(plain, 8.0 - TRANSLATE_LEAD_S))  # at the last moment
        held = played(8.0 + TRANSLATE_LEAD_S, TRANSLATE_LEAD_S)
        self.assertEqual(held.lines, plain.lines)  # the same lines, at the same moments

    def test_readings_that_agree_on_little_still_keep_up_with_the_picture(self) -> None:
        # 2026-11-07, a shouted drama scene: readings agreed on a word at a time, a line came
        # after the picture passed it
        text = " ".join(LEXICON[:30])
        for lead in (3.0, 5.0):
            script = Script(sentence(text, 1.0, gap=0.0), cost=0.08, unsure=True)
            run = Run(script, lead=lead).play(16.0).end()
            self.assertTrue(run.on_time(), lead)
            self.assertEqual(len(run.words()), 30, lead)

    def test_a_word_said_again_and_again_is_written_as_often(self) -> None:
        text = "Kalk kalk kalk hadi kalk ayağa kalk kalk dedim sana"
        for lead in (3.0, 5.0, 8.0):
            script = Script(sentence(text, 1.0, gap=0.0), cost=0.05, stop=True)
            run = Run(script, lead=lead).play(10.0).end()
            self.assertEqual(run.words(), [growing.norm(w) for w in text.split()], lead)

    def test_speech_with_no_pause_at_all_loses_no_word_and_writes_none_twice(self) -> None:
        # music under the words holds both detectors: 24 s with no pause between words
        words = LEXICON[:60]
        for lead in (3.0, 8.0, 30.0):
            script = Script(sentence(" ".join(words), 1.0, gap=0.0), cost=0.02)
            run = Run(script, lead=lead).play(30.0).end()
            self.assertEqual(run.words(), words, lead)
            self.assertTrue(run.on_time(), lead)

    def test_words_whose_times_were_guessed_wrong_are_still_written_once(self) -> None:
        # The words' times are spread by their letters: here they are spoken at uneven speeds,
        # so the guesses are off by up to a second (a reading's start held more written words
        # than guessed: the owner's live log, 2026-11-07, wrote a line again without its first
        # word).
        lengths = [0.9, 0.15, 0.6, 0.2, 0.8, 0.15, 0.25, 0.9, 0.3, 0.2, 0.7, 0.15]
        words, t = [], 1.0
        for i in range(48):
            length = lengths[i % len(lengths)]
            words.append((LEXICON[i], t, t + length))
            t += length + (0.12 if i % 5 == 4 else 0.02)
        names = [w for w, _, _ in words]
        for lead in (3.0, 5.0, 8.0):
            script = Script(words, cost=0.05, stop=True)
            run = Run(script, lead=lead).play(t + 2.0).end()
            self.assertEqual(run.words(), names, lead)
            self.assertTrue(run.on_time(), lead)

    def test_words_placed_a_second_early_are_not_written_again(self) -> None:
        # the same, made sure of: every word's time guessed a second early
        from unittest import mock

        place = growing.place

        def early(words, start, voiced):
            out = place(words, start, voiced)
            for w in out:
                w.start, w.end = w.start - 1.0, w.end - 1.0
            return out

        text = " ".join(LEXICON[:40])
        with mock.patch.object(growing, "place", early):
            for lead in (3.0, 5.0):
                script = Script(sentence(text, 1.0, gap=0.0), cost=0.05, stop=True)
                run = Run(script, lead=lead).play(25.0).end()
                self.assertEqual(run.words(), text.split(), lead)

    def test_a_hush_inside_a_word_is_no_place_to_cut_it(self) -> None:
        # The detector hears no voice for 0.12 s inside "bunu" (the owner's live log,
        # 2026-11-07: "Yani bütün hemşireler odada bu." then "konuşuyorlar."): nothing is
        # lost there and no full stop comes before the sentence ends.
        text = "Yani bütün hemşireler odada bunu konuşuyorlar artık her gün"
        words = sentence(text, 1.0, word_s=0.45, gap=0.03)
        bunu = next((start, end) for w, start, end in words if w == "bunu")
        middle = (bunu[0] + bunu[1]) / 2
        for lead in (3.0, 4.0, 5.0):
            script = Script(words, cost=0.05, stop=True, hushed=((middle - 0.06, middle + 0.06),))
            run = Run(script, lead=lead).play(10.0).end()
            self.assertEqual(run.words(), [growing.norm(w) for w in text.split()], lead)
            self.assertEqual(run.full_stops(), ["gün."], lead)

    def test_a_word_a_later_reading_runs_into_the_one_before_is_not_lost(self) -> None:
        # The owner's live log, 2026-11-08: a phrase like "kara kedi yolunu kesti" came as
        # "Kara" and "yolunu kesti.": a reading wrote "Kara" (its end cut "kedi"), a later one
        # ran the two together ("Karakedi") and that was taken for the written word. The other
        # way round ("Karakedi" written, read again as "Kara kedi"), "kedi" came twice.
        head = "Hediye değil mi evet söylemiyorum hazır mısın hazırım bak oraya Kara"
        tail = "yolunu kesti dedi annem ben de güldüm sonra çok güzel teşekkür ediyorum"
        words = sentence(head, 1.0, gap=0.03)
        t = words[-1][2] + 0.03
        words.append(("kedi", t, t + 1.0))  # drawn out: a reading can end inside it
        words += sentence(tail, t + 1.03, gap=0.03)
        said = [growing.norm(w) for w, _, _ in words]

        class Once(Script):  # runs the two together in one reading only
            def decode(self, samples: array) -> str:
                text = super().decode(samples)
                if "Karakedi" in text:
                    self.joins = ()
                return text

        cases = ((Script, 0.03, 2.5), (Script, 0.06, 4.0), (Once, 0.03, 3.0), (Once, 0.1, 3.0))
        for kind, cost, lead in cases:
            script = kind(words, cost=cost, stop=True, joins=(("Kara", "kedi"),))
            run = Run(script, lead=lead).play(16.0).end()
            heard = " ".join(run.words()).replace("karakedi", "kara kedi").split()
            self.assertEqual(heard, said, (kind.__name__, cost, lead))

    def test_an_utterance_the_picture_passed_is_not_read(self) -> None:
        script = Script(sentence("Geç kaldım", 1.0))
        run = Run(script, lead=0.0, held=True)
        run.viewer[0] = 50.0  # the picture is far past it
        run.play(4.0).end()
        self.assertEqual(script.readings, [])
        self.assertEqual(run.lines, [])

    def test_two_lines_never_start_or_end_alike(self) -> None:
        script = Script(sentence("Geldi. Gitti. Kaldı. Döndü.", 1.0, word_s=0.2, gap=0.05))
        run = Run(script, lead=8.0).play(6.0).end()
        starts = [s for _, s, _, _ in run.lines]
        ends = [e for _, _, e, _ in run.lines]
        self.assertEqual(len(set(starts)), len(starts))
        self.assertEqual(len(set(ends)), len(ends))
        self.assertEqual(starts, sorted(starts))

    def test_what_is_still_to_come_starts_at_the_first_word_not_out(self) -> None:
        script = Script(sentence("Burada her gün yeni bir mücadele var", 2.0))
        run = Run(script, lead=8.0).play(4.0)
        self.assertLessEqual(run.writer.frontier(), 2.0)  # going on: none of it is out
        run.play(10.0)
        self.assertAlmostEqual(run.writer.frontier(), run.writer.newest)  # all of it is out


if __name__ == "__main__":
    unittest.main()


class WorkerTest(unittest.TestCase):
    def run_of(self, script: Script):
        import queue
        import threading

        from services.captions.worker import TURKISH, _Run

        sent: list[tuple] = []
        run = object.__new__(_Run)
        run.stopped, run.blocks, run.rtf, run.run = threading.Event(), queue.Queue(), 0.5, 7
        run.link = type("Link", (), {"send": staticmethod(sent.append)})()
        run._said, run.language, run.viewer = None, TURKISH, [0.0]
        run.writer = GrowingWriter(
            script.decode,
            lambda window: script.voiced(float(window[0]) - 0.3),
            lambda window: script.voiced(float(window[0])),
            run.viewer,
            clock=lambda: script.clock[0],
        )
        return run, sent

    def test_turkish_lines_reach_the_captions_capitalised_with_numbers_as_digits(self) -> None:
        script = Script(sentence("tam yirmi sekiz yıllık bir mücadele", 1.0))
        run, sent = self.run_of(script)
        t = 0.0
        while t < 6.0:
            run.blocks.put((t, array("f", (t + i / RATE for i in range(1600)))))
            t += STEP
        run.blocks.put(None)
        run._stream()
        captions = [m for m in sent if m[0] == "caption"]
        self.assertEqual([m[4] for m in captions], ["Tam 28 yıllık bir mücadele."])
        self.assertEqual(captions[0][1:3], (7, run.writer._last[0]))

    def test_a_failing_turkish_model_hands_turkish_to_qwen(self) -> None:
        # the final review: the stream's thread ended, the writer stayed, and every block piled
        # up where nothing read it (no captions, "Can't keep up", no "ended")
        import queue
        from types import SimpleNamespace

        from services.captions import worker

        run, sent = self.run_of(Script(sentence("tam yirmi sekiz yıllık bir mücadele", 1.0)))

        def broken() -> list:
            raise RuntimeError("bad allocation")

        run.writer.work = broken
        model: Any = object.__new__(worker._Model)
        model.beam = model.turkish = object()
        model.moonshine, model._folders = True, {}
        run.model = model
        run.blocks.put((0.0, array("f", bytes(4 * 1600))))
        with self.assertLogs("services.captions.worker", "WARNING") as logs:
            run._stream()
        self.assertIsNone(run.writer)  # pieces again
        self.assertIn("RuntimeError: bad allocation", logs.records[0].getMessage())
        self.assertEqual((model.beam, model.turkish, model.moonshine), (None, None, False))
        self.assertTrue(run.qwen_writes())  # Qwen writes Turkish now
        pushed: list = []

        def push(at: float, floats: Any) -> list:
            pushed.append(at)
            return []

        run.cutter = SimpleNamespace(set_max=lambda seconds: None, push=push)
        run.pieces, run.newest = queue.Queue(), 0.0
        run._take(1.0, array("f", bytes(4 * 1600)))
        self.assertEqual(pushed, [1.0])  # cut into pieces, not left in the blocks
        self.assertTrue(run.blocks.empty())
        run._maybe_stream()  # ("language", "Turkish") again: no growing writer
        self.assertIsNone(run.writer)

    def test_a_reading_keeps_nothing_nobody_said(self) -> None:
        from types import SimpleNamespace

        from services.captions.worker import _Run

        run: Any = object.__new__(_Run)
        for text, kept in (
            ("Bu, iki bin on yedide, iki bin on yedide, iki bin on yedide!", ""),
            ("Тот самый день", ""),  # another language's letters
            ("Sevgi, iyilik ve umutla.", "Sevgi, iyilik ve umutla."),
        ):
            run.model = SimpleNamespace(
                decode=lambda samples, language, text=text, fast=False: text
            )
            self.assertEqual(run._moon_reading(array("f")), kept)


class LookbackTest(unittest.TestCase):
    def test_a_piece_leads_in_from_the_voice_the_held_detector_heard_late(self) -> None:
        from services.captions.pieces import Cutter

        def cutter(lookback: float) -> Cutter:
            return Cutter(
                lambda window: 2.5 <= float(window[0]) < 4.0,  # says speech half a second late
                lambda window: 2.0 <= float(window[0]) < 3.5,  # the voice itself
                lookback=lookback,
            )

        for lookback, start in ((0.0, 2.2), (0.6, 1.7)):
            c = cutter(lookback)
            pieces = []
            t = 0.0
            while t < 6.0:
                pieces += c.push(t, array("f", (t + i / RATE for i in range(1600))))
                t += STEP
            self.assertAlmostEqual(pieces[0].start, start, delta=0.05)


class ModelLoadTest(unittest.TestCase):
    """The captions process loads Qwen at once and, with its files given, our Turkish Moonshine
    read with our beam (2.10); the log names the model each language is written by."""

    def load(self, beam: Any, loaded: list, factory: Any = None, **extra: Any) -> Any:
        """worker._Model with a fake sherpa-onnx (Qwen says "Good evening.") and `beam` for
        moon_beam.BeamReader; the Turkish model's folder is named as its own. `factory`: the
        Turkish LM's loader (moon_reading's), `extra`: more of the load's spec."""
        from contextlib import nullcontext
        import shutil
        import sys
        import tempfile
        from types import SimpleNamespace
        from unittest import mock

        from services.captions import worker

        class Recognizer:
            def create_stream(self):
                class Stream:
                    result = SimpleNamespace(text="Good evening.")

                    def set_option(self, key, value):
                        pass

                    def accept_waveform(self, rate, samples):
                        pass

                return Stream()

            def decode_stream(self, stream):
                pass

        def qwen(**_):
            loaded.append("qwen")
            return Recognizer()

        fake = SimpleNamespace(
            OfflineRecognizer=SimpleNamespace(from_qwen3_asr=qwen),
            VadModelConfig=lambda: SimpleNamespace(silero_vad=SimpleNamespace()),
            VadModel=SimpleNamespace(create=lambda config: SimpleNamespace(is_speech=bool)),
        )
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        spec = {"model": dict.fromkeys(("conv_frontend", "encoder", "decoder", "tokenizer"), "x")}
        spec.update(vad="vad.onnx", turkish=folder, **extra)
        with (
            mock.patch.dict(sys.modules, {"sherpa_onnx": fake}),
            mock.patch("services.captions.moon_beam.BeamReader", beam),
            (
                mock.patch("services.captions.moon_reading._lstm", factory)
                if factory
                else nullcontext()
            ),
        ):
            return worker._Model(spec)

    class _Beam:
        """moon_beam.BeamReader: the beam alone writes "iyi akşamlar"; its model is
        test_captions_reading's, where the beam with an LM that prefers it writes "11"."""

        def __init__(self, folder, lm, threads=2):
            from test_captions_reading import FakeModel

            self.model = FakeModel()
            self.widths: list = []

        def warm(self):
            pass

        def transcribe(self, samples, width=None):
            self.widths.append(width)
            return " iyi akşamlar "

    def test_turkish_is_read_with_the_lm_the_load_names(self) -> None:
        # 2.12.1: the GUI names the Turkish model's LM once it is here beside the model
        from pathlib import Path

        from test_captions_reading import PrefersEleven, lm_file

        from services.captions import worker

        path, made = lm_file(self), []

        def factory(where, api):
            made.append(Path(where))
            return PrefersEleven()

        model = self.load(self._Beam, [], factory, turkish_lm=str(path))
        self.assertEqual(made, [path])  # loaded with the model (its warm-up)
        sound = array("f", bytes(4 * 1600))
        self.assertEqual(model.decode(sound, worker.TURKISH), "11")
        self.assertEqual(model.wrote(), model.name(worker._TURKISH_ROLE))  # named as before
        self.assertEqual(model.decode(sound, worker.TURKISH, fast=True), "iyi akşamlar")
        self.assertEqual(model.beam.beam.widths, [1])  # at 2x its best path alone, no LM
        self.assertEqual(made, [path])

    def test_without_the_lm_turkish_is_read_as_in_2_12_0(self) -> None:
        from services.captions import worker

        made: list = []
        model = self.load(self._Beam, [], lambda where, api: made.append(where))
        sound = array("f", bytes(4 * 1600))
        self.assertEqual(model.decode(sound, worker.TURKISH), "iyi akşamlar")
        self.assertEqual(model.beam.beam.widths, [None])  # the beam's own reading
        self.assertEqual(made, [])

    def test_an_lm_that_does_not_load_never_hands_turkish_to_qwen(self) -> None:
        from test_captions_reading import lm_file

        from services.captions import worker

        def broken(where, api):
            raise OSError("lm.int8.onnx is damaged")

        with self.assertLogs("services.captions.moon_reading", "WARNING") as logs:
            model = self.load(self._Beam, [], broken, turkish_lm=str(lm_file(self)))
        self.assertEqual(len(logs.records), 1)
        self.assertTrue(model.moonshine)
        self.assertEqual(model.decode(array("f", bytes(4 * 1600)), worker.TURKISH), "iyi akşamlar")
        self.assertNotEqual(model.writes(worker.TURKISH), "Qwen3-ASR")

    def test_an_lm_downloaded_since_the_load_is_read_with_from_the_next_piece(self) -> None:
        from test_captions_reading import PrefersEleven, lm_file

        from services.captions import worker

        model = self.load(self._Beam, [], lambda where, api: PrefersEleven())
        sound = array("f", bytes(4 * 1600))
        self.assertEqual(model.decode(sound, worker.TURKISH), "iyi akşamlar")
        model.use_turkish_lm(str(lm_file(self)))
        self.assertEqual(model.decode(sound, worker.TURKISH), "11")

    def test_a_turkish_lm_message_reaches_the_model(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from services.captions import worker

        used: list = []

        class Model:
            def __init__(self, spec):
                pass

            def use_turkish_lm(self, path):
                used.append(path)

        messages: list = [
            ("turkish_lm", "/early/lm.int8.onnx"),
            ("load", {}),
            ("turkish_lm", "/ai-captions/lm/lm.int8.onnx"),
            ("quit",),
        ]
        sent: list = []
        link = SimpleNamespace(conn=SimpleNamespace(recv=lambda: messages.pop(0)), send=sent.append)
        with mock.patch.object(worker, "_Model", Model):
            worker._serve(link)
        self.assertEqual(used, ["/ai-captions/lm/lm.int8.onnx"])  # before the load: no model
        self.assertIn(("ready",), sent)

    def test_a_turkish_model_that_does_not_load_leaves_turkish_to_qwen(self) -> None:
        # the final review: it made the whole process fail ("Couldn't start AI captions"), in
        # every language, for good
        from services.captions import worker

        class Fails:
            def __init__(self, folder, lm, threads=2):
                raise OSError("model.onnx is damaged")

        class WarmFails:
            def __init__(self, folder, lm, threads=2):
                pass

            def warm(self):
                raise RuntimeError("bad allocation")

        for beam, said in ((Fails, "OSError: model.onnx"), (WarmFails, "RuntimeError: bad")):
            with self.subTest(beam=beam.__name__):
                loaded: list = []
                with self.assertLogs("services.captions.worker", "WARNING") as logs:
                    model = self.load(beam, loaded)
                self.assertIn(said, logs.records[0].getMessage())
                self.assertEqual((model.beam, model.turkish, model.moonshine), (None, None, False))
                turkish = model.decode(array("f", bytes(4 * 1600)), worker.TURKISH)
                self.assertEqual(turkish, "Good evening.")  # Qwen's
                self.assertEqual(model.writes(worker.TURKISH), "Qwen3-ASR")

    def test_qwen_and_the_turkish_moonshine_load_at_once(self) -> None:
        import os
        import shutil
        import sys
        import tempfile
        from types import SimpleNamespace
        from unittest import mock

        from services.captions import worker

        loaded: list[str] = []

        class Recognizer:
            def create_stream(self):
                class Stream:
                    result = SimpleNamespace(text="Good evening.")

                    def set_option(self, key, value):
                        pass

                    def accept_waveform(self, rate, samples):
                        pass

                return Stream()

            def decode_stream(self, stream):
                pass

        def qwen(**_):
            loaded.append("qwen")
            return Recognizer()

        class Beam:
            def __init__(self, folder, lm, threads=2):
                loaded.append(("beam", folder.name, lm))

            def warm(self):
                pass

            def transcribe(self, samples, width=None):
                return " iyi akşamlar "

        fake = SimpleNamespace(
            OfflineRecognizer=SimpleNamespace(from_qwen3_asr=qwen),
            VadModelConfig=lambda: SimpleNamespace(silero_vad=SimpleNamespace()),
            VadModel=SimpleNamespace(create=lambda config: SimpleNamespace(is_speech=bool)),
        )
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        spec = {"model": dict.fromkeys(("conv_frontend", "encoder", "decoder", "tokenizer"), "x")}
        spec.update(vad="vad.onnx", turkish=folder)
        with (
            mock.patch.dict(sys.modules, {"sherpa_onnx": fake}),
            mock.patch("services.captions.moon_beam.BeamReader", Beam),
        ):
            model = worker._Model(spec)
        name = os.path.basename(folder)
        self.assertEqual(loaded, ["qwen", ("beam", name, None)])  # the beam alone, no n-gram
        self.assertIsNotNone(model.recognizer)
        self.assertEqual(model.decode(array("f", bytes(4 * 1600)), worker.TURKISH), "iyi akşamlar")
        self.assertEqual(model.wrote(), name)
        self.assertEqual(model.decode(array("f", bytes(4 * 1600)), "English"), "Good evening.")
        self.assertEqual(model.writes(worker.TURKISH), name)
        self.assertEqual(model.writes("English"), "Qwen3-ASR")


class TitlePrefixTest(unittest.TestCase):
    """2026-11-07, the owner: playlists name a channel after its country ("TR: …", "TR| …"): a
    channel whose captions language is not known yet takes it from there (made-up titles)."""

    def test_a_clear_prefix_names_the_language(self) -> None:
        from services.captions.languages import language_from_title as language

        self.assertEqual(language("TR: Channel One"), "Turkish")
        self.assertEqual(language("TR| Channel One"), "Turkish")
        self.assertEqual(language("TR | Channel One"), "Turkish")
        self.assertEqual(language("DE - Sport Eins"), "German")
        self.assertEqual(language("UK| News Channel"), "English")  # the United Kingdom's
        self.assertEqual(language("AR: Channel Two"), "Arabic")
        self.assertEqual(language("FR - Film Channel"), "French")
        self.assertEqual(language("TUR: Channel One"), "Turkish")
        self.assertEqual(language("Channel One", "TR | News"), "Turkish")  # its group's

    def test_anything_unclear_names_none(self) -> None:
        from services.captions.languages import language_from_title as language

        for title in (
            "Channel One",
            "HD: Channel One",
            "4K| Channel One",
            "Tr: Channel One",
            "TR Channel One",
            "TR-Channel One",
            "BE: Channel One",  # Dutch or French
            "IND: Channel One",  # a country, not a language
            "",
        ):
            self.assertEqual(language(title), "", title)

    def controller(self, title: str, group: str = ""):
        from types import SimpleNamespace

        from PySide6.QtWidgets import QApplication, QWidget

        from ui.captions_controller import CaptionsController

        QApplication.instance() or QApplication([])
        window = QWidget()
        target = SimpleNamespace(title=title, source="p", item={"group": group}, live=True)
        window.controller = SimpleNamespace(now_playing=lambda: target, live=None)
        captions = CaptionsController(window)
        captions._config_get = lambda key, default=None: default
        captions._track_language = lambda: ""
        return captions

    def test_the_controller_starts_a_guessed_language_and_says_so(self) -> None:
        captions = self.controller("TR: Channel One")
        captions._resolve_language()
        self.assertEqual(captions._language[0], "Turkish")
        self.assertTrue(captions._guessed)
        captions = self.controller("Channel One", "DE | Sport")
        captions._resolve_language()
        self.assertEqual(captions._language[0], "German")

    def test_a_language_the_viewer_chose_stays_theirs(self) -> None:
        captions = self.controller("TR: Channel One")
        captions._config_get = lambda key, default=None: (
            {captions._pick_key(): "English"} if key == "ai_captions_picks" else default
        )
        captions._resolve_language()
        self.assertEqual(captions._language[0], "English")
        self.assertFalse(captions._guessed)

    def guessed_run(self, said: list[str], language: str = "Turkish"):
        from types import SimpleNamespace
        from unittest import mock

        from services.captions.worker import _Run

        sent: list[tuple] = []
        texts = iter(said)
        model = SimpleNamespace(
            moonshine=True,
            turkish=object(),
            prepare=lambda language: None,
            is_speech=lambda window: False,
            is_voiced=lambda window: False,
            decode=lambda samples, language, fast=False: next(texts),
        )
        run = object.__new__(_Run)
        with mock.patch("services.captions.decoder.AudioDecoder"):
            _Run.__init__(
                run,
                SimpleNamespace(send=sent.append),
                model,
                4,
                "",
                0.0,
                None,
                language,
                [0.0],
                True,
            )
        with mock.patch("services.captions.worker.threading.Thread"):
            run._maybe_stream()
        return run, model, sent

    def test_a_guess_the_readings_bear_out_stands(self) -> None:
        from services.captions.growing import GrowingWriter

        said = ["Bu akşam tarih konuşacağız.", "Osmanlı devleti çok büyük bir devletti."] * 2
        run, model, sent = self.guessed_run(said)
        self.assertIsInstance(run.writer, GrowingWriter)  # Moonshine writes at once
        for _ in said:
            run._moon_reading(array("f", bytes(4 * 1600)))
        self.assertIn(("detected", 4, "Turkish"), sent)

    def test_a_guess_the_readings_belie_hands_over_to_detection(self) -> None:
        said = ["The weather is good today and we will go there."] * 4
        run, model, sent = self.guessed_run(said)
        for _ in said:
            run._moon_reading(array("f", bytes(4 * 1600)))
        self.assertEqual(run.language, "")  # not known: Qwen tells it, as without a prefix
        self.assertIsNone(run.writer)
        self.assertFalse([m for m in sent if m[0] == "detected"])

    def test_turkish_heard_on_a_file_goes_to_our_turkish_model_as_on_a_channel(self) -> None:
        # 2.11.1: a file's run (a song, a video file) starts with no language unless the viewer
        # or its audio track named one. Its pieces vote as a channel's do: Turkish locked, our
        # Turkish Moonshine takes the sound over (the growing writer) and the controller hears
        # it (which asks for that model when it isn't here: _offer_specialist).
        from types import SimpleNamespace
        from unittest import mock

        from services.captions.worker import TURKISH, _FilmRun, _Run

        said = "Bu akşam tarih konuşacağız ve Osmanlı devletini anlatacağız."
        for kind in (_Run, _FilmRun):
            with self.subTest(run=kind.__name__):
                sent: list[tuple] = []
                prepared: list[str] = []
                model = SimpleNamespace(
                    moonshine=True,
                    turkish=object(),
                    prepare=prepared.append,
                    is_speech=lambda window: False,
                    is_voiced=lambda window: False,
                )
                link = SimpleNamespace(send=sent.append)
                with (
                    mock.patch("services.captions.decoder.AudioDecoder"),
                    mock.patch("services.captions.film.FilmReader") as reader,
                    mock.patch("services.captions.worker.threading.Thread"),
                ):
                    if kind is _FilmRun:  # a file's run, as the process starts one
                        run = _FilmRun(link, model, 7, "file:///m/Şarkı.mp3", 0.0, None, "", [0.0])
                        run.start()
                        reader.return_value.start.assert_called_once()
                    else:
                        run = _Run(link, model, 7, "", 0.0, None, "", [0.0])
                    self.assertIsNone(run.writer)  # no language: Qwen's pieces first
                    for _ in range(3):
                        run._heard(said, 3.0)
                self.assertEqual(run.language, TURKISH)
                self.assertIn(TURKISH, prepared)
                self.assertIsInstance(run.writer, GrowingWriter)
                self.assertIn(("detected", 7, TURKISH), sent)
