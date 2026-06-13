"""Al Dente mock-API client.

Thin, efficient HTTP layer over the read-only Al Dente APIs. The agent never
pages or sums by hand: `fetch_all` walks pagination and `aggregate` computes
counts / sums / group-bys in Python (see PLAN.md §3). Field names here were
confirmed against live responses during the step-0 probe.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

import httpx

BASE_URL = os.environ.get("MOCK_API_BASE_URL", "https://aldente.yellowtest.it").rstrip("/")
TOKEN = os.environ.get("MOCK_API_TOKEN", "")
MAX_LIMIT = 200  # hard cap documented in API.md

_client = httpx.Client(
    base_url=BASE_URL,
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=httpx.Timeout(12.0, connect=5.0),
)


class AldenteError(RuntimeError):
    pass


def _params(d: dict[str, Any]) -> dict[str, Any]:
    """Drop None values; the API treats filters as exact-match & case-sensitive."""
    return {k: v for k, v in d.items() if v is not None and v != ""}


def get(path: str, **params: Any) -> dict[str, Any]:
    """Single GET. Returns the parsed JSON envelope. Raises on transport errors;
    a 404 returns its body so callers can detect 'not found' without an exception."""
    r = _client.get(path, params=_params(params))
    if r.status_code == 404:
        return {"data": [], "pagination": {"offset": 0, "limit": 0, "total": 0}, "_status": 404}
    if r.status_code >= 400:
        raise AldenteError(f"{path} -> {r.status_code}: {r.text[:200]}")
    return r.json()


def fetch_all(path: str, **filters: Any) -> list[dict[str, Any]]:
    """Walk pagination and return every row for the given filters.

    This is THE pagination-aware helper — counting only the first page is the
    single most common wrong answer (see CLAUDE.md). Uses limit=200 to minimise
    round-trips and bytes.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        env = get(path, limit=MAX_LIMIT, offset=offset, **filters)
        page = env.get("data", [])
        rows.extend(page)
        pag = env.get("pagination", {})
        total = pag.get("total", len(rows))
        offset += MAX_LIMIT
        if offset >= total or not page:
            break
    return rows


# --- Transcripts: extract, never download whole -----------------------------

def search_transcript(call_id: str, search: str, *, speaker: str | None = None,
                      limit: int = 12) -> dict[str, Any]:
    """Pull only the segments matching `search` (and optional speaker). Never
    download the full transcript (hundreds of segments = wasted tokens/efficiency)."""
    env = get(f"/calls/{call_id}/transcript", search=search, speaker=speaker, limit=limit)
    return {
        "call_id": call_id,
        "matched_segments": env.get("segments", []),
        "total_matches": env.get("pagination", {}).get("total", 0),
    }


# --- "Last / most-recent" ordering -------------------------------------------

def latest_by(rows: Iterable[dict[str, Any]], date_field: str) -> dict[str, Any] | None:
    """Return the most-recent row by an ISO date/datetime field. Never trust the
    API's default ordering (PLAN.md §3, Q3)."""
    rows = [r for r in rows if r.get(date_field)]
    if not rows:
        return None
    return max(rows, key=lambda r: r[date_field])


# --- Name-variant customer search --------------------------------------------

_WS = re.compile(r"\s+")


def _name_variants(name: str) -> list[str]:
    """A few normalized spellings to retry before declaring a customer absent
    (Q12: 'GranMercato' vs 'Gran Mercato'). Kept small so a genuine miss (Q8
    trap) still resolves fast."""
    n = name.strip()
    variants = [n]
    # Strip common legal suffixes for a looser match.
    core = re.sub(r"\b(S\.?p\.?A\.?|S\.?r\.?l\.?|S\.?n\.?c\.?)\b", "", n, flags=re.I).strip()
    if core and core != n:
        variants.append(core)
    base = variants[1] if len(variants) > 1 else n
    # Toggle spaces between CamelCase / collapse spaces.
    spaced = _WS.sub(" ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)).strip()
    nospace = base.replace(" ", "")
    for v in (spaced, nospace):
        if v and v not in variants:
            variants.append(v)
    return variants


def search_customers(name: str | None = None, *, channel: str | None = None,
                     status: str | None = None) -> dict[str, Any]:
    """Search customers. When a name is given and the first try is empty, retry a
    couple of normalized variants before concluding 'not found'."""
    tried: list[str] = []
    if name:
        for variant in _name_variants(name):
            tried.append(variant)
            rows = fetch_all("/crm/customers", search=variant, channel=channel, status=status)
            if rows:
                return {"data": rows, "total": len(rows), "matched_variant": variant}
        return {"data": [], "total": 0, "tried_variants": tried}
    rows = fetch_all("/crm/customers", channel=channel, status=status)
    return {"data": rows, "total": len(rows)}


# --- Aggregation (arithmetic in code, not in the prompt) ---------------------

def _customer_channel_map() -> dict[str, str]:
    """`{customer_id: channel}`, fetched once. Used to enrich rows whose group-by
    field (channel) lives on a different endpoint (Q6 cross-endpoint join)."""
    return {c["id"]: c.get("channel") for c in fetch_all("/crm/customers")}


def _row_contains(row: dict[str, Any], text: str) -> bool:
    """True if any string field of the row contains `text` (case-insensitive).
    Used to count free-text matches like a defect mentioned in topic/summary."""
    t = text.lower()
    return any(isinstance(v, str) and t in v.lower() for v in row.values())


def aggregate(path: str, *, op: str, value_field: str | None = None,
              group_by: str | None = None, filters: dict[str, Any] | None = None,
              stages: list[str] | None = None,
              text_contains: str | None = None) -> dict[str, Any]:
    """Fetch all matching rows and compute count / sum, optionally grouped.

    op: 'count' | 'sum'
    value_field: numeric field to sum (e.g. 'value_eur').
    group_by: a field on the row, OR the literal 'channel' which is joined from
              /crm/customers (the row must carry 'customer_id').
    stages: optional client-side filter on the row's 'stage' (the API filter is
            single-valued; 'open opportunities' = qualification + negotiation).
    text_contains: keep only rows where some string field contains this text
              (case-insensitive) — e.g. count calls whose defect is 'broken pasta'.
    """
    filters = filters or {}
    rows = fetch_all(path, **filters)
    if stages is not None:
        rows = [r for r in rows if r.get("stage") in stages]
    if text_contains:
        rows = [r for r in rows if _row_contains(r, text_contains)]

    sources_used = [path.lstrip("/")]
    chan_map: dict[str, str] = {}
    if group_by == "channel":
        chan_map = _customer_channel_map()
        sources_used.append("crm/customers")

    def key_of(r: dict[str, Any]) -> Any:
        if group_by == "channel":
            return chan_map.get(r.get("customer_id"), "unknown")
        return r.get(group_by) if group_by else None

    def val_of(r: dict[str, Any]) -> float:
        return float(r.get(value_field) or 0) if value_field else 0.0

    result: dict[str, Any] = {"op": op, "total_rows": len(rows), "_sources": sources_used}

    if group_by:
        groups: dict[Any, dict[str, float]] = {}
        for r in rows:
            g = groups.setdefault(key_of(r), {"count": 0, "sum": 0.0})
            g["count"] += 1
            g["sum"] += val_of(r)
        # Round sums; drop the sum key entirely for pure counts.
        result["groups"] = {
            str(k): ({"count": int(v["count"])} | ({"sum": round(v["sum"], 2)} if value_field else {}))
            for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))
        }
    else:
        result["count"] = len(rows)
        if value_field:
            result["sum"] = round(sum(val_of(r) for r in rows), 2)
    return result
