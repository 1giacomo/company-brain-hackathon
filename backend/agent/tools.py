"""Tool schemas + dispatch for the agent loop.

Thin typed wrappers over the Al Dente APIs and the KB. Enum values are inlined in
the descriptions so the model uses the exact-match, case-sensitive filters
correctly. Every call records the source(s) it touched on the Session, so the
final answer can report them without the model having to.

`aggregate` params route to aldente.aggregate so totals / group-bys (incl. the
cross-endpoint channel join) are computed in Python — never by the LLM.
"""

from __future__ import annotations

import json
from typing import Any

from . import aldente, kb

ROW_CAP = 40  # cap rows returned to the model for non-aggregate list calls

# Shared aggregate sub-schema.
_AGG = {
    "type": "object",
    "description": "Compute a count/sum in code over ALL matching rows (paginated server-side). "
                   "Use this for 'how many' / 'total value' / grouped questions instead of reading rows.",
    "properties": {
        "op": {"type": "string", "enum": ["count", "sum"]},
        "value_field": {"type": "string", "description": "Numeric field to sum, e.g. 'value_eur'."},
        "group_by": {"type": "string",
                     "description": "Field to group by. Use 'channel' to group by the customer's "
                                    "channel (joined from /crm/customers automatically)."},
        "stages": {"type": "array", "items": {"type": "string"},
                   "description": "Client-side stage filter; 'open opportunities' = "
                                  "['qualification','negotiation']."},
        "text_contains": {"type": "string",
                          "description": "Count only rows whose text mentions this (case-insensitive) "
                                         "across ALL rows — e.g. text_contains='broken pasta' to count "
                                         "calls reporting that defect. Use this for defect/keyword counts."},
    },
    "required": ["op"],
}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "crm_customers",
        "description": "Search/list customers — ALWAYS start here for a CRM question to resolve the "
                       "customer's id, then query opportunities/orders/invoices by that customer_id. "
                       "Tolerates spelling/case/spacing (exact + fuzzy). Returns matched customers WITH "
                       "their ids. If the result has match='fuzzy', confirm the company_name truly "
                       "matches before answering. If total=0, the customer does NOT exist — say so; "
                       "do not guess.",
        "parameters": {"type": "object", "properties": {
            "search": {"type": "string", "description": "Company name (full or partial)."},
            "channel": {"type": "string", "enum": ["GDO", "distributor", "horeca"]},
            "status": {"type": "string", "enum": ["active", "inactive", "prospect"]},
        }}}},
    {"type": "function", "function": {
        "name": "crm_customer",
        "description": "Get one customer by id (CUST-####).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "crm_opportunities",
        "description": "List or aggregate opportunities. Stages: qualification, negotiation, won, lost. "
                       "'open' = qualification + negotiation. Value field is 'value_eur'.",
        "parameters": {"type": "object", "properties": {
            "customer_id": {"type": "string"},
            "stage": {"type": "string", "enum": ["qualification", "negotiation", "won", "lost"]},
            "owner": {"type": "string"},
            "aggregate": _AGG,
        }}}},
    {"type": "function", "function": {
        "name": "crm_orders",
        "description": "List orders. Statuses: open, in_production, shipped, delivered, cancelled. "
                       "Dates 'date_from'/'date_to' are ISO YYYY-MM-DD.",
        "parameters": {"type": "object", "properties": {
            "customer_id": {"type": "string"},
            "status": {"type": "string",
                       "enum": ["open", "in_production", "shipped", "delivered", "cancelled"]},
            "date_from": {"type": "string"}, "date_to": {"type": "string"},
            "aggregate": _AGG,
        }}}},
    {"type": "function", "function": {
        "name": "crm_invoices",
        "description": "List invoices. Statuses: unpaid, paid, overdue.",
        "parameters": {"type": "object", "properties": {
            "customer_id": {"type": "string"},
            "status": {"type": "string", "enum": ["unpaid", "paid", "overdue"]},
            "order_id": {"type": "string"},
            "aggregate": _AGG,
        }}}},
    {"type": "function", "function": {
        "name": "calls_list",
        "description": "List/aggregate call logs (metadata incl. 'topic', 'summary', 'related_lot_id', "
                       "'date'). The defect/complaint is in 'topic'/'summary' — no transcript needed to "
                       "find or count defects. To find or count calls about a specific defect/topic "
                       "(e.g. 'foreign body', 'delivery delay', 'broken pasta') you MUST set "
                       "text_contains to that phrase — otherwise you get ALL calls, not the matching "
                       "ones. Types: sales, support. Outcomes: complaint_open, follow_up, order_placed, "
                       "resolved. Set latest=true for the most recent call.",
        "parameters": {"type": "object", "properties": {
            "customer_id": {"type": "string"},
            "type": {"type": "string", "enum": ["sales", "support"]},
            "outcome": {"type": "string",
                        "enum": ["complaint_open", "follow_up", "order_placed", "resolved"]},
            "text_contains": {"type": "string",
                              "description": "Keep only calls whose topic/summary mentions this phrase "
                                             "(case-insensitive). Use for 'which call about X' / counting "
                                             "calls by defect. For a count, combine with aggregate.op=count."},
            "date_from": {"type": "string"}, "date_to": {"type": "string"},
            "latest": {"type": "boolean", "description": "Return only the most recent call (by date)."},
            "aggregate": _AGG,
        }}}},
    {"type": "function", "function": {
        "name": "call_transcript",
        "description": "Extract only the transcript segments matching a search term (never the whole "
                       "transcript). Use after finding a call id.",
        "parameters": {"type": "object", "properties": {
            "call_id": {"type": "string"},
            "search": {"type": "string"},
            "speaker": {"type": "string", "enum": ["customer", "agent"]},
        }, "required": ["call_id", "search"]}}},
    {"type": "function", "function": {
        "name": "erp_production_orders",
        "description": "List production lots (id format LOT-2026-####). Statuses: planned, in_progress, "
                       "done, blocked. Pass lot_id to fetch a specific lot. NOTE: lots carry NO cost or "
                       "profit-margin data.",
        "parameters": {"type": "object", "properties": {
            "lot_id": {"type": "string"},
            "customer_id": {"type": "string"},
            "sku": {"type": "string"},
            "status": {"type": "string", "enum": ["planned", "in_progress", "done", "blocked"]},
        }}}},
    {"type": "function", "function": {
        "name": "erp_inventory",
        "description": "Inventory. type: finished_good | raw_material. below_min=true lists items under "
                       "minimum stock. search by sku or description. Raw materials carry 'supplier_id'.",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["finished_good", "raw_material"]},
            "below_min": {"type": "boolean"},
            "search": {"type": "string"},
        }}}},
    {"type": "function", "function": {
        "name": "erp_suppliers",
        "description": "Suppliers. Categories: semolina, wheat, packaging, labels, ink, logistics. "
                       "Pass supplier_id to resolve a specific supplier's name.",
        "parameters": {"type": "object", "properties": {
            "supplier_id": {"type": "string"},
            "search": {"type": "string"},
            "category": {"type": "string",
                         "enum": ["semolina", "wheat", "packaging", "labels", "ink", "logistics"]},
        }}}},
    {"type": "function", "function": {
        "name": "erp_bom",
        "description": "Bill of materials for a finished SKU: its raw components (raw_sku, qty_per_carton).",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}}, "required": ["sku"]}}},
    {"type": "function", "function": {
        "name": "erp_shipments",
        "description": "Shipments. Statuses: in_transit, delivered, delayed.",
        "parameters": {"type": "object", "properties": {
            "customer_id": {"type": "string"},
            "order_id": {"type": "string"},
            "status": {"type": "string", "enum": ["in_transit", "delivered", "delayed"]},
        }}}},
    {"type": "function", "function": {
        "name": "kb_search",
        "description": "Search the knowledge base (product spec sheets, quality/returns policies, the "
                       "2026 wholesale price list, customer capitolati). Returns whole documents. Price "
                       "list (DOC-015) prices are PER CARTON (20x500g) despite the 'per unit' header.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
]


class Session:
    """Per-request state: the set of sources touched by tool calls."""

    def __init__(self) -> None:
        self.sources: set[str] = set()
        self.tool_tally: dict[str, int] = {}

    # -- helpers --------------------------------------------------------------
    def _src(self, *ids: str) -> None:
        self.sources.update(i for i in ids if i)

    def _cap(self, rows: list[dict], path: str) -> dict[str, Any]:
        out: dict[str, Any] = {"total": len(rows), "data": rows[:ROW_CAP]}
        if len(rows) > ROW_CAP:
            out["note"] = f"Showing first {ROW_CAP} of {len(rows)} rows; use aggregate for totals."
        return out

    def _aggregate(self, path: str, agg: dict, filters: dict) -> dict[str, Any]:
        res = aldente.aggregate(
            path, op=agg.get("op", "count"), value_field=agg.get("value_field"),
            group_by=agg.get("group_by"), stages=agg.get("stages"),
            text_contains=agg.get("text_contains"), filters=filters)
        self._src(*res.pop("_sources", [path.lstrip("/")]))
        return res

    # -- dispatch -------------------------------------------------------------
    def run(self, name: str, args: dict[str, Any]) -> str:
        self.tool_tally[name] = self.tool_tally.get(name, 0) + 1
        try:
            result = self._run(name, args)
        except aldente.AldenteError as e:
            result = {"error": str(e)}
        return json.dumps(result, ensure_ascii=False, default=str)

    def _run(self, name: str, args: dict[str, Any]) -> Any:
        if name == "crm_customers":
            self._src("crm/customers")
            return aldente.search_customers(args.get("search"), channel=args.get("channel"),
                                            status=args.get("status"))
        if name == "crm_customer":
            self._src("crm/customers")
            env = aldente.get(f"/crm/customers/{args['id']}")
            return env.get("data", env)
        if name == "crm_opportunities":
            filters = {k: args.get(k) for k in ("customer_id", "stage", "owner")}
            if args.get("aggregate"):
                return self._aggregate("/crm/opportunities", args["aggregate"], filters)
            self._src("crm/opportunities")
            return self._cap(aldente.fetch_all("/crm/opportunities", **filters), "crm/opportunities")
        if name == "crm_orders":
            filters = {"customer_id": args.get("customer_id"), "status": args.get("status"),
                       "from": args.get("date_from"), "to": args.get("date_to")}
            if args.get("aggregate"):
                return self._aggregate("/crm/orders", args["aggregate"], filters)
            self._src("crm/orders")
            return self._cap(aldente.fetch_all("/crm/orders", **filters), "crm/orders")
        if name == "crm_invoices":
            filters = {k: args.get(k) for k in ("customer_id", "status", "order_id")}
            if args.get("aggregate"):
                return self._aggregate("/crm/invoices", args["aggregate"], filters)
            self._src("crm/invoices")
            return self._cap(aldente.fetch_all("/crm/invoices", **filters), "crm/invoices")
        if name == "calls_list":
            filters = {"customer_id": args.get("customer_id"), "type": args.get("type"),
                       "outcome": args.get("outcome"), "from": args.get("date_from"),
                       "to": args.get("date_to")}
            tc = args.get("text_contains")
            if args.get("aggregate"):
                agg = dict(args["aggregate"])
                if tc and not agg.get("text_contains"):  # honor top-level filter in the count
                    agg["text_contains"] = tc
                return self._aggregate("/calls", agg, filters)
            self._src("calls")
            rows = aldente.fetch_all("/calls", **filters)
            if tc:
                rows = [r for r in rows if aldente._row_contains(r, tc)]
            if args.get("latest"):
                latest = aldente.latest_by(rows, "date")
                return {"latest_call": latest, "matched_calls": len(rows)}
            return self._cap(rows, "calls")
        if name == "call_transcript":
            self._src(f"calls/{args['call_id']}/transcript")
            return aldente.search_transcript(args["call_id"], args["search"],
                                             speaker=args.get("speaker"))
        if name == "erp_production_orders":
            self._src("erp/production-orders")
            filters = {k: args.get(k) for k in ("customer_id", "sku", "status")}
            rows = aldente.fetch_all("/erp/production-orders", **filters)
            if args.get("lot_id"):
                rows = [r for r in rows if r.get("id") == args["lot_id"]]
                return {"lot": rows[0] if rows else None,
                        "found": bool(rows),
                        "note": "Lots carry no cost/margin data." if rows else "Lot not found."}
            return self._cap(rows, "erp/production-orders")
        if name == "erp_inventory":
            self._src("erp/inventory")
            return self._cap(aldente.fetch_all(
                "/erp/inventory", type=args.get("type"),
                below_min=("true" if args.get("below_min") else None),
                search=args.get("search")), "erp/inventory")
        if name == "erp_suppliers":
            self._src("erp/suppliers")
            rows = aldente.fetch_all("/erp/suppliers", search=args.get("search"),
                                     category=args.get("category"))
            if args.get("supplier_id"):
                rows = [r for r in rows if r.get("id") == args["supplier_id"]]
                return {"supplier": rows[0] if rows else None, "found": bool(rows)}
            return self._cap(rows, "erp/suppliers")
        if name == "erp_bom":
            self._src("erp/bom")
            env = aldente.get("/erp/bom", sku=args["sku"])
            return env.get("data", [])
        if name == "erp_shipments":
            self._src("erp/shipments")
            filters = {k: args.get(k) for k in ("customer_id", "order_id", "status")}
            return self._cap(aldente.fetch_all("/erp/shipments", **filters), "erp/shipments")
        if name == "kb_search":
            docs = kb.search(args["query"])
            self._src(*[d["doc_id"] for d in docs])
            return {"documents": docs}
        return {"error": f"unknown tool {name}"}
