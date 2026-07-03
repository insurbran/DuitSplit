# DuitSplit — Receipt Bill Splitter

Upload a receipt photo and your DuitNow/TNG QR code, let AI read the receipt into
structured items and tax, assign items to friends, and DuitSplit computes exactly
what each person owes (tax included) and shows your QR so everyone can pay.

---

## 1. Project Overview

### Problem statement
Splitting a group restaurant bill is slow and error-prone. One person squints at a
paper receipt, types every item into a calculator or notes app, works out who had
what, manually splits the tax, and then chases everyone for money. It's tedious and
mistakes are common — especially with itemised receipts and shared dishes.

### Target users
Anyone who eats out in groups — students, colleagues, friends. Especially common in
Malaysia (mamak, kopitiam, food courts) where one person pays the whole bill via a
DuitNow / Touch 'n Go QR and collects from everyone afterwards.

### System goal
Turn a photo of a receipt into a fair, itemised split in under a minute:
1. Extract items, quantities, and tax from the receipt image using AI.
2. Let the payer correct anything and assign items to friends (shared items split).
3. Compute each person's subtotal + proportional tax = total owed.
4. Show the payer's QR so each friend pays, and track who has paid.

---

## 2. System Architecture

### Data flow (input → processing → output)

```
INPUT                         PROCESSING                                OUTPUT
─────                         ──────────                                ──────
Receipt photo  ┐   ┌── local pre-check (no AI) ──┐
               ├──▶│   OCR  (Gemini Vision)       │  Bronze: raw image
QR code image  ┘   │   ▼                          │  Silver: OCR text
                   │   validate (Gemini)          │
                   │   ▼                           │
                   │   parse → Pydantic Receipt    │  Gold: SQLite session   ──▶  Editable items + tax
                   └───────────────────────────────┘
Assignments   ─────▶  calculator (pure Python: split units, proportional tax)  ──▶  Per-person totals
Mark as paid  ─────▶  update SQLite, delete session when all paid              ──▶  QR + Total/Paid/Remaining
```

- **Input:** receipt image + QR image (uploaded together), then friend assignments and
  paid ticks.
- **Processing:** a local image check, then three AI steps (OCR → validate → parse),
  then pure-Python maths for the split.
- **Output:** structured, editable items; a per-person bill summary; a QR to pay; and
  live payment tracking.

### Module breakdown

| Layer | File | Responsibility |
|---|---|---|
| Data | `backend/src/ocr.py` | Image pre-check, Gemini Vision OCR, Bronze/Silver ETL, timing |
| AI | `backend/src/parser.py` | Receipt validation + structured extraction (Pydantic), fallback |
| Logic | `backend/src/calculator.py` | Pure-Python: split units, proportional tax, totals |
| Models | `backend/src/models.py` | Pydantic schemas shared across all layers |
| Application | `backend/src/app.py` | FastAPI endpoints, SQLite, orchestration, rate limiting, cleanup |
| Frontend | `frontend/src/app.py` | Serves the UI (Jinja2) and proxies `/api/*` to the private backend |
| UI | `frontend/src/templates/index.html` | 4-step single-page app (Bootstrap 5) |

**Two containers, one network.** Only the **frontend** publishes a port (8000). The
**backend** (8001) is reachable only from the frontend over the internal Docker
network, so it is never exposed publicly. The frontend proxies every `/api/*` call to
the backend, so the browser only ever talks to the frontend.

---

## 3. Setup & Installation

### Dependencies / environment
- [uv](https://docs.astral.sh/uv/) (package manager), Python **3.14**
- A Google Gemini API key — https://aistudio.google.com/app/apikey
- Docker + Docker Compose (for the containerised run)

Configuration is via environment variables (loaded with `python-dotenv`):

```bash
cp .env.example .env      # then edit .env and set GOOGLE_API_KEY
```

| Variable | Purpose | Default |
|---|---|---|
| `GOOGLE_API_KEY` | Gemini API key (required for OCR/parse) | — |
| `DB_PATH` | SQLite file path | `data/duitsplit.db` |
| `DATA_DIR` | Root for Bronze/Silver files | `data` |
| `BACKEND_URL` | Frontend → backend URL | `http://backend:8001` |
| `SESSION_TTL_HOURS` | Auto-sweep sessions older than this (0 = off) | `24` |
| `SWEEP_INTERVAL_SECONDS` | How often the sweeper runs | `3600` |

### Run with Docker (recommended)

```bash
# one-time: generate lockfiles the Dockerfiles need
cd backend  && uv lock && cd ..
cd frontend && uv lock && cd ..

docker compose up --build
```

Open **http://localhost:8000**.

### Run locally (without Docker)

```bash
# Terminal 1 — backend on :8001
cd backend
uv sync
uv run uvicorn app:app --app-dir src --host 0.0.0.0 --port 8001

# Terminal 2 — frontend on :8000
cd frontend
echo "BACKEND_URL=http://localhost:8001" >> .env
uv sync
uv run uvicorn app:app --app-dir src --host 0.0.0.0 --port 8000
```

### Access from a phone (same Wi-Fi)
Find your PC's LAN IP (`ipconfig` → the adapter with a Default Gateway, e.g.
`192.168.1.42`) and open `http://192.168.1.42:8000` on the phone.

### Lint (engineering standard)
```bash
cd backend  && uv run ruff check . && uv run ruff format . && cd ..
cd frontend && uv run ruff check . && uv run ruff format . && cd ..
```

---

## 4. Features

- **Dual upload (receipt + QR).** Both are uploaded together. The receipt goes through
  the AI pipeline; the QR is saved as-is (no AI) and served back for payment.
- **Local image pre-check.** Before any AI call, the upload is validated (image format
  + minimum size). Junk/corrupt files are rejected instantly, wasting zero API quota.
- **AI OCR (Gemini Vision).** Transcribes the receipt image to raw text, with retry
  (3 attempts, 12s apart) and a graceful empty fallback.
- **AI receipt validation.** A separate classifier decides "is this actually a
  receipt?" and rejects non-receipts with a reason. Fails open so a good receipt is
  never wrongly rejected by a transient error.
- **AI structured extraction.** Items (name, quantity, price) **and tax** are extracted
  as JSON and validated by Pydantic before use. Each item gets a confidence score.
- **Confidence badges + editable review.** Every item and the tax show a confidence
  badge (green ≥70%, yellow <70%). Everything is editable; low-confidence rows are
  highlighted and auto-focused so mistakes get corrected.
- **Per-unit item assignment.** A quantity-N line is expanded into N individually
  assignable units (a qty-2 RM30 line becomes two RM15 units), and each line has a
  unique id so duplicate names stay independent. Items can be shared between friends.
- **Proportional tax split.** Tax is applied as a percentage of each friend's own item
  subtotal: `friend_tax = friend_subtotal × tax% / 100`.
- **QR payment + paid tracking.** The summary shows each person's total and the payer's
  QR; each friend has a "Mark as Paid" checkbox and a live Total / Paid / Remaining tally.
- **Auto-complete + cleanup.** When everyone is marked paid, the session (and its files)
  is deleted. Abandoned sessions are also swept automatically after a TTL.
- **OCR caching.** Extracted text is cached by image hash; re-uploading the same photo
  skips the Gemini OCR call.
- **Multi-user.** Sessions live in SQLite behind a shareable `/session/{id}` URL, so
  friends can open the same split on their own devices.
- **Rate limiting.** Per-IP limits via `slowapi` (120/min global, 15/min on `/upload`);
  policy documented in `rate_limit.txt`.
- **Processing-time feedback.** OCR + parse time is shown ("Extracted in 2.3s") with a
  warning if Gemini takes over 15 seconds.

### API summary

| Method | Path | Purpose |
|---|---|---|
| POST | `/upload` | Receipt + QR → OCR, save Bronze/Silver, return parsed receipt |
| POST | `/sessions` | Persist the (edited) receipt to SQLite (Gold) |
| GET | `/sessions/{id}` | Load a session |
| POST | `/sessions/{id}/friends` | Add a friend |
| POST | `/sessions/{id}/assign` | Assign items, compute tax shares |
| GET | `/sessions/{id}/summary` | Full bill summary with QR |
| PATCH | `/sessions/{id}/friends/{fid}/paid` | Mark paid; delete session if all paid |
| GET | `/sessions/{id}/qr` | Serve the QR image |

---

## 5. Technical Decisions

- **Two-service split (frontend proxy + private backend).** The browser only talks to
  the frontend, which proxies `/api/*` to the backend over the internal Docker network.
  *Trade-off:* one extra hop and a bit more code, in exchange for keeping the backend
  private, centralising config via `BACKEND_URL`, and being able to attach the real
  client IP for rate limiting.
- **Strict layer separation (Data / AI / Logic / App).** AI is used only where the data
  is unstructured (image → JSON). All money maths lives in `calculator.py` as pure
  Python. *Trade-off:* more files, but each layer is independently testable and the
  math is deterministic and explainable.
- **Bronze → Silver → Gold ETL.** Raw images (Bronze) and OCR text (Silver) are kept as
  files; structured sessions (Gold) live in SQLite. *Trade-off:* extra disk writes, but
  a clear, auditable pipeline and easy debugging (you can inspect exactly what the OCR saw).
- **SQLite + temporary sessions.** Lightweight, file-based, zero-config. Sessions are
  intentionally disposable (deleted when paid or after a TTL). *Trade-off:* single-node
  only — not built for concurrent multi-server scale.
- **Two-pass AI + Pydantic validation + fallback.** Validate then extract, and never
  trust model output without schema validation; on failure return an empty `Receipt`.
  *Trade-off:* an extra Gemini call per upload (cost) for much higher reliability.
- **Unique per-item ids + unit expansion.** Assignments key on item/unit id, not name,
  so duplicate lines and multi-quantity items behave correctly. *Trade-off:* slightly
  more data plumbing for correct splitting.
- **`gemini-2.5-flash`.** Fast and cheap, good enough for receipt OCR. *Trade-off:*
  lower accuracy than a larger model, mitigated by the editable review step.
- **Pinned deps, uv, Python 3.14, ruff, pathlib, dotenv.** Reproducible builds and
  consistent engineering standards.

---

## 6. Limitations

### Known issues
- **Hallucinations / OCR errors.** The model can misread prices or occasionally merge
  duplicate lines. Mitigated by confidence badges, the editable review step, and
  Pydantic validation — but a human should still check before confirming.
- **AI cost.** Up to three Gemini calls per new receipt (OCR, validate, parse). Caching
  removes the OCR call for repeats and the pre-check avoids calls on junk uploads.
- **Speed.** Extraction is bound by Gemini latency (usually a few seconds, sometimes
  >15s). Shown to the user and flagged when slow.
- **Single-node.** SQLite and the in-memory rate limiter don't scale horizontally.
- **Privacy.** Receipts/QRs contain personal data; files live only for the session's
  lifetime and are deleted on completion, but there's no auth yet.
- **Splitting granularity.** A shared item is split equally among its assigned friends;
  there's no "I only had a third of it" weighting.

### Future improvements
- **📷 Camera capture.** Let users snap the receipt and QR directly from the phone
  camera in-app (using the browser `getUserMedia` / `<input capture>` API), instead of
  uploading a saved photo — faster and more natural on mobile.
- **Auth & accounts** so people can see past splits and outstanding debts.
- **Blank/blurry-image detection** before the API call (add Pillow) to catch poor
  photos, not just wrong file types.
- **Weighted/uneven splits** (e.g. someone had half a dish).
- **Postgres + Redis** for horizontal scaling and a shared rate limiter.
- **Direct payment integration** (DuitNow API) instead of manual QR scanning.
- **Optional item-name translation/romanisation** for mixed-language receipts.
- **CI/CD + monitoring** (tests on push, basic request/latency dashboards).
```
