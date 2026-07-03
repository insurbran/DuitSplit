# DuitSplit — Code Walkthrough (line by line)

How every file connects, and what each part of the code does. Read section 1 to see
the wiring, then each file section for the detail.

---

## 1. How the files connect

```
                         BROWSER (index.html + JS)
                                │  fetch("/api/...")
                                ▼
        frontend/src/app.py  ── proxy ──►  backend/src/app.py
        (serves index.html,               (all endpoints)
         forwards /api/*)                        │
                                                 │ imports & calls
                    ┌────────────────────────────┼───────────────────────────┐
                    ▼                ▼            ▼             ▼              ▼
                 ocr.py          parser.py   calculator.py  models.py     SQLite
               (DATA layer)     (AI layer)   (LOGIC layer)  (schemas)   (Gold data)
```

**Import graph (who imports whom):**
- `app.py` imports `ocr`, `parser` (`parse_receipt`, `validate_receipt`),
  `calculator` (`calculate_bill`, `compute_shares`), and `models`.
- `parser.py` imports `models` (`Receipt`, `ReceiptItem`).
- `calculator.py` imports `models` (all the models).
- `ocr.py` imports only external libs (`google-genai`) + stdlib.
- `models.py` imports nothing of ours (it's the base everyone depends on).
- `frontend/app.py` imports nothing of ours — it only talks to the backend over HTTP.

**Rule of thumb:** `models.py` is the shared language; `ocr`/`parser`/`calculator`
are pure "library" modules; `app.py` is the conductor that calls them and talks to
the database; the frontend never touches the backend code directly — only via HTTP.

---

## 2. `backend/src/models.py` — the shared data shapes

Pydantic classes = typed data structures with automatic validation + JSON conversion.

```python
class ReceiptItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))  # unique per line
    name: str
    price: float          # the LINE total (e.g. RM30 for 2×15)
    quantity: int = 1
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)      # 0–1, validated range
```
- `id` uses `default_factory` → every item auto-gets a unique UUID so two "SAYUR"
  lines never collide. `ge=0.0, le=1.0` makes Pydantic reject confidences outside 0–1.

```python
class Receipt(BaseModel):
    items: list[ReceiptItem] = Field(default_factory=list)
    subtotal / tax_amount / tax_percent / tax_confidence / total   # numbers
    is_valid_receipt: bool = True      # set False if not a receipt
    validation_reason: str = ""        # why it was rejected
    cached: bool = False               # OCR came from cache?
    processing_ms: int = 0             # how long OCR+parse took
```
- This is the object the AI produces and the UI edits. `tax_amount` (absolute) and
  `tax_percent` (rate) are both stored; the split uses `tax_percent`.

```python
class Friend(BaseModel):
    id, session_id, name, avatar_color, total_owed=0.0, is_paid=False
```
- A friend is scoped to one session (`session_id`) and carries their computed
  `total_owed` and payment status.

```python
class ItemShare / FriendShare   # the computed result per friend
class Session                   # everything about one split (receipt + friends + assignments + paths)
class BillSummary               # session_id, friends[FriendShare], total, paid, remaining
```
- `FriendShare`/`BillSummary` are what the summary screen renders. `Session` is the
  in-memory representation of a Gold record rebuilt from the DB.

**Connects to:** imported by `parser`, `calculator`, and `app`. Pydantic gives us
`.model_dump_json()` (object → JSON string for the DB) and `.model_validate_json()`
(DB string → object).

---

## 3. `backend/src/ocr.py` — DATA layer (pixels → text + ETL)

```python
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))     # config from env, as a Path
BRONZE_DIR = DATA_DIR / "bronze"; SILVER_DIR = DATA_DIR / "silver"
MODEL_NAME = "gemini-2.5-flash"; MAX_ATTEMPTS = 3; RETRY_DELAY_SECONDS = 12
```

**`_detect_image_format(data)`** — reads the first few bytes ("magic bytes") to tell if
it's a real JPEG/PNG/WebP/etc. No decoding, no AI. Returns the format or `None`.

**`precheck_image(data)`** — the free gate before any API call:
```python
if not data: return False, "empty"
if _detect_image_format(data) is None: return False, "not a supported image"
if len(data) < MIN_IMAGE_BYTES: return False, "too small/corrupt"
return True, ""
```
Rejects junk uploads so they never cost a Gemini call.

**`_get_client()`** — builds the Gemini client from `GOOGLE_API_KEY`; raises if the key
is missing.

**`bronze_path()` / `silver_path()`** — compute the file paths for a session id.

**`save_bronze(sid, bytes)` / `save_silver(sid, text)`** — write the Bronze image /
Silver text with `pathlib`, wrapped in try/except so a disk error never crashes.

**`extract_text_from_image(path)`** — the actual OCR:
```python
image_bytes = image_path.read_bytes()
client = _get_client()
for attempt in range(1, MAX_ATTEMPTS+1):          # retry up to 3×
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[types.Part.from_bytes(...image...), _OCR_PROMPT])
    text = (response.text or "").strip()
    if text: return text                          # success
    time.sleep(RETRY_DELAY_SECONDS)               # wait 12s, retry
return ""                                          # fallback: empty string, never raises
```

**`run_ocr_pipeline(sid, bytes)`** — ties the ETL together and times it:
```python
src = save_bronze(sid, bytes)                     # BRONZE
start = time.perf_counter()
text = extract_text_from_image(src)               # OCR
elapsed_ms = int((perf_counter()-start)*1000)
if text: save_silver(sid, text)                   # SILVER
return text, elapsed_ms
```

**Connects to:** called by `app.upload()`. Returns plain text → handed to `parser`.

---

## 4. `backend/src/parser.py` — AI layer (text → validated Receipt)

**`_strip_code_fences(text)`** — models sometimes wrap JSON in ```` ```json ```` fences;
this strips them so `json.loads` works.

**`validate_receipt(raw_text)`** — the "is this a receipt?" classifier (AI call #2):
```python
if not raw_text.strip(): return False, "No text..."     # empty guard
response = client.models.generate_content(model, _VALIDATE_PROMPT.replace("{raw_text}", raw_text))
data = json.loads(_strip_code_fences(response.text))
return bool(data["is_receipt"]), str(data["reason"])
# on ANY error → return True, ""  (fail OPEN: never wrongly reject a good receipt)
```

**`parse_receipt(raw_text)`** — structured extraction (AI call #3):
```python
start = time.perf_counter()
response = client...generate_content(_PARSE_PROMPT.replace("{raw_text}", raw_text))
data = json.loads(_strip_code_fences(response.text))     # text → dict
receipt = Receipt.model_validate(data)                   # dict → validated Receipt (Pydantic)
# if validation fails → _salvage(data) recovers what it can
...
base = receipt.subtotal or sum(i.price for i in receipt.items)
receipt.tax_percent = round(receipt.tax_amount / base * 100, 2)   # derive tax %
receipt.processing_ms = int((perf_counter()-start)*1000)
return receipt
# on bad JSON / API error → return Receipt()  (empty fallback, never raises)
```
- **The key idea:** the AI's raw output is never trusted — it must pass through
  `Receipt.model_validate` (Pydantic) before anything uses it. If that fails,
  `_salvage()` builds a best-effort Receipt field-by-field.

**`_salvage(data)`** — defensive recovery: loops the items, coerces each field with
`try/except`, skips broken rows, and recomputes subtotal/total.

**Connects to:** called by `app.upload()`. Returns a `Receipt` (models.py).

---

## 5. `backend/src/calculator.py` — LOGIC layer (pure Python, no AI)

**`_round2(v)`** — round to 2 decimals (with a tiny epsilon to avoid float wobble).

**`_expand_units(receipt)`** — turn each line into individual units:
```python
for item in receipt.items:
    qty = item.quantity or 1
    unit_price = item.price / qty            # RM30 line, qty 2 → RM15 per unit
    for n in range(qty):
        units[f"{item.id}#{n}"] = (item.name, unit_price)   # unit id = "itemid#0", "itemid#1"
```

**`compute_shares(receipt, assignments, friends)`** — the core maths:
```python
units = _expand_units(receipt)
acc = {friend.id: {"items": [], "subtotal": 0.0} for friend in friends}
for unit_id, friend_ids in assignments.items():     # who shares this unit
    share = unit_price / len(sharers)               # split equally
    for fid in sharers:
        acc[fid]["items"].append(ItemShare(...share...))
        acc[fid]["subtotal"] += share
# then tax, per friend:
tax_share = subtotal * tax_percent / 100.0          # % of THEIR own items
total_owed = subtotal + tax_share
```
Returns a `list[FriendShare]` — each friend's items, subtotal, tax, total.

**`calculate_bill(session)`** — wraps it into a `BillSummary`:
```python
shares = compute_shares(session.receipt, session.assignments, session.friends)
total = sum(s.total_owed for s in shares)
paid = sum(s.total_owed for s in shares if s.is_paid)
remaining = total - paid
return BillSummary(..., friends=shares, total, paid, remaining)
```

**Connects to:** called by `app.assign_items()` and `app.session_summary()`. No I/O, no
AI — which is why it's easy to test and safe to trust.

---

## 6. `backend/src/app.py` — APPLICATION layer (the conductor)

**Config / imports (top):** loads env vars, sets `DB_PATH`, `DATA_DIR`, TTLs, and the
`SCHEMA` string (the four `CREATE TABLE IF NOT EXISTS` statements = the DB structure).

**`init_db()`** — makes the `data/`, `bronze/`, `silver/` folders and runs `SCHEMA`.

**`get_db()`** — a context manager: open SQLite, `yield` the connection, `commit()` on
success, always `close()`. Used as `with get_db() as conn:` everywhere.

**`_client_ip(request)`** — reads `X-Forwarded-For` (set by the proxy) so rate limiting
keys on the real user, not the proxy container.

**`limiter` + `lifespan()`** — sets up slowapi; `lifespan` runs `init_db()` +
`sweep_expired_sessions()` on startup and launches the hourly `_sweep_loop()` task.

**App setup:**
```python
app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # → 429
app.add_middleware(SlowAPIMiddleware)                                       # global 120/min
```

**Request bodies** (`UploadResponse`, `SessionCreate`, `FriendCreate`, `AssignBody`) —
Pydantic models that define what each endpoint accepts/returns.

### Endpoints (every one wrapped in try/except → JSON error, never crashes)

**`POST /upload`** — the whole ingest pipeline:
```python
receipt_bytes = await receipt.read(); qr_bytes = await qr.read()
ok = ocr.precheck_image(receipt_bytes)          # free local gate (no API)
qr_path.write_bytes(qr_bytes)                    # BRONZE (QR saved as-is)
image_hash = sha256(receipt_bytes)
if _cache_get(hash): reuse cached text           # CACHING
else: run_ocr_pipeline() → Gemini OCR + Bronze + Silver; _cache_put()
is_valid = validate_receipt(text)                # AI validate
parsed = parse_receipt(text)                     # AI extract → Receipt
return UploadResponse(session_id, receipt=parsed, qr_image_path, processing_ms, timeout_warning)
```

**`POST /sessions`** — GOLD write: `INSERT OR REPLACE INTO sessions (... receipt_json ...)`
storing the edited receipt as JSON. Returns the `Session`.

**`GET /sessions/{id}`** — `_load_session()` rebuilds a `Session` from the 3 tables.

**`POST /sessions/{id}/friends`** — `INSERT ... INTO friends` with an auto colour
(`_pick_color`). Returns the `Friend`.

**`POST /sessions/{id}/assign`** — the important one:
```python
session = _load_session(id)
shares = compute_shares(session.receipt, body.assignments, session.friends)  # LOGIC layer
DELETE FROM assignments WHERE session_id=?          # clear old
for share in shares:
    for item in share.items: INSERT INTO assignments (... item_id, price, tax_share ...)
    UPDATE friends SET total_owed=? WHERE id=?
return calculate_bill(_load_session(id))            # fresh BillSummary
```

**`GET /sessions/{id}/summary`** — `return calculate_bill(_load_session(id))`.

**`PATCH /sessions/{id}/friends/{fid}/paid`**:
```python
UPDATE friends SET is_paid=1 WHERE id=? AND session_id=?
rows = SELECT is_paid FROM friends WHERE session_id=?
all_paid = all(r["is_paid"] for r in rows)
if all_paid: _delete_session(id)                    # auto-cleanup
return {"all_paid":..., "session_deleted":...}
```

**`GET /sessions/{id}/qr`** — looks up `qr_image_path`, returns `FileResponse(path)`.

### Helpers
- **`_cache_get/_cache_put`** — read/write the `ocr_cache` table (keyed by image hash).
- **`_pick_color(session_id)`** — cycles the palette by friend count in that session.
- **`_require_session` / `_delete_session`** — existence check / cascade delete (rows + files).
- **`sweep_expired_sessions()` + `_sweep_loop()`** — startup + hourly cleanup of old sessions.
- **`_load_session(id)`** — the reverse of saving: reads `sessions` + `friends` +
  `assignments`, rebuilds the `Receipt` from `receipt_json`, and returns a `Session`
  object (assignments keyed by unit id).

---

## 7. `frontend/src/app.py` — the page server + proxy

```python
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8001")   # config
templates = Jinja2Templates(directory=".../templates")            # render index.html
```

**`GET /`** and **`GET /session/{id}`** — render `index.html`, injecting `backend_url`
and `session_id` (the session route lets a shared link auto-load that split).

**`ANY /api/{path}` → `proxy()`** — the bridge:
```python
url = f"{BACKEND_URL}/{path}"                # e.g. http://backend:8001/upload
body = await request.body()
headers = {drop host + content-length}
headers["X-Forwarded-For"] = real client IP  # so backend rate-limits per user
upstream = await httpx.AsyncClient().request(method, url, content=body, headers, params)
return Response(upstream.content, status, headers, media_type)   # passes JSON *and* the QR image
```
Because this runs server-to-server, the browser never crosses origins → no CORS needed,
and the backend stays private.

---

## 8. `frontend/src/templates/index.html` — the UI (Bootstrap + vanilla JS)

Four "step cards" toggled by `showStep(id)`. A `state` object holds everything:
`{ sessionId, receipt, friends, activeFriend, assignments, summary }`.

Key helpers:
- **`api(path, options)`** — wrapper around `fetch("/api/"+path)`; parses JSON, throws on
  non-200 so callers can `catch` and show an alert.
- **`confBadge(c)`** — green ≥70% / yellow <70% badge.

Step handlers (each maps to one backend endpoint):
- **Upload button** → builds `FormData(receipt, qr)` → `api("upload", POST)` →
  stores `state.receipt`, shows "Extracted in Xs", renders the editable table.
- **`renderEditableItems`** — builds an editable row per item (`data-id` keeps the item
  id), sets the tax %, auto-focuses the first low-confidence row.
- **`collectReceipt`** — reads the edited inputs back into a `receipt` object (preserving
  each `id`), recomputes subtotal/total.
- **Confirm** → `api("sessions", POST)` (Gold write) → moves to assign step.
- **Add friend** → `api("sessions/{id}/friends", POST)` → pushes to `state.friends`.
- **`assignableUnits()`** — expands each item into N unit rows (mirrors the backend's
  `_expand_units`); **`toggleAssign(unitId)`** adds/removes the active friend from that unit.
- **`clientShares()`** — the same tax maths as the backend, done live to show running
  totals on the friend chips.
- **View summary** → `api("sessions/{id}/assign", POST)` then `api(".../summary")` →
  `renderSummary` draws each friend's items/tax/total, one QR at the bottom, and paid
  checkboxes.
- **Mark as Paid** → `api(".../paid", PATCH)`; when all paid, reveals Complete Session
  (which redirects home).

---

## 9. One request, end to end (say this in the demo)

1. Browser POSTs receipt+QR to `/api/upload`.
2. `frontend/app.proxy()` forwards it to `backend:8001/upload` (adds X-Forwarded-For).
3. `app.upload()`: `ocr.precheck_image` → (cache or `ocr.run_ocr_pipeline` = Bronze+OCR+Silver)
   → `parser.validate_receipt` → `parser.parse_receipt` → returns a Pydantic `Receipt`.
4. User edits, hits Confirm → `app.create_session()` writes the **Gold** row.
5. Add friends (`/friends`), assign units (`/assign` → `calculator.compute_shares` →
   writes `assignments`), view `/summary` (`calculator.calculate_bill`).
6. Everyone pays → `/paid` ticks them; the last tick calls `_delete_session()`.

**The sentence that ties it together:** *"`ocr.py` turns the image into text and saves
Bronze/Silver, `parser.py` turns text into a validated Receipt, `calculator.py` does the
money maths in pure Python, and `app.py` wires it together and stores the Gold record in
SQLite — while the frontend only ever talks to a proxy, keeping the backend private."*
```
