"""DuitSplit backend: FastAPI + SQLite (Bronze/Silver/Gold ETL)."""

import asyncio
import hashlib
import logging
import os
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

import ocr
from calculator import calculate_bill, compute_shares
from models import BillSummary, Friend, Receipt, Session
from parser import parse_receipt, validate_receipt

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "data/duitsplit.db"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
QR_DIR = DATA_DIR / "bronze"
TIMEOUT_MS = 15_000
# Incomplete sessions are swept after this many hours (0 disables the sweep).
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
SWEEP_INTERVAL_SECONDS = int(os.getenv("SWEEP_INTERVAL_SECONDS", "3600"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT,
    receipt_json TEXT,
    tax_amount REAL DEFAULT 0,
    qr_image_path TEXT,
    silver_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS friends (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    name TEXT NOT NULL,
    avatar_color TEXT,
    total_owed REAL DEFAULT 0,
    is_paid INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    friend_id TEXT,
    item_id TEXT,
    item_name TEXT,
    item_price REAL,
    tax_share REAL,
    total_owed REAL,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (friend_id) REFERENCES friends(id)
);

CREATE TABLE IF NOT EXISTS ocr_cache (
    image_hash TEXT PRIMARY KEY,
    raw_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080",
]


def init_db() -> None:
    """Create the DB file, data directories, and tables if missing."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "bronze").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "silver").mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _client_ip(request: Request) -> str:
    """Rate-limit key: the real client IP, honouring the frontend proxy header."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip, default_limits=["120/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Init the DB, sweep stale sessions, and run a periodic sweeper."""
    try:
        init_db()
        sweep_expired_sessions()
    except Exception as exc:  # noqa: BLE001
        logger.error("Startup init/sweep failed: %s", exc)

    task = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="DuitSplit API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


# --------------------------------------------------------------------------- #
# Request/response bodies
# --------------------------------------------------------------------------- #
class UploadResponse(BaseModel):
    session_id: str
    receipt: Receipt
    qr_image_path: str
    silver_path: str
    processing_ms: int
    timeout_warning: bool


class SessionCreate(BaseModel):
    id: str | None = None
    name: str
    created_by: str
    receipt: Receipt
    qr_image_path: str = ""
    silver_path: str = ""


class FriendCreate(BaseModel):
    name: str


class AssignBody(BaseModel):
    # item_name -> list of friend ids
    assignments: dict[str, list[str]]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/upload")
@limiter.limit("15/minute")
async def upload(request: Request, receipt: UploadFile, qr: UploadFile) -> UploadResponse:
    """Upload receipt + QR. Runs OCR (Bronze->Silver), validates, and parses.

    Pipeline: hash -> OCR (cached, saves bronze+silver) -> AI receipt-check ->
    AI parse. The QR image is saved as-is, no AI.
    """
    try:
        session_id = str(uuid.uuid4())

        receipt_bytes = await receipt.read()
        if not receipt_bytes:
            raise HTTPException(status_code=400, detail="Empty receipt file uploaded.")
        qr_bytes = await qr.read()
        if not qr_bytes:
            raise HTTPException(status_code=400, detail="Empty QR file uploaded.")

        # Local pre-check BEFORE any Gemini call, so bad uploads waste no API quota.
        ok, reason = ocr.precheck_image(receipt_bytes)
        if not ok:
            return UploadResponse(
                session_id=session_id,
                receipt=Receipt(is_valid_receipt=False, validation_reason=reason),
                qr_image_path="",
                silver_path="",
                processing_ms=0,
                timeout_warning=False,
            )
        qr_ok, qr_reason = ocr.precheck_image(qr_bytes)
        if not qr_ok:
            return UploadResponse(
                session_id=session_id,
                receipt=Receipt(
                    is_valid_receipt=False,
                    validation_reason=f"QR image invalid: {qr_reason}",
                ),
                qr_image_path="",
                silver_path="",
                processing_ms=0,
                timeout_warning=False,
            )

        # Save the QR image as-is (Bronze); no processing needed.
        QR_DIR.mkdir(parents=True, exist_ok=True)
        qr_path = QR_DIR / f"qr_{session_id}.png"
        qr_path.write_bytes(qr_bytes)

        # OCR with caching. Cache hit still writes bronze+silver for this session.
        image_hash = hashlib.sha256(receipt_bytes).hexdigest()
        cached_text = _cache_get(image_hash)
        if cached_text is not None:
            ocr.save_bronze(session_id, receipt_bytes)
            ocr.save_silver(session_id, cached_text)
            raw_text, ocr_ms, from_cache = cached_text, 0, True
        else:
            raw_text, ocr_ms = ocr.run_ocr_pipeline(session_id, receipt_bytes)
            if not raw_text:
                raise HTTPException(
                    status_code=502, detail="OCR failed to read the receipt image."
                )
            _cache_put(image_hash, raw_text)
            from_cache = False

        is_valid, reason = validate_receipt(raw_text)
        if not is_valid:
            parsed = Receipt(
                is_valid_receipt=False,
                validation_reason=reason or "This does not look like a receipt.",
                cached=from_cache,
            )
        else:
            parsed = parse_receipt(raw_text)
            parsed.cached = from_cache

        parsed.processing_ms += ocr_ms
        return UploadResponse(
            session_id=session_id,
            receipt=parsed,
            qr_image_path=str(qr_path),
            silver_path=str(ocr.silver_path(session_id)),
            processing_ms=parsed.processing_ms,
            timeout_warning=parsed.processing_ms > TIMEOUT_MS,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("upload error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to process upload.")


@app.post("/sessions")
def create_session(body: SessionCreate) -> Session:
    """Gold layer: persist the (possibly edited) structured receipt to SQLite."""
    try:
        session_id = body.id or str(uuid.uuid4())
        session = Session(
            id=session_id,
            name=body.name.strip() or "Untitled",
            created_by=body.created_by,
            receipt=body.receipt,
            tax_amount=body.receipt.tax_amount,
            qr_image_path=body.qr_image_path,
            silver_path=body.silver_path,
        )
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, name, created_by, receipt_json, tax_amount, qr_image_path, "
                "silver_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.name,
                    session.created_by,
                    body.receipt.model_dump_json(),
                    session.tax_amount,
                    session.qr_image_path,
                    session.silver_path,
                ),
            )
        return session
    except Exception as exc:  # noqa: BLE001
        logger.error("create_session error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to create session.")


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> Session:
    try:
        return _load_session(session_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("get_session error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load session.")


@app.post("/sessions/{session_id}/friends")
def add_friend(session_id: str, body: FriendCreate) -> Friend:
    try:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="Friend name is required.")
        _require_session(session_id)
        friend = Friend(
            id=str(uuid.uuid4()),
            session_id=session_id,
            name=body.name.strip(),
            avatar_color=_pick_color(session_id),
        )
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO friends "
                "(id, session_id, name, avatar_color, total_owed, is_paid) "
                "VALUES (?, ?, ?, ?, 0, 0)",
                (friend.id, session_id, friend.name, friend.avatar_color),
            )
        return friend
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("add_friend error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to add friend.")


@app.post("/sessions/{session_id}/assign")
def assign_items(session_id: str, body: AssignBody) -> BillSummary:
    """Assign items to friends and recompute proportional tax + totals."""
    try:
        session = _load_session(session_id)
        session.assignments = body.assignments
        shares = compute_shares(session.receipt, body.assignments, session.friends)

        with get_db() as conn:
            conn.execute(
                "DELETE FROM assignments WHERE session_id = ?", (session_id,)
            )
            for share in shares:
                # Spread the friend's tax proportionally across their items.
                denom = share.subtotal or 1.0
                for item in share.items:
                    tax_share = share.tax_share * (item.price / denom)
                    conn.execute(
                        "INSERT INTO assignments "
                        "(id, session_id, friend_id, item_id, item_name, "
                        "item_price, tax_share, total_owed) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            session_id,
                            share.friend_id,
                            item.id,
                            item.name,
                            round(item.price, 2),
                            round(tax_share, 2),
                            round(item.price + tax_share, 2),
                        ),
                    )
                conn.execute(
                    "UPDATE friends SET total_owed = ? WHERE id = ?",
                    (share.total_owed, share.friend_id),
                )

        return calculate_bill(_load_session(session_id))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("assign_items error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to assign items.")


@app.get("/sessions/{session_id}/summary")
def session_summary(session_id: str) -> BillSummary:
    try:
        return calculate_bill(_load_session(session_id))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("session_summary error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to compute summary.")


@app.patch("/sessions/{session_id}/friends/{friend_id}/paid")
def mark_paid(session_id: str, friend_id: str) -> dict[str, object]:
    """Mark a friend as paid. When all friends are paid, delete the session."""
    try:
        with get_db() as conn:
            cur = conn.execute(
                "UPDATE friends SET is_paid = 1 WHERE id = ? AND session_id = ?",
                (friend_id, session_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Friend not found.")
            rows = conn.execute(
                "SELECT is_paid FROM friends WHERE session_id = ?", (session_id,)
            ).fetchall()

        all_paid = bool(rows) and all(r["is_paid"] for r in rows)
        if all_paid:
            _delete_session(session_id)
        return {"status": "ok", "all_paid": all_paid, "session_deleted": all_paid}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("mark_paid error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to mark paid.")


@app.get("/sessions/{session_id}/qr")
def get_qr(session_id: str) -> FileResponse:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT qr_image_path FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        path = Path(row["qr_image_path"]) if row and row["qr_image_path"] else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="QR image not found.")
        return FileResponse(path, media_type="image/png")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("get_qr error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load QR image.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cache_get(image_hash: str) -> str | None:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT raw_text FROM ocr_cache WHERE image_hash = ?",
                (image_hash,),
            ).fetchone()
        return row["raw_text"] if row else None
    except Exception as exc:  # noqa: BLE001
        logger.error("cache read error: %s", exc)
        return None


def _cache_put(image_hash: str, raw_text: str) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ocr_cache (image_hash, raw_text) VALUES (?, ?)",
                (image_hash, raw_text),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("cache write error: %s", exc)


def _pick_color(session_id: str) -> str:
    try:
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM friends WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
    except Exception:  # noqa: BLE001
        count = 0
    return _PALETTE[count % len(_PALETTE)]


def _require_session(session_id: str) -> None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found.")


def sweep_expired_sessions() -> int:
    """Delete sessions (and their files) older than SESSION_TTL_HOURS.

    Returns the number of sessions swept. A TTL of 0 disables the sweep.
    """
    if SESSION_TTL_HOURS <= 0:
        return 0
    cutoff = f"-{SESSION_TTL_HOURS} hours"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE created_at <= datetime('now', ?)",
            (cutoff,),
        ).fetchall()
    ids = [r["id"] for r in rows]
    for session_id in ids:
        _delete_session(session_id)
    if ids:
        logger.info("Swept %d expired session(s).", len(ids))
    return len(ids)


async def _sweep_loop() -> None:
    """Background task: periodically sweep expired sessions."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            sweep_expired_sessions()
        except Exception as exc:  # noqa: BLE001
            logger.error("Periodic sweep failed: %s", exc)


def _delete_session(session_id: str) -> None:
    """Remove the session, its friends/assignments, and its ETL files."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT qr_image_path, silver_path FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        conn.execute("DELETE FROM assignments WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM friends WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    for key in ("qr_image_path", "silver_path"):
        value = row[key] if row else None
        if value:
            try:
                Path(value).unlink(missing_ok=True)
            except OSError:
                pass
    try:
        ocr.bronze_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


def _load_session(session_id: str) -> Session:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, created_by, receipt_json, tax_amount, qr_image_path, "
            "silver_path FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found.")

        friend_rows = conn.execute(
            "SELECT id, session_id, name, avatar_color, total_owed, is_paid "
            "FROM friends WHERE session_id = ? ORDER BY name",
            (session_id,),
        ).fetchall()
        assign_rows = conn.execute(
            "SELECT friend_id, item_id FROM assignments WHERE session_id = ?",
            (session_id,),
        ).fetchall()

    friends = [
        Friend(
            id=r["id"],
            session_id=r["session_id"] or "",
            name=r["name"],
            avatar_color=r["avatar_color"] or "#888888",
            total_owed=r["total_owed"] or 0.0,
            is_paid=bool(r["is_paid"]),
        )
        for r in friend_rows
    ]

    try:
        receipt = Receipt.model_validate_json(row["receipt_json"] or "{}")
    except Exception:  # noqa: BLE001
        receipt = Receipt()

    # Keyed by item id so duplicate item names stay independent.
    assignments: dict[str, list[str]] = {}
    for r in assign_rows:
        assignments.setdefault(r["item_id"], []).append(r["friend_id"])

    return Session(
        id=row["id"],
        name=row["name"],
        created_by=row["created_by"] or "",
        receipt=receipt,
        tax_amount=row["tax_amount"] or 0.0,
        qr_image_path=row["qr_image_path"] or "",
        silver_path=row["silver_path"] or "",
        friends=friends,
        assignments=assignments,
    )
