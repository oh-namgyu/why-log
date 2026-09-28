"""why-log CLI.

run       harvest → reconstruct → gate (a) existence → gate (b) support → decisions.json → question queue → render
questions / answer / status   the question queue (qa.py)
render / undo                 transactional ADR output (render.py)
"""
import argparse
import datetime
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import harvest
import qa
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


DECISIONS = "decisions.json"


def _gate(repo: str, run_dir: str, pack: Dict, packs: List[Dict], model: Optional[str]) -> Dict:
    rejected: List[Dict] = []
    decisions = validate.check_existence(repo, pack, _reconstruct(run_dir, packs, model), rejected)
    if rejected:
        write_json_atomic(os.path.join(run_dir, "schema_rejected.json"), rejected)
    path, sup = _stage(run_dir, "support")
    if sup is None:
        sup = support.judge(decisions, model)
        write_json_atomic(path, sup)
    return {"decisions": sup["decisions"], "rejected": rejected, "malformed": sup.get("malformed") or []}


def run(repo: str, run_id: Optional[str], budget: int, hide: List[str], model: Optional[str],
        allow_shallow: bool, dry_run: bool, abstain: bool = False) -> Dict:
    repo = require_repo(repo)
    exclude_state_dir(repo)
    run_id = run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = state_path(repo, "run", run_id)
    packs = _harvest(run_dir, repo, budget, hide, allow_shallow)
    for pack in packs:
        w = pack["window"]
        print(f"[pack {w['index']}/{w['of']}] head {pack['head'][:12]} · commits {len(pack['commits'])} · "
              f"diffs {len(pack['diffs'])} ({pack['shrink']['mode']}) · docs {len(pack['docs'])} · "
              f"~{pack['estimated_tokens']} tokens", file=sys.stderr)
    # gate (a) sees the docs at HEAD (newest window only) and every window's selected diffs
    pack = dict(packs[0], diffs=[d for p in packs for d in p["diffs"]])
    if dry_run:
        return {"dry_run": True, "run_id": run_id, "windows": len(packs),
                "tokens": sum(p["estimated_tokens"] for p in packs), "docs": [d["path"] for d in pack["docs"]]}
    gated = _gate(repo, run_dir, pack, packs, model)
    decisions = gated["decisions"]
    write_json_atomic(state_path(repo, DECISIONS), {"schema": "why-log-decisions/1", "head": pack["head"],
                                                     "run_id": run_id, "hidden": hide, "decisions": decisions})
    queued = 0 if abstain else qa.enqueue(repo, decisions, run_id)
    rendered = render.render_current(repo, run_id=run_id)
    counts = {k: sum(1 for d in decisions if d["confidence"] == k) for k in ("explicit", "inferred", "guess")}
    moved = harvest.head_moved(repo, pack["head"])
    return {"run_id": run_id, "head": pack["head"], "decisions": len(decisions), "confidence": counts,
            "questions_queued": queued, "abstain": abstain, "files": rendered.get("files", []),
            "schema_rejected": len(gated["rejected"]), "judge_malformed": len(gated["malformed"]),
            "head_moved_to": moved}


def _run_exit(summary: Dict) -> int:
    if summary.get("dry_run"):
        return 0
    unresolved = summary["confidence"]["guess"] or summary["schema_rejected"] or summary["judge_malformed"]
    return 1 if unresolved else 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="why-log")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="reconstruct decisions from history")
    r.add_argument("repo")
    r.add_argument("--run-id")
    r.add_argument("--budget", type=int, default=600000)
    r.add_argument("--hide", action="append", default=[], help="glob to exclude from the pack (repeatable)")
    r.add_argument("--model")
    r.add_argument("--allow-shallow", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--abstain", action="store_true", help="queue no questions; list unknowns instead")
    q = sub.add_parser("questions", help="open questions, highest priority first")
    q.add_argument("repo")
    q.add_argument("--next", type=int, default=8)
    a = sub.add_parser("answer", help="record an answer to a queued question")
    a.add_argument("repo")
    a.add_argument("--id", required=True)
    mode = a.add_mutually_exclusive_group(required=True)
    mode.add_argument("--choice")
    mode.add_argument("--free")
    mode.add_argument("--unknown", action="store_true")
    for name, text in (("render", "write ADRs transactionally"), ("status", "queue and decision counts")):
        p = sub.add_parser(name, help=text)
        p.add_argument("repo")
        if name == "render":
            p.add_argument("--out", help="write into this repo-relative dir instead of .why-log/adr")
            p.add_argument("--preview", action="store_true")
    u = sub.add_parser("undo", help="restore files changed by a render")
    u.add_argument("repo")
    u.add_argument("run_id")
    for p in sub.choices.values():
        p.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def _dispatch(a: argparse.Namespace) -> Tuple[Dict, int]:
    if a.cmd == "run":
        out = run(a.repo, a.run_id, a.budget, a.hide, a.model, a.allow_shallow, a.dry_run, a.abstain)
        return out, _run_exit(out)
    repo = require_repo(a.repo)
    if a.cmd == "questions":
        return {"questions": qa.next_questions(repo, a.next)}, 0
    if a.cmd == "answer":
        mode = "choice" if a.choice is not None else "free" if a.free is not None else "unknown"
        return qa.answer(repo, a.id, mode, a.choice if a.choice is not None else a.free), 0
    if a.cmd == "render":
        return render.render_current(repo, out=a.out, preview=a.preview), 0
    if a.cmd == "undo":
        return render.undo(repo, a.run_id), 0
    return qa.status(repo), 0


def main(argv: Optional[List[str]] = None) -> int:
    a = _parser().parse_args(argv)
    try:
        out, code = _dispatch(a)
    except WhyLogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    print(json.dumps(out, ensure_ascii=False, indent=None if a.json else 2))
    return code


if __name__ == "__main__":
    sys.exit(main())
