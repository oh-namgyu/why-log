"""Leak check for hidden-path tracks: 8-gram overlap between the packs and the hidden files at the pinned head.

An overlap whose 8-gram also occurs in the pack's visible commit messages or diffs is text the maintainers
wrote in both places — visible by design, not a leak. The rest (e.g. copies inside visible docs) is reported
as residual. The same check against the gold texts is reported for reference.
"""
from typing import Dict, List, Sequence, Set, Tuple

from evalcore import normalize_ws

import harvest
from common import git


def hidden_files(repo: str, head: str, pack: Dict) -> Dict[str, str]:
    """Text of every file at `head` that the pack's hide rules exclude."""
    paths = git(repo, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", head).splitlines()
    return {p: git(repo, "show", f"{head}:{p}") for p in paths if harvest.pack_hides(pack, p)}


def _grams(pack: Dict, texts: Sequence[str], part: str) -> Set[Tuple[int, str]]:
    only = {"head": pack["head"], "commits": [], "diffs": [], "docs": []}
    only[part] = pack[part]
    return {(h["gold"], h["ngram"]) for h in harvest.leak_check(only, texts)}


def split_overlaps(pack: Dict, texts: Sequence[str]) -> Dict[str, Set[Tuple[int, str]]]:
    """All overlaps of one pack with `texts`, and the subset explained by visible messages/diffs."""
    total = {(h["gold"], h["ngram"]) for h in harvest.leak_check(pack, texts)}
    explained = (_grams(pack, texts, "commits") | _grams(pack, texts, "diffs")) & total
    return {"total": total, "explained": explained, "residual": total - explained}


def gold_texts(gold: List[Dict]) -> List[str]:
    return [normalize_ws(" ".join(str(g.get(k) or "") for k in ("title", "decision", "reason"))) for g in gold]


def report(repo: str, packs: List[Dict], gold: List[Dict], examples: int = 5) -> Dict:
    head = packs[0]["head"]
    files = hidden_files(repo, head, packs[0])
    names, texts = list(files), list(files.values())
    agg = {"total": set(), "explained": set(), "residual": set()}
    gold_hits: Set[Tuple[int, str]] = set()
    for pack in packs:
        for key, val in split_overlaps(pack, texts).items():
            agg[key] |= val
        gold_hits |= {(h["gold"], h["ngram"]) for h in harvest.leak_check(pack, gold_texts(gold))}
    residual = sorted(agg["residual"])
    return {"hidden_files": names, "overlap_8grams": len(agg["total"]),
            "explained_by_visible_messages_or_diffs": len(agg["explained"]),
            "residual_leak_8grams": len(residual),
            "residual_files": sorted({names[i] for i, _ in residual}),
            "residual_examples": [{"file": names[i], "ngram": g} for i, g in residual[:examples]],
            "gold_overlap_8grams": len(gold_hits),
            "gold_overlap_ids": sorted({gold[i]["id"] for i, _ in gold_hits})}
