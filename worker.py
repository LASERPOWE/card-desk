"""Background enrichment worker.

Runs in a daemon thread. Every WORKER_INTERVAL seconds it picks up rows with
enrich_status pending/failed (max MAX_ATTEMPTS tries each) and enriches them.
Rows marked 'done' or 'not_found' are NEVER retried.
"""
import logging
import re
import threading
import time

import requests

import ai
import db

log = logging.getLogger("card-desk.worker")

WORKER_INTERVAL = 60      # seconds between scans
MAX_ATTEMPTS = 3
BATCH_PER_RUN = 3
SLEEP_BETWEEN_CALLS = 3   # seconds

_lock = threading.Lock()


def open_source_lookup(company, website):
    """Free open-web extras layered on top of the AI research:
    DuckDuckGo instant answers + Wikipedia + the company's own homepage.
    Returns partial fields; never raises."""
    out, sources = {}, []
    ua = {"User-Agent": "Mozilla/5.0 (CardDesk enrichment bot)"}
    try:  # 1. DuckDuckGo instant answer for the company
        if company:
            j = requests.get("https://api.duckduckgo.com/",
                             params={"q": company, "format": "json", "no_html": 1},
                             headers=ua, timeout=8).json()
            if j.get("AbstractText"):
                out["company_core_business"] = j["AbstractText"][:400]
                if j.get("AbstractURL"):
                    sources.append(j["AbstractURL"])
    except Exception:
        pass
    try:  # 2. Wikipedia page for the company
        if company:
            j = requests.get("https://en.wikipedia.org/w/api.php",
                             params={"action": "opensearch", "search": company,
                                     "limit": 1, "format": "json"},
                             headers=ua, timeout=8).json()
            if j and len(j) >= 4 and j[3]:
                sources.append(j[3][0])
    except Exception:
        pass
    try:  # 3. Company website homepage meta description
        if website:
            url = website if str(website).startswith("http") else "https://" + str(website)
            html = requests.get(url, headers=ua, timeout=8).text[:60000]
            m = (re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{20,300})', html, re.I)
                 or re.search(r'<meta[^>]+content=["\']([^"\']{20,300})["\'][^>]+name=["\']description', html, re.I))
            if m and "company_core_business" not in out:
                out["company_core_business"] = m.group(1).strip()[:400]
            sources.append(url)
    except Exception:
        pass
    if sources:
        out["_sources"] = " | ".join(dict.fromkeys(sources))[:500]
    return out


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

    # Open-source extras (free): fill gaps the AI left + extra sources
    try:
        extra = open_source_lookup(company, card.get("website", ""))
        if extra.get("company_core_business") and not data.get("company_core_business"):
            data["company_core_business"] = extra["company_core_business"]
        if extra.get("_sources"):
            prev = data.get("enrich_sources", "") or ""
            data["enrich_sources"] = (prev + (" | " if prev else "") + extra["_sources"])[:800]
    except Exception:
        log.exception("open_source_lookup failed (continuing)")

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
