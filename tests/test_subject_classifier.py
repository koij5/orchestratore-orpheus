import unittest

from kaggle_kernel.subject_classifier import (
    LABEL_DEFINITIONS, SOURCE_MODEL, classify_transcript,
)


class FakeClassifier:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, text, **kwargs):
        self.calls.append((text, kwargs))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        labels, scores = zip(*answer)
        return {"labels": list(labels), "scores": list(scores)}


def answer(top="biologia", score=0.9, runner="altro", runner_score=0.1):
    return [(top, score), (runner, runner_score)]


def line(start, end, text):
    def stamp(n):
        h, rem = divmod(n, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"[{stamp(start)} - {stamp(end)}] {text}"


class SubjectClassifierTests(unittest.TestCase):
    def test_ordered_coverage_and_adjacent_merge(self):
        text = "\n".join(line(i * 10, i * 10 + 4, f"testo {i}") for i in range(7))
        fake = FakeClassifier([answer(), answer("fisica", .8, "chimica", .1)])
        result = classify_transcript(text, fake)
        self.assertEqual([(s["start_line"], s["end_line"], s["materia"]) for s in result["segments"]],
                         [(1, 6, "biologia"), (7, 7, "fisica")])
        self.assertEqual([s["status"] for s in result["segments"]], ["classified", "classified"])
        self.assertEqual(result["segments"][0]["confidence"], .9)
        self.assertEqual(result["source_model"], SOURCE_MODEL)
        self.assertEqual([call[0].count("\n") + 1 for call in fake.calls], [6, 1])

    def test_threshold_score_and_margin_fail_closed(self):
        text = "\n".join([line(0, 3, "a"), line(60, 67, "b"), line(120, 129, "c")])
        fake = FakeClassifier([answer("chimica", .54, "altro", .1),
                               answer("fisica", .8, "biologia", .7),
                               answer("inglese", .9, "altro", .1)])
        result = classify_transcript(text, fake)
        self.assertEqual(result["segments"][0]["materia"], None)
        self.assertEqual([(segment["start_line"], segment["end_line"], segment["materia"])
                          for segment in result["segments"]],
                         [(1, 2, None), (3, 3, "inglese")])
        self.assertEqual([segment["status"] for segment in result["segments"]],
                         ["uncertain", "classified"])

    def test_other_high_confidence_is_unsupported_and_low_confidence_uncertain(self):
        result = classify_transcript(line(0, 3, "lezione di matematica"),
                                     FakeClassifier([answer("altro", .99, "fisica", .01)]))
        segment = result["segments"][0]
        self.assertEqual((segment["materia"], segment["status"]), ("unsupported", "unsupported"))
        self.assertEqual((segment["start_line"], segment["end_line"]), (1, 1))
        self.assertEqual(segment["confidence"], .99)
        self.assertAlmostEqual(segment["runner_up_margin"], .98)

        low = classify_transcript(line(0, 3, "altro"),
                                  FakeClassifier([answer("altro", .54, "fisica", .1)]))
        self.assertEqual((low["segments"][0]["materia"], low["segments"][0]["status"]),
                         (None, "uncertain"))
        self.assertEqual(set(LABEL_DEFINITIONS), {"biologia", "chimica", "fisica", "inglese", "letteratura", "purgatorio", "altro"})
        self.assertIn("cantica Purgatorio", LABEL_DEFINITIONS["purgatorio"])
        self.assertIn("lingua inglese", LABEL_DEFINITIONS["inglese"])

    def test_window_boundaries_and_long_duration(self):
        rows = [line(i * 60, i * 60 + 5, f"parole {i}") for i in range(300)]
        fake = FakeClassifier([answer("chimica", .9, "altro", .05)])
        result = classify_transcript("\n".join(rows), fake)
        self.assertEqual(len(fake.calls), 300)
        self.assertTrue(all(len(call[0]) < 40 for call in fake.calls))
        self.assertEqual(result["segments"][0]["start_line"], 1)
        self.assertEqual(result["segments"][0]["end_line"], 300)
        self.assertEqual(result["segments"][0]["materia"], "chimica")
        self.assertEqual(result["segments"][0]["status"], "classified")

    def test_final_single_line_and_newline_styles(self):
        text = line(360000, 360001, "fine") + "\r\n"
        result = classify_transcript(text, FakeClassifier([answer("letteratura", .8, "altro", .1)]))
        segment = result["segments"][0]
        self.assertEqual((segment["start_line"], segment["end_line"], segment["materia"], segment["confidence"]),
                         (1, 1, "letteratura", .8))
        self.assertEqual(segment["status"], "classified")
        self.assertAlmostEqual(segment["runner_up_margin"], .7)

    def test_invalid_or_empty_input_fails_closed(self):
        fake = FakeClassifier([answer()])
        for bad in ("", "   ", "testo non timestamp", "[00:60:00 - 00:60:01] errato",
                    "[00:00:02 - 00:00:01] intervallo", "[00:00:00 - 00:00:01]   "):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                classify_transcript(bad, fake)
        self.assertEqual(fake.calls, [])

    def test_internal_newlines_are_continuations_and_keep_line_coverage(self):
        text = line(0, 1, "  prima frase ") + "\ncontinua\n\n" + line(2, 3, "fine")
        fake = FakeClassifier([answer()])
        result = classify_transcript(text, fake)
        self.assertEqual([(s["start_line"], s["end_line"]) for s in result["segments"]], [(1, 4)])
        self.assertIn("continua", fake.calls[0][0])
        with self.assertRaises(ValueError):
            classify_transcript("\n", FakeClassifier([answer()]))

    def test_runner_up_margin_is_reported(self):
        result = classify_transcript(line(0, 2, "lezione"),
                                     FakeClassifier([answer("fisica", .77, "chimica", .22)]))
        self.assertAlmostEqual(result["segments"][0]["runner_up_margin"], .55)
        self.assertAlmostEqual(result["segments"][0]["confidence"], .77)
        self.assertEqual(result["segments"][0]["status"], "classified")


if __name__ == "__main__":
    unittest.main()
