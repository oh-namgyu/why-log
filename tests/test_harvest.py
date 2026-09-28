"""harvest.py: input guards, HEAD pin, budget degradation order, time windows, hiding, leak check, signal."""
import os
import subprocess
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path + git isolation)
from helpers import commit, init_repo, sh

import harvest
import make_fixture
from common import CALL_OVERHEAD_TOKENS, WhyLogError
from reconstruct import render_diff

GOLD_HANDOFF = "We chose the queue broker over polling because the nightly batch kept missing late events.\n"
GOLD_ADR = "Status accepted. Context the ledger must survive restarts so we persist every entry to disk first.\n"
CLEAN_GOLD = "An entirely unrelated sentence about gardening tomatoes in raised beds during a warm summer."


class TempRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def fixture(self) -> str:
        repo = os.path.join(self.tmp, "fixture")
        make_fixture.build(repo)
        return repo


class InputGuards(TempRepoCase):
    def test_non_git_is_code_2(self) -> None:
        with self.assertRaises(WhyLogError) as ctx:
            harvest.build_packs(self.tmp)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("not a git repository", str(ctx.exception))

    def test_shallow_refused_then_flagged(self) -> None:
        src = self.fixture()
        dst = os.path.join(self.tmp, "shallow")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + src, dst], check=True, capture_output=True)
        with self.assertRaises(WhyLogError) as ctx:
            harvest.build_packs(dst)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("shallow", str(ctx.exception))
        packs = harvest.build_packs(dst, allow_shallow=True)
        self.assertTrue(packs[0]["history_truncated"])
        self.assertEqual(len(packs[0]["commits"]), 1)
        self.assertFalse(harvest.build_packs(src)[0]["history_truncated"])


class HeadPin(TempRepoCase):
    def test_pack_pinned_and_move_detected(self) -> None:
        repo = self.fixture()
        pinned = sh(repo, "rev-parse", "HEAD")
        pack = harvest.build_pack(repo)
        self.assertEqual(pack["head"], pinned)
        self.assertEqual(pack["commits"][0]["sha"], pinned[:12])
        self.assertIsNone(harvest.head_moved(repo, pinned))
        commit(repo, "Later change", {"late.txt": "x\n"}, day=28)
        moved = harvest.head_moved(repo, pinned)
        self.assertEqual(moved, sh(repo, "rev-parse", "HEAD"))
        self.assertNotEqual(moved, pinned)


class BudgetShrink(TempRepoCase):
    """Equal-size diffs with distinct signals, so 'lowest signal dropped first' is observable exactly."""

    def repo(self) -> str:
        repo = init_repo(os.path.join(self.tmp, "shrink"))
        body = "".join(f"line {n}\n" for n in range(40))
        commit(repo, "Tweak f1", {"f1.txt": body}, day=1)                                  # signal 0
        commit(repo, "Tweak f2 because the old one was slow", {"f2.txt": body}, day=2)     # signal 2
        commit(repo, "Revert f3 because it broke", {"f3.txt": body}, day=3)                # signal 5
        commit(repo, "Revert f4", {"f4.txt": body}, day=4)                                 # signal 5 (revert word)
        return repo

    def test_order_full_then_trimmed_then_messages_only(self) -> None:
        repo = self.repo()
        full = harvest.build_pack(repo)
        self.assertEqual(full["shrink"]["mode"], "full")
        self.assertEqual(len(full["diffs"]), 4)
        costs = [harvest._cost(render_diff(d)) for d in full["diffs"]]
        base = full["estimated_tokens"] - sum(costs)
        budget = base + 2 * max(costs) + min(costs) // 2
        trimmed = harvest.build_pack(repo, budget)
        self.assertEqual(trimmed["shrink"]["mode"], "diffs-trimmed")
        kept = {d["signal"] for d in trimmed["diffs"]}
        dropped = [harvest.signal_score(c) for c in full["commits"]
                   if c["sha"] not in {d["sha"] for d in trimmed["diffs"]}]
        self.assertEqual(len(trimmed["diffs"]), 2)
        self.assertGreaterEqual(min(kept), max(dropped))
        self.assertLessEqual(trimmed["estimated_tokens"], budget)
        bare = harvest.build_pack(repo, base + min(costs) // 2)
        self.assertEqual(bare["shrink"]["mode"], "messages-only")
        self.assertEqual(bare["diffs"], [])
        self.assertEqual(len(bare["commits"]), 4)
        self.assertLessEqual(bare["estimated_tokens"], base + min(costs) // 2)


class WindowSplit(TempRepoCase):
    def test_tiny_budget_splits_newest_first(self) -> None:
        repo = self.fixture()
        whole = harvest.build_pack(repo)
        budget = CALL_OVERHEAD_TOKENS + 400
        packs = harvest.build_packs(repo, budget)
        self.assertGreater(len(packs), 1)
        for n, pack in enumerate(packs, 1):
            self.assertLessEqual(pack["estimated_tokens"], budget)
            self.assertEqual(pack["window"], {"index": n, "of": len(packs)})
            self.assertEqual(pack["shrink"]["mode"], "windowed")
            self.assertEqual(pack["head"], whole["head"])
            if n > 1:
                self.assertEqual(pack["docs"], [])
                self.assertLessEqual(pack["commits"][0]["date"], packs[n - 2]["commits"][-1]["date"])
        self.assertEqual(packs[0]["docs"], whole["docs"])
        joined = [c["sha"] for p in packs for c in p["commits"]]
        self.assertEqual(joined, [c["sha"] for c in whole["commits"]])
        with self.assertRaises(WhyLogError):
            harvest.build_pack(repo, budget)

    def test_budget_below_one_commit_is_code_2(self) -> None:
        repo = self.fixture()
        with self.assertRaises(WhyLogError) as ctx:
            harvest.build_packs(repo, CALL_OVERHEAD_TOKENS + 30)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("budget too small", str(ctx.exception))


class Hiding(TempRepoCase):
    HIDE = ["HANDOFF*", "docs/adr"]

    def repo(self) -> dict:
        repo = init_repo(os.path.join(self.tmp, "hide"))
        shas = {"init": commit(repo, "Initial import", {"a.py": "x = 1\n", "HANDOFF.md": GOLD_HANDOFF,
                                                         "docs/adr/0001.md": GOLD_ADR, "README.md": "# app\n"}, 1)}
        shas["only"] = commit(repo, "Record the broker decision", {"HANDOFF.md": GOLD_HANDOFF + "more\n"}, 2)
        shas["mixed"] = commit(repo, "Tune a and notes", {"a.py": "x = 2\n", "HANDOFF.md": GOLD_HANDOFF}, 3)
        shas["delete"] = commit(repo, "Retire old ADR", {"docs/adr/0001.md": None, "a.py": "x = 3\n"}, 4)
        sh(repo, "mv", "HANDOFF.md", "docs/old-notes.md")
        shas["rename"] = commit(repo, "Archive notes", {}, 5)
        shas["late"] = commit(repo, "Edit archived notes", {"docs/old-notes.md": GOLD_HANDOFF + "x\n", "a.py": "x = 4\n"}, 6)
        return {"repo": repo, **shas}

    def test_hidden_files_diffs_docs_and_commits(self) -> None:
        r = self.repo()
        pack = harvest.build_pack(r["repo"], hide=self.HIDE)
        shas = [c["sha"] for c in pack["commits"]]
        self.assertEqual(shas, [r["late"], r["delete"], r["mixed"], r["init"]])
        for c in pack["commits"]:
            for f in c["files"]:
                self.assertNotIn("HANDOFF", f["path"])
                self.assertNotIn("adr", f["path"])
                self.assertNotIn("old-notes", f["path"])
        self.assertEqual(pack["hidden_renamed"], ["docs/old-notes.md"])
        self.assertEqual([d["path"] for d in pack["docs"]], ["README.md"])
        self.assertEqual(len(pack["diffs"]), 4)
        for d in pack["diffs"]:
            self.assertNotIn("broker", d["diff"])
            self.assertNotIn("ledger", d["diff"])
        self.assertEqual(harvest.leak_check(pack, [GOLD_HANDOFF, GOLD_ADR]), [])

    def test_globstar_prefix_also_hides_root_files(self) -> None:
        r = self.repo()
        pack = harvest.build_pack(r["repo"], hide=["**/HANDOFF*.md", "**/adr/**"])
        paths = [f["path"] for c in pack["commits"] for f in c["files"]]
        self.assertFalse([p for p in paths if "HANDOFF" in p or "adr" in p or "old-notes" in p])
        self.assertEqual([d["path"] for d in pack["docs"]], ["README.md"])
        self.assertFalse([d for d in pack["diffs"] if "broker" in d["diff"] or "ledger" in d["diff"]])

    def test_leak_check_positive_control(self) -> None:
        r = self.repo()
        pack = harvest.build_pack(r["repo"])
        hits = harvest.leak_check(pack, [CLEAN_GOLD, "  WE chose the QUEUE broker   over polling, because!"])
        self.assertTrue(hits)
        self.assertEqual({h["gold"] for h in hits}, {1})
        self.assertIn("we chose the queue broker over polling because", [h["ngram"] for h in hits])
        self.assertEqual(harvest.leak_check(pack, [CLEAN_GOLD]), [])


class RenameParsing(unittest.TestCase):
    def test_forms(self) -> None:
        self.assertEqual(harvest.split_rename("a.py"), ("a.py",))
        self.assertEqual(harvest.split_rename("a.py => b.py"), ("a.py", "b.py"))
        self.assertEqual(harvest.split_rename("src/{old => new}/f.py"), ("src/old/f.py", "src/new/f.py"))
        self.assertEqual(harvest.split_rename("{ => sub}/x.md"), ("x.md", "sub/x.md"))
        self.assertEqual(harvest.split_rename("sub/{x.md => y.md}"), ("sub/x.md", "sub/y.md"))


def _c(subject: str, files=(), body: str = "") -> dict:
    return {"subject": subject, "body": body, "files": [{"path": p, "added": a, "deleted": d} for p, a, d in files]}


class SignalScore(unittest.TestCase):
    def test_cases(self) -> None:
        cases = [
            (_c("Tweak spacing", [("a.py", 1, 1)]), 0),
            (_c("Dropdown styling"), 0),
            (_c("Revert \"add cache\""), 5),
            (_c("Bump deps", [("web/package.json", 1, 1)]), 3),
            (_c("Prune", [("old.py", 0, 201)]), 2),
            (_c("Prune", [("old.py", 0, 200)]), 0),
            (_c("Move", [("{a => b}/x.py", 0, 0)]), 2),
            (_c("CI", [(".github/ci.yml", 1, 0)]), 1),
            (_c("Tidy", body="done because reviewers asked"), 2),
            (_c("캐시 제거 — 메모리 때문"), 2),
            (_c("Replace requests with httpx", [("requirements.txt", 1, 1)]), 5),
        ]
        for commit_, expected in cases:
            with self.subTest(subject=commit_["subject"]):
                self.assertEqual(harvest.signal_score(commit_), expected)


if __name__ == "__main__":
    unittest.main()
