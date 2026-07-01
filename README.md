# DuitSplit — Receipt Bill Splitter

Upload a receipt photo plus your DuitNow/TNG QR code, let Gemini Vision pull out
the line items and tax, assign each item to whoever ordered it, and DuitSplit works
out exactly what everyone owes — tax distributed proportionally — then shows your QR
so each friend can pay and be ticked off as they do.

## Why AI is used

AI extracts and parses **unstructured** receipt image data into structured JSON that
the application can use for bill-splitting calculations — replacing manual data
entry. A person would otherwise type every item and price by hand; Gemini Vision
turns a photo into validated, editable line items in seconds. The maths itself
(`calculator.py`) is deliberately plain Python — AI is used only where the input is
genuinely unstructured.

## Architecture

```
frontend (FastAPI + Jinja2 + Bootstrap)   :8000
   │  proxies /api/* via httpx.AsyncClient (backend is never exposed publicly)
   ▼
backend  (FastAPI + SQLite)                :8001
   ├── ocr.py        data layer   — Gemini Vision + Bronze/Silver ETL + cache
   ├── parser.py     AI layer     — item + tax extraction, confidence, validation
   ├── calculator.py logic layer  — pure-Python proportional split
   └── models.py     Pydantic models
```

Layering is deliberate: the **data layer** (`ocr.py`) only turns pixels into text,
the **AI layer** (`parser.py`) only turns text into a validated `Receipt`, and the
**logic layer** (`calculator.py`) is pure Python with no AI — it just does math and
is trivially unit-testable.

### ETL: Bronze → Silver → Gold

- **Bronze** — the raw receipt image (`data/bronze/{session_id}_receipt.jpg`) and
  the QR image (`data/bronze/qr_{session_id}.png`) are saved as-is.
- **Silver** — the extracted OCR text is saved to `data/silver/{session_id}.txt`.
- **Gold** — the structured, validated session (receipt, friends, who-owes-what)
  is loaded into SQLite.

### Multi-step AI pipeline

Before any AI runs, a **local pre-check** (`ocr.precheck_image`) verifies the upload
is a real, non-trivial image (magic-byte format + minimum size). Invalid or corrupt
files are rejected instantly, so they never cost a Gemini call.

Each valid image then runs through three distinct AI-assisted steps, not a single
prompt:

1. **OCR** (`ocr.py`) — Gemini Vision transcribes the image to raw text. Results
   are cached in SQLite by image hash, so re-uploading the same photo skips the
   call entirely.
2. **Validation** (`parser.validate_receipt`) — a classifier pass decides whether
   the text is actually a receipt. Non-receipts are rejected with a reason.
3. **Extraction** (`parser.parse_receipt`) — structured items **and tax** with
   confidence scores, validated by Pydantic before anything downstream uses them.

OCR and parse time are measured and shown to the user ("Extracted in 2.3s"); a
warning appears if Gemini takes longer than 15 seconds.

## Engineering standards

- Python **3.14** (`.python-version` pinned to `3.14`)
- **uv** for all package management; every dependency pinned to an exact version
- `ruff==0.15.*` as a dev dependency (run `ruff check` and `ruff format` before commits)
- `pathlib.Path` for all filesystem paths
- `python-dotenv` for all configuration
- All endpoints and external calls wrapped in `try/except` — the app degrades
  gracefully and never crashes on bad input or API failures
- `.env` is git-ignored; `.env.example` is provided
- Dockerfiles use `uv sync --frozen`

## Prerequisites

- [uv](https://docs.astral.sh/uv/) installed
- A Google Gemini API key — https://aistudio.google.com/app/apikey
- Docker + Docker Compose (for the containerised run)

## Setup

```bash
cp .env.example .env
# edit .env and set GOOGLE_API_KEY
```

### Generate the lockfiles (one-time)

The Dockerfiles use `uv sync --frozen`, which requires a committed `uv.lock` in each
service directory. Generate them once:

```bash
cd backend  && uv lock && cd ..
cd frontend && uv lock && cd ..
```

## Run with Docker (recommended)

```bash
docker compose up --build
```

Then open http://localhost:8000.

## Run locally (without Docker)

Backend:

```bash
cd backend
uv sync
uv run uvicorn app:app --app-dir src --host 0.0.0.0 --port 8001
```

Frontend (in a second terminal):

```bash
cd frontend
# point the proxy at the local backend
echo "BACKEND_URL=http://localhost:8001" >> .env
uv sync
uv run uvicorn app:app --app-dir src --host 0.0.0.0 --port 8000
```

## API

| Method | Path                                     | Purpose                                       |
|--------|------------------------------------------|-----------------------------------------------|
| POST   | `/upload`                                | Upload receipt + QR → OCR, save bronze/silver |
| POST   | `/sessions`                              | Create session with parsed (edited) receipt   |
| GET    | `/sessions/{id}`                         | Get session details                           |
| POST   | `/sessions/{id}/friends`                 | Add a friend to the session                   |
| POST   | `/sessions/{id}/assign`                  | Assign items, compute tax shares              |
| GET    | `/sessions/{id}/summary`                 | Full bill summary with QR path                |
| PATCH  | `/sessions/{id}/friends/{fid}/paid`      | Mark friend paid; delete session if all paid  |
| GET    | `/sessions/{id}/qr`                       | Serve the QR image file                       |

The frontend also serves a shareable **`GET /session/{id}`** page so friends can
open the same split (summary + QR) on their own phones.

`POST /upload` returns the `session_id`, a `Receipt` (with `is_valid_receipt`,
`tax_amount`, `tax_confidence`, `cached`), `qr_image_path`, `processing_ms`, and a
`timeout_warning` flag.

## How the split works

0. A line item with quantity N is expanded into N individually-assignable units
   (a qty-2 RM30 line becomes two RM15 units).
1. Each unit's price is divided **equally** among the friends assigned to it.
2. Tax is applied as a **percentage** of each friend's own item subtotal (the tax
   rate is derived from the receipt and editable in Step 2):

   ```
   friend_tax   = friend_subtotal * (tax_percent / 100)
   friend_total = friend_subtotal + friend_tax
   ```
3. Unassigned items are charged to no one (so the split total can be less than the
   receipt total until everything is assigned).

Sessions are **temporary**: once every friend is marked paid, the session and its
Bronze/Silver files are deleted, keeping the database lightweight. Sessions that are
never completed are also swept automatically — on startup and hourly, any session
older than `SESSION_TTL_HOURS` (default 24h) is removed along with its files.

## Feature flow

1. **Upload** — receipt + QR together; receipt goes through OCR, QR is stored as-is.
2. **Review** — editable items/quantities/prices and a separately-extracted,
   editable tax amount, each with a confidence badge. Low-confidence rows are
   highlighted and auto-focused.
3. **Assign** — add friends as coloured avatars, tap items to assign (shareable),
   running totals update live.
4. **Summary** — per-friend items, tax share, and total; your QR with
   "Open TNG → Scan → Pay RM{amount}"; a *Mark as Paid* checkbox each; and a
   Total / Paid / Remaining tally. When all are paid, **Complete Session** clears it.

## Bonus features

- **OCR caching** — text stored in `ocr_cache` keyed by image hash; a repeat photo
  skips the Gemini OCR call.
- **Receipt validation** — a dedicated AI step rejects non-receipt images.
- **Payment tracking** — per-friend paid flags with auto-cleanup of finished sessions.
- **Multi-user** — sessions live in SQLite behind a shareable URL.
- **Rate limiting** — per-IP limits via `slowapi` (see `rate_limit.txt`).

## Limitations & realism

- **Hallucinations** — the model can misread prices or invent items. Every item and
  the tax carry a confidence score, low-confidence rows are flagged and made
  editable, and the logic layer never trusts AI output without Pydantic validation.
- **Speed** — extraction is bound by Gemini latency (typically a few seconds, but it
  can exceed 15s). Processing time is shown to the user and slow calls raise a
  warning. This is a known limitation, not a bug.
- **Cost** — each new receipt makes up to three Gemini calls (OCR, validate, parse).
  Caching removes the OCR call for repeat images; a production build would also cache
  parse results and batch requests.
- **Privacy** — receipts and QR codes contain personal data. Images are stored only
  under `data/bronze/` for the session's lifetime and deleted when it completes;
  nothing is sent anywhere except Google Gemini. For real deployment, add auth,
  encryption at rest, and a retention policy.
- **Scaling** — SQLite and the in-memory rate limiter are single-node. Scaling out
  means moving to Postgres and a shared limiter store (e.g. Redis).

## Notes on tooling versions

Python 3.14 is recent enough that some pinned libraries only ship wheels in newer
releases (e.g. `pydantic` 2.12+). The Docker images use a current `uv` for that
reason; older `uv` predates 3.14 support.

## Linting

```bash
cd backend  && uv run ruff check . && uv run ruff format .
cd frontend && uv run ruff check . && uv run ruff format .
```

## Project layout

```
duitsplit/
├── backend/   FastAPI API, OCR, parser, calculator, models, Dockerfile
├── frontend/  FastAPI proxy + Jinja2 UI, Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```
