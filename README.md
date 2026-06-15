# Al Dente Company Brain

The **company brain** of Al Dente S.r.l. (a pasta maker): an agent that answers questions about the company by orchestrating its CRM / ERP / call-log APIs plus a knowledge base, and serves it all through a simulated-OS web UI.

Built for the Coding Agent Hackathon. The public contract is `POST /ask` (frozen schema); the rest is a desktop-style frontend with windowed apps.

> Background docs: **`AGENTS.md`** is the full spec + an "Implementation (current state)" section describing the codebase. `API.md` documents the company APIs, `SAMPLE_QUESTIONS.md` the evaluator's question shapes, `DEPLOY.md` the Railway deploy, `DOCKER.md` the Docker fallback. This README is setup + overview.

## What's built

- **Agent loop** (`backend/agent/`) — a single tool-calling loop over the Al Dente APIs and the KB, with a hard 26s wall-clock budget, grounding/abstention rules (honest "not available" on traps), pagination-aware aggregation, and answer sanitization.
- **RAG** — whole-document BM25 over `backend/data/kb/` with a hard variant filter (SKU / Bio / format) to disambiguate near-identical spec sheets. No embeddings, no infra.
- **`POST /ask`** — the frozen, public, no-auth endpoint the evaluator hits; always HTTP 200, < 30s.
- **"Al Dente OS"** — the UI at `GET /`: a desktop with draggable windows and discomorphism icons. Apps: **Company Brain** (chat + the "Fusilli" mascot), **Knowledge Base** (Finder-style browser), **RAG** (retrieval playground + knowledge graph), **API Tables** (table explorer), **Preview** (doc viewer).
- **Eval harness** (`backend/eval_agent.py`) — runs the 12 sample questions + stress probes, reporting correct/wrong/abstain + latency p50/p95.

## Quick start

```bash
cd backend/
cp .env.example .env        # then fill in your keys and token (see below)
uv sync
uv run uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000** for the Al Dente OS UI. API docs at `/docs`. Try the endpoint directly:

```bash
curl -s -X POST localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question":"Is SKU PAS-PEN-500 below its minimum stock?"}'
```

Run the local eval (needs the env loaded):

```bash
cd backend && set -a && source .env && set +a && uv run python eval_agent.py
```

**Docker fallback**: `docker compose -f docker-compose.dev.yml up -d` (see `DOCKER.md`).

## Configuration (`backend/.env`)

| Var                 | What                                                                                     |
| ------------------- | ---------------------------------------------------------------------------------------- |
| `LLM_BASE_URL`      | `https://api.regolo.ai/v1` (or Mistral)                                                  |
| `LLM_API_KEY`       | your provider key                                                                        |
| `MODEL`             | `qwen3.5-122b` (chosen via the eval A/B; must support tool calling)                      |
| `MOCK_API_BASE_URL` | `https://aldente.yellowtest.it`                                                          |
| `MOCK_API_TOKEN`    | your token from the platform dashboard                                                   |
| `PUBLIC_BASE_URL`   | this backend's public URL (Railway in prod; localhost otherwise) — drives `artifact_url` |

Never commit `.env` (git-ignored). In prod it's also stored as GitHub Actions secrets and set on Railway.

## Project layout

```tree
.
├── AGENTS.md                # Full spec + Implementation (current state)  (CLAUDE.md → symlink)
├── API.md                   # Al Dente mock API reference
├── SAMPLE_QUESTIONS.md      # 12 public questions WITH answers
├── DEPLOY.md / DOCKER.md    # Railway deploy / Docker fallback
├── PLAN.md / PLAN_V2.md / PLAN_V3.md   # design docs (agent, OS UI, performance)
└── backend/                 # Everything that gets deployed
    ├── main.py              # FastAPI: /ask, /apps/*, /api/*, /graph, /kb/*, /files/
    ├── agent/               # the agent
    │   ├── loop.py          #   tool-calling loop (budget, grounding, verticale)
    │   ├── tools.py         #   tool schemas + dispatch + source tracking
    │   ├── aldente.py       #   Al Dente API client (pagination, aggregate, fuzzy customer search)
    │   ├── kb.py            #   whole-doc BM25 RAG + hard variant filter
    │   ├── llm.py           #   OpenAI-compatible client (budgeted timeouts)
    │   ├── artifacts.py     #   inline HTML + binary docx/pptx/pdf/xlsx
    │   └── graph.py         #   cached knowledge-graph data
    ├── eval_agent.py        # local eval harness
    ├── static/              # Al Dente OS UI
    │   ├── index.html       #   desktop shell + window manager + disco icons
    │   ├── apps/            #   brain · kb · rag · tables · preview
    │   └── files/           #   generated binary artifacts, served at /files/
    └── data/kb/             # 35 company documents (the RAG corpus)
```

## Constraints recap (details in `AGENTS.md`)

- `POST /ask`: `{"question"}` → `{"answer", "sources", "verticale", "artifact_url"?}`. Frozen, public, no auth, no streaming, HTTP 200 always.
- **30 seconds** max per question (the agent enforces a 26s budget and abstains rather than time out).
- Only the provided sources (APIs + `data/kb/`). Never invent data — traps exist, honest abstention wins.
- Efficiency is measured **server-side** via your API token: targeted calls beat bulk downloads.

## Deploy & submit

Deploy the `backend/` to Railway (`DEPLOY.md`), set the env vars (and `PUBLIC_BASE_URL` to the Railway URL), run the platform endpoint check, and submit the backend URL + repo. Keep the URL up during evaluation.

## Troubleshooting

- **`uv: command not found`**: install uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`) or use the Docker fallback.
- **401 from the Al Dente APIs**: `MOCK_API_TOKEN` missing/wrong in `.env`.
- **LLM 401/404**: check `LLM_BASE_URL`, `LLM_API_KEY`, `MODEL` (Regolo ids are case-sensitive).
- **Artifact links point to localhost**: set `PUBLIC_BASE_URL` to the deployed URL.
- **Deploy issues**: `DEPLOY.md` → Common issues.
