"""Isolated evaluation clones. The live source repository is only ever read by `git clone --no-local`.

T2 clone: the source at the pinned head, detached, with no remote and no other refs.
T3 clone: a further clone of the T2 clone whose commit messages are reduced to their subject line.
"""
import os
import subprocess
from typing import Dict, List, Optional

T3_FILTER = "head -n1"


def _git(repo: Optional[str], *args: str, env: Optional[Dict[str, str]] = None) -> str:
    cmd = ["git"] + (["-C", repo] if repo else []) + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed: {proc.stderr.strip()[-400:]}")
    return proc.stdout


def _refs(repo: str) -> List[str]:
    return [r for r in _git(repo, "for-each-ref", "--format=%(refname)").splitlines() if r]


def _only_ref(repo: str, branch: str) -> None:
    """Drop the remote and every ref except refs/heads/<branch>, so later steps cannot reach other history."""
    if "origin" in _git(repo, "remote").split():
        _git(repo, "remote", "remove", "origin")
    for ref in _refs(repo):
        if ref != f"refs/heads/{branch}":
            _git(repo, "update-ref", "-d", ref)


def clone_at(source: str, dest: str, head: str, branch: str = "eval") -> str:
    """Clone `source` (path or URL) into `dest` pinned at `head`; reuse an existing clone at the same head."""
    if os.path.isdir(os.path.join(dest, ".git")):
        if _git(dest, "rev-parse", "HEAD").strip() != head:
            raise RuntimeError(f"existing clone {dest} is not at {head}; remove it first")
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _git(None, "clone", "--quiet", "--no-local", "--no-checkout", source, dest)
    _git(dest, "checkout", "--quiet", "-B", branch, head)
    _only_ref(dest, branch)
    _git(dest, "checkout", "--quiet", "--detach", head)
    return dest


def subject_only_clone(t2_clone: str, dest: str, branch: str = "eval") -> str:
    """T3: clone the T2 clone and rewrite every commit message to its subject line. Returns the new head."""
    if not os.path.isdir(os.path.join(dest, ".git")):
        head = _git(t2_clone, "rev-parse", "HEAD").strip()
        _git(None, "clone", "--quiet", "--no-local", "--no-checkout", t2_clone, dest)
        _git(dest, "checkout", "--quiet", "-B", branch, head)
        _only_ref(dest, branch)
        env = dict(os.environ, FILTER_BRANCH_SQUELCH_WARNING="1")
        _git(dest, "filter-branch", "-f", "--msg-filter", T3_FILTER, "--", "--all", env=env)
        for ref in _refs(dest):
            if ref.startswith("refs/original/"):
                _git(dest, "update-ref", "-d", ref)
        _git(dest, "checkout", "--quiet", "--detach", branch)
    return _git(dest, "rev-parse", "HEAD").strip()


def bodies_left(repo: str) -> int:
    """Commits that still carry a message body (0 after the T3 rewrite)."""
    out = _git(repo, "log", "--format=%x1e%b")
    return sum(1 for block in out.split("\x1e")[1:] if block.strip())
