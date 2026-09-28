"""why-log CLI: harvest → reconstruct → gate (a) existence → gate (b) support → render. Each stage checkpoints."""
import argparse
import datetime
import os
import sys
from typing import Dict, List, Optional

import harvest
import reconstruct
import render
import support
import validate
from common import WhyLogError, exclude_state_dir, read_json, require_repo, state_path, write_json_atomic


def _stage(run_dir: str, name: str):
    path = os.path.join(run_dir, f"{name}.json")
    return path, (read_json(path) if os.path.exists(path) else None)


def _harvest(run_dir: str, repo: str, budget: int, hide: List[str], allow_shallow: bool) -> List[Dict]:
    """Per-window pack checkpoints; windows.json is written last, so a partial harvest is redone."""
    index = os.path.join(run_dir, "windows.json")
    if os.path.exists(index):
        count = read_json(index)["count"]
        return [read_json(os.path.join(run_dir, f"pack-w{n:02d}.json")) for n in range(1, count + 1)]
    packs = harvest.build_packs(repo, budget, hide, allow_shallow)
    for n, pack in enumerate(packs, 1):
        write_json_atomic(os.path.join(run_dir, f"pack-w{n:02d}.json"), pack)
    write_json_atomic(index, {"count": len(packs), "head": packs[0]["head"]})
    return packs


def _reconstruct(run_dir: str, packs: List[Dict], model: Optional[str]) -> List[Dict]:
    recs = []
    for n, pack in enumerate(packs, 1):
        path, rec = _stage(run_dir, f"reconstruct-w{n:02d}")
        if rec is None:
            rec = reconstruct.reconstruct(pack, model)
            write_json_atomic(path, rec)
        recs.append(rec)
    return reconstruct.merge_windows(recs)


def run(repo: str, run_id: Optional[str], budget: int, hide: List[str], model: Optional[str],
        allow_shallow: bool, dry_run: bool) -> int:
    repo = require_repo(repo)
    exclude_state_dir(repo)
    run_id = run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = state_path(repo, "run", run_id)
    packs = _harvest(run_dir, repo, budget, hide, allow_shallow)
    for pack in packs:
        w = pack["window"]
        print(f"[pack {w['index']}/{w['of']}] head {pack['head'][:12]} · commits {len(pack['commits'])} · "
              f"diffs {len(pack['diffs'])} ({pack['shrink']['mode']}) · docs {len(pack['docs'])} · "
              f"~{pack['estimated_tokens']} tokens")
    # gate (a) sees the docs at HEAD (newest window only) and every window's selected diffs
    pack = dict(packs[0], diffs=[d for p in packs for d in p["diffs"]])
    if dry_run:
        print("[dry-run] nothing sent to the model. docs:", ", ".join(d["path"] for d in pack["docs"]))
        return 0
    rejected: List[Dict] = []
    decisions = validate.check_existence(repo, pack, _reconstruct(run_dir, packs, model), rejected)
    if rejected:
        write_json_atomic(os.path.join(run_dir, "schema_rejected.json"), rejected)
    path, sup = _stage(run_dir, "support")
    if sup is None:
        sup = support.judge(decisions, model)
        write_json_atomic(path, sup)
    decisions = sup["decisions"]
    malformed = sup.get("malformed") or []
    written = render.render(decisions, pack["head"], os.path.join(run_dir, "adr"))
    counts = {k: sum(1 for d in decisions if d["confidence"] == k) for k in ("explicit", "inferred", "guess")}
    print(f"[done] decisions {len(decisions)} {counts} · ADR files {len(written)} · "
          f"schema-rejected {len(rejected)} · judge-malformed {len(malformed)} · {run_dir}")
    moved = harvest.head_moved(repo, pack["head"])
    if moved:
        print(f"[warn] HEAD moved: {pack['head'][:12]}→{moved[:12]}; results are pinned to {pack['head'][:12]}")
    return 0 if counts["guess"] == 0 and not rejected and not malformed else 1


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
