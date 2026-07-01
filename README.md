# DuitSplit — Receipt Bill Splitter

Snap a photo of a receipt, let Gemini Vision pull out the line items, assign each
item to whoever ordered it, and DuitSplit works out exactly what everyone owes —
tax included, split proportionally.

## Architecture

```
frontend (FastAPI + Jinja2 + Bootstrap)   :8000
   │  proxies /api/* via httpx.AsyncClient
   ▼
backend  (FastAPI + SQLite)                :8001
   ├── ocr.py        data layer   — Gemini Vision (gemini-2.5-flash)
   ├── parser.py     AI layer     — structured item extraction + confidence
   ├── calculator.py logic layer  — pure-Python bill splitting
   └── models.py     Pydantic models
```

Layering is deliberate: the **data layer** (`ocr.py`) only turns pixels into text,
the **AI layer** (`parser.py`) only turns text into a validated `Receipt`, and the
**logic layer** (`calculator.py`) is pure Python with no AI — it just does math and
is trivially unit-testable.

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

| Method | Path                          | Purpose                                    |
|--------|-------------------------------|--------------------------------------------|
| POST   | `/ocr`                        | Upload an image → returns a `Receipt`      |
| POST   | `/friends`                    | Create a friend                            |
| GET    | `/friends`                    | List all friends                           |
| DELETE | `/friends/{id}`               | Delete a friend                            |
| POST   | `/sessions`                   | Create a split session from a receipt      |
| GET    | `/sessions/{id}`              | Load a session with assignments            |
| POST   | `/sessions/{id}/assign`       | Set item → friend assignments              |
| GET    | `/sessions/{id}/summary`      | Compute each friend's share (`BillSummary`)|

## How the split works

1. Each item's price is divided **equally** among the friends assigned to it.
2. Tax is then distributed **proportionally** to each friend's share of the
   assigned subtotal.
3. Unassigned items are charged to no one (so the split total can be less than the
   receipt total until everything is assigned).

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
