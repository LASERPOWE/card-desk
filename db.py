"""SQLite storage for Card Desk — with automatic GitHub backup/restore so data
survives free-tier container restarts. Every write is pushed to the repo's
backup/ folder (needs GITHUB_TOKEN env); on boot, the newest backup is restored.
"""
import base64 as _b64
import logging
import os
import sqlite3
import threading
import time as _time
from contextlib import contextmanager

import requests as _rq

_blog = logging.getLogger("card-desk.backup")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "cards.db")

# ── GitHub-backed persistence ────────────────────────────────────────────────
BACKUP_REPO = os.environ.get("BACKUP_REPO", "LASERPOWE/card-desk")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
_BK_LOCK = threading.Lock()
_BK_LAST = 0.0


def _gh_put(path, content_bytes, message):
    """Create/update one file in the repo. Returns True on success."""
    if not GITHUB_TOKEN:
        return False
    url = "https://api.github.com/repos/%s/contents/%s" % (BACKUP_REPO, path)
    H = {"Authorization": "Bearer " + GITHUB_TOKEN,
         "Accept": "application/vnd.github+json"}
    sha = None
    try:
        r = _rq.get(url, headers=H, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass
    body = {"message": message, "content": _b64.b64encode(content_bytes).decode()}
    if sha:
        body["sha"] = sha
    r = _rq.put(url, headers=H, json=body, timeout=45)
    return r.status_code in (200, 201)


def backup_now(image_paths=None, force=False):
    """Push cards.db (+ any new card images) to GitHub. Throttled to 1/min
    unless force. Never raises — data safety must not break the app."""
    global _BK_LAST
    if not GITHUB_TOKEN:
        return
    with _BK_LOCK:
        if not force and _time.time() - _BK_LAST < 60:
            return
        try:
            with open(DB_PATH, "rb") as f:
                if _gh_put("backup/cards.db", f.read(), "auto-backup: cards.db"):
                    _BK_LAST = _time.time()
                    _blog.info("DB backed up to GitHub")
            for p in (image_paths or []):
                try:
                    with open(p, "rb") as f:
                        _gh_put("backup/uploads/" + os.path.basename(p), f.read(),
                                "auto-backup: card image")
                except Exception:
                    _blog.exception("image backup failed: %s", p)
        except Exception:
            _blog.exception("backup failed (continuing)")


def backup_async(image_paths=None, force=False):
    threading.Thread(target=backup_now, args=(image_paths, force), daemon=True).start()


def restore_from_backup(upload_dir):
    """On boot: if there is no live DB, pull the newest backup from GitHub
    (public raw for the DB, API listing for images). Never raises."""
    try:
        if os.path.exists(DB_PATH):
            return False
        base = "https://raw.githubusercontent.com/%s/main/backup/" % BACKUP_REPO
        r = _rq.get(base + "cards.db", timeout=25)
        if r.status_code != 200 or not r.content.startswith(b"SQLite"):
            return False
        with open(DB_PATH, "wb") as f:
            f.write(r.content)
        _blog.info("Restored cards.db from GitHub backup")
        try:
            idx = _rq.get("https://api.github.com/repos/%s/contents/backup/uploads" % BACKUP_REPO,
                          timeout=25)
            if idx.status_code == 200:
                for it in idx.json():
                    dest = os.path.join(upload_dir, it.get("name", ""))
                    if it.get("download_url") and not os.path.exists(dest):
                        d = _rq.get(it["download_url"], timeout=40)
                        if d.status_code == 200:
                            with open(dest, "wb") as f:
                                f.write(d.content)
        except Exception:
            _blog.exception("image restore failed (continuing)")
        return True
    except Exception:
        _blog.exception("restore failed (continuing)")
        return False

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
    enrich_attempts INTEGER DEFAULT 0,
    owner_email TEXT DEFAULT ''
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
        if "owner_email" not in existing:
            conn.execute("ALTER TABLE cards ADD COLUMN owner_email TEXT DEFAULT ''")


def insert_card(created_at, image_file, extraction, owner_email=""):
    cols = ["created_at", "image_file", "owner_email"] + EXTRACT_FIELDS
    vals = [created_at, image_file, owner_email or ""] + [str(extraction.get(f, "") or "") for f in EXTRACT_FIELDS]
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


def list_cards(owner=None):
    """All cards, or only one owner's cards when owner is given."""
    with get_conn() as conn:
        if owner is None:
            rows = conn.execute("SELECT * FROM cards ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cards WHERE owner_email=? ORDER BY id DESC", (owner,)
            ).fetchall()
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
