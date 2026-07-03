# DuitSplit — Pitch & Technical Guide

Everything you need to present, demo, and answer questions. Read top-to-bottom
once; keep the "Cheat sheet" and "Q&A" sections open during the QnA.

---

## 1. The 30-second pitch

**Problem:** Splitting a group restaurant bill is annoying and error-prone. Someone
squints at a paper receipt, types every item into a calculator or a notes app,
figures out who had what, works out each person's share of the tax, then chases
everyone for money over different e-wallets.

**Users:** Anyone who eats out in a group — students, colleagues, friends. Very
common in Malaysia (mamak, kopitiam) where bills are itemised and paid by one
person via a DuitNow/TNG QR.

**Solution:** DuitSplit. Snap the receipt + your payment QR. AI reads the receipt
into structured, editable items and tax. You add friends, tap items to assign them
(shared items split automatically), and DuitSplit computes each person's total incl.
their share of tax. It shows your QR so each friend scans and pays, and you tick
them off as they do. When everyone's paid, the session self-destructs.

**Why AI:** The input (a photo of a receipt) is *unstructured*. AI (Gemini Vision)
turns pixels into structured JSON — replacing manual data entry. The maths is plain
Python; AI is used *only* where the data is genuinely unstructured.

---

## 2. What we achieved (rubric self-assessment)

| Rubric area | What we did |
|---|---|
| **Problem & Impact (15%)** | Real, common problem; clear users; AI used where it's genuinely needed (unstructured image → structured data). |
| **Solution Quality (20%)** | Full end-to-end flow works: upload → review/edit → assign → summary → pay → complete. Structured, Pydantic-validated AI output. Failure cases handled (bad image, non-receipt, API failure, empty parse). |
| **Innovation (10%)** | Not a chatbot. Multi-step AI pipeline (OCR → validate → parse), per-unit item splitting, proportional tax, QR-based payment loop with paid tracking. |
| **Technical Implementation (25%)** | Clean Data/AI/Logic/App separation; Bronze→Silver→Gold ETL; env vars; try/except on every endpoint; Docker Compose; backend never exposed publicly; pinned deps; ruff; pathlib. |
| **Presentation & Demo (10%)** | This guide + a working demo. |
| **Feasibility & Realism (5%)** | README "Limitations" covers hallucinations, cost, privacy, scaling. |
| **Technical Understanding (15%)** | This document explains every file and line-level decision. |
| **Bonus (+25%)** | Caching (OCR cache), multi-step AI, multi-user (shareable session URL), rate limiting, performance/timing, auto-cleanup sweeper. |

---

## 3. Architecture at a glance

```
┌─────────────────────────────────────────────┐
│ Browser (Bootstrap 5 UI, one-page, 4 steps)  │
└───────────────┬─────────────────────────────┘
                │ HTTP (only port 8000 is public)
┌───────────────▼─────────────────────────────┐
│ FRONTEND  frontend/src/app.py   (FastAPI)     │
│  • serves index.html (Jinja2)                 │
│  • proxies /api/* → backend via httpx         │
│  • adds X-Forwarded-For (real client IP)      │
└───────────────┬─────────────────────────────┘
                │ internal Docker network (not public)
┌───────────────▼─────────────────────────────┐
│ BACKEND  backend/src/app.py     (FastAPI)     │
│  ├── ocr.py        DATA layer  (Gemini + ETL) │
│  ├── parser.py     AI layer    (extract+valid) │
│  ├── calculator.py LOGIC layer (pure Python)   │
│  ├── models.py     Pydantic schemas            │
│  └── SQLite  (sessions / friends / assignments │
│               / ocr_cache)                     │
└──────────────────────────────────────────────┘
```

Two containers, one bridge network. **Only the frontend publishes a port (8000).**
The backend (8001) is reachable *only* from the frontend inside the Docker network —
that's the "backend not exposed publicly" requirement.

---

## 4. The three-layer separation (say this in the demo)

- **Data layer — `ocr.py`:** turns *pixels into text*. Saves the raw image (Bronze),
  calls Gemini Vision, saves the extracted text (Silver). Also does the cheap local
  image pre-check and the OCR cache lookup.
- **AI layer — `parser.py`:** turns *text into a validated `Receipt`*. Two AI calls:
  one classifies "is this a receipt?", one extracts structured items + tax with
  confidence scores. Pydantic validates the output; on failure it returns an empty
  `Receipt` (fallback).
- **Logic layer — `calculator.py`:** *pure Python, no AI.* Splits items among
  friends, expands quantities into units, applies tax as a percentage. Deterministic
  and trivially testable.
- **Application layer — `app.py`:** FastAPI endpoints, SQLite, orchestration, error
  handling, rate limiting, ETL Gold write, cleanup.

Key line: *"AI only where the data is unstructured; the money maths is plain Python
so it's deterministic and explainable."*

---

## 5. ETL: Bronze → Silver → Gold (facilitator asked for this)

- **Bronze** = raw, untouched inputs. Receipt image → `data/bronze/{sid}_receipt.jpg`;
  QR image → `data/bronze/qr_{sid}.png`.
- **Silver** = cleaned/extracted intermediate. OCR text → `data/silver/{sid}.txt`.
- **Gold** = structured, business-ready. The validated session (receipt JSON, friends,
  who-owes-what) in **SQLite**.

All three are tied to a session and deleted when it completes (or is swept).

---

## 6. File-by-file — what each does and how it connects

### `backend/src/models.py` — the shared vocabulary
Pydantic models used by every other file (this is the "contract" between layers).
- `ReceiptItem` — `id` (unique per line, so duplicate names stay separate), `name`,
  `price` (line total), `quantity`, `confidence` (0–1).
- `Receipt` — `items`, `subtotal`, `tax_amount`, `tax_percent`, `tax_confidence`,
  `total`, `is_valid_receipt`, `validation_reason`, `cached`, `processing_ms`.
- `Friend` — `id`, `session_id`, `name`, `avatar_color`, `total_owed`, `is_paid`.
- `ItemShare` / `FriendShare` — a friend's computed items, subtotal, tax share, total.
- `Session` — a whole bill-split (receipt + friends + assignments + QR/silver paths).
- `BillSummary` — the final result: per-friend shares + Total/Paid/Remaining.

**Connects to:** everything imports from here. Pydantic gives free validation +
JSON (de)serialisation.

### `backend/src/ocr.py` — DATA layer
- `precheck_image(bytes)` → checks magic bytes + min size **before any API call**
  (saves quota on junk uploads).
- `save_bronze` / `save_silver` — write the ETL files with `pathlib.Path`.
- `extract_text_from_image(path)` → the actual Gemini Vision call, with **retry
  (3 attempts, 12s apart)** and empty-string fallback (never raises).
- `run_ocr_pipeline(sid, bytes)` → Bronze → OCR → Silver, returns `(text, elapsed_ms)`.

**Connects to:** called by `app.py`'s `/upload`. Uses `google-genai` + `GOOGLE_API_KEY`.

### `backend/src/parser.py` — AI layer
- `validate_receipt(text)` → Gemini classifier: "is this a receipt?" Returns
  `(bool, reason)`. **Fails open** (returns True on API error) so a good receipt is
  never wrongly rejected by a hiccup.
- `parse_receipt(text)` → Gemini extraction → JSON → **Pydantic `Receipt`**. Derives
  `tax_percent` from `tax_amount / subtotal`. Measures parse time. On any failure →
  empty `Receipt` (fallback). `_salvage()` recovers partial JSON.

**Connects to:** called by `/upload`. Returns a `Receipt` (models.py).

### `backend/src/calculator.py` — LOGIC layer (pure Python)
- `_expand_units(receipt)` → a qty-2 RM30 line becomes two RM15 **units**
  (`"{item_id}#0"`, `"{item_id}#1"`) so each unit can go to a different person.
- `compute_shares(receipt, assignments, friends)` → for each unit, split its price
  among assigned friends; sum per-friend subtotal; **tax = subtotal × tax_percent/100**.
- `calculate_bill(session)` → wraps it into a `BillSummary` with Total/Paid/Remaining.

**Connects to:** called by `/assign` and `/summary`. No AI, no I/O — just maths.

### `backend/src/app.py` — APPLICATION layer
FastAPI app + SQLite. Notable pieces:
- **`SCHEMA`** — 4 tables (`sessions`, `friends`, `assignments`, `ocr_cache`),
  all `CREATE TABLE IF NOT EXISTS`.
- **`get_db()`** — a context manager that opens SQLite, commits, and always closes.
- **`lifespan()`** — on startup: create tables + sweep old sessions + start the
  hourly sweeper; on shutdown: cancel the sweeper.
- **Rate limiting** — `slowapi`: 120/min globally, 15/min on `/upload`, keyed by the
  real client IP (from `X-Forwarded-For` set by the frontend proxy).
- **Endpoints** (all wrapped in try/except → JSON error, never crash):
  - `POST /upload` — the pipeline: pre-check → cache/OCR (Bronze+Silver) → validate →
    parse. Returns receipt + timing + `timeout_warning`.
  - `POST /sessions` — Gold write: persist the (edited) receipt to SQLite.
  - `GET /sessions/{id}` — load a full session.
  - `POST /sessions/{id}/friends` — add a friend (auto colour).
  - `POST /sessions/{id}/assign` — compute shares, write `assignments` rows.
  - `GET /sessions/{id}/summary` — compute the `BillSummary`.
  - `PATCH /sessions/{id}/friends/{fid}/paid` — mark paid; **delete session when all paid**.
  - `GET /sessions/{id}/qr` — serve the QR image file.
- **Helpers** — `_cache_get/_cache_put`, `_pick_color`, `_load_session` (rebuilds a
  `Session` from the 3 tables), `_delete_session`, `sweep_expired_sessions`.

**Connects to:** imports `ocr`, `parser`, `calculator`, `models`. Talks to SQLite.

### `frontend/src/app.py` — the proxy + page server
- `GET /` and `GET /session/{id}` → render `index.html` (Jinja2), injecting
  `backend_url` and `session_id`.
- `ANY /api/{path}` → forwards the request to the backend with `httpx.AsyncClient`,
  adding `X-Forwarded-For` (so rate-limiting sees the real user), stripping hop-by-hop
  headers. Returns the backend's response verbatim (works for JSON *and* the QR image).

**Why a proxy:** the browser only ever talks to the frontend; the backend stays
private. `BACKEND_URL` comes from an env var.

### `frontend/src/templates/index.html` — the whole UI
One page, four "step cards" shown/hidden by `showStep()`. Vanilla JS + Bootstrap 5.
- **Step 1 Upload:** two file inputs (receipt + QR), both required → `POST /api/upload`.
- **Step 2 Review:** editable items table + editable **Tax rate (%)**, confidence
  badges (green ≥70%, yellow <70%), low-confidence rows highlighted & auto-focused,
  live subtotal/total, "Extracted in Xs" timing → `POST /api/sessions`.
- **Step 3 Assign:** add friends (coloured avatars), pick active friend, tap **units**
  to assign; running total per friend → `POST /api/sessions/{id}/assign`.
- **Step 4 Summary:** per-friend items + tax share + total, one big QR at the bottom,
  Mark-as-Paid checkboxes → `PATCH .../paid`; Total/Paid/Remaining; Complete Session.

### Config / infra
- `docker-compose.yml` — 2 services on a bridge network; only `frontend` publishes
  `8000`; `backend` gets `GOOGLE_API_KEY`, `DB_PATH`, `DATA_DIR`, TTL envs and a
  `backend-data` volume for the DB + ETL files.
- `backend/Dockerfile`, `frontend/Dockerfile` — `python:3.14-slim`, install deps with
  `uv sync --frozen`.
- `.env` / `.env.example` — secrets & config (`.env` is git-ignored).
- `rate_limit.txt` — documents the rate-limit policy.

---

## 7. End-to-end flow (follow this in the demo)

1. **Upload** — browser sends receipt + QR to `/api/upload`. Frontend proxy forwards
   to backend `/upload`.
2. Backend: `precheck_image` (no API) → hash → `ocr_cache` lookup → if miss,
   `run_ocr_pipeline` saves Bronze, calls **Gemini OCR**, saves Silver →
   `validate_receipt` (**Gemini #2**) → `parse_receipt` (**Gemini #3**) → returns a
   Pydantic `Receipt` + timing.
3. **Review/edit** — user fixes any wrong item/price/tax → `/api/sessions` writes the
   **Gold** record to SQLite.
4. **Assign** — add friends (`/friends`), tap units, then `/assign` → `calculator`
   computes shares, backend stores `assignments` rows and each friend's `total_owed`.
5. **Summary** — `/summary` → `calculate_bill` → per-person totals + QR.
6. **Pay** — each friend scans the QR, pays, user ticks "Mark as Paid" (`PATCH .../paid`).
   When the last one is ticked, the backend **deletes the session** and its files.

---

## 8. Bonus features (call these out)

- **Caching:** `ocr_cache` table keyed by SHA-256 of the image — same photo skips the
  Gemini OCR call.
- **Multi-step AI:** three separate Gemini calls (OCR, validate, extract), not one prompt.
- **Multi-user:** shareable `/session/{id}` URL opens the same split on any device.
- **Rate limiting:** `slowapi`, per-IP, documented in `rate_limit.txt`.
- **Performance/observability:** OCR + parse timing shown to the user; >15s warning.
- **Auto-cleanup:** hourly sweeper deletes abandoned sessions (keeps DB lightweight).

---

## 9. Feasibility & limitations (have an answer ready)

- **Hallucinations:** model can misread prices; every item + tax has a confidence
  score, low-confidence rows are flagged & editable, and nothing is used without
  Pydantic validation.
- **Cost:** up to 3 Gemini calls per new receipt; caching removes the OCR call for
  repeats; pre-check avoids calls on junk uploads.
- **Privacy:** receipts/QRs are personal; files live only for the session's life and
  are deleted on completion; only text (not the image) is cached; nothing leaves except
  the Gemini call. Production would add auth + encryption + retention policy.
- **Scaling:** SQLite + in-memory rate limiter are single-node; scale-out = Postgres +
  Redis.
- **Speed:** bound by Gemini latency; shown to the user, warned if slow.

---

## 10. Likely Q&A

**Q: Where does the AI actually run / which model?**
`gemini-2.5-flash`, called from `ocr.py` (vision) and `parser.py` (validate + extract).

**Q: How do you handle a non-receipt or blank/corrupt image?**
Two gates: a free local `precheck_image` (format + size) before any API call, then an
AI `validate_receipt` classifier. Either can reject with a reason; no items are shown.

**Q: What if Gemini returns bad JSON or fails?**
`parse_receipt` catches it and returns an empty `Receipt` (fallback). `_salvage()`
tries to recover partial JSON first. The endpoint is in try/except → JSON error, no crash.

**Q: How is tax split?**
As a percentage of each person's own items: `friend_tax = friend_subtotal × tax%/100`.
The % is derived from the receipt and editable.

**Q: Two identical items / a qty-2 line?**
Each line has a unique `id`; a qty-N line is expanded into N units (`id#0`, `id#1`),
each individually assignable. Keeps duplicates independent.

**Q: Why a frontend proxy instead of calling the backend directly?**
Keeps the backend private (never exposed publicly), centralises config via
`BACKEND_URL`, and lets us attach the real client IP for rate limiting.

**Q: Why SQLite / is data permanent?**
Lightweight and file-based; sessions are intentionally temporary — deleted when paid
or swept after 24h. The OCR cache persists to save API calls.

**Q: How do layers stay separated / how would you test it?**
`calculator.py` is pure Python with no I/O or AI, so it's unit-testable with plain
dicts. The data and AI layers are isolated behind function boundaries.

**Q: Error handling?**
Every endpoint is wrapped in try/except and returns a meaningful JSON error; external
calls (Gemini, DB, files) degrade gracefully and never crash the server.

---

## 11. Cheat sheet (glance during QnA)

- Stack: **FastAPI × FastAPI**, SQLite, Gemini 2.5 Flash, Bootstrap 5, Docker Compose, uv, Python 3.14.
- Layers: `ocr.py` data · `parser.py` AI · `calculator.py` logic · `app.py` app · `models.py` schemas.
- ETL: Bronze (images) → Silver (text) → Gold (SQLite).
- 3 AI calls: OCR → validate → parse. 1 local pre-check before any of them.
- Ports: frontend **8000 (public)**, backend **8001 (private)**.
- Bonus: caching · multi-step AI · multi-user · rate limiting · timing · auto-sweep.
- Run: `docker compose up --build` → http://localhost:8000.
```
