"""why-log CLI: harvest → reconstruct → gate (a) existence → gate (b) support → render. Each stage checkpoints."""
import argparse
import datetime
import os
import sys
from typing import List, Optional

import harvest
import reconstruct
import render
import support
import validate
from common import WhyLogError, exclude_state_dir, git, read_json, require_repo, state_path, write_json_atomic


def _stage(run_dir: str, name: str):
    path = os.path.join(run_dir, f"{name}.json")
    return path, (read_json(path) if os.path.exists(path) else None)


def run(repo: str, run_id: Optional[str], budget: int, hide: List[str], model: Optional[str],
        allow_shallow: bool, dry_run: bool) -> int:
    repo = require_repo(repo)
    exclude_state_dir(repo)
    run_id = run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = state_path(repo, "run", run_id)
    path, pack = _stage(run_dir, "pack")
    if pack is None:
        pack = harvest.build_pack(repo, budget, hide, allow_shallow)
        write_json_atomic(path, pack)
    print(f"[pack] head {pack['head'][:12]} · commits {len(pack['commits'])} · diffs {len(pack['diffs'])} · "
          f"docs {len(pack['docs'])} · ~{pack['estimated_tokens']} tokens")
    if dry_run:
        print("[dry-run] nothing sent to the model. docs:", ", ".join(d["path"] for d in pack["docs"]))
        return 0
    path, rec = _stage(run_dir, "reconstruct")
    if rec is None:
        rec = reconstruct.reconstruct(pack, model)
        write_json_atomic(path, rec)
    decisions = validate.check_existence(repo, pack, rec["decisions"])
    path, sup = _stage(run_dir, "support")
    if sup is None:
        sup = support.judge(decisions, model)
        write_json_atomic(path, sup)
    decisions = sup["decisions"]
    written = render.render(decisions, pack["head"], os.path.join(run_dir, "adr"))
    counts = {k: sum(1 for d in decisions if d["confidence"] == k) for k in ("explicit", "inferred", "guess")}
    print(f"[done] decisions {len(decisions)} {counts} · ADR files {len(written)} · {run_dir}")
    if git(repo, "rev-parse", "HEAD").strip() != pack["head"]:
        print(f"[warn] HEAD moved during the run; results are pinned to {pack['head'][:12]}")
    return 0 if counts["guess"] == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="why-log")
    ap.add_argument("repo")
    ap.add_argument("--run-id")
    ap.add_argument("--budget", type=int, default=600000)
    ap.add_argument("--hide", action="append", default=[], help="glob to exclude from the pack (repeatable)")
    ap.add_argument("--model")
    ap.add_argument("--allow-shallow", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        return run(a.repo, a.run_id, a.budget, a.hide, a.model, a.allow_shallow, a.dry_run)
    except WhyLogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
