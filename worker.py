"""Background enrichment worker.

Runs in a daemon thread. Every WORKER_INTERVAL seconds it picks up rows with
enrich_status pending/failed (max MAX_ATTEMPTS tries each) and enriches them.
Rows marked 'done' or 'not_found' are NEVER retried.
"""
import logging
import re
import threading
import time
import urllib.parse

import requests

import ai
import db

log = logging.getLogger("card-desk.worker")

WORKER_INTERVAL = 30      # seconds between scans (faster research turnaround)
MAX_ATTEMPTS = 3
BATCH_PER_RUN = 4
SLEEP_BETWEEN_CALLS = 1   # seconds

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
    try:  # 4. Top open-web pages about the company (DuckDuckGo organic results)
        if company:
            r = requests.get("https://html.duckduckgo.com/html/",
                             params={"q": '"%s"' % company}, headers=ua, timeout=8)
            links = []
            for enc in re.findall(r'uddg=([^&"]+)', r.text)[:8]:
                u = urllib.parse.unquote(enc).split("&")[0]
                if u.startswith("http") and "duckduckgo" not in u:
                    links.append(u)
            if links:
                out["_links"] = list(dict.fromkeys(links))[:4]
    except Exception:
        pass
    if sources:
        out["_sources"] = " | ".join(dict.fromkeys(sources))[:500]
    return out


# ── Exact-match helpers: a profile URL only counts when its slug/handle
# actually contains part of the person's name. Wrong link is worse than none. ──
def _name_tokens(name):
    toks = re.findall(r"[a-z]+", str(name or "").lower())
    return [t for t in toks if len(t) >= 3]


def _li_slug(url):
    m = re.search(r"linkedin\.com/in/([^/?#]+)", str(url or ""), re.I)
    return urllib.parse.unquote(m.group(1)).lower() if m else ""


def li_score(url, name):
    """How many of the person's name tokens appear in the linkedin.com/in/ slug.
    -1 = not a profile URL at all; 0 = profile but name doesn't match."""
    slug = _li_slug(url)
    if not slug:
        return -1
    return sum(1 for t in _name_tokens(name) if t in slug)


def _path_score(url, name):
    """Name-token match for non-LinkedIn profile URLs (facebook/x/instagram)."""
    try:
        path = urllib.parse.unquote(urllib.parse.urlparse(str(url)).path).lower()
    except Exception:
        return 0
    return sum(1 for t in _name_tokens(name) if t in path.replace("-", "").replace(".", "")
               or t in path)


_SOCIAL_TARGETS = [
    ("linkedin",  "site:linkedin.com/in", ["linkedin.com/in/"]),
    ("facebook",  "site:facebook.com",    ["facebook.com/"]),
    ("twitter",   "site:x.com OR site:twitter.com", ["twitter.com/", "x.com/"]),
    ("instagram", "site:instagram.com",   ["instagram.com/"]),
]
_SOCIAL_BAD = ["/search", "/share", "/login", "/dir/", "/hashtag/", "/groups/",
               "/pages/category", "facebook.com/public", "/status/", "/reel"]


def find_social_profiles(name, company):
    """Free social-profile hunt via DuckDuckGo HTML search (no API key).
    Only returns EXACT matches: the profile slug/handle must contain part of
    the person's name. All platforms searched in PARALLEL for speed.
    Returns {platform: url}; never raises."""
    found = {}
    if not name:
        return found
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CardDesk"}
    q_base = '"%s" %s' % (name, company or "")

    def _one(key, filt, domains):
        try:
            r = requests.get("https://html.duckduckgo.com/html/",
                             params={"q": q_base + " " + filt},
                             headers=ua, timeout=8)
            best, best_score = None, 0
            for enc in re.findall(r'uddg=([^&"]+)', r.text)[:10]:
                url = urllib.parse.unquote(enc).split("&")[0]
                if not any(d in url for d in domains) or any(b in url for b in _SOCIAL_BAD):
                    continue
                s = li_score(url, name) if key == "linkedin" else _path_score(url, name)
                if s > best_score:
                    best, best_score = url, s
            if best:  # name-verified match only — never a random same-name page
                found[key] = best
        except Exception:
            pass

    threads = [threading.Thread(target=_one, args=t, daemon=True) for t in _SOCIAL_TARGETS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)
    return found


# ── Photo resolution: only store a URL that ACTUALLY serves an image ─────────
def _photo_ok(url):
    try:
        r = requests.get(url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0 CardDesk"})
        return (r.status_code == 200
                and r.headers.get("content-type", "").startswith("image")
                and len(r.content) > 500)
    except Exception:
        return False


def resolve_photo(linkedin_url, emails, twitter_url, current=""):
    """Try every free photo source in order and return the first URL that
    really returns an image: AI-found CDN photo → LinkedIn avatar → email
    avatar → gravatar → X/Twitter avatar. '' when nothing works (frontend
    then shows initials)."""
    import hashlib
    cands = []
    if current and current.startswith("http"):
        cands.append(current)
    m = re.search(r"linkedin\.com/in/([a-zA-Z0-9\-_%\.]+)", str(linkedin_url or ""), re.I)
    if m:
        cands.append("https://unavatar.io/linkedin/" + m.group(1) + "?fallback=false")
    em = ""
    for part in re.split(r"[,;\s]+", str(emails or "")):
        if "@" in part:
            em = part.strip().lower()
            break
    if em:
        cands.append("https://unavatar.io/" + urllib.parse.quote(em) + "?fallback=false")
        cands.append("https://www.gravatar.com/avatar/"
                     + hashlib.md5(em.encode()).hexdigest() + "?d=404&s=256")
    tm = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,})", str(twitter_url or ""))
    if tm:
        cands.append("https://unavatar.io/x/" + tm.group(1) + "?fallback=false")
    for u in cands:
        if _photo_ok(u):
            return u
    return ""


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

    # Social-profile hunt (free, DuckDuckGo) — name-verified matches only
    socials = {}
    try:
        socials = find_social_profiles(name, company)
        prev = data.get("other_web_profiles", "") or ""
        adds = []
        for k in ("facebook", "twitter", "instagram"):
            u = socials.get(k)
            if u and u not in prev:
                adds.append(k.capitalize() + ": " + u)
        if adds:
            data["other_web_profiles"] = (prev + ("\n" if prev else "") + "\n".join(adds))[:900]
    except Exception:
        log.exception("find_social_profiles failed (continuing)")

    # LinkedIn EXACT-match rule: keep a link only when its slug contains the
    # person's name; between the AI's link and the search-verified link, keep
    # the better-matching one. A wrong profile is worse than none.
    ai_li, ddg_li = data.get("linkedin_url", ""), socials.get("linkedin", "")
    ai_s, ddg_s = li_score(ai_li, name), li_score(ddg_li, name)
    if ai_s < 1:
        ai_li = ""
    if ddg_s < 1:
        ddg_li = ""
    data["linkedin_url"] = ai_li if (ai_li and ai_s >= ddg_s) else (ddg_li or ai_li)

    # Profile photo: test every source and store only a URL that actually
    # serves an image (AI CDN → LinkedIn avatar → email → gravatar → X).
    try:
        data["linkedin_photo_url"] = resolve_photo(
            data.get("linkedin_url", ""), card.get("emails", ""),
            socials.get("twitter", ""), current=data.get("linkedin_photo_url", ""))
    except Exception:
        log.exception("resolve_photo failed (continuing)")

    # Open-source extras (free): fill gaps the AI left + extra sources
    try:
        extra = open_source_lookup(company, card.get("website", ""))
        if extra.get("company_core_business") and not data.get("company_core_business"):
            data["company_core_business"] = extra["company_core_business"]
        if extra.get("_sources"):
            prev = data.get("enrich_sources", "") or ""
            data["enrich_sources"] = (prev + (" | " if prev else "") + extra["_sources"])[:800]
        # Sheet should never be empty: when no personal social profile was
        # verified, fill Other Profiles with company/news/reference links.
        if not (data.get("other_web_profiles") or "").strip():
            refs = []
            for u in (extra.get("_sources") or "").split(" | "):
                u = u.strip()
                if u.startswith("http"):
                    refs.append(u)
            refs.extend(extra.get("_links") or [])
            w = str(card.get("website", "") or "").strip()
            if w:
                refs.append(w if w.startswith("http") else "https://" + w)
            if refs:
                data["other_web_profiles"] = "\n".join(
                    "Company: " + u for u in dict.fromkeys(refs))[:900]
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

    # Title backfill: ID badges print no job title. When research found the
    # person's current role, use it so the Title column isn't blank.
    try:
        if not str(card.get("designation") or "").strip():
            role = ""
            lp = data.get("linkedin_position", "") or ""
            m = re.search(r"Current Role\s*[:\-]\s*(.+)", lp, re.I)
            if m:
                role = m.group(1).strip().splitlines()[0].strip(" .-")
            if role and role.lower() not in ("not publicly available", "unknown", "n/a"):
                db.set_fields(card_id, designation=role[:90])
    except Exception:
        log.exception("designation backfill failed (continuing)")

    log.info("Card #%s -> %s (LinkedIn: %s)", card_id, status, data.get("linkedin_url") or "-")


def revalidate_bad_links():
    """One pass per boot: (a) any saved LinkedIn link whose slug does NOT
    contain the person's name is wrong — clear it and queue fresh research;
    (b) every card's photo is TESTED — broken/missing photos get re-resolved
    from all free sources, so avatars actually load."""
    try:
        n = 0
        for c in db.list_cards():
            li = (c.get("linkedin_url") or "").strip()
            if li and li_score(li, c.get("full_name", "")) < 1:
                db.update_enrichment(c["id"], {"linkedin_url": "", "linkedin_photo_url": ""},
                                     "pending", bump_attempt=False)
                db.reset_for_reenrich(c["id"])
                n += 1
                log.info("Card #%s: LinkedIn link failed name check -> re-research", c["id"])
                continue
            photo = (c.get("linkedin_photo_url") or "").strip()
            if not photo or not _photo_ok(photo):
                new = resolve_photo(li, c.get("emails", ""), "", current="")
                if new != photo:
                    db.update_enrichment(c["id"], {"linkedin_photo_url": new},
                                         c.get("enrich_status") or "done", bump_attempt=False)
                    log.info("Card #%s: photo %s", c["id"], "fixed -> " + new if new else "none available")
        if n:
            log.info("Queued %s card(s) for exact-match LinkedIn re-research", n)
        db.backup_now(force=True)
    except Exception:
        log.exception("revalidate_bad_links failed (continuing)")


def _loop():
    try:
        revalidate_bad_links()
    except Exception:
        pass
    while True:
        try:
            with _lock:
                rows = db.rows_needing_enrichment(MAX_ATTEMPTS, BATCH_PER_RUN)
                for row in rows:
                    enrich_card_now(row["id"])
                    time.sleep(SLEEP_BETWEEN_CALLS)
                db.mark_exhausted(MAX_ATTEMPTS)
                if rows:
                    db.backup_now()  # research results persist too (throttled)
        except Exception:
            log.exception("Worker loop error")
        time.sleep(WORKER_INTERVAL)


def start_worker():
    t = threading.Thread(target=_loop, name="enrich-worker", daemon=True)
    t.start()
    log.info("Enrichment worker started (every %ss, max %s attempts/card)",
             WORKER_INTERVAL, MAX_ATTEMPTS)
