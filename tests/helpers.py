"""Shared test setup: puts scripts/ and fixtures/ on sys.path and builds throwaway git repos.

Every test module imports this first. Git runs with the user's global/system config disabled so hooks,
signing or templates on the developer machine cannot affect the repos built here.
"""
import os
import subprocess
import sys
from typing import Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
for sub in (os.path.join(HERE, "..", "scripts"), os.path.join(HERE, "fixtures")):
    if os.path.abspath(sub) not in sys.path:
        sys.path.insert(0, os.path.abspath(sub))

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"


def sh(repo: str, *args: str, date: Optional[str] = None) -> str:
    env = dict(os.environ)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env, check=True)
    return proc.stdout.strip()


def init_repo(repo: str) -> str:
    os.makedirs(repo, exist_ok=True)
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "config", "user.name", "test")
    sh(repo, "config", "user.email", "test@example.invalid")
    return repo


def commit(repo: str, msg: str, files: Dict[str, Optional[str]], day: int = 1) -> str:
    """Write (or delete, when the body is None) files, commit them with a fixed date, return the short SHA."""
    for path, body in files.items():
        full = os.path.join(repo, path)
        if body is None:
            os.remove(full)
            continue
        os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(body)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "--allow-empty", "-m", msg, date=f"2026-01-{day:02d}T10:00:00")
    return sh(repo, "rev-parse", "--short=12", "HEAD")


def _no_model(*_args, **_kwargs):
    raise AssertionError("tests must never call the model")


if os.environ.get("WHYLOG_LIVE") != "1":  # live model tests opt in explicitly
    import llm  # noqa: E402  (needs the sys.path entry above)

    llm.call = _no_model
