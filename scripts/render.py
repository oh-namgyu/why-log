"""Render graded decisions as MADR-style ADRs plus an index and an unknowns list."""
import os
import re
from typing import Dict, List, Optional

MARKER = "<!-- why-log:generated -->"
BADGE = {"explicit": "근거 명시 (code)", "inferred": "추론 (code, inferred)", "guess": "근거 없음 — 질문 필요"}


def _slug(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_]+", "-", s)[:50] or "decision"


def _adr(n: int, d: Dict, head: str) -> str:
    ev = [e for e in d.get("evidence") or [] if e.get("exists")]
    lines = [MARKER, f"# {n:04d}. {d.get('title', '')}", "",
             f"- Status: reconstructed from history @ `{head[:12]}`",
             f"- Provenance: **{BADGE.get(d['confidence'], d['confidence'])}**", "",
             "## Decision", d.get("decision", ""), ""]
    if d.get("alternatives"):
        lines += ["## Considered alternatives", *[f"- {a}" for a in d["alternatives"]], ""]
    lines += ["## Reason", d.get("reason", "") if d["confidence"] != "guess" else "_Unknown — see question below._", ""]
    if d.get("conflict"):
        lines += [f"> ⚠ Evidence conflicts with the stated reason: {d['conflict']}", ""]
    if ev:
        lines += ["## Evidence", *[f"- `{e['kind']}:{e['ref']}` — “{e['quote']}”" for e in ev], ""]
    return "\n".join(lines) + "\n"


def render(decisions: List[Dict], head: str, out_dir: str) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    written, index, unknowns = [], ["# Decision log (why-log)", "", MARKER, ""], ["# Unknowns", "", MARKER, ""]
    kept = [d for d in decisions if d["confidence"] in ("explicit", "inferred")]
    for n, d in enumerate(kept, 1):
        name = f"{n:04d}-{_slug(d.get('title', ''))}.md"
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(_adr(n, d, head))
        written.append(name)
        index.append(f"- [{n:04d} {d.get('title', '')}]({name}) — {BADGE[d['confidence']]}")
    for d in decisions:
        if d["confidence"] == "guess" or d.get("conflict"):
            unknowns.append(f"- **{d.get('title', '')}** — ask: {d.get('question') or 'why was this chosen?'}")
    for name, body in (("README.md", index), ("unknowns.md", unknowns)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(body) + "\n")
    return written


def render_current(repo: str, run_id: Optional[str] = None, out: Optional[str] = None, preview: bool = False) -> Dict:
    """STUB (stage 4): render <repo>/.why-log/decisions.json transactionally into .why-log/adr or `out`."""
    raise NotImplementedError


def undo(repo: str, run_id: str) -> Dict:
    """STUB (stage 4): restore the files a render changed, from its manifest."""
    raise NotImplementedError
