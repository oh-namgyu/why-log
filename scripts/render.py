"""Write the ADRs built by adr_text.py transactionally, and undo a render.

render_current stages every file
under .why-log/run/<id>/staging/, writes COMPLETE, backs up what it will change, records manifest.json,
then applies file by file with os.replace. undo() reverses one run from its manifest.
"""
import datetime
import hashlib
import os
import re
import shutil
from typing import Dict, List, Optional, Tuple

from adr_text import MARKER, build_files
from common import WhyLogError, read_json, state_path, write_json_atomic

COMPLETE = "COMPLETE"


def _write_all(out_dir: str, files: Dict[str, str]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for name, body in sorted(files.items()):
        with open(os.path.join(out_dir, name), "wb") as fh:
            fh.write(body.encode("utf-8"))


def render(decisions: List[Dict], head: str, out_dir: str) -> List[str]:
    """Plain (non-transactional) write of every generated file; returns the ADR file names."""
    files = build_files(decisions, head)
    _write_all(out_dir, files)
    return sorted(n for n in files if n not in ("README.md", "unknowns.md"))

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: str) -> Optional[bytes]:
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def _generated(path: str) -> bool:
    """A file we may overwrite or delete: a regular file whose first line is the marker."""
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    with open(path, "rb") as fh:
        return fh.readline().decode("utf-8", "replace").rstrip("\r\n") == MARKER


def _target(repo: str, out: Optional[str]) -> Tuple[str, str]:
    """(absolute target dir, repo-relative form). `out` must stay inside the repo."""
    if out is None:
        return state_path(repo, "adr"), os.path.join(".why-log", "adr")
    parts = re.split(r"[\\/]+", out)
    if not out.strip() or os.path.isabs(out) or out.startswith(("/", "\\")) or ".." in parts:
        raise WhyLogError(f"--out must be a repo-relative path without '..': {out!r}", code=2)
    rel = os.path.normpath(out)
    full = os.path.join(repo, rel)
    real_repo = os.path.realpath(repo)
    if os.path.commonpath([real_repo, os.path.realpath(full)]) != real_repo:
        raise WhyLogError(f"--out resolves outside the repository: {out!r}", code=2)
    return full, rel


def plan(target: str, files: Dict[str, str]) -> Dict[str, List[str]]:
    """added / modified / deleted names; refuses (code 2) when a user-authored file would be overwritten."""
    if os.path.lexists(target) and not os.path.isdir(target):
        raise WhyLogError(f"render target is not a directory: {target}", code=2)
    existing = sorted(n for n in os.listdir(target) if os.path.isfile(os.path.join(target, n))) \
        if os.path.isdir(target) else []
    added, modified, blocked = [], [], []
    for name in sorted(files):
        path = os.path.join(target, name)
        current = _read(path)
        if current is None and not os.path.lexists(path):
            added.append(name)
        elif current != files[name].encode("utf-8"):
            (modified if _generated(path) else blocked).append(name)
    if blocked:
        raise WhyLogError(f"refusing to overwrite files without the why-log marker in {target}: "
                          f"{', '.join(blocked)}", code=2)
    deleted = [n for n in existing if n not in files and _generated(os.path.join(target, n))]
    return {"added": added, "modified": modified, "deleted": deleted}


def _stage(run_dir: str, files: Dict[str, str]) -> str:
    """Staging with COMPLETE is reused only if it holds exactly these bytes; anything else is rebuilt."""
    staging = os.path.join(run_dir, "staging")
    done = os.path.exists(os.path.join(staging, COMPLETE))
    if done and sorted(os.listdir(staging)) == sorted([*files, COMPLETE]) and all(
            _read(os.path.join(staging, n)) == body.encode("utf-8") for n, body in files.items()):
        return staging
    shutil.rmtree(staging, ignore_errors=True)
    _write_all(staging, files)
    with open(os.path.join(staging, COMPLETE), "w", encoding="utf-8") as fh:
        fh.write("ok\n")
    return staging


def _backup(run_dir: str, target: str, rel: str, todo: Dict[str, List[str]], files: Dict[str, str]) -> Dict:
    backup = os.path.join(run_dir, "backup")
    shutil.rmtree(backup, ignore_errors=True)
    os.makedirs(backup)
    before = {}
    for name in todo["modified"] + todo["deleted"]:
        shutil.copyfile(os.path.join(target, name), os.path.join(backup, name))
        before[name] = _sha(_read(os.path.join(backup, name)) or b"")
    written = {n: _sha(files[n].encode("utf-8")) for n in todo["added"] + todo["modified"]}
    manifest = {"target": rel, **todo, "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "written_sha256": written, "before_sha256": before, "applied": False}
    write_json_atomic(os.path.join(run_dir, "manifest.json"), manifest)
    return manifest


def _apply(repo: str, run_dir: str, manifest: Dict) -> None:
    """Idempotent: a staged file already moved into place is skipped, so a crashed apply resumes cleanly."""
    target = os.path.join(repo, manifest["target"])
    os.makedirs(target, exist_ok=True)
    for name in manifest["added"] + manifest["modified"]:
        src = os.path.join(run_dir, "staging", name)
        if os.path.exists(src):
            shutil.copyfile(src, src + ".apply")
            os.replace(src + ".apply", os.path.join(target, name))
    for name in manifest["deleted"]:
        if os.path.lexists(os.path.join(target, name)):
            os.remove(os.path.join(target, name))
    manifest["applied"] = True
    write_json_atomic(os.path.join(run_dir, "manifest.json"), manifest)


def _run_dir(repo: str, run_id: Optional[str]) -> Tuple[str, str]:
    """A run whose render already applied is never reused: its manifest is what undo needs."""
    base = _check_id(run_id) if run_id else datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for n in range(1, 1000):
        rid = base if n == 1 else f"{base}-r{n}"
        path = os.path.join(state_path(repo, "run", rid), "manifest.json")
        if not os.path.exists(path) or not read_json(path).get("applied"):
            return rid, state_path(repo, "run", rid)
    raise WhyLogError(f"too many renders under run id {base}", code=2)


def _load(repo: str) -> Dict:
    path = state_path(repo, "decisions.json")
    if not os.path.exists(path):
        raise WhyLogError("no .why-log/decisions.json — run `why-log run` first", code=2)
    data = read_json(path)
    if data.get("schema") != "why-log-decisions/1" or not isinstance(data.get("decisions"), list):
        raise WhyLogError(f"unsupported decisions file: {path}", code=2)
    return data


def _check_id(run_id: str) -> str:
    if not run_id or run_id in (".", "..") or os.path.basename(run_id) != run_id or "\\" in run_id:
        raise WhyLogError(f"invalid run id: {run_id!r}", code=2)
    return run_id


def _resume(repo: str, run_id: Optional[str]) -> None:
    """Finish a render of this run that crashed after its manifest was written."""
    path = state_path(repo, "run", _check_id(run_id), "manifest.json") if run_id else ""
    if path and os.path.exists(path) and not read_json(path).get("applied"):
        _apply(repo, os.path.dirname(path), read_json(path))


def render_current(repo: str, run_id: Optional[str] = None, out: Optional[str] = None, preview: bool = False) -> Dict:
    """Render <repo>/.why-log/decisions.json transactionally into .why-log/adr or the repo-relative `out`."""
    data = _load(repo)
    target, rel = _target(repo, out)
    files = build_files(data["decisions"], str(data.get("head") or ""))
    if not preview:
        _resume(repo, run_id)
    todo = plan(target, files)
    result = {"target": rel, "files": sorted(files), **todo, "run_id": None}
    if preview or not any(todo.values()):
        return result
    rid, run_dir = _run_dir(repo, run_id)
    _stage(run_dir, files)
    _apply(repo, run_dir, _backup(run_dir, target, rel, todo, files))
    return dict(result, run_id=rid)


def _undo_checks(target: str, backup: str, m: Dict) -> List[str]:
    changed = []
    for name in m["added"] + m["modified"] + m["deleted"]:
        cur = _read(os.path.join(target, name))
        ok = {m["written_sha256"].get(name), m["before_sha256"].get(name)}
        if cur is None:
            fine = name not in m["modified"]
        else:
            fine = _sha(cur) in ok and not os.path.islink(os.path.join(target, name))
        if not fine or (name in m["deleted"] + m["modified"] and not os.path.exists(os.path.join(backup, name))):
            changed.append(name)
    return changed


def undo(repo: str, run_id: str) -> Dict:
    """Restore the files a render changed, from its manifest; refuses if any was edited since."""
    path = state_path(repo, "run", _check_id(run_id), "manifest.json")
    if not os.path.exists(path):
        raise WhyLogError(f"no render manifest for run {run_id!r}", code=2)
    m = read_json(path)
    target, backup = os.path.join(repo, m["target"]), os.path.join(os.path.dirname(path), "backup")
    changed = _undo_checks(target, backup, m)
    if changed:
        raise WhyLogError(f"files changed since render {run_id}; not undoing: {', '.join(changed)}", code=2)
    removed, restored = [], []
    for name in m["added"]:
        if os.path.exists(os.path.join(target, name)):
            os.remove(os.path.join(target, name))
            removed.append(name)
    for name in m["modified"] + m["deleted"]:
        dest = os.path.join(target, name)
        if _read(dest) != _read(os.path.join(backup, name)):
            os.makedirs(target, exist_ok=True)
            shutil.copyfile(os.path.join(backup, name), dest + ".undo")
            os.replace(dest + ".undo", dest)
            restored.append(name)
    write_json_atomic(path, dict(m, undone=True))
    return {"run_id": run_id, "target": m["target"], "removed": sorted(removed), "restored": sorted(restored)}
