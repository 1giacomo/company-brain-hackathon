"""Knowledge-base retrieval over data/kb/.

Whole-document BM25 (the docs are small and near-identical — chunking hurts).
The #1 risk is near-duplicate spec sheets: base 500g vs Bio vs 250g variants are
textually almost identical, so distinguishing tokens (SKU / 'Bio' / format) act
as a HARD FILTER on the candidate set, not a soft ranking boost (PLAN.md §5).
"""

from __future__ import annotations

import re
from pathlib import Path

from rank_bm25 import BM25Okapi

_KB_DIR = Path(__file__).resolve().parent.parent / "data" / "kb"

_SKU_RE = re.compile(r"\b(?:PAS|RAW)-[A-Z]{2,4}-\d{2,3}\b", re.I)
_DOC_RE = re.compile(r"\bDOC-\d{3}\b", re.I)
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.I)


class _Doc:
    __slots__ = ("doc_id", "title", "text", "tokens", "skus", "is_bio", "fmt")

    def __init__(self, path: Path):
        self.text = path.read_text(encoding="utf-8")
        first = self.text.splitlines()[0] if self.text else ""
        self.title = first.lstrip("# ").strip()
        m = re.search(r"\*\*Document ID:\*\*\s*(DOC-\d{3})", self.text)
        self.doc_id = m.group(1) if m else path.stem
        self.tokens = [t.lower() for t in _TOKEN_RE.findall(self.text)]
        self.skus = {s.upper() for s in _SKU_RE.findall(self.text)}
        head = (self.title + " " + self.text[:400]).lower()
        self.is_bio = "bio" in head
        self.fmt = "250g" if "250g" in head else ("500g" if "500g" in head else None)


_DOCS: list[_Doc] = []
_BM25: BM25Okapi | None = None


def _load() -> None:
    global _DOCS, _BM25
    if _DOCS:
        return
    _DOCS = [_Doc(p) for p in sorted(_KB_DIR.glob("DOC-*.md"))]
    _BM25 = BM25Okapi([d.tokens for d in _DOCS])


def _hard_filter(query: str, docs: list[_Doc]) -> list[_Doc]:
    """Restrict candidates by distinguishing tokens present in the query. Only
    narrows when a discriminator is actually mentioned; otherwise returns all."""
    q = query.lower()
    skus = {s.upper() for s in _SKU_RE.findall(query)}
    doc_ids = {d.upper() for d in _DOC_RE.findall(query)}

    cand = docs
    if doc_ids:
        exact = [d for d in cand if d.doc_id.upper() in doc_ids]
        if exact:
            return exact
    if skus:
        exact = [d for d in cand if d.skus & skus]
        if exact:
            cand = exact  # SKU pins the product family; keep narrowing by format below
    # Format / variant discriminators.
    wants_bio = "bio" in q
    wants_250 = "250g" in q or "250 g" in q
    wants_500 = "500g" in q or "500 g" in q
    if wants_bio:
        cand = [d for d in cand if d.is_bio] or cand
    else:
        # Only exclude Bio when the query clearly is not about Bio AND we have non-Bio options.
        non_bio = [d for d in cand if not d.is_bio]
        if non_bio:
            cand = non_bio
    if wants_250:
        cand = [d for d in cand if d.fmt == "250g"] or cand
    elif wants_500:
        cand = [d for d in cand if d.fmt != "250g"] or cand
    return cand


def search(query: str, k: int = 3) -> list[dict[str, str]]:
    """Return up to k whole documents most relevant to the query, after applying
    the hard variant filter. Each result: {doc_id, title, text}."""
    _load()
    assert _BM25 is not None
    candidates = _hard_filter(query, _DOCS)
    q_tokens = [t.lower() for t in _TOKEN_RE.findall(query)]
    scores = _BM25.get_scores(q_tokens)
    idx_by_doc = {id(d): i for i, d in enumerate(_DOCS)}
    ranked = sorted(candidates, key=lambda d: scores[idx_by_doc[id(d)]], reverse=True)
    return [{"doc_id": d.doc_id, "title": d.title, "text": d.text} for d in ranked[:k]]


def warmup() -> None:
    """Build the index ahead of the first request (called at startup)."""
    _load()
