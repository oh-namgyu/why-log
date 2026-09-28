"""Question queue (qa-queue/1) — STUB: interface fixed by the CLI, implemented in stage 3.

State: <repo>/.why-log/questions.jsonl (one question per line) and <repo>/.why-log/decisions.json.
"""
from typing import Dict, List, Optional


def enqueue(repo: str, decisions: List[Dict], run_id: str) -> int:
    """Queue questions for unresolved decisions; idempotent across runs. Returns how many were added."""
    raise NotImplementedError


def next_questions(repo: str, n: int) -> List[Dict]:
    """Open questions, highest priority first."""
    raise NotImplementedError


def answer(repo: str, qid: str, mode: str, text: Optional[str]) -> Dict:
    """Record an answer (mode choice|free|unknown); update decisions.json testimony; report conflicts."""
    raise NotImplementedError


def status(repo: str) -> Dict:
    """Counts of decisions by provenance/confidence and questions by status."""
    raise NotImplementedError
