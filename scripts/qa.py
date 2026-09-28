"""Question queue (qa-queue/1): ask the maintainer about decisions history cannot explain.

State: <repo>/.why-log/questions.jsonl (one question per line) and <repo>/.why-log/decisions.json.
Lines that fail to parse or validate are moved to questions.jsonl.corrupt; every write is atomic.
"""
import datetime
import json
import os
from typing import Dict, List, Optional, Tuple

import schema
from common import WhyLogError, normalize_ws, read_json, state_path, write_json_atomic

QUEUE = "questions.jsonl"
DECISIONS = "decisions.json"
UNKNOWN = "Unknown / don't remember"
CONF = {"explicit": 1.0, "inferred": 0.5, "guess": 0.0}
MODES = ("choice", "free", "unknown")
MAX_FREE = 2000
MAX_EVIDENCE, EVIDENCE_CHARS, OPTION_CHARS = 3, 160, 300
CONFLICT_FLOOR = 0.9


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _key(title: str) -> str:
    return normalize_ws(title or "").casefold()


def _clip(text: str, limit: int) -> str:
    text = normalize_ws(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


# --- storage -------------------------------------------------------------------------------------------------

def _load(repo: str) -> List[Dict]:
    """Valid questions in file order; bad lines are appended to the .corrupt file and dropped from the queue."""
    path = state_path(repo, QUEUE)
    if not os.path.exists(path):
        return []
    rules = schema.load("qa-queue")
    good: List[Dict] = []
    bad: List[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                bad.append(line)
                continue
            if schema.validate(item, rules):
                bad.append(line)
            else:
                good.append(item)
    if bad:
        with open(path + ".corrupt", "a", encoding="utf-8") as fh:
            fh.writelines(b if b.endswith("\n") else b + "\n" for b in bad)
        _save(repo, good)
    return good


def _save(repo: str, questions: List[Dict]) -> None:
    """Write the whole queue to a temp file, fsync, then os.replace: a crash leaves the old or the new queue."""
    path = state_path(repo, QUEUE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for q in questions:
            fh.write(json.dumps(q, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _load_decisions(repo: str) -> Optional[Dict]:
    path = state_path(repo, DECISIONS)
    return read_json(path) if os.path.exists(path) else None


# --- building questions ------------------------------------------------------------------------------------------

def _needs_question(d: Dict) -> bool:
    return d.get("confidence") in ("guess", "inferred") or d.get("conflict") is not None


def _priority(d: Dict) -> float:
    impact = d.get("impact") if d.get("impact") in (1, 2, 3) else 2
    value = impact / 3 * (1 - CONF.get(d.get("confidence"), 0.0))
    if d.get("conflict") is not None:
        value = max(value, CONFLICT_FLOOR)
    return round(value, 4)


def _ref(ev: Dict) -> str:
    ref = str(ev.get("ref", ""))
    if ev.get("kind") == "commit":
        return ref[:7]
    if ev.get("kind") == "file" and "@" in ref:
        path, sha = ref.rsplit("@", 1)
        return f"{path}@{sha[:7]}"
    return ref


def _evidence(d: Dict) -> List[str]:
    """Only quotes that passed the existence gate; never text the model wrote itself."""
    quotes = [ev for ev in d.get("evidence") or [] if ev.get("exists") is True and ev.get("quote")]
    return [_clip(f"{_ref(ev)} {ev['quote']}", EVIDENCE_CHARS) for ev in quotes[:MAX_EVIDENCE]]


def _options(d: Dict) -> List[str]:
    """Inferred: the quote-grounded hypothesis + Unknown. Guess/conflict: Unknown only (no anchoring)."""
    reason = normalize_ws(str(d.get("reason") or ""))
    if d.get("confidence") == "inferred" and d.get("conflict") is None and reason:
        return [_clip(reason, OPTION_CHARS), UNKNOWN]
    return [UNKNOWN]


def _question_text(d: Dict) -> str:
    text = normalize_ws(str(d.get("question") or ""))
    return _clip(text or f"Why was this chosen: {normalize_ws(d.get('title', ''))}?", 1000)


def _refresh(q: Dict, d: Dict, run_id: str) -> None:
    q.update(subject=d["id"], title=_clip(d.get("title", ""), 300), question=_question_text(d),
             evidence=_evidence(d), options=_options(d), priority=_priority(d), run_id=run_id)


def _next_id(questions: List[Dict]) -> str:
    nums = [int(q["id"][1:]) for q in questions if q["id"][1:].isdigit()]
    return f"Q{(max(nums) if nums else 0) + 1:03d}"


def _find(decisions: List[Dict], q: Dict) -> Optional[Dict]:
    """Titles survive renumbering across runs, so match by title first and fall back to the decision id."""
    key = _key(q.get("title") or "")
    by_title = [d for d in decisions if key and _key(d.get("title", "")) == key]
    return (by_title or [d for d in decisions if d.get("id") == q["subject"]] or [None])[0]


# --- applying answers ------------------------------------------------------------------------------------------

def _apply(d: Dict, ans: Dict) -> bool:
    """Write testimony into a decision. Code evidence is never overwritten: disagreement goes to `conflict`."""
    text, mode = ans["text"], ans["mode"]
    d["testimony"] = {"answer": text, "mode": mode, "at": ans["at"]}
    d["provenance"] = "unknown" if mode == "unknown" else "testimony"
    if mode == "unknown":
        return d.get("conflict") is not None
    reason = normalize_ws(str(d.get("reason") or ""))
    if d.get("confidence") == "explicit" or d.get("conflict") is not None:
        if d.get("conflict") is not None or _key(text) != _key(reason):
            note = f'testimony says "{text}"; history says "{reason}"'
            d["conflict"] = f"{d['conflict']} | {note}" if d.get("conflict") else note
        return d.get("conflict") is not None
    if _key(text) != _key(reason):
        d.setdefault("model_reason", d.get("reason", ""))
        d["reason"] = text
    return False


def _summary(d: Dict) -> Dict:
    keys = ("id", "title", "confidence", "provenance", "reason", "conflict", "testimony")
    return {k: d.get(k) for k in keys}


def _reapply(repo: str, questions: List[Dict]) -> None:
    """A new run rewrites decisions.json; carry earlier answers over so testimony is never lost."""
    data = _load_decisions(repo)
    if not data:
        return
    changed = False
    for q in (item for item in questions if item["status"] == "answered" and item["answer"]):
        d = _find(data["decisions"], q)
        ans = q["answer"]
        if d is not None and d.get("testimony") != {"answer": ans["text"], "mode": ans["mode"], "at": ans["at"]}:
            _apply(d, q["answer"])
            changed = True
    if changed:
        write_json_atomic(state_path(repo, DECISIONS), data)


def _check(q: Dict, mode: str, text: Optional[str]) -> Tuple[str, str]:
    if mode not in MODES:
        raise WhyLogError(f"mode must be one of {', '.join(MODES)}", code=2)
    if mode == "choice":
        pick = normalize_ws(text or "")
        if pick not in q["options"]:
            raise WhyLogError(f"--choice must be one of the options of {q['id']}: {q['options']}", code=2)
        return ("unknown", "") if pick == UNKNOWN else ("choice", pick)
    if mode == "free":
        free = (text or "").strip()
        if not free or len(free) > MAX_FREE:
            raise WhyLogError(f"--free needs 1..{MAX_FREE} characters of text", code=2)
        return "free", free
    if text:
        raise WhyLogError("--unknown takes no text", code=2)
    return "unknown", ""


# --- public interface (fixed by whylog.py) -------------------------------------------------------------------

def enqueue(repo: str, decisions: List[Dict], run_id: str) -> int:
    """Queue questions for unresolved decisions; idempotent across runs. Returns how many were added."""
    questions = _load(repo)
    by_key = {_key(q["title"]): q for q in questions if q.get("title")}
    seen = set()
    added = 0
    for d in decisions:
        if not _needs_question(d):
            continue
        key = _key(d.get("title", ""))
        seen.add(key)
        q = by_key.get(key)
        if q is None:
            q = {"schema": "qa-queue/1", "id": _next_id(questions), "source": "why-log", "status": "open",
                 "answer": None, "created_at": _now()}
            questions.append(q)
            by_key[key] = q
            added += 1
        if q["status"] == "open":
            _refresh(q, d, run_id)
        else:
            q["subject"] = d["id"]  # answered/skipped questions keep their content; follow renumbering only
    for q in questions:  # an open question whose decision is gone or now resolved is moot
        if q["status"] == "open" and _key(q.get("title") or "") not in seen:
            q["status"] = "skipped"
    _save(repo, questions)
    _reapply(repo, questions)
    return added


def next_questions(repo: str, n: int) -> List[Dict]:
    """Open questions, highest priority first (ties: queue id order)."""
    open_q = [q for q in _load(repo) if q["status"] == "open"]
    open_q.sort(key=lambda q: (-q["priority"], len(q["id"]), q["id"]))
    return open_q[:max(n, 0)]


def answer(repo: str, qid: str, mode: str, text: Optional[str], by: str = "user") -> Dict:
    """Record an answer (mode choice|free|unknown); update decisions.json testimony; report conflicts."""
    if by not in ("user", "mock"):
        raise WhyLogError(f"invalid answer source: {by}", code=2)
    questions = _load(repo)
    q = next((item for item in questions if item["id"] == qid), None)
    if q is None:
        raise WhyLogError(f"unknown question id: {qid}", code=2)
    mode, text = _check(q, mode, text)
    q["status"] = "answered"
    q["answer"] = {"text": text, "mode": mode, "by": by, "at": _now()}
    _save(repo, questions)  # the queue is the record of answers; decisions.json is re-derivable from it
    data = _load_decisions(repo)
    d = _find(data["decisions"], q) if data else None
    conflict = _apply(d, q["answer"]) if d is not None else False
    if d is not None:
        write_json_atomic(state_path(repo, DECISIONS), data)
    return {"question": q, "decision": _summary(d) if d is not None else None, "conflict": conflict}


def _provenance(d: Dict) -> str:
    return d.get("provenance") or ("code" if d.get("confidence") in ("explicit", "inferred") else "unknown")


def status(repo: str) -> Dict:
    """Counts of decisions by provenance/confidence and questions by status."""
    data = _load_decisions(repo) or {}
    decisions = data.get("decisions") or []
    conf = {k: 0 for k in CONF}
    prov = {k: 0 for k in ("code", "testimony", "unknown")}
    for d in decisions:
        conf[d.get("confidence")] = conf.get(d.get("confidence"), 0) + 1
        prov[_provenance(d)] = prov.get(_provenance(d), 0) + 1
    questions = _load(repo)
    by_status = {k: sum(1 for q in questions if q["status"] == k) for k in ("open", "answered", "skipped")}
    corrupt = state_path(repo, QUEUE + ".corrupt")
    quarantined = 0
    if os.path.exists(corrupt):
        with open(corrupt, encoding="utf-8") as fh:
            quarantined = sum(1 for _ in fh)
    return {"run_id": data.get("run_id"), "head": data.get("head"),
            "decisions": {"total": len(decisions), "confidence": conf, "provenance": prov},
            "questions": {"total": len(questions), **by_status}, "quarantined_lines": quarantined}
