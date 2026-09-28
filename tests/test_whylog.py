"""whylog.py wiring: non-git exit code, per-window checkpoints and resume, window merge. Never calls the model."""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (sys.path + git isolation + model guard)

import harvest
import make_fixture
import reconstruct
import whylog
from common import CALL_OVERHEAD_TOKENS


def _quiet(argv) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return whylog.main(argv)


class WhylogWiring(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_non_git_exit_2_writes_nothing(self) -> None:
        self.assertEqual(_quiet([self.tmp, "--dry-run"]), 2)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_windowed_dry_run_checkpoints_and_resumes(self) -> None:
        repo = os.path.join(self.tmp, "fx")
        make_fixture.build(repo)
        budget = str(CALL_OVERHEAD_TOKENS + 400)
        self.assertEqual(_quiet([repo, "--run-id", "t", "--budget", budget, "--dry-run"]), 0)
        run_dir = os.path.join(repo, ".why-log", "run", "t")
        names = sorted(os.listdir(run_dir))
        count = len([n for n in names if n.startswith("pack-w")])
        self.assertGreater(count, 1)
        self.assertIn("windows.json", names)
        with mock.patch.object(harvest, "build_packs", side_effect=AssertionError("harvest re-ran")):
            self.assertEqual(_quiet([repo, "--run-id", "t", "--budget", budget, "--dry-run"]), 0)

    def test_reconstruct_per_window_then_merge(self) -> None:
        packs = [{"window": {"index": 1}}, {"window": {"index": 2}}]
        replies = iter([{"decisions": [{"id": "D001", "title": "Use SQLite"}, {"id": "D002", "title": "Drop UI"}]},
                        {"decisions": [{"id": "D001", "title": "use  sqlite"}, {"id": "D002", "title": "Cron"}]}])
        with mock.patch.object(reconstruct, "reconstruct", side_effect=lambda *_: next(replies)):
            merged = whylog._reconstruct(self.tmp, packs, None)
        self.assertEqual([(d["id"], d["title"]) for d in merged],
                         [("D001", "Use SQLite"), ("D002", "Drop UI"), ("D003", "Cron")])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "reconstruct-w02.json")))
        with mock.patch.object(reconstruct, "reconstruct", side_effect=AssertionError("re-ran")):
            self.assertEqual(len(whylog._reconstruct(self.tmp, packs, None)), 3)


if __name__ == "__main__":
    unittest.main()
