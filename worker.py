"""Background enrichment worker.

Runs in a daemon thread. Every WORKER_INTERVAL seconds it picks up rows with
enrich_status pending/failed (max MAX_ATTEMPTS tries each) and enriches them.
Rows marked 'done' or 'not_found' are NEVER retried.
"""
import logging
import threading
import time

import ai
import db

log = logging.getLogger("card-desk.worker")

WORKER_INTERVAL = 60      # seconds between scans
MAX_ATTEMPTS = 3
BATCH_PER_RUN = 3
SLEEP_BETWEEN_CALLS = 3   # seconds

_lock = threading.Lock()


def enrich_card_now(card_id):
    """Enrich a single card synchronously. Safe to call from API or worker."""
    card = db.get_card(card_id)
    if not card:
        return
    name, desig, company = card["full_name"], card["designation"], card["company"]
    if not name and not company:
        db.update_enrichment(card_id, {}, "not_found")
        return

    log.info("Enriching card #%s: %s @ %s", card_id, name, company)
    data, api_ok = ai.enrich_person(name, desig, company)

    # Avatar fallback: derive unavatar URL from LinkedIn slug when no CDN photo
    if not data.get("linkedin_photo_url") and data.get("linkedin_url"):
        data["linkedin_photo_url"] = ai.derive_avatar_from_linkedin(data["linkedin_url"])

    if api_ok:
        # API responded properly. Even if linkedin_url is empty (person not on
        # LinkedIn), we mark done -> no infinite retry loop.
        status = "done"
    else:
        attempts = card["enrich_attempts"] + 1
        status = "not_found" if attempts >= MAX_ATTEMPTS else "failed"

    db.update_enrichment(card_id, data, status)
    log.info("Card #%s -> %s (LinkedIn: %s)", card_id, status, data.get("linkedin_url") or "-")


def _loop():
    while True:
        try:
            with _lock:
                rows = db.rows_needing_enrichment(MAX_ATTEMPTS, BATCH_PER_RUN)
                for row in rows:
                    enrich_card_now(row["id"])
                    time.sleep(SLEEP_BETWEEN_CALLS)
                db.mark_exhausted(MAX_ATTEMPTS)
        except Exception:
            log.exception("Worker loop error")
        time.sleep(WORKER_INTERVAL)


def start_worker():
    t = threading.Thread(target=_loop, name="enrich-worker", daemon=True)
    t.start()
    log.info("Enrichment worker started (every %ss, max %s attempts/card)",
             WORKER_INTERVAL, MAX_ATTEMPTS)
