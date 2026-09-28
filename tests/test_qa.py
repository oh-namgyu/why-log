"""qa.py question queue: priorities, anti-anchoring options, idempotency, answers, conflicts, robustness, CLI."""
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path + git isolation + model guard)

import qa
import schema
import whylog
from common import WhyLogError, read_json, state_path, write_json_atomic

SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"


def _ev(quote: str, exists: bool = True) -> dict:
    return {"kind": "commit", "ref": SHA, "quote": quote, "exists": exists}


DECISIONS = [
    {"id": "D001", "title": "Use SQLite", "reason": "", "impact": 3, "confidence": "guess",
     "question": "Why SQLite over Postgres?", "evidence": [_ev("switch store to sqlite"), _ev("fake", False)]},
    {"id": "D002", "title": "Drop Redis", "reason": "single node only", "impact": 3, "confidence": "inferred",
     "evidence": [_ev("drop redis: single-node only " + "x" * 300)]},
    {"id": "D003", "title": "Cron jobs", "reason": "simpler ops", "impact": 1, "confidence": "inferred",
     "evidence": [_ev("move to cron")]},
    {"id": "D004", "title": "MIT license", "reason": "permissive", "impact": 2, "confidence": "explicit",
     "evidence": [_ev("license: MIT, permissive")]},
    {"id": "D005", "title": "Remove UI", "reason": "performance", "impact": 1, "confidence": "guess",
     "conflict": "quote says the UI was unused", "evidence": [_ev("remove ui: nobody used it")]},
]


class QueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = helpers.init_repo(os.path.join(self._tmp.name, "repo"))
        helpers.commit(self.repo, "init", {"a.txt": "a\n"})
        self.decisions = copy.deepcopy(DECISIONS)
        self._write_decisions(self.decisions)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_decisions(self, decisions) -> None:
        write_json_atomic(state_path(self.repo, "decisions.json"), {"schema": "why-log-decisions/1", "head": SHA,
                                                                    "run_id": "r1", "hidden": [],
                                                                    "decisions": decisions})

    def _decision(self, did: str) -> dict:
        return next(d for d in read_json(state_path(self.repo, "decisions.json"))["decisions"] if d["id"] == did)

    def _lines(self) -> list:
        with open(state_path(self.repo, "questions.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _qid(self, did: str) -> str:
        return next(q["id"] for q in self._lines() if q["subject"] == did)

    def assertQueueValid(self) -> None:
        rules = schema.load("qa-queue")
        for q in self._lines():
            self.assertEqual(schema.validate(q, rules), [], q)

    def test_priority_ordering_and_selection(self) -> None:
        self.assertEqual(qa.enqueue(self.repo, self.decisions, "r1"), 4)
        order = [(q["subject"], q["priority"]) for q in qa.next_questions(self.repo, 10)]
        self.assertEqual(order, [("D001", 1.0), ("D005", 0.9), ("D002", 0.5), ("D003", 0.1667)])
        self.assertEqual(len(qa.next_questions(self.repo, 2)), 2)
        self.assertQueueValid()

    def test_options_and_evidence(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        qs = {q["subject"]: q for q in self._lines()}
        self.assertEqual(qs["D001"]["options"], [qa.UNKNOWN])
        self.assertEqual(qs["D005"]["options"], [qa.UNKNOWN])
        self.assertEqual(qs["D002"]["options"], ["single node only", qa.UNKNOWN])
        self.assertEqual(qs["D001"]["question"], "Why SQLite over Postgres?")
        self.assertEqual(qs["D002"]["question"], "Why was this chosen: Drop Redis?")
        self.assertEqual(qs["D001"]["evidence"], ["a1b2c3d switch store to sqlite"])  # non-existent quote dropped
        self.assertEqual(len(qs["D002"]["evidence"][0]), 160)

    def test_non_answer_reason_is_not_offered_as_an_option(self) -> None:
        d = {"id": "D009", "title": "Doctor is read-only", "reason": "The motive is not stated beyond the docs.",
             "confidence": "inferred", "impact": 2, "evidence": [_ev("doctor never repairs")]}
        qa.enqueue(self.repo, [d], "r1")
        self.assertEqual(self._lines()[0]["options"], [qa.UNKNOWN])

    def test_enqueue_idempotent_and_answers_survive(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        qa.answer(self.repo, self._qid("D003"), "free", "we had no queue server")
        rerun = copy.deepcopy(DECISIONS)
        rerun[0]["title"] = "  use   SQLITE "
        rerun[2]["id"] = "D009"  # renumbered in a new run
        self._write_decisions(rerun)
        self.assertEqual(qa.enqueue(self.repo, rerun, "r2"), 0)
        lines = self._lines()
        self.assertEqual(len(lines), 4)
        cron = next(q for q in lines if q["title"] == "Cron jobs")
        self.assertEqual((cron["status"], cron["subject"]), ("answered", "D009"))
        self.assertEqual(self._decision("D009")["testimony"]["answer"], "we had no queue server")
        self.assertQueueValid()

    def test_open_question_for_resolved_decision_is_skipped(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        qa.enqueue(self.repo, [d for d in self.decisions if d["id"] != "D003"], "r2")
        self.assertNotIn("D003", [q["subject"] for q in qa.next_questions(self.repo, 10)])
        self.assertEqual(qa.status(self.repo)["questions"]["skipped"], 1)

    def test_answer_choice(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        out = qa.answer(self.repo, self._qid("D002"), "choice", "single node only")
        self.assertFalse(out["conflict"])
        self.assertEqual(out["question"]["answer"]["by"], "user")
        d = self._decision("D002")
        self.assertEqual((d["provenance"], d["testimony"]["mode"], d["reason"]), ("testimony", "choice",
                                                                                 "single node only"))
        self.assertQueueValid()

    def test_answer_free_replaces_unproven_reason(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        qa.answer(self.repo, self._qid("D001"), "free", "  one binary to ship  ", by="mock")
        d = self._decision("D001")
        self.assertEqual((d["reason"], d["provenance"], d["model_reason"]), ("one binary to ship", "testimony", ""))
        self.assertEqual(self._lines()[0]["answer"]["by"], "mock")
        self.assertEqual(schema.validate(d, schema.load("decision")), [])

    def test_answer_unknown(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        out = qa.answer(self.repo, self._qid("D003"), "unknown", None)
        d = self._decision("D003")
        self.assertEqual((d["provenance"], d["reason"], d["testimony"]["answer"]), ("unknown", "simpler ops", ""))
        self.assertEqual(out["question"]["answer"]["mode"], "unknown")
        qa.answer(self.repo, self._qid("D001"), "choice", qa.UNKNOWN)
        self.assertEqual(self._decision("D001")["testimony"]["mode"], "unknown")

    def test_invalid_answers_rejected(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        qid = self._qid("D001")
        bad = [("choice", "performance"), ("free", "   "), ("free", "x" * 2001), ("unknown", "text"),
               ("maybe", None)]
        for mode, text in bad:
            with self.assertRaises(WhyLogError) as cm:
                qa.answer(self.repo, qid, mode, text)
            self.assertEqual(cm.exception.code, 2)
        with self.assertRaises(WhyLogError) as cm:
            qa.answer(self.repo, "Q999", "unknown", None)
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(len(qa.next_questions(self.repo, 10)), 4)

    def test_conflict_keeps_both_versions(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        out = qa.answer(self.repo, self._qid("D005"), "free", "it was too slow")
        self.assertTrue(out["conflict"])
        d = self._decision("D005")
        self.assertEqual(d["reason"], "performance")
        self.assertIn("quote says the UI was unused", d["conflict"])
        self.assertIn("it was too slow", d["conflict"])

    def test_explicit_decision_not_overwritten(self) -> None:
        d = copy.deepcopy(DECISIONS[3])
        q = {"id": "Q001", "subject": "D004", "title": "MIT license"}
        ans = {"text": "company policy", "mode": "free", "at": "2026-01-01T00:00:00+00:00"}
        self.assertTrue(qa._apply(d, ans))
        self.assertEqual(d["reason"], "permissive")
        self.assertIn("company policy", d["conflict"])
        self.assertIs(qa._find([d], q), d)

    def test_corrupt_lines_quarantined(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        path = state_path(self.repo, "questions.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"schema": "qa-queue/1", "id": "Q\n')
            fh.write(json.dumps({"schema": "qa-queue/1", "id": "Q050"}) + "\n")
        self.assertEqual(len(qa.next_questions(self.repo, 10)), 4)
        with open(path + ".corrupt", encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 2)
        self.assertEqual(qa.status(self.repo)["quarantined_lines"], 2)
        self.assertQueueValid()
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_next_questions_after_partial_answers(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        qa.answer(self.repo, self._qid("D001"), "unknown", None)
        qa.answer(self.repo, self._qid("D002"), "choice", qa.UNKNOWN)
        self.assertEqual([q["subject"] for q in qa.next_questions(self.repo, 8)], ["D005", "D003"])
        st = qa.status(self.repo)
        self.assertEqual(st["questions"], {"total": 4, "open": 2, "answered": 2, "skipped": 0})
        self.assertEqual(st["decisions"]["provenance"], {"code": 2, "testimony": 0, "unknown": 3})

    def test_status_on_fresh_repo(self) -> None:
        os.remove(state_path(self.repo, "decisions.json"))
        st = qa.status(self.repo)
        self.assertEqual(st["decisions"]["total"], 0)
        self.assertEqual(st["questions"], {"total": 0, "open": 0, "answered": 0, "skipped": 0})
        self.assertEqual(qa.next_questions(self.repo, 5), [])

    def _cli(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = whylog.main(list(argv))
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None)

    def test_cli_round_trip(self) -> None:
        qa.enqueue(self.repo, self.decisions, "r1")
        code, out = self._cli("questions", self.repo, "--next", "2", "--json")
        self.assertEqual((code, [q["subject"] for q in out["questions"]]), (0, ["D001", "D005"]))
        qid = out["questions"][0]["id"]
        code, out = self._cli("answer", self.repo, "--id", qid, "--free", "one binary", "--json")
        self.assertEqual((code, out["decision"]["provenance"]), (0, "testimony"))
        self.assertEqual(self._cli("answer", self.repo, "--id", qid, "--choice", "nope", "--json")[0], 2)
        self.assertEqual(self._cli("answer", self.repo, "--id", "Q404", "--unknown", "--json")[0], 2)
        code, out = self._cli("status", self.repo, "--json")
        self.assertEqual((code, out["questions"]["answered"], out["decisions"]["provenance"]["testimony"]), (0, 1, 1))
        self.assertQueueValid()


if __name__ == "__main__":
    unittest.main()
