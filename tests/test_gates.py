"""Gate (a) existence, gate (b) support and schema adoption, against the synthetic fixture repo."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402,F401  (shared sys.path + git isolation)

import make_fixture  # noqa: E402
import schema  # noqa: E402
import support  # noqa: E402
import validate  # noqa: E402

TRAP = "drop redis: single-node only"


def _run(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


def decision(evidence: List[Dict], reason: str = "performance", confidence: str = "explicit", did: str = "D001") -> Dict:
    return {"id": did, "title": "Remove redis", "decision": "use an in-process dict", "reason": reason,
            "impact": 2, "confidence": confidence, "evidence": evidence, "question": None}


def stub(verdict: Optional[str] = None, text: Optional[str] = None, seen: Optional[List[str]] = None):
    """A fake llm.call answering `verdict` for every claim id in the prompt (or returning raw `text`)."""
    def call(prompt: str, model: Optional[str] = None) -> Dict:
        if seen is not None:
            seen.append(prompt)
        claims = json.loads(prompt.rsplit("<claims>", 1)[1].split("</claims>")[0])
        body = text if text is not None else json.dumps(
            [{"id": c["id"], "verdict": verdict, "why": "stub"} for c in claims])
        return {"text": body, "usage": {}, "cost_usd": 0, "seconds": 0}
    return call


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="whylog-gates-")
        cls.repo = os.path.join(cls.tmp, "repo")
        truth = make_fixture.build(cls.repo)
        cls.sha = {d["id"]: d["sha"] for d in truth["decisions"]}
        cls.head = _run(cls.repo, "rev-parse", "HEAD")
        cls.pack = {"head": cls.head, "diffs": [], "docs": [{"path": "README.md", "text": "# demo\nAdmin UI removed.\n"}]}

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def gate(self, ev: Dict, pack: Optional[Dict] = None) -> Dict:
        out = validate.check_existence(self.repo, pack or self.pack, [decision([dict(ev)])])
        return out[0]["evidence"][0]


class ExistenceGateTest(FixtureCase):
    def test_real_quote_with_short_sha_stores_full_sha(self) -> None:
        ev = self.gate({"kind": "commit", "ref": self.sha["F11"][:7], "quote": TRAP})
        self.assertTrue(ev["exists"])
        self.assertEqual(len(ev["ref"]), 40)
        self.assertTrue(ev["ref"].startswith(self.sha["F11"]))

    def test_quote_from_a_hidden_file_does_not_exist(self) -> None:
        quote = "gateway returns 503 under load"
        ev = {"kind": "commit", "ref": self.sha["F06"], "quote": quote}
        self.assertTrue(self.gate(ev)["exists"])  # control: visible when nothing is hidden
        hidden = dict(self.pack, hidden=["app/pay.py"])
        self.assertFalse(self.gate(ev, hidden)["exists"])
        file_ev = self.gate({"kind": "file", "ref": f"app/pay.py@{self.sha['F06']}", "quote": quote}, hidden)
        self.assertFalse(file_ev["exists"])
        self.assertEqual(file_ev["rejected"], "path is hidden for this run")

    def test_fake_sha(self) -> None:
        self.assertFalse(self.gate({"kind": "commit", "ref": "deadbeefdeadbeef", "quote": TRAP})["exists"])

    def test_symbolic_ref_is_not_a_sha(self) -> None:
        self.assertFalse(self.gate({"kind": "commit", "ref": "HEAD", "quote": TRAP})["exists"])

    def test_forged_quote_on_real_commit(self) -> None:
        ev = self.gate({"kind": "commit", "ref": self.sha["F11"], "quote": "drop redis: it was too slow for us"})
        self.assertFalse(ev["exists"])

    def test_quote_only_in_model_output(self) -> None:
        d = decision([{"kind": "commit", "ref": self.sha["F11"], "quote": "redis was a performance bottleneck"}],
                     reason="redis was a performance bottleneck")
        self.assertFalse(validate.check_existence(self.repo, self.pack, [d])[0]["evidence"][0]["exists"])

    def test_short_quote_rejected(self) -> None:
        self.assertFalse(self.gate({"kind": "commit", "ref": self.sha["F11"], "quote": "  drop \n red "})["exists"])

    def test_quote_spanning_diff_line_break(self) -> None:
        ev = self.gate({"kind": "commit", "ref": self.sha["F05"], "quote": "json: 41ms\norjson: 6ms"})
        self.assertTrue(ev["exists"], ev.get("rejected"))

    def test_quote_from_code_comment_in_diff(self) -> None:
        ev = self.gate({"kind": "commit", "ref": self.sha["F06"], "quote": "gateway returns 503 under load"})
        self.assertTrue(ev["exists"])

    def test_file_ref_real_and_missing_path(self) -> None:
        ok = self.gate({"kind": "file", "ref": f"app/pay.py@{self.sha['F06'][:8]}", "quote": "gateway returns 503"})
        self.assertTrue(ok["exists"])
        self.assertEqual(ok["ref"], f"app/pay.py@{_run(self.repo, 'rev-parse', self.sha['F06'])}")
        missing = self.gate({"kind": "file", "ref": f"app/nope.py@{self.sha['F06']}", "quote": "gateway returns 503"})
        self.assertFalse(missing["exists"])
        self.assertFalse(self.gate({"kind": "file", "ref": "app/pay.py", "quote": "gateway returns 503"})["exists"])

    def test_doc_only_from_pack(self) -> None:
        self.assertTrue(self.gate({"kind": "doc", "ref": "README.md", "quote": "Admin UI removed."})["exists"])
        # docs/notes.md exists in the repo but is not in the pack
        self.assertFalse(self.gate({"kind": "doc", "ref": "docs/notes.md", "quote": "misc misc misc"})["exists"])

    def test_commit_outside_head_history(self) -> None:
        tree = _run(self.repo, "rev-parse", "HEAD^{tree}")
        side = _run(self.repo, "commit-tree", tree, "-p", "HEAD", "-m", f"{TRAP} (side branch)")
        self.assertFalse(self.gate({"kind": "commit", "ref": side, "quote": TRAP})["exists"])
        self.assertFalse(self.gate({"kind": "file", "ref": f"app/cache.py@{side}", "quote": "CACHE = {}  # in-process"})["exists"])

    def test_commit_after_pinned_head(self) -> None:
        pinned = dict(self.pack, head=_run(self.repo, "rev-parse", self.sha["F05"]))
        self.assertFalse(self.gate({"kind": "commit", "ref": self.sha["F11"], "quote": TRAP}, pinned)["exists"])

    def test_model_claimed_exists_is_ignored(self) -> None:
        ev = self.gate({"kind": "commit", "ref": "0000000000", "quote": TRAP, "exists": True})
        self.assertFalse(ev["exists"])


class SupportGateTest(FixtureCase):
    def trap(self, reason: str = "performance", confidence: str = "explicit") -> List[Dict]:
        d = decision([{"kind": "commit", "ref": self.sha["F11"], "quote": TRAP}], reason, confidence)
        return validate.check_existence(self.repo, self.pack, [d])

    def test_counterexample_real_quote_wrong_reason(self) -> None:
        for verdict in ("unrelated", "contradicts", "implies"):
            d = support.judge(self.trap(), call=stub(verdict))["decisions"][0]
            self.assertNotEqual(d["confidence"], "explicit", verdict)
        d = support.judge(self.trap(), call=stub("contradicts"))["decisions"][0]
        self.assertEqual(d["confidence"], "guess")
        self.assertTrue(d["conflict"])

    def test_never_upgrades_model_guess(self) -> None:
        d = support.judge(self.trap("single-node deployment", "guess"), call=stub("states"))["decisions"][0]
        self.assertEqual(d["confidence"], "guess")
        self.assertEqual(d["model_confidence"], "guess")

    def test_states_keeps_explicit(self) -> None:
        d = support.judge(self.trap("single-node deployment"), call=stub("states"))["decisions"][0]
        self.assertEqual(d["confidence"], "explicit")

    def test_malformed_output_fails_closed(self) -> None:
        for text in ("not json at all", "[]", json.dumps([{"id": "C1", "verdict": "maybe"}]),
                     json.dumps([{"id": "C1", "verdict": "states"}, {"id": "C1", "verdict": "unrelated"}])):
            out = support.judge(self.trap("single-node deployment"), call=stub(text=text))
            self.assertEqual(out["decisions"][0]["confidence"], "guess", text)
            self.assertEqual(out["malformed"], ["D001"], text)

    def test_zero_existing_quotes_never_sent(self) -> None:
        seen: List[str] = []
        d = decision([{"kind": "commit", "ref": "deadbeefdead", "quote": TRAP}], "single-node")
        checked = validate.check_existence(self.repo, self.pack, [d])
        out = support.judge(checked, call=stub("states", seen=seen))
        self.assertEqual(seen, [])
        self.assertEqual(out["decisions"][0]["confidence"], "guess")
        self.assertEqual(out["malformed"], [])

    def test_judge_is_blind(self) -> None:
        seen: List[str] = []
        d = decision([{"kind": "commit", "ref": self.sha["F11"], "quote": TRAP},
                      {"kind": "commit", "ref": self.sha["F11"], "quote": "forged: redis was far too slow"}])
        support.judge(validate.check_existence(self.repo, self.pack, [d]), call=stub("unrelated", seen=seen))
        self.assertEqual(len(seen), 1)
        self.assertIn(TRAP, seen[0])
        for hidden in ("forged: redis", "Remove redis", "in-process dict", "D001"):
            self.assertNotIn(hidden, seen[0])


class SchemaTest(unittest.TestCase):
    def test_sanitize_drops_invalid_items(self) -> None:
        good = decision([{"kind": "commit", "ref": "abc1234", "quote": TRAP}])
        bad = [
            "not an object",
            {k: v for k, v in good.items() if k != "reason"},
            dict(good, confidence="certain"),
            dict(good, impact="3"),
            dict(good, impact=True),
            dict(good, evidence=[{"kind": "url", "ref": "x", "quote": TRAP}]),
            dict(good, evidence="none"),
            dict(good, title=""),
        ]
        adopted, rejected = schema.sanitize([good] + bad)
        self.assertEqual(adopted, [good])
        self.assertEqual([r["index"] for r in rejected], list(range(1, len(bad) + 1)))
        self.assertTrue(all(r["errors"] for r in rejected))

    def test_sanitize_non_list(self) -> None:
        adopted, rejected = schema.sanitize({"id": "D001"})
        self.assertEqual(adopted, [])
        self.assertEqual(len(rejected), 1)

    def test_nullable_fields(self) -> None:
        good = decision([], confidence="guess")
        self.assertEqual(schema.validate(dict(good, conflict=None, testimony=None), schema.load()), [])
        self.assertTrue(schema.validate(dict(good, testimony={"answer": "x", "mode": "maybe", "at": "t"}), schema.load()))

    def test_unsupported_keyword_raises(self) -> None:
        with self.assertRaises(ValueError):
            schema.validate("x", {"type": "string", "pattern": "^x$"})

    def test_check_existence_reports_rejects(self) -> None:
        rejected: List[Dict] = []
        out = validate.check_existence(".", {"head": "HEAD", "docs": [], "diffs": []}, [{"id": "D1"}], rejected)
        self.assertEqual(out, [])
        self.assertEqual(rejected[0]["id"], "D1")


@unittest.skipUnless(os.environ.get("WHYLOG_LIVE") == "1", "live model call; set WHYLOG_LIVE=1")
class LiveSupportTest(FixtureCase):
    def test_trap_reason_is_not_stated(self) -> None:
        d = decision([{"kind": "commit", "ref": self.sha["F11"], "quote": TRAP}], "performance")
        out = support.judge(validate.check_existence(self.repo, self.pack, [d]))
        verdict = out["decisions"][0]["support"]
        print(f"\n[live] F11 trap + 'performance' -> {verdict}", file=sys.stderr)
        self.assertNotEqual(verdict["verdict"], "states")
        self.assertNotEqual(out["decisions"][0]["confidence"], "explicit")


if __name__ == "__main__":
    unittest.main()
