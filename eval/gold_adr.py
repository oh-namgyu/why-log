"""Build a why-log gold file from a repository's ADR directory, so public benchmarks use the same scorer.

title ← first heading (leading "N." numbering removed) · decision ← "Decision" section · reason ← Context /
Decision drivers / Rationale / Why / Problem / Motivation sections · status ← "Status" section or line.
`traceable` stays null: whether each ADR was implemented is not verified here.

  python3 eval/gold_adr.py REPO --head SHA --dir docs/adr --name NAME --out gold.json
"""
import argparse
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
NUMBERING = re.compile(r"^(ADR[-\s]*)?\d+[.:)\-]?\s+", re.IGNORECASE)
DECISION = re.compile(r"^decision( outcome)?\b", re.IGNORECASE)
REASON = re.compile(r"^(context|decision drivers|rationale|why|problem|the problem|motivation)\b", re.IGNORECASE)
STATUS_LINE = re.compile(r"^\s*status\s*:\s*(.+)$", re.IGNORECASE)
DATE_LINE = re.compile(r"^\s*date\s*:\s*(\S+)", re.IGNORECASE)
REASON_CHARS, DECISION_CHARS = 2000, 1500


def sections(text: str) -> List[Tuple[int, str, List[str]]]:
    """(level, heading, body lines) in order; headings inside fenced code blocks are body text."""
    out: List[Tuple[int, str, List[str]]] = [(0, "", [])]
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        m = None if fenced else HEADING.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2).strip(), []))
        else:
            out[-1][2].append(line)
    return out


def section_text(secs: List[Tuple[int, str, List[str]]], pattern: "re.Pattern[str]") -> str:
    """Text of every section whose heading matches, including its subsections."""
    parts: List[str] = []
    for i, (level, head, body) in enumerate(secs):
        if level == 0 or not pattern.match(head):
            continue
        chunk = list(body)
        for sub_level, sub_head, sub_body in secs[i + 1:]:
            if sub_level <= level:
                break
            chunk += [sub_head] + sub_body
        parts.append(" ".join(" ".join(chunk).split()))
    return " ".join(p for p in parts if p)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _line_value(text: str, pattern: "re.Pattern[str]") -> Optional[str]:
    m = next((pattern.match(line) for line in text.splitlines() if pattern.match(line)), None)
    return m.group(1).strip() if m else None


def parse_adr(text: str) -> Dict:
    secs = sections(text)
    heads = [(lvl, h) for lvl, h, _ in secs if lvl]
    title = NUMBERING.sub("", min(heads, key=lambda x: x[0])[1]).strip(" .") if heads else ""
    status = section_text(secs, re.compile(r"^status\b", re.IGNORECASE)) or _line_value(text, STATUS_LINE)
    return {"title": title, "decision": _clip(section_text(secs, DECISION), DECISION_CHARS),
            "reason": _clip(section_text(secs, REASON), REASON_CHARS),
            "status": (status or "").split(" ")[0] or None, "date": _line_value(text, DATE_LINE)}


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def build(repo: str, head: str, adr_dir: str, name: str) -> Dict:
    paths = [p for p in _git(repo, "ls-tree", "-r", "--name-only", head, "--", adr_dir).splitlines()
             if p.lower().endswith(".md") and re.search(r"(^|/)\d+", p.rsplit("/", 1)[-1])]
    decisions = []
    for n, path in enumerate(sorted(paths), 1):
        adr = parse_adr(_git(repo, "show", f"{head}:{path}"))
        decisions.append({"id": f"G{n:02d}", **adr, "source": path, "traceable": None,
                          "trace_note": "not verified (built from the ADR text only)"})
    return {"schema": "why-log-gold/1", "repo": name, "head": head, "adr_dir": adr_dir, "decisions": decisions}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="eval/gold_adr.py")
    ap.add_argument("repo")
    ap.add_argument("--head", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    sys.path.insert(0, __import__("os").path.dirname(__file__))
    from evalcore import write_json_atomic
    gold = build(a.repo, a.head, a.dir, a.name)
    write_json_atomic(a.out, gold)
    empty = [d["id"] for d in gold["decisions"] if not d["reason"] or not d["decision"]]
    print(f"{len(gold['decisions'])} decisions → {a.out}; missing reason/decision: {empty or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
