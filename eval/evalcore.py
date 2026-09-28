"""Shared evaluation plumbing: scripts/ on sys.path, a call ledger around llm.call, JSON helpers.

Every model call made by the evaluation goes through `ledger_call`, which uses llm.call (the pipeline's only,
tool-less and config-isolated model path) and appends usage/cost/seconds to a JSONL ledger.
"""
import json
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import llm  # noqa: E402
from common import extract_json, normalize_ws, read_json, write_json_atomic  # noqa: E402,F401

Caller = Callable[..., Dict[str, Any]]
_LOCK = threading.Lock()


def ledger_call(ledger: str, purpose: str) -> Caller:
    """A llm.call-compatible callable that records every call in `ledger` (one JSON object per line)."""
    def call(prompt: str, model: Optional[str] = None, **kw: Any) -> Dict[str, Any]:
        reply = llm.call(prompt, model=model, **kw)
        append_jsonl(ledger, {"purpose": purpose, "usage": reply.get("usage"), "cost_usd": reply.get("cost_usd"),
                              "seconds": reply.get("seconds"), "model": reply.get("model"),
                              "prompt_chars": len(prompt)})
        return reply
    return call


def append_jsonl(path: str, item: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _LOCK, open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_or(path: str, default: Any = None) -> Any:
    return read_json(path) if os.path.exists(path) else default


def ask_json(call: Caller, prompt: str, model: Optional[str], retries: int = 1) -> Any:
    """One call; on unparseable output retry once with the same prompt. Returns parsed JSON or None."""
    for _ in range(retries + 1):
        parsed = extract_json(call(prompt, model=model).get("text") or "")
        if parsed is not None:
            return parsed
    return None


def contains_quote(quote: str, text: str, min_chars: int = 10) -> bool:
    """Whitespace-insensitive verbatim containment with a minimum quote length."""
    q = normalize_ws(quote or "")
    return len(q) >= min_chars and q in normalize_ws(text or "")
