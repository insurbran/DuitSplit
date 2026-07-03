"""Pretty-print the DuitSplit SQLite database and list the ETL files.

Usage (inside the running backend container):
    docker compose exec backend python scripts/db_dump.py

Or locally from the backend/ directory:
    uv run python scripts/db_dump.py
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "data/duitsplit.db"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
MAX_CELL = 40  # truncate long values (receipt_json, raw_text) for readability


def _fmt(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def _print_table(conn: sqlite3.Connection, table: str) -> None:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    print(f"\n=== {table}  ({len(rows)} row(s)) ===")
    if not rows:
        print("  (empty)")
        return

    cols = rows[0].keys()
    cells = [[_fmt(r[c]) for c in cols] for r in rows]
    widths = [
        max(len(col), *(len(row[i]) for row in cells)) for i, col in enumerate(cols)
    ]

    header = "  " + " | ".join(col.ljust(widths[i]) for i, col in enumerate(cols))
    print(header)
    print("  " + "-+-".join("-" * w for w in widths))
    for row in cells:
        print("  " + " | ".join(row[i].ljust(widths[i]) for i in range(len(cols))))


def _list_files() -> None:
    print("\n=== ETL files ===")
    for layer in ("bronze", "silver"):
        folder = DATA_DIR / layer
        files = sorted(folder.glob("*")) if folder.is_dir() else []
        print(f"  {layer}/ ({len(files)} file(s)):")
        for f in files:
            print(f"    - {f.name}  ({f.stat().st_size} bytes)")


def main() -> None:
    print(f"Database: {DB_PATH}")
    if not DB_PATH.is_file():
        print("  (database file does not exist yet — run a split first)")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        for table in tables:
            _print_table(conn, table)
    finally:
        conn.close()

    _list_files()


if __name__ == "__main__":
    main()
