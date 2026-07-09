# DuitSplit — Q&A Prep (rehearse these out loud)

Short, say-it-out-loud answers for the 15-minute QnA. Grouped by what judges tend to
probe. If you blank, fall back to the one-liner at the very bottom.

---

## Architecture & flow

**Q: Walk me through the system from input to output.**
User uploads a receipt photo + their payment QR. The frontend proxies it to the
backend. The backend does a free local image check, then Gemini OCR reads the image to
text (saved as Bronze image + Silver text), a second AI call checks it's really a
receipt, and a third extracts structured items + tax as JSON, validated by Pydantic.
The user edits anything wrong, adds friends, taps items to assign them; we compute each
person's share plus proportional tax; the summary shows totals and the QR to pay, and
we tick people off as they pay. When all are paid, the session self-deletes.

**Q: Why two services (frontend and backend) instead of one?**
The frontend serves the UI and proxies all `/api/*` calls to the backend server-side.
That keeps the backend private — only the frontend publishes a port — centralises
config via `BACKEND_URL`, and lets us attach the real client IP for rate limiting.

**Q: How do the layers separate?**
`ocr.py` is the data layer (pixels → text), `parser.py` is the AI layer (text →
validated Receipt), `calculator.py` is pure-Python logic (the money maths), and
`app.py` is the application layer that wires them together and talks to SQLite. AI is
used only where the input is unstructured; the maths is deterministic Python.

**Q: Why do you have a proxy — isn't that extra work?**
Yes, one extra hop, but it keeps the backend off the public network and means the
browser never makes a cross-origin request. That's also why we don't need CORS.

---

## The AI component

**Q: Which model, and where does it run?**
`gemini-2.5-flash`, called from `ocr.py` (vision OCR) and `parser.py` (validate +
extract). Three AI calls per new receipt.

**Q: Why three AI calls instead of one big prompt?**
Separation of concerns and reliability: OCR just transcribes, one call decides "is this
a receipt?", one extracts structured data. Each is simple, easier to debug, and we can
reject non-receipts before wasting effort.

**Q: How do you stop the AI from breaking the app with bad output?**
Two things. Nothing is trusted until it passes Pydantic validation (`Receipt`). And
every AI call has a fallback: `parse_receipt` returns an empty Receipt on any error,
`validate_receipt` "fails open" (assumes valid) so a hiccup never wrongly rejects a good
receipt. Every endpoint is also wrapped in try/except.

**Q: What about hallucinations / wrong prices?**
Every item and the tax carry a confidence score shown as a badge; low-confidence rows
are highlighted and editable, so a human corrects them before confirming. The logic
layer never trusts raw AI output.

**Q: Is the AI doing the maths?**
No. AI only turns the image into structured JSON. All splitting and tax maths is plain
Python in `calculator.py` — deterministic and testable.

---

## Data, ETL & storage

**Q: What's your data pipeline?**
Bronze → Silver → Gold. Bronze = raw receipt + QR images on disk. Silver = the OCR text
file. Gold = the structured, validated session in SQLite (sessions, friends,
assignments). `ocr.py` writes Bronze/Silver; `app.py` writes Gold.

**Q: What is "Gold"?**
The clean, query-ready business data — our SQLite rows. It's the final form the app
actually uses to compute the bill.

**Q: Where is the data actually stored?**
The backend writes to `/app/data`, which is mounted to a Docker named volume
`backend-data`. So data lives in a Docker-managed volume, not inside the container — it
survives rebuilds and is only wiped by `docker compose down -v`.

**Q: So it's in the container?**
The app runs in the container, but the data is in the volume Docker plugs into it.
Container is disposable; volume persists. Like a laptop vs an external drive.

**Q: Show me it's real.**
`docker compose exec backend python scripts/db_dump.py` — prints every table and lists
the Bronze/Silver files. Or open DevTools Network tab and watch the live API calls.

**Q: Why SQLite?**
Lightweight, file-based, zero-config, perfect for a temporary-session app. Trade-off:
single-node — I'd move to Postgres to scale out.

---

## Features / logic details

**Q: How is tax split?**
As a percentage of each person's own items: `friend_tax = friend_subtotal × tax% / 100`.
The rate is derived from the receipt and editable.

**Q: Two identical items, or a quantity-2 line?**
Every line has a unique id, and a qty-N line is expanded into N units (`id#0`, `id#1`),
each individually assignable — so duplicates and multi-quantity items work correctly.

**Q: What happens when everyone pays?**
Each "Mark as Paid" hits `PATCH .../paid`. When the last one is ticked, the backend
deletes the session and its files — keeps the DB lightweight.

**Q: What if a session is abandoned?**
A background sweeper runs on startup and hourly, deleting sessions older than 24h
(configurable).

**Q: Caching?**
OCR text is cached by SHA-256 of the image in an `ocr_cache` table; the same photo skips
the Gemini OCR call.

**Q: Rate limiting?**
`slowapi`: 120 requests/min globally, 15/min on `/upload`, keyed by the real client IP
(via the X-Forwarded-For header the proxy sets).

**Q: Multi-user?**
Sessions live in SQLite behind a shareable `/session/{id}` URL, so friends can open the
same split on their own devices.

---

## Engineering / decisions

**Q: How do you handle errors?**
Every endpoint is in try/except and returns a meaningful JSON error; external calls
(Gemini, DB, file writes) degrade gracefully — the server never crashes on bad input.

**Q: How do you manage config/secrets?**
All via environment variables loaded with `python-dotenv`; `.env` is git-ignored and
`.env.example` documents the keys. The API key is never hard-coded.

**Q: How is it containerised?**
Docker Compose: two services on a bridge network; only the frontend publishes port 8000;
the backend is private. Images are `python:3.14-slim`, deps installed with
`uv sync --frozen`.

**Q: Why no CORS?**
Because the browser only ever talks to the frontend, and the proxy calls the backend
server-side — there's no cross-origin browser request, so CORS would be a no-op.

**Q: How would you test it?**
`calculator.py` is pure Python with no I/O, so it's unit-testable with plain dicts —
feed it a receipt + assignments and assert the totals. The data/AI layers are isolated
behind function boundaries so they can be mocked.

**Q: Biggest limitation / what's next?**
Single-node SQLite and Gemini latency. Next: in-app camera capture, auth/accounts,
Postgres + Redis for scale, and CI/CD + monitoring.

---

## Curveballs

**Q: What if the image isn't a receipt (a cat photo)?**
Local pre-check catches non-images/corrupt files with no API call; the AI validator
catches "real image but not a receipt" and we reject it with a reason.

**Q: What if Gemini is down or slow?**
OCR retries 3× (12s apart) then returns empty gracefully; parse falls back to an empty
receipt; the UI shows processing time and warns if it's over 15s. Nothing crashes.

**Q: Is the payment real?**
No — we display the payer's existing DuitNow/TNG QR for friends to scan manually. We
don't move money. Future work is a direct payment API.

**Q: What data is sensitive / privacy?**
Receipts and QRs contain personal info; files exist only for the session's lifetime and
are deleted on completion; only text (not the image) is cached; nothing leaves except
the Gemini call. Production needs auth + encryption + a retention policy.

---

## If you blank — the universal fallback

*"DuitSplit turns a receipt photo into a fair split. `ocr.py` reads the image to text
and saves Bronze/Silver, `parser.py` turns text into a validated Receipt with AI,
`calculator.py` does the money maths in pure Python, and `app.py` ties it together and
stores the Gold record in SQLite. The frontend only talks to a proxy, so the backend
stays private."*
```
