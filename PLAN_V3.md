# Plan — Agent v3.0: performance & accuracy

Goal: raise the evaluation score by cutting **wrong answers** (they score
negative) and eliminating **>30s timeouts**, without regressing the strong ERP/KB
verticals or the good API efficiency.

## Diagnosis (eval run #52: 9 correct / 0 partial / 2 no-answer / 5 wrong)

| Vertical | c/p/n/w | Read |
|---|---|---|
| erp | 3/0/0/1 | strong |
| kb  | 3/0/0/1 | strong |
| crm | 1/0/2/1 | **weak** — retrieval/routing fails (2 no-answer) + 1 hallucination |
| calls | 2/0/0/2 | **negative** — 2 wrong (grounding/traps) |

Scoring is asymmetric: **wrong = negative, no-answer = 0, correct = positive.** So the
highest-value move is turning wrongs into corrects — and, failing that, into honest
abstentions (still beats wrong). Three root causes, grounded in the current code:

1. **Latency p95 > 30s → timeouts.** `loop.run()` checks `DEADLINE_S=20` only *between*
   rounds, but `llm.chat()` uses a **25s** client timeout with **2 retries + backoff**
   (`llm.py`). One stalled call → 25–50s. And after the deadline breaks, the loop fires
   *another* full `llm.chat` (the "forced final"), adding up to 25s more. Timeouts likely
   cause the CRM no-answers and some wrongs.
2. **Hallucination / traps → wrong.** The honesty rules are prompt-only; the model
   (qwen3.5-122b) still invents facts or answers trap questions instead of abstaining.
3. **CRM retrieval is brittle.** `aldente.search_customers` relies on the API's
   exact-match, case-sensitive `search`; `_name_variants` covers only spacing/legal-suffix
   cases. A miss → the agent abstains (no-answer) or guesses (wrong).

## Workstream A — Latency: guarantee a response < 30s  *(do first)*

The timeout risk is existential (a timeout scores as wrong). Make the whole request
fit a hard budget.

- **Global wall-clock budget** in `loop.run()`: `BUDGET_S ≈ 26`. Track `elapsed`; before
  every `llm.chat`, compute `remaining` and pass a per-call `timeout = min(cap, remaining - reserve)`.
  If `remaining < ~4s`, stop looping and finalize from what's gathered.
- **Lower per-call timeout** in `llm.py`: client/per-request timeout **25s → ~10–12s**
  (normal responses are 1–4s, so this catches stalls fast) and make it overridable per call.
- **Curtail retries on the hot path**: `retries 2 → 1`, short backoff, and **only retry if
  budget remains**. Never let backoff push past the wall.
- **Budget-gate the forced-final call**: only run the no-tools synthesis if `remaining > ~5s`;
  otherwise return the best gathered fact or an honest abstention. Give it a small `max_tokens`.
- **Tighten the loop**: `ROUND_CAP 7 → 5`; keep `max_tokens` modest (deck answers excepted).
- **Cache**: confirm the `/ask` question cache short-circuits repeats (self-test repeats
  questions) — already in `main.py`, verify it's hit.
- **Model latency check**: re-benchmark p50/p95 under a realistic multi-round load for
  qwen3.5-122b vs alternatives (gpt-oss-120b leaked reasoning before — re-test with the
  reasoning stripped). Pick the fastest model that stays accurate; keep it env-configurable.

## Workstream B — Grounding / anti-hallucination  *(turns wrong → correct/abstain)*

- **Strengthen the grounding contract** in `SYSTEM`: "Every fact in your answer must come
  verbatim from a tool result in this conversation. If you did not retrieve it, you do not
  know it." Add an explicit **abstention preference**: "If data is missing or you are unsure,
  answer exactly that it is not available — a wrong answer scores far worse than an honest
  'not available'."
- **Enforce premise verification in code** (not just prompt): if the question names an
  entity (customer / SKU / lot) and no tool call returned a non-empty match for it, the only
  allowed outcome is the "not found" abstention. Detect entity mentions + confirmed matches in
  the `Session`; if unconfirmed, override a confident answer with the honest negative.
- **Expand the trap list** in the prompt: profit margin, cost, COGS, salaries, headcount,
  market share, forecasts, ROI — "these are NOT stored in any source; say so."
- **Optional budget-gated verifier pass**: a cheap second call that checks the drafted
  answer's key figures/names appear in the gathered tool outputs; if a claim isn't supported,
  downgrade to abstention. Only when `remaining > ~6s` so it never causes a timeout.
- Keep `temperature = 0`. Add 1–2 few-shot exemplars (a trap → "not available"; a missing
  customer → specific "no customer named X").

## Workstream C — CRM routing & retrieval  *(fix the weakest vertical)*

- **Robust customer resolution** in `aldente.search_customers`:
  - Broaden `_name_variants`: diacritics fold, `&`/`e`/`and`, punctuation strip, first
    significant token.
  - **Fuzzy fallback**: if all variants return empty, fetch all customers **once** (66 rows,
    cheap, cache it) and match client-side (case-insensitive substring + token overlap),
    returning ranked candidates. This kills both CRM no-answers (now found) and false
    "not found"/guesses (the agent sees the real match or a confident empty set).
- **Clearer tool routing** in tool descriptions / prompt: when to use customers vs
  opportunities vs orders vs invoices; ID formats; that customer lookup now tolerates
  spelling/case. Hint the chain: resolve `customer_id` first, then query the rest by it.
- **Surface the id**: customer search returns the matched `id` prominently so chaining is reliable.

## Workstream D — Local eval harness  *(measure every change)*

- Build a runner over the 12 sample questions (we have reference answers) plus a few
  CRM-stress probes (misspelled/cased customer names, a missing customer, a margin trap).
  Report per-question correct/wrong/abstain **and** latency p50/p95.
- Add lightweight timing + tool-trace logging in `run()` (which tools fired, per-round ms)
  to distinguish timeout-driven from retrieval-driven failures.
- Run before/after each workstream; redeploy and run the platform self-test after A and C.

## Order & guardrails

1. **A (latency)** — removes timeout-driven failures; quick, mechanical.
2. **C (CRM fuzzy retrieval)** — converts CRM no-answers/wrongs to correct.
3. **B (grounding/abstention)** — converts remaining wrongs to correct-or-abstain.
4. **D** runs throughout.

Guardrails: keep API efficiency (only the one-time **cached** customer list is new bulk
traffic); `/ask` stays frozen + always HTTP 200; re-run local eval + platform self-test
after each step; don't touch the v2 OS UI.

## Target

Move the 5 wrong → mostly correct (some honest abstentions), recover the 2 CRM
no-answers, and keep every response comfortably < 30s (p95 ≤ ~26s).
