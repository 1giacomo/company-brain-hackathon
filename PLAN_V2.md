# Plan — Company Brain v2.0: "Al Dente OS"

A desktop-OS-style shell over the v1 agent. Open the page → a **desktop** with a
dotted background and a few **discomorphism** (disco-ball / glassy) app icons.
Click an icon → it opens as a draggable **window**, like a simulated OS.

The v1 backend (agent loop, RAG, /ask, /graph, /kb, /files) stays **untouched and
functional** — v2 is a UI shell plus a handful of read-only data endpoints. The
v1 chat+graph experience becomes one of the apps.

## 0. Principles

- **No build step.** Vanilla HTML/CSS/JS, same as v1 (deploys as-is on Railway).
- **Reuse, don't rewrite.** The agent, `/ask`, `/graph`, `/kb/{id}` are done.
- **Each app is self-contained** and loaded into a window via `<iframe>` from its
  own route — modular, isolated styles/JS, and the existing brain UI drops in.
- **Functional first**, then the disco polish. Every app does something real.

## 1. The four apps

| App                | What it does                                                                                                                                                                   | Backed by                                 |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------- |
| **Company Brain**  | The v1 experience: chat (`/ask`) + the knowledge graph.                                                                                                                        | `/ask`, `/graph` (exist)                  |
| **Knowledge Base** | A filesystem-style browser over `data/kb/`: list docs, click to read the rendered doc.                                                                                         | `/api/kb/list` (new), `/kb/{id}` (exists) |
| **RAG**            | A retrieval playground: type a query → see which docs BM25 returns, their scores, and which hard-filter (SKU/Bio/format) fired. Makes the retrieval transparent.               | `/api/rag` (new)                          |
| **API Tables**     | Explore the Al Dente API tables (customers, opportunities, orders, invoices, calls, production-orders, inventory, suppliers, bom, shipments) in a paginated, filterable table. | `/api/explore/{table}` (new proxy)        |

## 2. Backend additions (`main.py` + small helpers)

All read-only, all HTTP 200. The browser can't call the Al Dente API directly
(the token is server-side), so **API Tables goes through a backend proxy**.

| Route                                               | Returns                                                                                                                                                                          |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /`                                             | the desktop shell (rewritten `index.html`)                                                                                                                                       |
| `GET /apps/{name}`                                  | serves `static/apps/{name}.html` (whitelist: brain, kb, rag, tables)                                                                                                             |
| `GET /api/kb/list`                                  | `[{doc_id, title, filename, bytes}]` parsed from `data/kb/` (reuse `kb._Doc`)                                                                                                    |
| `GET /api/rag?query=&k=`                            | `{filtered_doc_ids, results:[{doc_id,title,score}], filter_applied}` — extend `kb.py` with a `search_debug()` that also returns scores + which variant filter fired              |
| `GET /api/explore/{table}?limit=&offset=&<filters>` | proxy to the matching Al Dente endpoint via `aldente.get`, **whitelisted** table→path map, passes through documented filters + pagination, returns `{columns, data, pagination}` |

- Mount `static/` so app assets (shared css/js) serve cleanly, or keep per-route
  `FileResponse`. Whitelist app/table names (no path traversal — same guard as `/kb`).
- **Metering note:** API Tables hits the live API on every page/filter. Default to
  `limit=50`, cache identical proxy queries in-process, and surface `pagination.total`.

## 3. Desktop shell (`static/index.html` + `static/os/os.css`, `os.js`)

- **Dotted background:** `background-image: radial-gradient(circle, #2a2f3a 1px, transparent 1px); background-size: 22px 22px;` over the dark base.
- **Desktop icons:** absolutely-positioned grid, each = disco icon + label. Double-
  click (and single-tap) opens the app. Optional: a top menu bar (clock, "Al Dente OS").
- **Taskbar / dock** at the bottom: one button per open window (focus / restore).
- **Window manager** (`os.js`, ~150 lines, no deps):
    - `openApp(name)` → spawn a `.window` with titlebar (icon, title, ⌄ minimize, ✕ close)
      and a body `<iframe src="/apps/{name}">`.
    - **Drag** by titlebar (pointer events), **resize** via a corner grip, **focus**
      raises z-index, **minimize** to dock, **close** removes. Re-open focuses if already open.
    - Sensible default sizes per app; cascade new windows; clamp within viewport.
    - State kept in a small JS object; no persistence needed (nice-to-have: localStorage).

## 4. Discomorphism icons (the "disco-ball Spotify" look)

Per-app **SVG** icon (crisp at any size), one technique, recolored per app:

1. **Sphere base:** radial gradient (light top-left → saturated mid → dark rim) for volume.
2. **Disco facets:** an SVG `<pattern>` of small tiles (≈10×10 grid) with slight
   random lightness per tile, clipped to the circle; a soft `feGaussianBlur`/overlay
   sells the mirrored-mosaic feel. Add 2–3 bright "sparkle" tiles.
3. **Specular highlight:** a white radial blob top-left at low opacity (glassy).
4. **Rim shading:** inner shadow at the circle edge for sphericity.
5. **Glyph:** the app symbol in near-black, centered (Brain = Spotify-style arcs or a
   node cluster; KB = stacked files; RAG = magnifier over docs; Tables = grid).

- Per-app hue: Brain = violet, KB = amber/gold, RAG = teal, Tables = blue.
- Build one `discoIcon(hue, glyph)` generator (JS returns an inline SVG string) so all
  four are consistent; drop shadow + subtle hover scale in CSS.

## 5. App implementations (`static/apps/*.html`, shared `static/apps/app.css`)

- **brain.html** — move v1's chat + graph here (chat left, graph right), unchanged logic.
  Doc chips keep the in-window overlay viewer (or, in OS mode, could open the KB app — keep
  overlay for simplicity).
- **kb.html** — left: file list from `/api/kb/list` grouped by type (specs / policies /
  price list / capitolati, inferred from title); right: rendered doc in an iframe to
  `/kb/{id}`. A breadcrumb + search box filters the list.
- **rag.html** — query input → `GET /api/rag`; show the ranked docs with score bars and a
  badge for the hard-filter that fired ("SKU pin", "Bio", "500g"); click a result → open it.
- **tables.html** — a dropdown of the 10 tables; renders columns + rows from
  `/api/explore/{table}`; filter inputs for that table's documented filters; prev/next
  pager using `pagination.total`. Read-only.

## 6. Build order

1. **Window manager + desktop shell** with dotted bg and 4 placeholder icons that open
   empty windows. (Prove the OS feel first.)
2. **Move v1 UI → `/apps/brain`**; new `index.html` is the desktop. End-to-end: open
   Brain, ask a question. (Parity with v1.)
3. **KB app** + `/api/kb/list`. **RAG app** + `/api/rag` (+ `kb.search_debug`).
4. **API Tables** + `/api/explore/{table}` proxy (whitelist + pagination + cache).
5. **Discomorphism icons** — replace placeholders with the SVG disco icons; polish hover,
   shadows, taskbar.
6. Responsive fallback (small screens → windows maximize); redeploy; smoke-test all apps.

## 7. Non-goals / risks

- Not a real multi-user OS; no auth, no persistence beyond optional localStorage.
- `/ask` contract is **frozen** — do not touch it; the evaluator only cares about that.
- API Tables is metered: cache and default to small pages; never auto-load all tables at once.
- iframes per app keep styles isolated but mean cross-window state is independent — fine here.
