# Decision: vector-DB RAG — considered, NOT adopted

**Status: rejected (2026-06).** A vector DB / embeddings retriever was evaluated for
the KB RAG and deliberately **not** shipped. This file records why, so it isn't
re-litigated.

## Why not

The current RAG (`backend/agent/kb.py`) is whole-document **BM25 + a hard variant
filter** (SKU / Bio / 250g / 500g). It already scores the **KB vertical at ceiling**
(4/4, 40 pts in eval run #138; 14/14 locally). A vector DB cannot improve an
already-maxed vertical — it can only change retrieval on queries the evaluator
doesn't actually ask.

A paraphrase spike settled it. On 6 deliberately keyword-poor paraphrases
(target doc known), **hit@3 was BM25 3/6 vs Vector 4/6** — vectors are *not*
clearly better, and they failed in exactly the predicted way: for "substances that
cause a reaction" and "smashed noodles" the vector retriever surfaced near-identical
**spec sheets** (DOC-003/006) instead of the correct policy doc — the very
near-duplicate confusion the hard filter exists to prevent.

Two conclusions:
1. **Evaluator questions are keyword-rich** (product names, SKUs, domain terms like
   "shelf life", "returns policy", "price of Fusilli n.98 PAS-FUS-500"), so BM25 +
   the hard filter already nails them. Embeddings add nothing there.
2. **Vector-alone regresses** the near-duplicate cases. Only a *hybrid* would help,
   and only on paraphrases the evaluator doesn't pose.

For a scored challenge, this is over-engineering with downside risk: cost (an
embedding dependency + the offline `.npz` to keep in sync), latency (an extra
per-query embedding call inside the 26s budget), and a real regression path — for
zero evaluator-facing gain. So: **keep lexical BM25 + the hard filter.**

## Revisit only if

- the corpus grows large and the queries become genuinely paraphrase-heavy /
  semantic (not code/term lookups), **and**
- a spike shows BM25-top-3 *missing* the right doc on representative real questions.

## If revisited, the only sane shape (notes)

Keep it simple and hybrid — never vector-alone:
- In-process numpy matrix (`35 × 4096`) + cosine; **no separate vector service.**
- Regolo `Qwen3-Embedding-8B` (verified: 4096-dim) via the existing client/key;
  embeddings precomputed offline and committed (`kb_embeddings.npz`), aligned **by
  doc_id**, with a corpus-hash guard; embed only the query at request time.
- Keep BM25 + the hard variant filter; only **cosine-rerank when the candidate set
  is ≥4** (min-max fusion over 1–3 candidates is degenerate). Immediate BM25
  fallback on any embedding error/timeout; budget-aware embed timeout (~3s, no retry)
  so it can't eat the 26s wall. No `RAG_MODE` flag — "no npz / model mismatch" is the
  off switch. numpy already ships (transitive via rank-bm25).
