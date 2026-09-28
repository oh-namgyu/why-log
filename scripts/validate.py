"""Gate (a): every quote must exist verbatim where it claims to come from. Deterministic, no model.

Sources are read from git (pinned to the pack's HEAD history) or from the pack's docs — never from anything
the model wrote, so a quote that only appears in the model's own output cannot pass.
"""
import re
from typing import Dict, List, Optional, Tuple

import schema
import harvest
from common import git, git_ok, normalize_ws

MIN_QUOTE = 10
_HEX = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SEP = "\n\x00\n"  # joins independent sources; not whitespace, so a quote can never span two of them


def resolve_commit(repo: str, head: str, ref: str) -> Tuple[Optional[str], Optional[str]]:
    """Full SHA for a (short) hex ref reachable from the pinned head, else (None, reason)."""
    if not _HEX.match(ref):
        return None, "ref is not a hex commit id"
    full = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip()
    if not full or not full.startswith(ref.lower()):
        return None, "no such commit (or ambiguous short id)"
    if not git_ok(repo, "merge-base", "--is-ancestor", full, head):
        return None, "commit is not in the pinned HEAD's history"
    return full, None


def _strip_diff_markers(text: str) -> str:
    """Drop the one-column +/-/space prefix of diff lines so quotes spanning a line break still match."""
    out = []
    for line in text.splitlines():
        if line[:1] in "+- " and not line.startswith(("+++", "---")):
            line = line[1:]
        out.append(line)
    return "\n".join(out)


def _commit_text(repo: str, pack: Dict, sha: str) -> str:
    message = git(repo, "show", "-s", "--format=%B", sha)
    # Re-read with the pack's hidden paths still excluded, or a hidden (gold) file would count as evidence.
    diffs = [git(repo, "show", "--format=", "--unified=3", "-M", sha, "--", *harvest.pack_pathspec(pack))]
    diffs += [d["diff"] for d in pack.get("diffs") or [] if d.get("sha") == sha]
    parts = [message] + [v for d in diffs for v in (d, _strip_diff_markers(d))]
    return _SEP.join(parts)


def _file_text(repo: str, pack: Dict, head: str, ref: str) -> Tuple[Optional[str], str, Optional[str]]:
    """Returns (text, canonical ref, rejection reason) for a `path@sha` ref."""
    if "@" not in ref:
        return None, ref, "file ref must be path@sha"
    path, sha = ref.rsplit("@", 1)
    if not path or path.startswith("/") or ".." in path.split("/"):
        return None, ref, "file path must be repo-relative"
    if harvest.pack_hides(pack, path):
        return None, ref, "path is hidden for this run"
    full, why = resolve_commit(repo, head, sha)
    if not full:
        return None, ref, why
    if not git_ok(repo, "cat-file", "-e", f"{full}:{path}"):
        return None, ref, "path does not exist at that commit"
    return git(repo, "show", f"{full}:{path}"), f"{path}@{full}", None


def _source(repo: str, pack: Dict, ev: Dict) -> Tuple[Optional[str], str, Optional[str]]:
    """Returns (source text, canonical ref, rejection reason)."""
    kind, ref, head = ev.get("kind"), str(ev.get("ref", "")).strip(), pack["head"]
    if kind == "commit":
        full, why = resolve_commit(repo, head, ref)
        return (_commit_text(repo, pack, full), full, None) if full else (None, ref, why)
    if kind == "file":
        return _file_text(repo, pack, head, ref)
    if kind == "doc":
        doc = next((d["text"] for d in pack.get("docs") or [] if d.get("path") == ref), None)
        return (doc, ref, None) if doc is not None else (None, ref, "doc is not in the pack")
    return None, ref, f"unknown evidence kind {kind!r}"


def check_evidence(repo: str, pack: Dict, ev: Dict, cache: Dict[str, Tuple]) -> None:
    """Set ev["exists"] (and ev["rejected"] when False); rewrite ref to its full-SHA form."""
    key = f"{ev.get('kind')}:{ev.get('ref')}"
    if key not in cache:
        text, ref, why = _source(repo, pack, ev)
        cache[key] = (normalize_ws(text) if text is not None else None, ref, why)
    source, ref, why = cache[key]
    ev["ref"] = ref
    quote = normalize_ws(str(ev.get("quote", "")))
    if source is None:
        ev["exists"], ev["rejected"] = False, why
    elif len(quote) < MIN_QUOTE:
        ev["exists"], ev["rejected"] = False, f"quote shorter than {MIN_QUOTE} chars"
    elif quote not in source:
        ev["exists"], ev["rejected"] = False, "quote not found verbatim in the source"
    else:
        ev["exists"], ev["rejected"] = True, None


def check_existence(repo: str, pack: Dict, decisions: List[Dict], rejected: Optional[List[Dict]] = None) -> List[Dict]:
    """Adopt only schema-valid decisions, then mark every evidence item; never trusts the model's own claim.

    Schema-invalid decisions are dropped and appended to `rejected` when a list is given.
    """
    adopted, bad = schema.sanitize(decisions)
    if rejected is not None:
        rejected.extend(bad)
    cache: Dict[str, Tuple] = {}
    for d in adopted:
        for ev in d["evidence"]:
            check_evidence(repo, pack, ev, cache)
    return adopted
