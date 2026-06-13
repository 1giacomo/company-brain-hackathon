"""Al Dente Company Brain - backend entry point.

POST /ask runs the agent (agent/loop.py) over the Al Dente mock APIs + the KB
and returns the FROZEN schema. The contract is locked — the evaluator depends on
it: always HTTP 200, single JSON object, no streaming, <30s. See AGENTS.md.
"""

import json
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

from agent import graph, kb, loop  # noqa: E402  (after load_dotenv so env is populated)

app = FastAPI(title="Al Dente Company Brain")

_STATIC = Path(__file__).resolve().parent / "static"
_FILES = _STATIC / "files"
_FILES.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=_FILES), name="files")

_KB_DIR = Path(__file__).resolve().parent / "data" / "kb"
_DOC_ID = re.compile(r"DOC-\d{3}")

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


@app.get("/kb/{doc_id}", include_in_schema=False)
def kb_document(doc_id: str) -> HTMLResponse:
    """Render a knowledge-base document as a formatted HTML page (opens inline in
    a new tab when cited as a source in an answer)."""
    if not _DOC_ID.fullmatch(doc_id):
        raise HTTPException(status_code=404, detail="not found")
    path = _KB_DIR / f"{doc_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    # Embed the markdown safely as a JS string and render client-side with marked.
    md_js = json.dumps(path.read_text(encoding="utf-8")).replace("</", "<\\/")
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{doc_id} · Al Dente</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<style>
  body {{ max-width: 820px; margin: 0 auto; padding: 40px 24px; background: #f7f7f5; color: #1c1c1c;
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif; line-height: 1.6; }}
  h1,h2,h3 {{ line-height: 1.25; }} h1 {{ border-bottom: 2px solid #e0b341; padding-bottom: 8px; }}
  table {{ border-collapse: collapse; margin: 12px 0; }}
  th,td {{ border: 1px solid #ccc; padding: 6px 12px; text-align: left; }} th {{ background: #efe9d8; }}
  code {{ background: #ececec; padding: 1px 5px; border-radius: 4px; }}
  .doc-badge {{ display:inline-block; font: 12px ui-monospace,monospace; color:#7a5c00;
    background:#f5e6b8; border:1px solid #e0b341; border-radius:6px; padding:2px 8px; margin-bottom:16px; }}
</style></head><body>
<div class="doc-badge">📄 {doc_id} · Al Dente knowledge base</div>
<div id="doc"></div>
<script>document.getElementById('doc').innerHTML = marked.parse({md_js});</script>
</body></html>"""
    return HTMLResponse(page)


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
