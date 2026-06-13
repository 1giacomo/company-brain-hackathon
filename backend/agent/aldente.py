"""Al Dente mock-API client.

Thin, efficient HTTP layer over the read-only Al Dente APIs. The agent never
pages or sums by hand: `fetch_all` walks pagination and `aggregate` computes
counts / sums / group-bys in Python (see PLAN.md §3). Field names here were
confirmed against live responses during the step-0 probe.
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Iterable

import httpx

BASE_URL = os.environ.get("MOCK_API_BASE_URL", "https://aldente.yellowtest.it").rstrip("/")
TOKEN = os.environ.get("MOCK_API_TOKEN", "")
MAX_LIMIT = 200  # hard cap documented in API.md

# Short timeout: the API normally responds <1s. Tool execution happens between
# LLM calls and isn't budget-checked mid-round, so a stalled API call must fail
# fast rather than eat the 30s wall (PLAN_V3.md §A).
_client = httpx.Client(
    base_url=BASE_URL,
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=httpx.Timeout(7.0, connect=4.0),
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
    """Pull only the segments matching `search` (and optional speaker). If the
    search matches nothing, broaden once (drop the speaker filter, then return
    the opening segments as context) rather than leaving the agent empty-handed —
    still bounded, never the full transcript."""
    env = get(f"/calls/{call_id}/transcript", search=search, speaker=speaker, limit=limit)
    segs = env.get("segments", [])
    total = env.get("pagination", {}).get("total", 0)
    fallback = None
    if not segs:
        if speaker:  # retry without the speaker filter
            env = get(f"/calls/{call_id}/transcript", search=search, limit=limit)
            segs = env.get("segments", [])
            total = env.get("pagination", {}).get("total", 0)
        if not segs:  # still nothing → opening segments for context
            ctx = get(f"/calls/{call_id}/transcript", limit=8)
            segs = ctx.get("segments", [])
            fallback = "no segments matched the search term; showing the opening segments"
    out = {"call_id": call_id, "matched_segments": segs, "total_matches": total}
    if fallback:
        out["note"] = fallback
    return out


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


_LEGAL = re.compile(r"\b(S\.?p\.?A\.?|S\.?r\.?l\.?|S\.?n\.?c\.?|S\.?a\.?s\.?)\b", re.I)


def _fold(s: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace — for fuzzy
    comparison (the API's own `search` is exact-match & case-sensitive)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return _WS.sub(" ", s).strip()


def _name_variants(name: str) -> list[str]:
    """Spelling variants to try against the exact-match API `search` before
    falling back to fuzzy matching."""
    n = name.strip()
    variants = [n]
    core = _LEGAL.sub("", n).strip()
    if core and core != n:
        variants.append(core)
    base = variants[-1]
    # Split CamelCase, collapse/strip spaces. (No single-token probe — a common
    # word like "Supermercati" would falsely match many customers via the API's
    # substring search and break the missing-customer trap.)
    spaced = _WS.sub(" ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)).strip()
    nospace = base.replace(" ", "")
    for v in (spaced, nospace):
        if v and v not in variants:
            variants.append(v)
    return variants


_CUSTOMERS_CACHE: list[dict[str, Any]] | None = None


def _all_customers() -> list[dict[str, Any]]:
    """All customers, fetched once and cached (66 rows; cheap). Backs the fuzzy
    fallback so a misspelled/cased name still resolves instead of 404-ing."""
    global _CUSTOMERS_CACHE
    if _CUSTOMERS_CACHE is None:
        _CUSTOMERS_CACHE = fetch_all("/crm/customers")
    return _CUSTOMERS_CACHE


def _fuzzy_customers(name: str) -> list[dict[str, Any]]:
    """Rank customers by distinctive substring / high token overlap against a
    folded query. Deliberately strict so a non-existent customer (a trap) does
    NOT match just because it shares a generic word like 'Supermercati'."""
    q_core = _fold(_LEGAL.sub("", name))
    q_nospace = q_core.replace(" ", "")
    q_tokens = set(t for t in q_core.split(" ") if len(t) > 2)
    scored: list[tuple[float, dict[str, Any]]] = []
    for c in _all_customers():
        cn = _fold(_LEGAL.sub("", c.get("company_name", "")))
        if not cn:
            continue
        cn_nospace = cn.replace(" ", "")
        score = 0.0
        # Distinctive substring match (ignoring spacing), e.g. 'granmercato'.
        if len(q_nospace) >= 4 and (q_nospace in cn_nospace or cn_nospace in q_nospace):
            score = 3.0
        else:
            c_tokens = set(t for t in cn.split(" ") if len(t) > 2)
            if q_tokens and c_tokens:
                ratio = len(q_tokens & c_tokens) / len(q_tokens)
                # Require a strong overlap so a single shared generic word fails.
                if ratio >= 0.6:
                    score = 2.0 * ratio
        if score >= 2.0:
            scored.append((score, c))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [c for _, c in scored][:5]


def search_customers(name: str | None = None, *, channel: str | None = None,
                     status: str | None = None) -> dict[str, Any]:
    """Search customers. Tries the API's exact-match `search` across spelling
    variants, then a cached fuzzy fallback. Returns the matched customers (with
    their ids) so the agent can chain or honestly report 'not found'."""
    if not name:
        rows = fetch_all("/crm/customers", channel=channel, status=status)
        return {"data": rows, "total": len(rows)}

    tried: list[str] = []
    for variant in _name_variants(name):
        tried.append(variant)
        rows = fetch_all("/crm/customers", search=variant, channel=channel, status=status)
        if rows:
            return {"data": rows, "total": len(rows), "matched_variant": variant}

    # Fuzzy fallback over the full (cached) customer list.
    fuzzy = _fuzzy_customers(name)
    if channel:
        fuzzy = [c for c in fuzzy if c.get("channel") == channel]
    if status:
        fuzzy = [c for c in fuzzy if c.get("status") == status]
    if fuzzy:
        return {"data": fuzzy, "total": len(fuzzy), "match": "fuzzy",
                "note": "No exact match; these are the closest customers by name. "
                        "Confirm the name matches before answering."}
    return {"data": [], "total": 0, "tried_variants": tried,
            "note": f"No customer matching '{name}' exists in the CRM."}


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
