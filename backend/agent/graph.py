"""Knowledge-graph data for the UI.

Builds the company's materials network — customers, suppliers, finished products,
raw materials and the edges between them — from the Al Dente APIs. Built ONCE and
cached in-process: the graph is metered API traffic, so we never rebuild it per
page load (PLAN.md §10).
"""

from __future__ import annotations

from typing import Any

from . import aldente

_CACHE: dict[str, Any] | None = None

_CHANNELS = {"GDO", "distributor", "horeca"}


def build() -> dict[str, Any]:
    """Return {nodes, edges}, cached after the first call."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    def node(node_id: str, label: str, group: str, **extra: Any) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"id": node_id, "label": label, "group": group, **extra})

    # Channel hubs.
    for ch in sorted(_CHANNELS):
        node(f"CH::{ch}", ch, "channel")

    # Customers → channel.
    for c in aldente.fetch_all("/crm/customers"):
        cid = c["id"]
        node(cid, c.get("company_name", cid), "customer",
             channel=c.get("channel"), city=c.get("city"), status=c.get("status"))
        if c.get("channel") in _CHANNELS:
            edges.append({"from": cid, "to": f"CH::{c['channel']}", "label": "channel"})

    # Suppliers.
    for s in aldente.fetch_all("/erp/suppliers"):
        node(s["id"], s.get("name", s["id"]), "supplier", category=s.get("category"))

    # Inventory: finished goods + raw materials; raw → supplier.
    finished_skus: list[str] = []
    for item in aldente.fetch_all("/erp/inventory"):
        sku = item.get("sku")
        if not sku:
            continue
        if item.get("type") == "raw_material":
            node(sku, item.get("description", sku), "raw_material",
                 below_min=item.get("below_min"))
            if item.get("supplier_id"):
                edges.append({"from": sku, "to": item["supplier_id"], "label": "supplied by"})
        else:
            node(sku, item.get("description", sku), "product",
                 below_min=item.get("below_min"))
            finished_skus.append(sku)

    # Finished product → raw materials (bill of materials).
    for sku in finished_skus:
        try:
            bom = aldente.get("/erp/bom", sku=sku).get("data", [])
        except aldente.AldenteError:
            continue
        for entry in bom:
            for comp in entry.get("components", []):
                raw = comp.get("raw_sku")
                if not raw:
                    continue
                node(raw, comp.get("description", raw), "raw_material")
                edges.append({"from": sku, "to": raw, "label": "uses"})

    _CACHE = {"nodes": nodes, "edges": edges}
    return _CACHE
