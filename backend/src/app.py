"""DuitSplit backend: FastAPI + SQLite."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from calculator import calculate_bill
from models import BillSummary, Friend, Receipt, Session
from ocr import extract_text_from_image
from parser import parse_receipt

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "data/duitsplit.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS friends (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    avatar_color TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT,
    receipt_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS assignments (
    session_id TEXT,
    friend_id TEXT,
    item_name TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (friend_id) REFERENCES friends(id)
);
"""

_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080",
]


def init_db() -> None:
    """Create the database file and tables if they do not exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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


app = FastAPI(title="DuitSplit API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        logger.error("DB init failed: %s", exc)


# --------------------------------------------------------------------------- #
# Request/response bodies
# --------------------------------------------------------------------------- #
class FriendCreate(BaseModel):
    name: str
    avatar_color: str | None = None


class SessionCreate(BaseModel):
    name: str
    created_by: str
    receipt: Receipt


class AssignBody(BaseModel):
    # item_name -> list of friend ids
    assignments: dict[str, list[str]]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ocr")
async def ocr_endpoint(file: UploadFile) -> Receipt:
    """Accept a receipt image, OCR it, parse it, return a structured Receipt."""
    tmp_path: Path | None = None
    try:
        suffix = Path(file.filename or "upload.jpg").suffix or ".jpg"
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Empty file uploaded.")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)

        raw_text = extract_text_from_image(tmp_path)
        if not raw_text:
            raise HTTPException(status_code=502, detail="OCR failed to read the image.")
        return parse_receipt(raw_text)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("OCR endpoint error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to process receipt.")
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


@app.post("/friends")
def create_friend(body: FriendCreate) -> Friend:
    try:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="Friend name is required.")
        friend = Friend(
            id=str(uuid.uuid4()),
            name=body.name.strip(),
            avatar_color=body.avatar_color or _pick_color(),
        )
        with get_db() as conn:
            conn.execute(
                "INSERT INTO friends (id, name, avatar_color) VALUES (?, ?, ?)",
                (friend.id, friend.name, friend.avatar_color),
            )
        return friend
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("create_friend error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to create friend.")


@app.get("/friends")
def list_friends() -> list[Friend]:
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, name, avatar_color FROM friends ORDER BY name"
            ).fetchall()
        return [Friend(**dict(row)) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("list_friends error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to list friends.")


@app.delete("/friends/{friend_id}")
def delete_friend(friend_id: str) -> dict[str, str]:
    try:
        with get_db() as conn:
            cur = conn.execute("DELETE FROM friends WHERE id = ?", (friend_id,))
            conn.execute("DELETE FROM assignments WHERE friend_id = ?", (friend_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Friend not found.")
        return {"status": "deleted", "id": friend_id}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("delete_friend error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to delete friend.")


@app.post("/sessions")
def create_session(body: SessionCreate) -> Session:
    try:
        session = Session(
            id=str(uuid.uuid4()),
            name=body.name.strip() or "Untitled",
            created_by=body.created_by,
            receipt=body.receipt,
            friends=[],
            assignments={},
        )
        with get_db() as conn:
            conn.execute(
                "INSERT INTO sessions (id, name, created_by, receipt_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    session.id,
                    session.name,
                    session.created_by,
                    body.receipt.model_dump_json(),
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


@app.post("/sessions/{session_id}/assign")
def assign_items(session_id: str, body: AssignBody) -> Session:
    try:
        # Validate the session exists first.
        _load_session(session_id)
        with get_db() as conn:
            conn.execute(
                "DELETE FROM assignments WHERE session_id = ?", (session_id,)
            )
            rows = [
                (session_id, friend_id, item_name)
                for item_name, friend_ids in body.assignments.items()
                for friend_id in friend_ids
            ]
            if rows:
                conn.executemany(
                    "INSERT INTO assignments (session_id, friend_id, item_name) "
                    "VALUES (?, ?, ?)",
                    rows,
                )
        return _load_session(session_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("assign_items error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to assign items.")


@app.get("/sessions/{session_id}/summary")
def session_summary(session_id: str) -> BillSummary:
    try:
        session = _load_session(session_id)
        return calculate_bill(session)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("session_summary error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to compute summary.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pick_color() -> str:
    try:
        with get_db() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM friends").fetchone()["c"]
    except Exception:  # noqa: BLE001
        count = 0
    return _PALETTE[count % len(_PALETTE)]


def _load_session(session_id: str) -> Session:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, created_by, receipt_json FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found.")

        assign_rows = conn.execute(
            "SELECT friend_id, item_name FROM assignments WHERE session_id = ?",
            (session_id,),
        ).fetchall()

        # All friends currently involved in this session's assignments.
        friend_ids = {r["friend_id"] for r in assign_rows}
        friends: list[Friend] = []
        if friend_ids:
            placeholders = ",".join("?" for _ in friend_ids)
            frows = conn.execute(
                f"SELECT id, name, avatar_color FROM friends WHERE id IN ({placeholders})",
                tuple(friend_ids),
            ).fetchall()
            friends = [Friend(**dict(fr)) for fr in frows]

    try:
        receipt = Receipt.model_validate_json(row["receipt_json"] or "{}")
    except Exception:  # noqa: BLE001
        receipt = Receipt()

    assignments: dict[str, list[str]] = {}
    for r in assign_rows:
        assignments.setdefault(r["item_name"], []).append(r["friend_id"])

    return Session(
        id=row["id"],
        name=row["name"],
        created_by=row["created_by"] or "",
        receipt=receipt,
        friends=friends,
        assignments=assignments,
    )
