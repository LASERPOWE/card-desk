"""SQLite storage for Card Desk.

DB lives under DATA_DIR when set (e.g. a mounted persistent disk on a host),
otherwise next to this file (local-PC behaviour, unchanged).
"""
import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "cards.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    image_file TEXT NOT NULL,
    full_name TEXT DEFAULT '',
    designation TEXT DEFAULT '',
    company TEXT DEFAULT '',
    department TEXT DEFAULT '',
    mobile TEXT DEFAULT '',
    office_phones TEXT DEFAULT '',
    fax TEXT DEFAULT '',
    emails TEXT DEFAULT '',
    website TEXT DEFAULT '',
    address TEXT DEFAULT '',
    city TEXT DEFAULT '',
    pin_code TEXT DEFAULT '',
    country TEXT DEFAULT '',
    confidence TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    raw_summary TEXT DEFAULT '',
    linkedin_url TEXT DEFAULT '',
    linkedin_photo_url TEXT DEFAULT '',
    linkedin_position TEXT DEFAULT '',
    other_web_profiles TEXT DEFAULT '',
    company_core_business TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    enrich_sources TEXT DEFAULT '',
    enrich_status TEXT DEFAULT 'pending',
    enrich_attempts INTEGER DEFAULT 0
);
"""

EXTRACT_FIELDS = [
    "full_name", "designation", "company", "department", "mobile",
    "office_phones", "fax", "emails", "website", "address", "city",
    "pin_code", "country", "confidence", "notes", "raw_summary",
]

ENRICH_FIELDS = [
    "linkedin_url", "linkedin_photo_url", "linkedin_position",
    "other_web_profiles", "company_core_business", "enrich_sources",
    "summary",
]


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migration: add columns that older databases may not have
        existing = {r[1] for r in conn.execute("PRAGMA table_info(cards)").fetchall()}
        if "summary" not in existing:
            conn.execute("ALTER TABLE cards ADD COLUMN summary TEXT DEFAULT ''")


def insert_card(created_at, image_file, extraction):
    cols = ["created_at", "image_file"] + EXTRACT_FIELDS
    vals = [created_at, image_file] + [str(extraction.get(f, "") or "") for f in EXTRACT_FIELDS]
    placeholders = ",".join("?" * len(cols))
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO cards ({}) VALUES ({})".format(",".join(cols), placeholders), vals
        )
        return cur.lastrowid


def update_enrichment(card_id, data, status, bump_attempt=True):
    sets, vals = [], []
    for f in ENRICH_FIELDS:
        if f in data:
            sets.append(f + "=?")
            vals.append(str(data.get(f, "") or ""))
    sets.append("enrich_status=?")
    vals.append(status)
    if bump_attempt:
        sets.append("enrich_attempts=enrich_attempts+1")
    vals.append(card_id)
    with get_conn() as conn:
        conn.execute("UPDATE cards SET {} WHERE id=?".format(",".join(sets)), vals)


def list_cards():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM cards ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def get_card(card_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        return dict(row) if row else None


def delete_card(card_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM cards WHERE id=?", (card_id,))


def reset_for_reenrich(card_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE cards SET enrich_status='pending', enrich_attempts=0 WHERE id=?",
            (card_id,),
        )


def rows_needing_enrichment(max_attempts=3, limit=3):
    """Rows to retry: pending or failed, under the attempt cap.
    'done' and 'not_found' rows are never retried -- this fixes the
    infinite-retry / API-cost bug from the Apps Script version.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM cards
               WHERE enrich_status IN ('pending','failed')
                 AND enrich_attempts < ?
                 AND (full_name != '' OR company != '')
               ORDER BY id ASC LIMIT ?""",
            (max_attempts, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_exhausted(max_attempts=3):
    with get_conn() as conn:
        conn.execute(
            """UPDATE cards SET enrich_status='not_found'
               WHERE enrich_status IN ('pending','failed') AND enrich_attempts >= ?""",
            (max_attempts,),
        )
