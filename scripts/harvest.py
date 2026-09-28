"""Build a history pack: every commit message + stat, plus diffs of high-signal commits, within a token budget."""
import fnmatch
import re
from typing import Dict, List, Sequence

from common import WhyLogError, estimate_tokens, git, require_repo

MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile",
             "pom.xml", "build.gradle", "Dockerfile", "docker-compose.yml", "setup.py", "setup.cfg")
CONFIG_EXT = (".yml", ".yaml", ".toml", ".ini", ".cfg", ".env.example")
WHY_WORDS = re.compile(r"because|instead|so that|in order to|rather than|replace|switch|migrat|drop |revert|"
                       r"왜|대신|때문|이유|기각|교체|전환|폐기", re.IGNORECASE)
DOC_GLOBS = ("README*", "CHANGELOG*", "docs/*.md", "docs/**/*.md", "doc/**/*.md", "ARCHITECTURE*", "DESIGN*")
DIFF_CAP_CHARS = 12000
REC, FLD = "\x1e", "\x1f"


def _commits(repo: str, sha: str) -> List[Dict]:
    raw = git(repo, "log", "--format=" + REC + "%H" + FLD + "%ad" + FLD + "%s" + FLD + "%b" + FLD,
              "--date=short", "--numstat", "-M", sha)
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


def _hidden(path: str, hide: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) or path.startswith(g.rstrip("/") + "/") for g in hide)


def _docs(repo: str, sha: str, hide: Sequence[str]) -> List[Dict]:
    tracked = git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
    docs = []
    for path in tracked:
        if _hidden(path, hide) or not any(fnmatch.fnmatch(path, g) for g in DOC_GLOBS):
            continue
        docs.append({"path": path, "text": git(repo, "show", f"{sha}:{path}")[:20000]})
    return docs


def build_pack(repo: str, budget_tokens: int = 700000, hide: Sequence[str] = (), allow_shallow: bool = False) -> Dict:
    repo = require_repo(repo)
    shallow = git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    if shallow and not allow_shallow:
        raise WhyLogError("shallow clone: history is truncated (pass --allow-shallow to continue anyway)", code=2)
    sha = git(repo, "rev-parse", "HEAD").strip()
    commits = _commits(repo, sha)
    for c in commits:
        c["files"] = [f for f in c["files"] if not _hidden(f["path"], hide)]
    commits = [c for c in commits if c["files"] or not hide]
    pack = {"schema": "why-log-pack/1", "head": sha, "history_truncated": shallow, "hidden": list(hide),
            "commits": commits, "docs": _docs(repo, sha, hide), "diffs": []}
    used = estimate_tokens(str(pack))
    if used > budget_tokens:
        raise WhyLogError(f"history alone needs ~{used} tokens (> budget {budget_tokens}); time-window split not implemented yet", code=2)
    # Highest-signal diffs first, then every other diff while the budget lasts: reasons often hide in code comments.
    for c in sorted(commits, key=signal_score, reverse=True):
        diff = git(repo, "show", "--format=", "--unified=2", "-M", c["sha"], "--",
                   *[f":(exclude){g}" for g in hide] or ["."])[:DIFF_CAP_CHARS]
        cost = estimate_tokens(diff)
        if used + cost > budget_tokens:
            continue
        pack["diffs"].append({"sha": c["sha"], "signal": signal_score(c), "diff": diff})
        used += cost
    pack["estimated_tokens"] = used
    return pack
