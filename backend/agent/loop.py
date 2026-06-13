"""The agent loop.

Single tool-calling loop: route the question → call Al Dente APIs / the KB →
compose the answer. Honesty rules live in the system prompt; arithmetic and
pagination live in the tools. Stops on `submit_answer`, a round cap, or a
~22s wall-clock deadline (the real guard against slow multi-hop chains).
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import artifacts, llm, tools

ROUND_CAP = 7
DEADLINE_S = 20.0  # leave headroom under the 30s wall for the final synthesis call

# Map each data tool to the verticale it represents (fallback for verticale).
_TOOL_VERTICALE = {
    "crm_customers": "crm", "crm_customer": "crm", "crm_opportunities": "crm",
    "crm_orders": "crm", "crm_invoices": "crm",
    "calls_list": "calls", "call_transcript": "calls",
    "erp_production_orders": "erp", "erp_inventory": "erp", "erp_suppliers": "erp",
    "erp_bom": "erp", "erp_shipments": "erp",
    "kb_search": "kb",
}

SYSTEM = """You are the company brain of Al Dente S.r.l., an Italian pasta maker selling to \
supermarkets (GDO), distributors and restaurants (horeca). Answer questions about the company \
by calling tools, then giving a concise, factual answer.

Data sources (verticali): crm (customers, opportunities, orders, invoices), erp (production \
lots, inventory, suppliers, bill of materials, shipments), calls (call logs + transcripts), \
kb (spec sheets, quality/returns policies, the 2026 wholesale price list, customer capitolati).

RULES:
- Use ONLY data from the tools. Never invent figures, names, statuses or documents.
- Verify a named entity exists before answering about it. If a customer/SKU/lot is not found, \
say so specifically (e.g. "There is no customer named X in the CRM") — do not guess.
- Some questions are traps: the figure simply does not exist in any source (e.g. profit margin \
or cost — these are NOT stored anywhere). Say plainly that it is not available and name what \
is missing. A specific honest "not available" is the correct answer.
- For "how many" / "total value" / grouped questions, use the tool's `aggregate` parameter so \
the count/sum is computed over ALL rows in code — never add up numbers yourself.
- For "last"/"most recent", use latest=true.
- When a phone call and an official document disagree, the official document is authoritative.
- Price-list (DOC-015) prices are PER CARTON (20 x 500g units), even though the table header \
says "EUR / unit". Never multiply by 20.
- Be efficient: filter your tool calls; never fetch broadly when a filter exists.

When you have the answer, call submit_answer with the natural-language answer and the dominant \
verticale. If the question asks for an HTML or markdown deck/report, put the full HTML/markdown \
INLINE in the answer. If (and only if) it explicitly asks for a downloadable docx/pptx/pdf/xlsx \
file, first call create_artifact, then pass the returned URL to submit_answer."""

_META_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "create_artifact",
        "description": "Render a downloadable binary file (docx/pptx/pdf/xlsx) from data you already "
                       "gathered, and get back its URL. Only for explicit file-format requests.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(artifacts.BINARY_KINDS)},
            "title": {"type": "string"},
            "sections": {"type": "array", "description": "For docx/pdf.", "items": {
                "type": "object", "properties": {
                    "heading": {"type": "string"}, "body": {"type": "string"}}}},
            "slides": {"type": "array", "description": "For pptx.", "items": {
                "type": "object", "properties": {
                    "title": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}}}}},
            "table": {"type": "object", "description": "For xlsx.", "properties": {
                "columns": {"type": "array", "items": {"type": "string"}},
                "rows": {"type": "array", "items": {"type": "array"}}}},
            "sheet_name": {"type": "string"},
        }, "required": ["kind", "title"]}}},
    {"type": "function", "function": {
        "name": "submit_answer",
        "description": "Provide the final answer and finish.",
        "parameters": {"type": "object", "properties": {
            "verticale": {"type": "string", "enum": ["crm", "erp", "calls", "kb"],
                          "description": "The dominant source for this question."},
            "answer": {"type": "string",
                       "description": "Natural-language answer, or inline HTML/markdown artifact."},
            "artifact_url": {"type": "string",
                             "description": "Only if a binary file was created via create_artifact."},
        }, "required": ["verticale", "answer"]}}},
]

_ALL_TOOLS = tools.TOOL_SCHEMAS + _META_TOOLS


def _args(tool_call: Any) -> dict[str, Any]:
    raw = tool_call.function.arguments or "{}"
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _assistant_dict(msg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [{
            "id": tc.id, "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
        } for tc in msg.tool_calls]
    return out


def _unwrap(text: str, session: "tools.Session") -> tuple[str, str]:
    """Some models emit the structured answer as plain JSON text instead of
    calling submit_answer. Unwrap {"answer":..., "verticale":...} if present;
    otherwise treat the text as the answer and infer the verticale."""
    s = text.strip()
    if s.startswith("{") and '"answer"' in s:
        try:
            obj = json.loads(s)
            ans = obj.get("answer")
            if isinstance(ans, str) and ans.strip():
                vert = obj.get("verticale") or _fallback_verticale(session)
                return ans, vert
        except json.JSONDecodeError:
            pass
    return text, _fallback_verticale(session)


def _fallback_verticale(session: tools.Session) -> str:
    best, best_n = "kb", -1
    counts: dict[str, int] = {}
    for name, n in session.tool_tally.items():
        v = _TOOL_VERTICALE.get(name)
        if v:
            counts[v] = counts.get(v, 0) + n
    for v, n in counts.items():
        if n > best_n:
            best, best_n = v, n
    return best


def run(question: str) -> dict[str, Any]:
    """Run the agent and return {answer, sources, verticale, artifact_url}."""
    session = tools.Session()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    start = time.monotonic()
    artifact_url: str | None = None

    for round_i in range(ROUND_CAP):
        over_deadline = (time.monotonic() - start) > DEADLINE_S
        if over_deadline:
            break
        msg = llm.chat(messages, tools=_ALL_TOOLS, max_tokens=1400)
        if not msg.tool_calls:
            # Model answered directly without submit_answer — accept its text.
            text = llm.content_of(msg)
            if text.strip():
                ans, vert = _unwrap(text, session)
                return _finalize(ans, vert, session, artifact_url)
            # Empty turn: nudge once more.
            messages.append({"role": "assistant", "content": ""})
            continue

        messages.append(_assistant_dict(msg))
        for tc in msg.tool_calls:
            name = tc.function.name
            args = _args(tc)
            if name == "submit_answer":
                verticale = args.get("verticale") or _fallback_verticale(session)
                return _finalize(args.get("answer", ""), verticale, session,
                                 args.get("artifact_url") or artifact_url)
            if name == "create_artifact":
                try:
                    artifact_url = artifacts.create(
                        args.get("kind", "pdf"), args.get("title", "Al Dente report"),
                        sections=args.get("sections"), slides=args.get("slides"),
                        table=args.get("table"), sheet_name=args.get("sheet_name"))
                    result = json.dumps({"artifact_url": artifact_url})
                except Exception as e:  # noqa: BLE001
                    result = json.dumps({"error": f"artifact generation failed: {e}"})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                continue
            result = session.run(name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # Deadline or round cap hit: force a final plain-text answer (no tools).
    messages.append({"role": "user", "content":
                     "Time is up. Give your best final answer now from what you have gathered, "
                     "in one or two sentences. If a value was not available in the sources, say so."})
    try:
        final = llm.chat(messages, tools=None, max_tokens=600)
        text = llm.content_of(final) or "I cannot answer that right now."
    except Exception:  # noqa: BLE001
        text = "I cannot answer that right now."
    return _finalize(text, _fallback_verticale(session), session, artifact_url)


def _finalize(answer: str, verticale: str, session: tools.Session,
              artifact_url: str | None) -> dict[str, Any]:
    return {
        "answer": answer.strip() or "I cannot answer that right now.",
        "sources": sorted(session.sources),
        "verticale": verticale if verticale in ("crm", "erp", "calls", "kb") else "kb",
        "artifact_url": artifact_url,
    }
