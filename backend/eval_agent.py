"""Local evaluation harness for the agent (v3 performance work).

Runs the agent over the 12 public sample questions (we have reference answers)
plus a few CRM-stress probes, and reports per-question correct / wrong / abstain
AND latency. Heuristic grading (substring/number match) — meant for tracking
regressions across changes, not as the official scorer.

    cd backend && set -a && source .env && set +a && uv run python eval_agent.py
"""

from __future__ import annotations

import re
import time

from agent import loop

ABSTAIN_MARKERS = [
    "not available", "no customer", "cannot", "could not", "not found",
    "no record", "not stored", "unable", "does not exist", "no information",
    "isn't available", "is not available", "n/a",
]


def _norm(s: str) -> str:
    return re.sub(r"[,\s]", "", s.lower())


# (id, question, must_include[], is_trap, is_html)
CASES = [
    ("crm-1", "How many open opportunities does Primato Supermercati S.p.A. (CUST-0132) have, and what is their total value?",
     ["4", "740000"], False, False),
    ("erp-2", "Is SKU PAS-PEN-500 (Penne Rigate n.73 - 500g box) below its minimum stock? Give the on-hand quantity.",
     ["below", "462"], False, False),
    ("calls-3", "In the last call with NordSpesa S.p.A. (CUST-0137), what was the complaint and which lot did it concern?",
     ["broken pasta", "lot-2026-0658"], False, False),
    ("kb-4", "What is the shelf life (TMC) and the declared allergens for Spaghetti n.5 - 500g box (SKU PAS-SPA-500)?",
     ["36months", "gluten", "soy", "mustard"], False, False),
    ("calls-5", "Does the complaint from that last NordSpesa S.p.A. (CUST-0137) call qualify for a return under the quality policy?",
     ["yes"], False, False),
    ("crm-6", "Total value of opportunities in the negotiation stage, grouped by customer channel (GDO / distributor / horeca).",
     ["3301000", "1931000", "3040000"], False, False),
    ("erp-7", "What is the profit margin on lot LOT-2026-0658?",
     [], True, False),
    ("crm-8", "What is the status of the order for Supermercati Bianchi?",
     [], True, False),
    ("crm-9", "Generate a 4-slide HTML deck for the sales rep visiting Primato Supermercati S.p.A. (CUST-0132): profile, open deals, order/lot status, recent call complaints.",
     [], False, True),
    ("erp-10", "Which semolina does SKU PAS-SPA-500 use (per its bill of materials), which supplier provides it, and is that raw material below minimum stock?",
     ["raw-sem-003", "molino san giorgio"], False, False),
    ("calls-11", "Across ALL recorded calls (there are 80 - page through the entire call log), count how many quality complaints concern the defect 'broken pasta'. Give the exact number.",
     ["9"], False, False),
    ("kb-12", "GranMercato S.p.A. (also written 'Gran Mercato S.p.A.') asked about the price of Fusilli n.98 (PAS-FUS-500). A call mentions one figure and the official 2026 wholesale price list mentions another. Which is the correct list price, and why?",
     ["8.07"], False, False),
    # --- CRM stress probes (new) ---
    ("crm-lc", "how many open opportunities does primato supermercati have",  # lowercase, no id/suffix
     ["4", "740000"], False, False),
    ("crm-trap2", "What is the order status for Pastifici Rossi Verdi S.p.A.?",  # invented customer
     [], True, False),
]


def classify(ans: str, must: list[str], is_trap: bool, is_html: bool) -> str:
    low = ans.lower()
    norm = _norm(ans)
    abstained = any(m in low for m in ABSTAIN_MARKERS)
    if is_trap:
        return "correct" if abstained else "wrong"
    if is_html:
        return "correct" if re.search(r"<(html|div|section|table|h[1-9]|ul|!doctype)", low) else "wrong"
    hits = sum(1 for m in must if _norm(m) in norm)
    if hits == len(must):
        return "correct"
    if abstained:
        return "abstain"
    return "wrong"


def main() -> None:
    rows = []
    lats = []
    for cid, q, must, trap, html in CASES:
        t = time.monotonic()
        try:
            r = loop.run(q)
            ans = r.get("answer", "")
            verticale = r.get("verticale", "")
        except Exception as e:  # noqa: BLE001
            ans, verticale = f"ERROR: {e}", "-"
        dt = time.monotonic() - t
        lats.append(dt)
        verdict = classify(ans, must, trap, html)
        rows.append((cid, verdict, dt, verticale, ans))
        print(f"[{verdict:7}] {cid:10} {dt:5.1f}s  vert={verticale:5}  {ans[:90]!r}")

    print("\n=== summary ===")
    from collections import Counter
    c = Counter(v for _, v, _, _, _ in rows)
    print(dict(c))
    lats_sorted = sorted(lats)
    p50 = lats_sorted[len(lats_sorted) // 2]
    p95 = lats_sorted[max(0, int(len(lats_sorted) * 0.95) - 1)]
    print(f"latency: p50={p50:.1f}s  p95={p95:.1f}s  max={max(lats):.1f}s")
    over = [cid for cid, _, dt, _, _ in rows if dt > 30]
    if over:
        print(f"OVER 30s: {over}")


if __name__ == "__main__":
    main()
