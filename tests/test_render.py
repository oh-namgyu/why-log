"""Stage 4: ADR content per provenance, determinism, the staging transaction, --out safety and undo."""
import json
import os
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path + git isolation + model guard)

import render
from adr_text import MARKER
from common import WhyLogError

HEAD = "0123456789abcdef0123"


def _ev(quote: str, exists: bool = True) -> dict:
    return {"kind": "commit", "ref": "abc1234", "quote": quote, "exists": exists}


def _decisions() -> list:
    return [
        {"id": "D001", "title": "Use SQLite queue", "decision": "Replace Redis with SQLite.",
         "alternatives": ["Keep Redis"], "reason": "Single-node deployment only.", "confidence": "explicit",
         "provenance": "code", "evidence": [_ev("drop redis: single-node only"), _ev("forged quote", False)]},
        {"id": "D002", "title": "Cron scheduler", "decision": "Use cron.", "reason": "Fewer moving parts.",
         "confidence": "inferred", "provenance": "code", "evidence": [_ev("switch to cron")]},
        {"id": "D003", "title": "Drop the web UI", "decision": "Remove UI.", "reason": "SECRET-GUESS-REASON",
         "confidence": "guess", "provenance": "unknown", "question": "Why was the UI removed?", "evidence": []},
        {"id": "D004", "title": "Batch size 50", "decision": "Batch 50.", "reason": "Throughput tuning.",
         "confidence": "inferred", "provenance": "testimony", "conflict": "maintainer disagrees with history",
         "testimony": {"answer": "API rate limit", "mode": "free", "at": "2026-01-05T10:00:00Z"},
         "evidence": [_ev("batch=50")]},
        {"id": "D005", "title": "Keep Python 3.9", "decision": "Support 3.9.", "reason": "GUESS-REASON-2",
         "confidence": "guess", "provenance": "testimony", "evidence": [],
         "testimony": {"answer": "Customer servers run 3.9", "mode": "choice", "at": "2026-01-05T10:00:00Z"}},
    ]


class RenderBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = helpers.init_repo(os.path.join(self._tmp.name, "repo"))
        helpers.commit(self.repo, "init", {"a.txt": "a\n"})
        self.write_decisions(_decisions())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_decisions(self, decisions: list) -> None:
        os.makedirs(os.path.join(self.repo, ".why-log"), exist_ok=True)
        with open(os.path.join(self.repo, ".why-log", "decisions.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": "why-log-decisions/1", "head": HEAD, "run_id": "r0", "hidden": [],
                       "decisions": decisions}, fh)

    def path(self, *parts: str) -> str:
        return os.path.join(self.repo, *parts)

    def read(self, *parts: str) -> str:
        with open(self.path(*parts), encoding="utf-8") as fh:
            return fh.read()

    def snapshot(self, rel: str) -> dict:
        d = self.path(rel)
        return {n: self.read(rel, n) for n in sorted(os.listdir(d))} if os.path.isdir(d) else {}


class Content(RenderBase):
    def test_provenance_labels_and_sections(self) -> None:
        files = render.build_files(_decisions(), HEAD)
        self.assertEqual(sorted(files), ["0001-use-sqlite-queue.md", "0002-cron-scheduler.md", "0004-batch-size-50.md",
                                         "0005-keep-python-39.md", "README.md", "unknowns.md"])
        for body in files.values():
            self.assertEqual(body.splitlines()[0], MARKER)
        explicit = files["0001-use-sqlite-queue.md"]
        self.assertIn("# 0001. Use SQLite queue", explicit)
        self.assertIn("reconstructed from history @ `0123456789ab`", explicit)
        self.assertIn("Provenance: **Stated in history (verbatim evidence)**", explicit)
        self.assertIn("- Keep Redis", explicit)
        self.assertIn("`commit:abc1234` — “drop redis: single-node only”", explicit)
        self.assertNotIn("forged quote", explicit)
        self.assertIn("Provenance: **Inferred from history**", files["0002-cron-scheduler.md"])

    def test_testimony_keeps_inferred_reason_and_shows_conflict(self) -> None:
        body = render.build_files(_decisions(), HEAD)["0004-batch-size-50.md"]
        self.assertIn("Provenance: **Maintainer testimony (free text)**", body)
        self.assertIn("**Maintainer:** API rate limit", body)
        self.assertIn("### Inferred from history\nThroughput tuning.", body)
        self.assertIn("⚠ **Conflict**", body)
        self.assertIn("> - History: Throughput tuning.", body)
        self.assertIn("> - Maintainer: API rate limit", body)

    def test_answer_that_replaced_reason_keeps_the_history_version(self) -> None:
        # qa.answer moves the reconstructed reason to model_reason when the maintainer's answer differs
        d = dict(_decisions()[1], reason="Ops team asked for it", model_reason="Fewer moving parts.",
                 provenance="testimony", testimony={"answer": "Ops team asked for it", "mode": "free", "at": "t"})
        body = render.build_files([d], HEAD)["0001-cron-scheduler.md"]
        self.assertIn("**Maintainer:** Ops team asked for it", body)
        self.assertIn("### Inferred from history\nFewer moving parts.", body)

    def test_confirmed_inferred_reason_is_shown_once(self) -> None:
        d = dict(_decisions()[1], provenance="testimony",
                 testimony={"answer": "Fewer moving parts.", "mode": "choice", "at": "t"})
        body = render.build_files([d], HEAD)["0001-cron-scheduler.md"]
        self.assertIn("**Maintainer confirmed** the reason inferred from history:", body)
        self.assertEqual(body.count("Fewer moving parts."), 1)

    def test_guess_never_gets_reason_or_adr(self) -> None:
        files = render.build_files(_decisions(), HEAD)
        everything = "".join(files.values())
        self.assertNotIn("SECRET-GUESS-REASON", everything)
        self.assertNotIn("GUESS-REASON-2", everything)
        self.assertFalse(any("drop-the-web-ui" in n for n in files))
        self.assertIn("Why was the UI removed?", files["unknowns.md"])
        self.assertIn("Maintainer testimony (choice)", files["0005-keep-python-39.md"])
        index = files["README.md"]
        self.assertIn("- ADRs: 4", index)
        self.assertIn("- Unknowns: 1", index)
        self.assertIn("(0004-batch-size-50.md) — Maintainer testimony (free text)", index)

    def test_order_independent(self) -> None:
        self.assertEqual(render.build_files(_decisions(), HEAD), render.build_files(_decisions()[::-1], HEAD))


class Transaction(RenderBase):
    def test_render_is_deterministic_and_idempotent(self) -> None:
        first = render.render_current(self.repo, run_id="r1")
        self.assertEqual(first["run_id"], "r1")
        self.assertEqual(len(first["added"]), 6)
        before = self.snapshot(".why-log/adr")
        again = render.render_current(self.repo, run_id="r1")
        self.assertEqual((again["added"], again["modified"], again["deleted"], again["run_id"]), ([], [], [], None))
        self.assertEqual(self.snapshot(".why-log/adr"), before)
        self.assertEqual(os.listdir(self.path(".why-log", "run")), ["r1"])

    def test_preview_writes_nothing(self) -> None:
        out = render.render_current(self.repo, out="docs/adr", preview=True)
        self.assertEqual(len(out["added"]), 6)
        self.assertFalse(os.path.exists(self.path("docs")))
        self.assertFalse(os.path.exists(self.path(".why-log", "run")))

    def test_refuses_to_overwrite_user_file(self) -> None:
        os.makedirs(self.path("docs", "adr"))
        with open(self.path("docs", "adr", "README.md"), "w", encoding="utf-8") as fh:
            fh.write("# my own notes\n")
        with self.assertRaises(WhyLogError) as ctx:
            render.render_current(self.repo, run_id="r1", out="docs/adr")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("README.md", str(ctx.exception))
        self.assertEqual(os.listdir(self.path("docs", "adr")), ["README.md"])
        self.assertEqual(self.read("docs", "adr", "README.md"), "# my own notes\n")

    def test_unrelated_user_file_is_left_alone(self) -> None:
        os.makedirs(self.path("docs", "adr"))
        with open(self.path("docs", "adr", "0099-manual.md"), "w", encoding="utf-8") as fh:
            fh.write("manual\n")
        render.render_current(self.repo, run_id="r1", out="docs/adr")
        self.write_decisions(_decisions()[:1])
        out = render.render_current(self.repo, run_id="r2", out="docs/adr")
        self.assertNotIn("0099-manual.md", out["deleted"])
        self.assertEqual(self.read("docs", "adr", "0099-manual.md"), "manual\n")

    def test_partial_staging_is_rebuilt(self) -> None:
        staging = self.path(".why-log", "run", "r1", "staging")
        os.makedirs(staging)
        with open(os.path.join(staging, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("half-written")
        with open(os.path.join(staging, "junk.md"), "w", encoding="utf-8") as fh:
            fh.write("junk")
        render.render_current(self.repo, run_id="r1")
        self.assertNotIn("junk.md", os.listdir(staging))
        self.assertIn("COMPLETE", os.listdir(staging))
        self.assertEqual(self.read(".why-log", "adr", "README.md"),
                         render.build_files(_decisions(), HEAD)["README.md"])
        self.assertFalse(os.path.exists(self.path(".why-log", "adr", "junk.md")))

    def test_out_path_rejects_absolute_and_parent(self) -> None:
        for bad in ("/tmp/adr", "../adr", "docs/../../adr", "", "docs\\..\\..\\x"):
            with self.assertRaises(WhyLogError, msg=bad) as ctx:
                render.render_current(self.repo, out=bad, preview=True)
            self.assertEqual(ctx.exception.code, 2)


class Undo(RenderBase):
    def _second_render(self) -> dict:
        render.render_current(self.repo, run_id="r1")
        self.first = self.snapshot(".why-log/adr")
        changed = _decisions()
        changed[0]["title"] = "Use SQLite job queue"   # 0001 renamed: one delete + one add
        changed[1]["reason"] = "Nothing else needed."  # 0002 modified
        self.write_decisions(changed)
        return render.render_current(self.repo, run_id="r2")

    def test_undo_restores_modified_deleted_and_removes_added(self) -> None:
        out = self._second_render()
        self.assertEqual(out["added"], ["0001-use-sqlite-job-queue.md"])
        self.assertEqual(out["deleted"], ["0001-use-sqlite-queue.md"])
        self.assertIn("0002-cron-scheduler.md", out["modified"])
        undone = render.undo(self.repo, "r2")
        self.assertEqual(undone["removed"], ["0001-use-sqlite-job-queue.md"])
        self.assertIn("0001-use-sqlite-queue.md", undone["restored"])
        self.assertEqual(self.snapshot(".why-log/adr"), self.first)

    def test_undo_refuses_after_manual_edit(self) -> None:
        self._second_render()
        with open(self.path(".why-log", "adr", "0002-cron-scheduler.md"), "a", encoding="utf-8") as fh:
            fh.write("hand edit\n")
        with self.assertRaises(WhyLogError) as ctx:
            render.undo(self.repo, "r2")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("0002-cron-scheduler.md", str(ctx.exception))
        self.assertTrue(os.path.exists(self.path(".why-log", "adr", "0001-use-sqlite-job-queue.md")))

    def test_undo_unknown_run(self) -> None:
        for rid in ("nope", "../x"):
            with self.assertRaises(WhyLogError) as ctx:
                render.undo(self.repo, rid)
            self.assertEqual(ctx.exception.code, 2)

    def test_crash_after_manifest_resumes_apply(self) -> None:
        render.render_current(self.repo, run_id="r1")
        changed = _decisions()
        changed[1]["reason"] = "Nothing else needed."
        self.write_decisions(changed)
        real_apply = render._apply
        render._apply = lambda *_a: (_ for _ in ()).throw(RuntimeError("crash"))
        try:
            with self.assertRaises(RuntimeError):
                render.render_current(self.repo, run_id="r2")
        finally:
            render._apply = real_apply
        out = render.render_current(self.repo, run_id="r2")
        self.assertEqual((out["modified"], out["run_id"]), ([], None))
        self.assertIn("Nothing else needed.", self.read(".why-log", "adr", "0002-cron-scheduler.md"))
        render.undo(self.repo, "r2")
        self.assertNotIn("Nothing else needed.", self.read(".why-log", "adr", "0002-cron-scheduler.md"))


if __name__ == "__main__":
    unittest.main()
