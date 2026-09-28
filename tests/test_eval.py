"""eval/: 1:1 matching, metric arithmetic, mock quote verification, ADR parsing, T3 clones. No model calls."""
import os
import sys
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path + git isolation + model guard)
from helpers import commit, init_repo

sys.path.insert(0, os.path.join(helpers.HERE, "..", "eval"))
import clones  # noqa: E402
import gold_adr  # noqa: E402
import mock_user  # noqa: E402
import score  # noqa: E402

GOLD = [{"id": "G01", "title": "Use SQLite", "reason": "the nightly batch needs a single-file store we can copy",
         "traceable": True},
        {"id": "G02", "title": "Drop Redis", "reason": "only one node is ever deployed", "traceable": True},
        {"id": "G03", "title": "Keep cron", "reason": "nobody wanted another daemon", "traceable": False}]


def _dec(did: str, conf: str, support: str = "states", model_conf: str = "") -> dict:
    return {"id": did, "title": did, "reason": f"reason {did}", "confidence": conf,
            "model_confidence": model_conf or conf, "support": {"verdict": support}, "evidence": []}


class OneToOne(unittest.TestCase):
    def test_greedy_by_score_and_only_same(self) -> None:
        cands = [{"gold": "G01", "rec": "D1", "verdict": "same", "score": 0.7},
                 {"gold": "G01", "rec": "D2", "verdict": "same", "score": 0.9},
                 {"gold": "G02", "rec": "D2", "verdict": "same", "score": 0.8},
                 {"gold": "G02", "rec": "D3", "verdict": "related", "score": 1.0},
                 {"gold": "G03", "rec": "D1", "verdict": "same", "score": 0.6},
                 {"gold": "G09", "rec": "D1", "verdict": "same", "score": 1.0}, "junk"]
        pairs = score.one_to_one(cands, ["G01", "G02", "G03"], ["D1", "D2", "D3"])
        self.assertEqual([(p["gold"], p["rec"]) for p in pairs], [("G01", "D2"), ("G03", "D1")])

    def test_tie_is_deterministic(self) -> None:
        cands = [{"gold": "G02", "rec": "D1", "verdict": "same", "score": 1},
                 {"gold": "G01", "rec": "D1", "verdict": "same", "score": 1}]
        self.assertEqual(score.one_to_one(cands, ["G01", "G02"], ["D1"]), [{"gold": "G01", "rec": "D1", "score": 1.0}])


class Metrics(unittest.TestCase):
    def setUp(self) -> None:
        self.decisions = [_dec("D1", "explicit"), _dec("D2", "inferred"), _dec("D3", "guess", "unrelated", "explicit"),
                          _dec("D4", "inferred", "implies"), _dec("D5", "guess", "unjudged")]
        self.pairs = [{"gold": "G01", "rec": "D1"}, {"gold": "G02", "rec": "D3"}, {"gold": "G03", "rec": "D2"}]
        self.verdicts = {("G01", "D1"): {"verdict": "consistent"}, ("G02", "D3"): {"verdict": "consistent"},
                         ("G03", "D2"): {"verdict": "contradicts"}}

    def test_arithmetic(self) -> None:
        m = score.metrics(GOLD, self.decisions, self.pairs, self.verdicts)
        self.assertEqual(m["recall"], 1.0)  # G01, G02 traceable and matched
        self.assertEqual(m["recall_all_gold"], 1.0)
        self.assertEqual(m["reasoned_recall"], 1.0)
        self.assertEqual(m["nonguess"], 3)
        self.assertEqual(m["precision"], round(2 / 3, 4))  # D1, D2 matched among D1, D2, D4
        self.assertEqual(m["contradiction_rate"], round(1 / 3, 4))  # D2
        self.assertEqual(m["unsupported_rate"], 0.0)  # gate lowers unrelated to guess
        self.assertEqual(m["unsupported_rate_model_claimed"], 0.25)  # D3 among D1-D4
        self.assertEqual(m["question_targets"], [{"id": "G03", "title": "Keep cron", "matched": True}])
        self.assertEqual(m["pair_verdicts"]["consistent"], 2)

    def test_efficiency_and_changed_pairs(self) -> None:
        pre = score.metrics(GOLD, self.decisions, self.pairs, {})
        post = score.metrics(GOLD, self.decisions, self.pairs, self.verdicts)
        self.assertEqual(score.efficiency(pre, post, 4), 0.5)
        self.assertIsNone(score.efficiency(pre, post, 0))
        after = [dict(d, reason="new") if d["id"] == "D3" else d for d in self.decisions]
        self.assertEqual(score.changed_pairs(self.pairs, self.decisions, after), [{"gold": "G02", "rec": "D3"}])

    def test_unverified_traceable_counts(self) -> None:
        gold = [dict(g, traceable=None) for g in GOLD]
        self.assertEqual(score.metrics(gold, [], [], {})["gold_traceable"], 3)


class MockQuote(unittest.TestCase):
    def test_verify(self) -> None:
        ok = mock_user.verify({"gold": "G01", "quote": "single-file   store we can"}, GOLD)
        self.assertEqual((ok["valid"], ok["found_in"]), (True, "G01"))
        other = mock_user.verify({"gold": "G01", "quote": "only one node is ever"}, GOLD)
        self.assertEqual(other["found_in"], "G02")
        short = mock_user.verify({"gold": "G02", "quote": "one node"}, GOLD)
        para = mock_user.verify({"gold": "G02", "quote": "we only run a single node"}, GOLD)
        self.assertFalse(short["valid"] or para["valid"] or short["declared_unknown"])
        unk = mock_user.verify({"gold": None, "quote": "UNKNOWN"}, GOLD)
        self.assertEqual((unk["valid"], unk["declared_unknown"]), (False, True))
        self.assertFalse(mock_user.verify(None, GOLD)["declared_unknown"])

    def test_summary_leak_rate(self) -> None:
        recs = [{"valid": True, "declared_unknown": False}, {"valid": False, "declared_unknown": True},
                {"valid": False, "declared_unknown": False}, {"valid": False, "declared_unknown": False}]
        self.assertEqual(mock_user.summarize(recs)["leak_rate"], 0.5)


ADR = """# 7. Use minijinja for templates

Date: 2025-01-21

## Status

Accepted

## Context

We need templates.
### Options
```
# not a heading
```

## Decision

Use minijinja.

## Consequences

Fine.
"""


class AdrParsing(unittest.TestCase):
    def test_parse(self) -> None:
        adr = gold_adr.parse_adr(ADR)
        self.assertEqual(adr["title"], "Use minijinja for templates")
        self.assertEqual(adr["status"], "Accepted")
        self.assertEqual(adr["date"], "2025-01-21")
        self.assertEqual(adr["decision"], "Use minijinja.")
        self.assertEqual(adr["reason"], "We need templates. Options ``` # not a heading ```")

    def test_why_heading_and_status_line(self) -> None:
        adr = gold_adr.parse_adr("# Add TOML\nStatus: proposed\n## Why do this?\nBecause.\n## Decision\nDo it.\n")
        self.assertEqual((adr["title"], adr["status"], adr["reason"], adr["decision"]),
                         ("Add TOML", "proposed", "Because.", "Do it."))


class SubjectOnlyClone(unittest.TestCase):
    def test_t2_then_t3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = init_repo(os.path.join(tmp, "src"))
            commit(src, "Add a\n\nbecause of reasons", {"a.txt": "a\n"}, 1)
            head = helpers.sh(src, "rev-parse", "HEAD")
            commit(src, "Later work\n\nnot part of the eval", {"b.txt": "b\n"}, 2)
            t2 = clones.clone_at(src, os.path.join(tmp, "T2"), head)
            self.assertEqual(helpers.sh(t2, "rev-parse", "HEAD"), head)
            self.assertEqual(helpers.sh(t2, "for-each-ref", "--format=%(refname)"), "refs/heads/eval")
            self.assertEqual(clones.bodies_left(t2), 1)
            clones.subject_only_clone(t2, os.path.join(tmp, "T3"))
            t3 = os.path.join(tmp, "T3")
            self.assertEqual(clones.bodies_left(t3), 0)
            self.assertEqual(helpers.sh(t3, "log", "--format=%s"), "Add a")
            self.assertEqual(helpers.sh(src, "rev-list", "--count", "HEAD"), "2")  # source untouched


if __name__ == "__main__":
    unittest.main()
