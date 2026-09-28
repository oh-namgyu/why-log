"""Build history packs: every commit message + stat, plus diffs of high-signal commits, within a token budget.

Over budget, the packer degrades in a fixed order (appendix §F): drop diffs lowest-signal first → messages +
stat only → split the history into consecutive time windows (newest first), each pack within the budget.
"""
import fnmatch
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from common import CALL_OVERHEAD_TOKENS, WhyLogError, estimate_tokens, git, require_repo
from reconstruct import render_commit, render_diff, render_doc, render_pack

MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile",
             "pom.xml", "build.gradle", "Dockerfile", "docker-compose.yml", "setup.py", "setup.cfg")
CONFIG_EXT = (".yml", ".yaml", ".toml", ".ini", ".cfg", ".env.example")
WHY_WORDS = re.compile(r"because|instead|so that|in order to|rather than|replace|switch|migrat|drop |revert|"
                       r"왜|대신|때문|이유|기각|교체|전환|폐기", re.IGNORECASE)
DOC_GLOBS = ("README*", "CHANGELOG*", "docs/*.md", "docs/**/*.md", "doc/**/*.md", "ARCHITECTURE*", "DESIGN*")
DIFF_CAP_CHARS = 12000
DOC_CAP_CHARS = 20000
MIN_DIFF_TOKENS = 30  # below this no real diff fits; stop fetching
REC, FLD = "\x1e", "\x1f"
BRACE_RENAME = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")
WORD = re.compile(r"\w+")


def _commits(repo: str, sha: str) -> List[Dict]:
    raw = git(repo, "-c", "core.quotePath=false", "log", "--format=" + REC + "%H" + FLD + "%ad" + FLD + "%s" + FLD
              + "%b" + FLD, "--date=short", "--numstat", "-M", sha)
    commits = []
    for block in raw.split(REC)[1:]:
        head, _, numstat = block.rpartition(FLD)
        full, date, subject, body = head.split(FLD, 3)
        files = []
        for line in numstat.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                added, deleted, path = parts
                files.append({"path": path, "added": int(added) if added.isdigit() else 0,
                              "deleted": int(deleted) if deleted.isdigit() else 0})
        commits.append({"sha": full[:12], "date": date, "subject": subject.strip(), "body": body.strip(),
                        "files": files})
    return commits


def signal_score(commit: Dict) -> int:
    """How likely a commit records a decision (reverts, dependency swaps, big removals, renames, config, why-words)."""
    text = commit["subject"] + " " + commit["body"]
    paths = [f["path"] for f in commit["files"]]
    score = 0
    score += 3 if commit["subject"].lower().startswith("revert") else 0
    score += 3 if any(p.rsplit("/", 1)[-1] in MANIFESTS for p in paths) else 0
    score += 2 if sum(f["deleted"] for f in commit["files"]) > 200 else 0
    score += 2 if any("=>" in p for p in paths) else 0
    score += 1 if any(p.endswith(CONFIG_EXT) for p in paths) else 0
    score += 2 if WHY_WORDS.search(text) else 0
    return score


def head_moved(repo: str, pinned: str) -> Optional[str]:
    """The current HEAD if it differs from the SHA a run was pinned to, else None."""
    now = git(repo, "rev-parse", "HEAD").strip()
    return None if now == pinned else now


# ---- hiding (evaluation track T2): paths, their renamed copies, their diffs, docs, hidden-only commits ----

def split_rename(path: str) -> Tuple[str, ...]:
    """numstat rename notation → (old, new); a plain path → (path,)."""
    m = BRACE_RENAME.match(path)
    if m:
        pre, old, new, post = m.groups()
        return tuple(re.sub("/+", "/", pre + side + post).lstrip("/") for side in (old, new))
    if " => " in path:
        old, new = path.split(" => ", 1)
        return old, new
    return (path,)


def _glob_hidden(path: str, hide: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, g) or path.startswith(g.rstrip("/") + "/") for g in hide)


def _rename_closure(commits: List[Dict], hide: Sequence[str]) -> Set[str]:
    """Paths that were ever renamed to/from a hidden path: their content is the hidden content."""
    pairs = [split_rename(f["path"]) for c in commits for f in c["files"]]
    pairs = [p for p in pairs if len(p) == 2]
    extra: Set[str] = set()
    changed = bool(hide)
    while changed:
        changed = False
        for old, new in pairs:
            hit = [p for p in (old, new) if p in extra or _glob_hidden(p, hide)]
            if hit and not {old, new} <= extra:
                extra |= {old, new}
                changed = True
    return {p for p in extra if not _glob_hidden(p, hide)}


def _is_hidden(path: str, hide: Sequence[str], extra: Set[str]) -> bool:
    return any(p in extra or _glob_hidden(p, hide) for p in split_rename(path))


def _apply_hide(commits: List[Dict], hide: Sequence[str], extra: Set[str]) -> List[Dict]:
    """Strip hidden files; drop commits whose only changes were hidden (their message describes hidden content)."""
    kept = []
    for c in commits:
        visible = [f for f in c["files"] if not _is_hidden(f["path"], hide, extra)]
        if c["files"] and not visible:
            continue
        kept.append(dict(c, files=visible))
    return kept


def _pathspec(hide: Sequence[str], extra: Set[str]) -> List[str]:
    return ["."] + [f":(exclude){g}" for g in hide] + [f":(exclude,literal){p}" for p in sorted(extra)]


def pack_pathspec(pack: Dict) -> List[str]:
    """The pathspec that re-reads a commit with the pack's hidden paths still excluded."""
    return _pathspec(pack.get("hidden") or [], set(pack.get("hidden_renamed") or []))


def pack_hides(pack: Dict, path: str) -> bool:
    return _is_hidden(path, pack.get("hidden") or [], set(pack.get("hidden_renamed") or []))


def _docs(repo: str, sha: str, hide: Sequence[str], extra: Set[str]) -> List[Dict]:
    tracked = git(repo, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", sha).splitlines()
    docs = []
    for path in tracked:
        if _is_hidden(path, hide, extra) or not any(fnmatch.fnmatchcase(path, g) for g in DOC_GLOBS):
            continue
        docs.append({"path": path, "text": git(repo, "show", f"{sha}:{path}")[:DOC_CAP_CHARS]})
    return docs


def leak_check(pack: Dict, gold_texts: Sequence[str], n: int = 8) -> List[Dict]:
    """Word n-grams shared by the rendered pack and each gold text (case/whitespace/punctuation-insensitive)."""
    def grams(text: str) -> Set[Tuple[str, ...]]:
        words = WORD.findall(text.lower())
        return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}

    pack_grams = grams(render_pack(pack))
    hits = []
    for idx, gold in enumerate(gold_texts):
        for gram in sorted(grams(gold) & pack_grams):
            hits.append({"gold": idx, "ngram": " ".join(gram)})
    return hits


# ---- budget packing ----

def _cost(text: str) -> int:
    return estimate_tokens(text) + 1  # +1 covers the joining newline


def _split(costs: List[int], docs_cost: int, capacity: int, commits: List[Dict]) -> List[Tuple[int, int]]:
    """Consecutive [start, end) commit ranges; docs ride in the first. Balanced so every window keeps diff room."""
    total = docs_cost + sum(costs)
    if total <= capacity:
        return [(0, len(costs))]
    target = -(-total // -(-total // capacity))
    windows, start, used = [], 0, docs_cost
    for i, cost in enumerate(costs):
        if i > start and (used + cost > target or used + cost > capacity):
            windows.append((start, i))
            start, used = i, 0
        if used + cost > capacity:
            what = "docs at HEAD plus commit" if start == 0 and docs_cost else "commit"
            raise WhyLogError(f"budget too small: {what} {commits[i]['sha']} alone needs ~{used + cost} tokens "
                              f"(> {capacity} available per window)", code=2)
        used += cost
    windows.append((start, len(costs)))
    return windows


def _fill_diffs(repo: str, commits: List[Dict], room: int, spec: List[str]) -> Tuple[List[Dict], int]:
    """Highest-signal diffs first (newer first on ties); a diff that does not fit is dropped."""
    kept, dropped = [], 0
    ranked = sorted(range(len(commits)), key=lambda i: (-signal_score(commits[i]), i))
    for pos, i in enumerate(ranked):
        if room < MIN_DIFF_TOKENS:
            return kept, dropped + len(ranked) - pos
        c = commits[i]
        diff = git(repo, "show", "--format=", "--unified=2", "-M", c["sha"], "--", *spec)[:DIFF_CAP_CHARS]
        if not diff.strip():
            continue
        item = {"sha": c["sha"], "signal": signal_score(c), "diff": diff}
        cost = _cost(render_diff(item))
        if cost > room:
            dropped += 1
            continue
        kept.append(item)
        room -= cost
    return kept, dropped


def _shrink_mode(windows: int, kept: int, dropped: int) -> str:
    if windows > 1:
        return "windowed"
    return "full" if not dropped else ("diffs-trimmed" if kept else "messages-only")


def _finish(pack: Dict, budget: int) -> Dict:
    """Exact estimate on the rendered text; defensive trim of lowest-priority diffs if it overshoots."""
    while True:
        pack["estimated_tokens"] = CALL_OVERHEAD_TOKENS + estimate_tokens(render_pack(pack))
        if pack["estimated_tokens"] <= budget or not pack["diffs"]:
            return pack
        pack["diffs"].pop()
        pack["shrink"]["diffs_kept"] -= 1
        pack["shrink"]["diffs_dropped"] += 1


def _prepare(repo: str, allow_shallow: bool) -> Tuple[str, str, bool]:
    repo = require_repo(repo)
    shallow = git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    if shallow and not allow_shallow:
        raise WhyLogError("shallow clone: history is truncated (pass --allow-shallow to continue anyway)", code=2)
    return repo, git(repo, "rev-parse", "HEAD").strip(), shallow


def build_packs(repo: str, budget_tokens: int = 600000, hide: Sequence[str] = (),
                allow_shallow: bool = False) -> List[Dict]:
    """One pack per time window (newest window first), each estimated within budget_tokens."""
    repo, sha, shallow = _prepare(repo, allow_shallow)
    raw = _commits(repo, sha)
    extra = _rename_closure(raw, hide)
    commits = _apply_hide(raw, hide, extra)
    docs, spec = _docs(repo, sha, hide, extra), _pathspec(hide, extra)
    fixed = CALL_OVERHEAD_TOKENS + _cost(render_pack({"head": sha, "commits": [], "diffs": [], "docs": []}))
    costs = [_cost(render_commit(c)) for c in commits]
    docs_cost = sum(_cost(render_doc(d)) for d in docs)
    windows = _split(costs, docs_cost, budget_tokens - fixed, commits)
    packs = []
    for n, (start, end) in enumerate(windows):
        used = fixed + sum(costs[start:end]) + (docs_cost if n == 0 else 0)
        diffs, dropped = _fill_diffs(repo, commits[start:end], budget_tokens - used, spec)
        pack = {"schema": "why-log-pack/1", "head": sha, "history_truncated": shallow, "hidden": list(hide),
                "hidden_renamed": sorted(extra), "window": {"index": n + 1, "of": len(windows)},
                "commits": commits[start:end], "docs": docs if n == 0 else [], "diffs": diffs,
                "shrink": {"mode": _shrink_mode(len(windows), len(diffs), dropped),
                           "diffs_kept": len(diffs), "diffs_dropped": dropped}}
        packs.append(_finish(pack, budget_tokens))
    return packs


def build_pack(repo: str, budget_tokens: int = 600000, hide: Sequence[str] = (), allow_shallow: bool = False) -> Dict:
    """Single-window convenience wrapper; fails if the history needs a time-window split."""
    packs = build_packs(repo, budget_tokens, hide, allow_shallow)
    if len(packs) > 1:
        raise WhyLogError(f"history needs {len(packs)} time windows at budget {budget_tokens}; use build_packs", code=2)
    return packs[0]
