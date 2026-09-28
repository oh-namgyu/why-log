"""Stage 6 evaluation driver.

For each repo spec {name, source, head, hide, gold, tracks, answers}: isolated clone at the pinned head
(T3 = subject-only messages) → whylog.run with the hide globs → PRE snapshot → mock user answers the top
questions (answers=true only) → POST snapshot → score + leak check. Finished stages are cached per
(repo, track) under --out; the live source repository is only read by `git clone --no-local`.

  python3 eval/run.py --spec SPEC.json --work SCRATCH --out RESULTS [--only NAME] [--source NAME=PATH] [--jobs 2]
"""
import argparse
import concurrent.futures
import glob
import os
import sys
import time
import traceback
from typing import Dict, List, Optional

import evalcore
from evalcore import load_or, read_json, write_json_atomic

import clones
import leak
import mock_user
import score
import whylog
from common import state_path

RUN_ID = "eval"


def load_spec(path: str, only: List[str], sources: Dict[str, str]) -> List[Dict]:
    base = os.path.dirname(os.path.abspath(path))
    repos = []
    for r in read_json(path)["repos"]:
        if only and r["name"] not in only:
            continue
        r = dict(r, gold=os.path.join(base, r["gold"]), source=sources.get(r["name"], r["source"]))
        head = read_json(r["gold"])["head"]
        if r.setdefault("head", head) != head:
            raise SystemExit(f"{r['name']}: spec head {r['head']} != gold head {head}")
        repos.append(r)
    return repos


def prepare(r: Dict, track: str, work: str) -> str:
    t2 = clones.clone_at(r["source"], os.path.join(work, r["name"], "T2"), r["head"])
    if track == "T2":
        return t2
    if track == "T3":
        dest = os.path.join(work, r["name"], "T3")
        clones.subject_only_clone(t2, dest)
        return dest
    raise SystemExit(f"unsupported track {track}")


def pipeline_calls(clone: str) -> List[Dict]:
    run_dir = state_path(clone, "run", RUN_ID)
    calls = []
    for path in sorted(glob.glob(os.path.join(run_dir, "reconstruct-w*.json"))):
        calls += [dict(c, purpose="reconstruct") for c in read_json(path).get("calls") or []]
    calls += [dict(c, purpose="support") for c in (load_or(os.path.join(run_dir, "support.json"), {}) or {})
              .get("calls") or []]
    return calls


def packs_of(clone: str) -> List[Dict]:
    run_dir = state_path(clone, "run", RUN_ID)
    return [read_json(p) for p in sorted(glob.glob(os.path.join(run_dir, "pack-w*.json")))]


def reconstruct(r: Dict, clone: str, res: str, a: argparse.Namespace) -> Dict:
    path = os.path.join(res, "pre.json")
    if os.path.exists(path):
        return read_json(path)
    dry = whylog.run(clone, RUN_ID, a.budget, r["hide"], a.model, False, True)
    if dry["windows"] > a.max_windows:
        raise RuntimeError(f"{dry['windows']} windows > --max-windows {a.max_windows} (budget guard)")
    started = time.time()
    summary = whylog.run(clone, RUN_ID, a.budget, r["hide"], a.model, False, False)
    snap = {"summary": summary, "dry_run": dry, "wall_seconds": round(time.time() - started, 1),
            "bodies_left": clones.bodies_left(clone), "pipeline_calls": pipeline_calls(clone),
            **read_json(state_path(clone, "decisions.json"))}
    write_json_atomic(path, snap)
    return snap


def answer(r: Dict, clone: str, res: str, gold: List[Dict], a: argparse.Namespace) -> Optional[Dict]:
    path = os.path.join(res, "post.json")
    if not r.get("answers"):
        return None
    if os.path.exists(path):
        return read_json(path)
    call = evalcore.ledger_call(os.path.join(res, "calls.jsonl"), "mock")
    records = mock_user.answer_top(clone, gold, os.path.join(res, "answers.jsonl"), call, a.model, a.questions)
    snap = {"answers": records, "mock": mock_user.summarize(records),
            **read_json(state_path(clone, "decisions.json"))}
    write_json_atomic(path, snap)
    return snap


def score_track(clone: str, res: str, gold: List[Dict], pre: Dict, post: Optional[Dict],
                a: argparse.Namespace) -> Dict:
    ledger = os.path.join(res, "calls.jsonl")
    m_path, v_path, p_path = (os.path.join(res, f) for f in ("match.json", "verdicts-pre.json", "verdicts-post.json"))
    m = load_or(m_path) or score.match(gold, pre["decisions"], evalcore.ledger_call(ledger, "match"), a.model)
    write_json_atomic(m_path, m)
    judge = evalcore.ledger_call(ledger, "reason-verdict")
    v_pre = load_or(v_path) or _listed(score.judge_reasons(m["pairs"], gold, pre["decisions"], judge, a.model))
    write_json_atomic(v_path, v_pre)
    out = {"pairs": m["pairs"], "match_malformed": m["malformed"], "verdicts_pre": v_pre,
           "PRE": score.metrics(gold, pre["decisions"], m["pairs"], _keyed(v_pre))}
    if post is not None:
        changed = score.changed_pairs(m["pairs"], pre["decisions"], post["decisions"])
        v_post = load_or(p_path) or _listed(score.judge_reasons(changed, gold, post["decisions"], judge, a.model))
        write_json_atomic(p_path, v_post)
        merged = {**_keyed(v_pre), **_keyed(v_post)}
        out.update(verdicts_post=v_post, POST=score.metrics(gold, post["decisions"], m["pairs"], merged),
                   mock=post["mock"])
        out["question_efficiency"] = score.efficiency(out["PRE"], out["POST"], post["mock"]["asked"])
    out["leak"] = leak.report(clone, packs_of(clone), gold)
    return out


def _listed(verdicts: Dict) -> List[Dict]:
    return [{"gold": g, "rec": r, **v} for (g, r), v in sorted(verdicts.items())]


def _keyed(items: List[Dict]) -> Dict:
    return {(v["gold"], v["rec"]): {"verdict": v["verdict"], "why": v.get("why", "")} for v in items}


def run_track(r: Dict, track: str, a: argparse.Namespace) -> Dict:
    res = os.path.join(a.out, r["name"], track)
    os.makedirs(res, exist_ok=True)
    gold = read_json(r["gold"])["decisions"]
    clone = prepare(r, track, a.work)
    pre = reconstruct(r, clone, res, a)
    post = answer(r, clone, res, gold, a)
    result = score_track(clone, res, gold, pre, post, a)
    result.update(repo=r["name"], track=track, head=pre["head"], wall_seconds=pre["wall_seconds"],
                  questions_queued=pre["summary"]["questions_queued"], bodies_left=pre["bodies_left"],
                  pipeline_calls=pre["pipeline_calls"], eval_calls=evalcore.read_jsonl(os.path.join(res, "calls.jsonl")))
    write_json_atomic(os.path.join(res, "score.json"), result)
    return result


def run_repo(r: Dict, a: argparse.Namespace) -> List[Dict]:
    out = []
    for track in r["tracks"]:
        t = time.time()
        try:
            out.append(run_track(r, track, a))
            print(f"[{r['name']} {track}] done in {time.time() - t:.0f}s", file=sys.stderr)
        except Exception as exc:  # one failing track must not stop the others; the error is recorded
            err = {"repo": r["name"], "track": track, "error": f"{type(exc).__name__}: {exc}",
                   "trace": traceback.format_exc()[-2000:]}
            write_json_atomic(os.path.join(a.out, r["name"], track, "error.json"), err)
            print(f"[{r['name']} {track}] FAILED: {err['error']}", file=sys.stderr)
            out.append(err)
    return out


def parse(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="eval/run.py")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--work", required=True, help="scratch dir for isolated clones")
    ap.add_argument("--out", required=True, help="results dir")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--source", action="append", default=[], help="NAME=PATH_OR_URL override")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--budget", type=int, default=600000)
    ap.add_argument("--max-windows", type=int, default=1, help="refuse a track whose pack needs more windows")
    ap.add_argument("--questions", type=int, default=10)
    ap.add_argument("--model")
    a = ap.parse_args(argv)
    a.work, a.out = os.path.abspath(a.work), os.path.abspath(a.out)
    return a


def main(argv: Optional[List[str]] = None) -> int:
    a = parse(argv)
    repos = load_spec(a.spec, a.only, dict(s.split("=", 1) for s in a.source))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(a.jobs, 2))) as pool:
        results = [x for batch in pool.map(lambda r: run_repo(r, a), repos) for x in batch]
    failed = [x for x in results if "error" in x]
    print(f"{len(results) - len(failed)} track(s) scored, {len(failed)} failed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
