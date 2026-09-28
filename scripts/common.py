"""Shared helpers: git access pinned to one commit, JSON I/O, token estimates."""
import json
import os
import subprocess
from typing import Any, List, Optional

STATE_DIR = ".why-log"


class WhyLogError(RuntimeError):
    """A failure the CLI reports with a specific exit code."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def git(repo: str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False)
    if check and proc.returncode != 0:
        raise WhyLogError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "replace")


def git_ok(repo: str, *args: str) -> bool:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False).returncode == 0


def require_repo(repo: str) -> str:
    if not git_ok(repo, "rev-parse", "--is-inside-work-tree"):
        raise WhyLogError(f"not a git repository: {repo}", code=2)
    return git(repo, "rev-parse", "--show-toplevel").strip()


CALL_OVERHEAD_TOKENS = 3000


def estimate_tokens(text: str) -> int:
    """Deliberately high: ASCII 2.2 chars/token (diffs are symbol-dense), other scripts 1.5 tokens/char.

    Fitted on two measured calls (English fixture, Korean-heavy repo) so both land at or above actual usage.
    """
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return int(ascii_chars / 2.2 + (len(text) - ascii_chars) * 1.5)


def normalize_ws(text: str) -> str:
    return " ".join(text.split())


def state_path(repo: str, *parts: str) -> str:
    return os.path.join(repo, STATE_DIR, *parts)


def exclude_state_dir(repo: str) -> None:
    """Keep .why-log/ out of commits without touching the tracked .gitignore."""
    info = os.path.join(git(repo, "rev-parse", "--git-dir").strip(), "info")
    info = info if os.path.isabs(info) else os.path.join(repo, info)
    os.makedirs(info, exist_ok=True)
    exclude = os.path.join(info, "exclude")
    lines = open(exclude, encoding="utf-8").read().splitlines() if os.path.exists(exclude) else []
    if f"{STATE_DIR}/" not in lines:
        with open(exclude, "a", encoding="utf-8") as fh:
            fh.write(f"{STATE_DIR}/\n")


def write_json_atomic(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON array/object out of a model reply (fences and prose tolerated)."""
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def chunks(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]
