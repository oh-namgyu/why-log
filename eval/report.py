"""Summarize scored tracks into summary.json / summary.md, and write a human spot-check sample.

  python3 eval/report.py --out RESULTS [--spec SPEC --spotcheck FILE --fraction 0.2 --seed 6]
"""
import argparse
import glob
import os
import random
import sys
from typing import Dict, List, Optional

from evalcore import read_json, read_jsonl, write_json_atomic
from run import load_spec

COLS = ("decisions", "nonguess", "recall", "reasoned_recall", "precision", "contradiction_rate", "unsupported_rate",
        "unsupported_rate_model_claimed")


def _fmt(v: object) -> str:
    return "—" if v is None else (f"{v:.1%}" if isinstance(v, float) else str(v))


def _usage(calls: List[Dict]) -> Dict:
    tokens = sum(sum(int(u or 0) for k, u in (c.get("usage") or {}).items()
                     if k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                 for c in calls)
    return {"calls": len(calls), "input_tokens": tokens,
            "output_tokens": sum(int((c.get("usage") or {}).get("output_tokens") or 0) for c in calls),
            "cost_usd": round(sum(float(c.get("cost_usd") or 0) for c in calls), 2),
            "seconds": round(sum(float(c.get("seconds") or 0) for c in calls), 1)}


def row(s: Dict) -> Dict:
    pre, post = s["PRE"], s.get("POST")
    out = {"repo": s["repo"], "track": s["track"], **{f"pre_{k}": pre.get(k) for k in COLS},
           "post_recall": post and post["recall"], "post_reasoned_recall": post and post["reasoned_recall"],
           "question_efficiency": s.get("question_efficiency"), "questions_queued": s.get("questions_queued"),
           "mock": s.get("mock"), "leak": {k: s["leak"][k] for k in ("overlap_8grams", "residual_leak_8grams",
                                                                    "gold_overlap_8grams")},
           "pipeline": _usage(s.get("pipeline_calls") or []), "eval": _usage(s.get("eval_calls") or []),
           "reconstruct_wall_seconds": s.get("wall_seconds")}
    return out


def table(rows: List[Dict]) -> str:
    head = ["repo", "track", "dec (non-guess)", "PRE recall", "PRE reasoned", "POST recall", "POST reasoned",
            "precision", "contradiction", "unsupported (gate)", "unsupported (model-claimed)", "q-eff", "mock leak",
            "8-gram overlap / residual"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        mock = r["mock"] or {}
        lines.append("| " + " | ".join([
            r["repo"], r["track"], f"{r['pre_decisions']} ({r['pre_nonguess']})", _fmt(r["pre_recall"]),
            _fmt(r["pre_reasoned_recall"]), _fmt(r["post_recall"]), _fmt(r["post_reasoned_recall"]),
            _fmt(r["pre_precision"]), _fmt(r["pre_contradiction_rate"]), _fmt(r["pre_unsupported_rate"]),
            _fmt(r["pre_unsupported_rate_model_claimed"]), _fmt(r["question_efficiency"]),
            _fmt(mock.get("leak_rate")), f"{r['leak']['overlap_8grams']} / {r['leak']['residual_leak_8grams']}"]) + " |")
    return "\n".join(lines)


def _pick(items: List, fraction: float, rng: random.Random) -> List:
    k = min(len(items), max(1, round(len(items) * fraction))) if items else 0
    return sorted(rng.sample(items, k), key=str)


def spotcheck(out: str, fraction: float, seed: int, golds: Dict[str, str]) -> str:
    rng = random.Random(seed)
    lines = [f"# Judge spot-check sample ({fraction:.0%} of matched pairs, seed {seed})", "",
             "Mark each line OK / WRONG. Matching rule: same subject and same chosen option.", ""]
    for path in sorted(glob.glob(os.path.join(out, "*", "*", "score.json"))):
        lines += _spot_track(path, fraction, rng, golds)
    return "\n".join(lines) + "\n"


def _spot_track(path: str, fraction: float, rng: random.Random, golds: Dict[str, str]) -> List[str]:
    s, res = read_json(path), os.path.dirname(path)
    gold = {g["id"]: g for g in read_json(golds[s["repo"]])["decisions"]}
    recs = {d["id"]: d for d in read_json(os.path.join(res, "pre.json"))["decisions"]}
    verdicts = {(v["gold"], v["rec"]): v for v in s["verdicts_pre"]}
    lines = [f"## {s['repo']} {s['track']} — {len(s['pairs'])} matched pairs", ""]
    for p in _pick(s["pairs"], fraction, rng):
        g, d, v = gold.get(p["gold"], {}), recs[p["rec"]], verdicts.get((p["gold"], p["rec"]), {})
        lines += [f"### {p['gold']} ↔ {p['rec']} (match score {p['score']}) — reason verdict: **{v.get('verdict')}**"
                  f" ({v.get('why', '')})",
                  f"- gold: {g.get('title')} — {g.get('decision')}", f"- gold reason: {g.get('reason')}",
                  f"- rec [{d.get('confidence')}]: {d.get('title')} — {d.get('decision')}",
                  f"- rec reason: {d.get('reason')}", "- [ ] match OK  - [ ] verdict OK", ""]
    matched_g = {p["gold"] for p in s["pairs"]}
    lines.append("Unmatched gold: " + "; ".join(f"{i} {g['title']}" for i, g in sorted(gold.items())
                                                if i not in matched_g))
    matched_r = {p["rec"] for p in s["pairs"]}
    lines += ["", "Unmatched reconstructions (non-guess): " + "; ".join(
        f"{i} {d.get('title')}" for i, d in sorted(recs.items()) if i not in matched_r and d.get("confidence") != "guess"),
        ""]
    return lines + _spot_mock(res, s)


def _spot_mock(res: str, s: Dict) -> List[str]:
    """Every mock answer (at most 10 per track) with the POST verdict of the pair it changed, if any."""
    records = read_jsonl(os.path.join(res, "answers.jsonl"))
    if not records:
        return []
    post = {v["rec"]: v for v in s.get("verdicts_post") or []}
    lines = ["Mock answers (check the quote fits the question):", ""]
    for r in records:
        v = post.get(r.get("subject"), {})
        lines.append(f"- {r['qid']} {r['subject']} {r.get('title')}: {r['recorded']} "
                     f"{'from ' + str(r['found_in']) + ' ' if r['found_in'] else ''}\"{r['quote']}\""
                     f"{' → POST verdict ' + v['verdict'] + ' vs ' + v['gold'] if v else ''}")
    return lines + [""]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="eval/report.py")
    ap.add_argument("--out", required=True)
    ap.add_argument("--spotcheck", help="write a spot-check sample to this file (needs --spec)")
    ap.add_argument("--spec", help="the spec used for the run (gold file locations)")
    ap.add_argument("--fraction", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=6)
    a = ap.parse_args(argv)
    rows = [row(read_json(p)) for p in sorted(glob.glob(os.path.join(a.out, "*", "*", "score.json")))]
    write_json_atomic(os.path.join(a.out, "summary.json"), rows)
    with open(os.path.join(a.out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(table(rows) + "\n")
    if a.spotcheck:
        with open(a.spotcheck, "w", encoding="utf-8") as fh:
            golds = {r["name"]: r["gold"] for r in load_spec(a.spec, [], {})}
            fh.write(spotcheck(a.out, a.fraction, a.seed, golds))
    print(table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
