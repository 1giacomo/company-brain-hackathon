"""Al Dente Company Brain - backend entry point.

POST /ask runs the agent (agent/loop.py) over the Al Dente mock APIs + the KB
and returns the FROZEN schema. The contract is locked — the evaluator depends on
it: always HTTP 200, single JSON object, no streaming, <30s. See AGENTS.md.
"""

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

from agent import graph, kb, loop  # noqa: E402  (after load_dotenv so env is populated)

app = FastAPI(title="Al Dente Company Brain")

_STATIC = Path(__file__).resolve().parent / "static"
_FILES = _STATIC / "files"
_FILES.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=_FILES), name="files")

# In-memory answer cache: the self-test repeats questions, so repeats are instant.
_CACHE: dict[str, dict] = {}


@app.on_event("startup")
def _warmup() -> None:
    # Build the small BM25 index ahead of the first request (healthcheck-safe).
    try:
        kb.warmup()
    except Exception:  # noqa: BLE001 - never let startup work fail the healthcheck
        pass


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)


class AskResponse(BaseModel):
    answer: str
    sources: list[str]
    verticale: str  # one of: "crm", "erp", "calls", "kb"
    artifact_url: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/graph", include_in_schema=False)
def graph_data() -> JSONResponse:
    """Knowledge-graph nodes/edges for the UI. Built once and cached server-side
    so the UI never re-downloads the metered graph per page load."""
    try:
        return JSONResponse(graph.build())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"nodes": [], "edges": [], "error": str(e)})


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    key = " ".join(request.question.split()).lower()
    if key in _CACHE:
        return AskResponse(**_CACHE[key])
    try:
        result = loop.run(request.question)
    except Exception as e:  # noqa: BLE001
        # Honest 200 beats a 5xx (CLAUDE.md): never signal "no info" with an error.
        print(f"[ask] error: {e}")
        return AskResponse(
            answer="I cannot answer that right now due to a temporary issue.",
            sources=[], verticale="kb", artifact_url=None)
    _CACHE[key] = result
    return AskResponse(**result)
