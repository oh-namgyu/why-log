"""ADR content builders: pure functions, so the same decisions.json always yields byte-identical files."""
import re
from typing import Dict, List, Optional, Tuple

MARKER = "<!-- why-log:generated -->"
HISTORY = {"explicit": "Stated in history (verbatim evidence)", "inferred": "Inferred from history"}
SPOKEN = {"choice": "choice", "free": "free text"}


def _slug(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_]+", "-", s)[:50] or "decision"


def _testimony(d: Dict) -> Optional[Dict]:
    t = d.get("testimony")
    return t if isinstance(t, dict) and t.get("mode") in SPOKEN else None


def provenance(d: Dict) -> Optional[str]:
    """The ADR provenance label, or None when the decision gets no ADR (guess / unknown)."""
    t = _testimony(d)
    if t:
        return f"Maintainer testimony ({SPOKEN[t['mode']]})"
    return HISTORY.get(d.get("confidence", ""))


def _reason(d: Dict) -> List[str]:
    t = _testimony(d)
    history = HISTORY.get(d.get("confidence", ""))
    if not t:
        return ["## Reason", d.get("reason", ""), ""]
    lines = ["## Reason", f"**Maintainer:** {t['answer']}", ""]
    if history and d.get("reason"):
        lines += [f"### {history}", d["reason"], ""]
    return lines


def _conflict(d: Dict) -> List[str]:
    if not d.get("conflict"):
        return []
    t = _testimony(d)
    history = HISTORY.get(d.get("confidence", ""))
    lines = ["> ⚠ **Conflict** — the sources disagree:", ">"]
    lines.append(f"> - History: {d['reason']}" if history and d.get("reason") else
                 "> - History: the reconstructed reason is contradicted by its own evidence")
    if t:
        lines.append(f"> - Maintainer: {t['answer']}")
    return lines + [f"> - Note: {d['conflict']}", ""]


def adr(n: int, d: Dict, head: str) -> str:
    ev = [e for e in d.get("evidence") or [] if e.get("exists") is True]
    lines = [MARKER, f"# {n:04d}. {d.get('title', '')}", "",
             f"- Status: reconstructed from history @ `{head[:12]}`", f"- Provenance: **{provenance(d)}**", "",
             "## Decision", d.get("decision", ""), ""]
    if d.get("alternatives"):
        lines += ["## Considered alternatives", *[f"- {a}" for a in d["alternatives"]], ""]
    lines += _reason(d) + _conflict(d)
    if ev:
        lines += ["## Evidence", *[f"- `{e['kind']}:{e['ref']}` — “{e['quote']}”" for e in ev], ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _index(entries: List[Tuple[int, str, Dict]], unknown: int, head: str) -> str:
    counts: Dict[str, int] = {}
    for _n, _name, d in entries:
        counts[provenance(d) or ""] = counts.get(provenance(d) or "", 0) + 1
    lines = [MARKER, "# Decision log (why-log)", "", f"Reconstructed from history @ `{head[:12]}`.", "",
             f"- ADRs: {len(entries)}", *[f"  - {k}: {counts[k]}" for k in sorted(counts)],
             f"- Unknowns: {unknown} (see [unknowns.md](unknowns.md))", "", "## Decisions"]
    lines += [f"- [{n:04d}. {d.get('title', '')}]({name}) — {provenance(d)}" for n, name, d in entries]
    return "\n".join(lines) + "\n"


def _unknowns(rows: List[Dict], conflicts: List[Dict]) -> str:
    lines = [MARKER, "# Unknowns", "", "No reason could be grounded in history for these; ask the maintainer.", ""]
    lines += [f"- **{d.get('title', '')}** (`{d.get('id', '')}`) — ask: {d.get('question') or 'why was this chosen?'}"
              for d in rows] or ["- none"]
    if conflicts:
        lines += ["", "## Conflicts", *[f"- **{d.get('title', '')}** — ⚠ {d['conflict']}" for d in conflicts]]
    return "\n".join(lines) + "\n"


def build_files(decisions: List[Dict], head: str) -> Dict[str, str]:
    """name -> content for every generated file. ADR numbers follow the id-sorted position of all decisions,
    so a guess later promoted by testimony does not renumber the other ADRs."""
    ordered = sorted(decisions, key=lambda d: str(d.get("id", "")))
    files: Dict[str, str] = {}
    entries, rows = [], []
    for n, d in enumerate(ordered, 1):
        if provenance(d) is None:
            rows.append(d)
            continue
        name = f"{n:04d}-{_slug(d.get('title', ''))}.md"
        files[name] = adr(n, d, head)
        entries.append((n, name, d))
    files["README.md"] = _index(entries, len(rows), head)
    files["unknowns.md"] = _unknowns(rows, [d for d in ordered if d.get("conflict")])
    return files
