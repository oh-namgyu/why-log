"""Gate (a): every quote must exist verbatim where it claims to come from. Deterministic, no model."""
from typing import Dict, List

from common import git, git_ok, normalize_ws


def _source_text(repo: str, pack: Dict, ev: Dict, cache: Dict[str, str]) -> str:
    kind, ref = ev.get("kind"), str(ev.get("ref", ""))
    key = f"{kind}:{ref}"
    if key in cache:
        return cache[key]
    text = ""
    if kind == "commit" and ref and git_ok(repo, "cat-file", "-e", f"{ref}^{{commit}}"):
        text = git(repo, "show", "--format=%B", "--unified=2", ref)
    elif kind == "doc":
        text = next((d["text"] for d in pack["docs"] if d["path"] == ref), "")
    elif kind == "file" and "@" in ref:
        path, sha = ref.rsplit("@", 1)
        if git_ok(repo, "cat-file", "-e", f"{sha}:{path}"):
            text = git(repo, "show", f"{sha}:{path}")
    cache[key] = normalize_ws(text)
    return cache[key]


def check_existence(repo: str, pack: Dict, decisions: List[Dict]) -> List[Dict]:
    """Mark each evidence item with exists=True/False; never trusts the model's own claim."""
    cache: Dict[str, str] = {}
    for d in decisions:
        for ev in d.get("evidence") or []:
            quote = normalize_ws(str(ev.get("quote", "")))
            source = _source_text(repo, pack, ev, cache)
            ev["exists"] = bool(quote) and len(quote) >= 10 and quote in source
    return decisions
