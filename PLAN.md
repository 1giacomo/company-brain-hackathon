# Agent implementation plan — Al Dente Company Brain

Goal: implement `POST /ask` as a single tool-calling agent over the Al Dente
mock APIs + a KB retrieval tool, honest on traps, fast (<30s), with
pagination-correct aggregates and binary/inline artifacts.

## 0. Constraints that drive the design

- `/ask` schema is frozen: `{answer, sources[], verticale, artifact_url}`, always HTTP 200, no streaming, <30s.
- Provider: OpenAI-compatible (Regolo/Mistral). **Don't hardcode the model** — hit `GET {LLM_BASE_URL}/models`
  at setup, confirm the id is live + tool-capable, and benchmark latency; pick the *fastest accurate*
  tool-calling model, not the biggest (latency budget below). Regolo ids are case-sensitive.
- Efficiency is metered → targeted filtered calls, no bulk downloads, extract transcripts with `?search=`.
- Arithmetic in code, never in the prompt. Whole-document KB retrieval.
- **Wall-clock deadline, not just a round cap**: p95 ≈ 26s vs a 30s wall and multi-hop questions are
  4–5 sequential LLM round-trips. Enforce a ~22s deadline inside the loop and force synthesis if exceeded.

## 1. File layout (all under `backend/`)

```
backend/
  main.py              # wire /ask -> agent.run(), cache, error fallback (HTTP 200)
  agent/
    __init__.py
    llm.py             # OpenAI client, chat wrapper, reasoning_content handling, retry/backoff
    aldente.py         # httpx client for mock APIs: auth, pagination, fetch_all, server-side aggregate
    tools.py           # tool JSON schemas + dispatch table; tracks sources used
    kb.py              # load 35 docs, BM25 (+ optional embedding) whole-doc retrieval
    loop.py            # agent loop, step cap, verticale derivation, source collection
    artifacts.py       # docx/pptx/pdf/xlsx generation -> static/files/, absolute URL
  static/              # UI + knowledge-graph (L2 deliverable)
```

## 2. LLM layer (`llm.py`)

- `OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)`, model from `MODEL`.
- `chat(messages, tools)` wrapper:
    - reads `choice.message.content`, falls back to `reasoning_content` if content empty.
    - retry w/ exponential backoff on 429/5xx (Mistral free tier ~1 req/s if used).
    - hard timeout per call; on terminal failure raise → caller returns honest 200.
- Keep system prompt short (latency + token budget).

## 3. Al Dente API client (`aldente.py`)

- `httpx.Client` with base URL + `Authorization: Bearer MOCK_API_TOKEN`, ~10s timeout.
- `get(path, **params)` → raw JSON. Drop None params. Filters are exact-match & case-sensitive.
- `fetch_all(path, **filters)` → loops `offset` until `pagination.total`, returns all rows
  (this is the pagination-aware helper; the LLM never pages by hand).
- `aggregate(path, filters, op, value_field=None, group_by=None, enrich=None)` → fetch_all then compute
  count / sum / group-by **in Python**. Covers Q1 (count+sum) and Q11 (count by defect).
  Returns computed numbers, not raw rows, to the model.
- **Cross-endpoint join (Q6 fix — `group_by` field may live on another endpoint):** `channel` is on
  `/crm/customers`, NOT on `/crm/opportunities`. The `enrich` step fetches `/crm/customers` **once**
  (paginate fully → `{customer_id: channel}` dict) and joins in Python before grouping. **Never** look
  up each opportunity's customer individually (N+1, slow + metered). Record *both* sources used.
- Transcript helper: `search_transcript(call_id, term)` → `/calls/{id}/transcript?search=`,
  returns only matching segments (never full transcript).
- **"Last/most-recent" ordering (Q3 fix):** never trust API default order. Fetch the rows, sort by the
  date/timestamp field in Python, take the most recent. Verify the actual field name on a live response early.
- **Name-variant retry (Q12 fix):** `search` is exact-match & case-sensitive. On an empty customer lookup,
  retry a couple of normalized variants ("GranMercato" ↔ "Gran Mercato", case folds) **before** concluding
  "not found" — but cap the retries so a genuine miss (Q8 trap) still resolves to an honest negative fast.

## 4. Tools exposed to the model (`tools.py`)

Thin typed wrappers — one per endpoint family, enum values inlined in descriptions so the
model uses exact-match filters. Each call appends its source id to a per-request `sources` set.

- `crm_customers(search?, channel?, status?)`, `crm_customer(id)`
- `crm_opportunities(customer_id?, stage?, owner?, aggregate?)`
- `crm_orders(customer_id?, status?, from?, to?)`
- `crm_invoices(customer_id?, status?, order_id?)`
- `calls_list(customer_id?, type?, outcome?, from?, to?, aggregate?)`
- `call_transcript(call_id, search)` ← search required, forces targeted extraction
- `erp_production_orders(...)`, `erp_inventory(type?, below_min?, search?)`,
  `erp_suppliers(search?, category?)`, `erp_bom(sku)`, `erp_shipments(...)`
- `kb_search(query)` → top whole documents (text + DOC id)
- List tools accept `aggregate={op,value_field,group_by,enrich}` → routed to `aldente.aggregate`,
  so totals/group-bys (incl. the cross-endpoint channel join) are computed server-side, never by the LLM.
- `calls_list` — confirm early whether the **defect/complaint type is in `/calls` metadata** (Q11 → cheap
  group-by) or only in transcripts. If only in transcripts: narrow with `outcome=complaint_open` first,
  then targeted `transcript?search=broken` on that subset only — never 80 full-transcript fetches.
- Source tracking: API tools record e.g. `"crm/opportunities"`; `kb_search` records `DOC-xxx`.

## 5. KB retrieval (`kb.py`)

- Load all 35 docs at startup (tiny → fast, healthcheck-safe). Parse DOC id + title from header.
- **Whole-document** retrieval (docs are small & near-identical; chunking hurts).
- BM25 (`rank-bm25`) over full doc text for ranking.
- **Near-duplicate disambiguation is the #1 KB risk (C2).** The corpus has three overlapping spec-sheet
  families — base 500g (DOC-001–010), **Bio** (DOC-018–022), **250g** (DOC-023–025) — textually almost
  identical. SKU / format / Bio matching must be a **HARD FILTER, not a soft boost**: parse the
  distinguishing tokens from the question (SKU regex `PAS-XXX-###`/`RAW-XXX-###`, plus `Bio`, `250g`,
  `500g`) and **exclude** non-matching variants from the candidate set before BM25. Only fall back to soft
  ranking when no distinguishing token is present. Return top 2–3 full docs.
- **No embeddings** (dropped vs first draft): embeddings make near-identical spec sheets *more* confusable,
  add infra + sync time + latency, and BM25 + the hard filter already disambiguates. Skip chromadb/faiss.

## 6. Agent loop (`loop.py`)

- System prompt: role, the 4 verticali, tool list, and the **honesty rules**:
    - verify a named entity exists (customer / SKU / lot — search first) before answering about it;
    - if not found, say so specifically ("no customer named X in the CRM");
    - never invent figures absent from sources (e.g. profit margin → not available), even if it *looks*
      derivable — name exactly what's missing;
    - official documents beat call transcripts on conflicts (Q12);
    - respect a document's own unit convention — DOC-015 prices are **per carton (20×500g)** even though
      the table header says "EUR / unit" (C3 trap). Never multiply by 20 to "fix" units.
- Loop: send question → model emits tool_calls → execute → feed results → repeat.
  Encourage **parallel tool calls in one round** when independent (e.g. BOM + inventory once SKU known).
- **Stop conditions:** ~5–6 rounds AND a wall-clock deadline (~22s). Whichever hits first → force a final
  answer from what's gathered (the deadline is the real guard for slow multi-hop chains, see §0).
- Collect `sources` from executed tools (the join in §3 records every endpoint touched).
- **`verticale` (I4 fix):** the model emits its own `verticale` label in the final structured output
  (it knows what the question is *fundamentally* about — Q5 is `calls`, Q12 is `kb`, even when both touch
  multiple sources). Use tool-usage tally only as a fallback/sanity check, not the primary signal.

## 7. Traps

- No tool returns margin/cost → model can't fetch it; prompt forbids inventing → honest answer.
- Premise check generalizes to **customers, SKUs and lots** (Q8's pattern): search first; empty result
  (after the name-variant retries in §3) → explicit "not found". Guard so variant retries don't mask a
  genuine miss.
- These score full marks only if specific, so the prompt mandates naming what's missing.

## 8. Artifacts (`artifacts.py`)

- Detect format in the question.
    - HTML / markdown deck → return **inline in `answer`**, `artifact_url=None`.
    - docx / pptx / pdf / xlsx → generate file in `static/files/`, return `f"{PUBLIC_BASE_URL}/files/<name>"` (absolute).
- Flow: agent gathers the real data (facts judged first), then a render step builds the file
  from a structured spec. Uncomment `python-docx`, `python-pptx`, `fpdf2`, `openpyxl` in
  `pyproject.toml` and `uv sync`.

## 9. `main.py` wiring

- `/ask`: normalize question → in-memory cache (repeated self-tests = sub-second) →
  `agent.run()` → build `AskResponse`.
- **Never 4xx/5xx for "no info"**: wrap the loop in try/except; on any error return HTTP 200
  with an honest "I cannot answer right now" + empty sources.
- Keep `/health` trivial (no heavy startup work in the handler).

## 10. UI + knowledge graph (graded L2)

- Replace `static/index.html`: ask box → calls `/ask`, renders answer + sources + verticale,
  shows inline HTML artifacts and artifact_url links.
- Knowledge-graph view: nodes = customers / suppliers / products / raw materials, edges =
  orders, BOM, supplier links (built from API data). Use a light JS lib (e.g. vis-network /
  cytoscape via CDN). Required & scored — budget real time for it.
- **Build the graph data once and cache server-side** (or a one-time snapshot) — do NOT re-download the
  full customer/supplier/BOM graph on every page load (large metered download). Warm it at startup.

## 11. Build order (maps to the 6-hour pacing)

0. **Probe first** (cheap, prevents wrong assumptions): `GET {LLM_BASE_URL}/models` → pick fastest
   tool-capable model + note latency; hit one live `/calls`, `/crm/opportunities`, `/crm/customers`
   response to confirm real field names (date field for "last call", whether defect type is in call
   metadata, channel on customers).
1. `aldente.py` + `fetch_all` + one CRM tool + minimal loop → first end-to-end CRM answer (Q1).
2. Add ERP, calls, transcript-search, `kb.py` (with the hard variant filter) → cover all 4 verticali (Q2–Q5,Q10).
3. **Deploy to Railway** (rough is fine), run endpoint check.
4. Aggregation + cross-endpoint channel join (Q6), Q11 defect count, trap honesty (Q7,Q8), Q12
   conflict + price-unit rule, name-variant retry; wall-clock deadline; redeploy + self-test.
5. Artifacts inline + binary (Q9 + hidden file questions).
6. UI + knowledge graph (cached graph data); final self-test, verify artifact URLs on deployed app, submit.

## Verification targets (the 12 sample questions)

Q1 count+sum • Q2 inventory below_min • Q3 last call complaint+lot • Q4 KB shelf life/allergens •
Q5 call→returns policy chain • Q6 sum by channel • Q7 margin trap • Q8 missing-customer trap •
Q9 HTML deck • Q10 BOM→supplier→stock chain • Q11 paged count by defect • Q12 doc-vs-call conflict.
