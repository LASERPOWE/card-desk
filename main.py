"""Card Desk — Business card scanner + AI enrichment + directory website.

Run locally:  python -m uvicorn main:app --host 0.0.0.0 --port 8000
On a host:    uvicorn main:app --host 0.0.0.0 --port $PORT
Open:         http://localhost:8000

Data (cards.db + uploaded images) lives under DATA_DIR when set — point it at a
mounted persistent disk on your host so nothing is lost across restarts/deploys.
On first boot, if DATA_DIR is empty, the bundled seed/ data is copied in.
"""
import base64
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()  # reads .env (OPENAI_API_KEY) when present

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai
import auth
import db
import worker

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("card-desk")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
STATIC_DIR = BASE_DIR
SEED_DIR = os.path.join(BASE_DIR, "seed")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_FILE_MB = 15

app = FastAPI(title="Card Desk")


def _seed_data():
    """On first boot copy bundled seed data into DATA_DIR so the existing
    cards + images are present. Never overwrites live data."""
    try:
        seed_db = os.path.join(SEED_DIR, "cards.db")
        live_db = os.path.join(DATA_DIR, "cards.db")
        if os.path.exists(seed_db) and not os.path.exists(live_db):
            shutil.copy2(seed_db, live_db)
            log.info("Seeded database from %s", seed_db)
        seed_uploads = os.path.join(SEED_DIR, "uploads")
        if os.path.isdir(seed_uploads) and not os.listdir(UPLOAD_DIR):
            for f in os.listdir(seed_uploads):
                shutil.copy2(os.path.join(seed_uploads, f), os.path.join(UPLOAD_DIR, f))
            log.info("Seeded %s upload images", len(os.listdir(UPLOAD_DIR)))
    except Exception:
        log.exception("Seed step failed (continuing)")


@app.on_event("startup")
def startup():
    _seed_data()
    db.init_db()
    worker.start_worker()


# ── Authentication ───────────────────────────────────────────────────────────
PUBLIC_PATHS = {"/login", "/api/login", "/api/google-login", "/healthz"}
# Prefixes anyone can reach without logging in — the public "scan a QR → save
# to your own Google Contacts" flow lives here.
PUBLIC_PREFIXES = ("/c/", "/api/pub/")


@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    if not auth.auth_enabled() or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    token = request.cookies.get(auth.COOKIE_NAME)
    if auth.verify_token(token):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse("/login")


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


def _identity(request: Request):
    """Who is logged in — the admin username or a Google email — else None."""
    return auth.parse_token(request.cookies.get(auth.COOKIE_NAME))


def _is_admin(ident):
    return bool(ident) and ident == os.environ.get("APP_USERNAME", "admin")


@app.get("/api/me")
def api_me(request: Request):
    ident = _identity(request)
    if not ident and not auth.auth_enabled():
        ident = os.environ.get("APP_USERNAME", "admin")
    if not ident:
        raise HTTPException(401, "Not authenticated")
    return {"identity": ident, "admin": _is_admin(ident)}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"), headers={"Cache-Control": "no-store, must-revalidate"})


@app.post("/api/login")
def api_login(body: LoginBody):
    if not auth.check_login(body.username, body.password):
        raise HTTPException(401, "Invalid username or password")
    resp = JSONResponse({"ok": True})
    # secure cookie so it works over https on the hosted URL
    resp.set_cookie(auth.COOKIE_NAME, auth.create_token(body.username),
                    max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=True)
    return resp


class GoogleLoginBody(BaseModel):
    access_token: str = ""


@app.post("/api/google-login")
def api_google_login(body: GoogleLoginBody):
    """Sign in with Google: verify the access token with Google, then issue
    the same session cookie the app uses, so the person is actually logged in."""
    token = (body.access_token or "").strip()
    if not token:
        raise HTTPException(401, "Missing Google token")
    try:
        r = requests.get("https://www.googleapis.com/oauth2/v3/userinfo",
                         headers={"Authorization": "Bearer " + token}, timeout=10)
    except Exception:
        raise HTTPException(502, "Could not reach Google to verify sign-in")
    if r.status_code != 200:
        raise HTTPException(401, "Invalid or expired Google sign-in")
    info = r.json()
    email = (info.get("email") or "").strip()
    if not email:
        raise HTTPException(401, "Google did not return an email")
    resp = JSONResponse({"ok": True, "email": email, "name": info.get("name", "")})
    resp.set_cookie(auth.COOKIE_NAME, auth.create_token(email),
                    max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=True)
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ── Pages & static ───────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/user")
def user_page():
    return FileResponse(os.path.join(STATIC_DIR, "user.html"), headers={"Cache-Control": "no-store, must-revalidate"})


# ── Public "scan → save to your Google Contacts" flow ─────────────────────────
_PUB_FIELDS = [
    "id", "full_name", "designation", "company", "department", "mobile",
    "office_phones", "emails", "website", "address", "city", "pin_code",
    "country", "linkedin_url",
]


@app.get("/c/{card_id}")
def public_card_page(card_id: int):
    """The page a phone opens after scanning a card's QR — no login required."""
    return FileResponse(os.path.join(STATIC_DIR, "save.html"), headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/api/pub/cards/{card_id}")
def public_card_data(card_id: int):
    """Public, read-only card details for the save page."""
    card = db.get_card(card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    return {k: card.get(k, "") for k in _PUB_FIELDS}


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── API ──────────────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_cards(request: Request, background: BackgroundTasks, files: list[UploadFile] = File(...)):
    owner = _identity(request) or os.environ.get("APP_USERNAME", "admin")
    results = []
    for f in files:
        try:
            mime = (f.content_type or "").lower()
            if mime not in ALLOWED_MIME:
                raise ValueError("Unsupported file type: " + mime)
            content = await f.read()
            if len(content) > MAX_FILE_MB * 1024 * 1024:
                raise ValueError("File too large (max {} MB)".format(MAX_FILE_MB))

            # 1. Save image locally
            safe_base = re.sub(r"[^a-zA-Z0-9_-]", "_", os.path.splitext(f.filename or "card")[0])[:40]
            fname = "{}_{}{}".format(int(time.time() * 1000), safe_base, ALLOWED_MIME[mime])
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as out:
                out.write(content)

            # 2. Vision extraction (fast, synchronous)
            b64 = base64.b64encode(content).decode("ascii")
            extraction = ai.extract_with_vision(b64, mime)

            # 3. Insert into SQLite
            card_id = db.insert_card(
                datetime.now(timezone.utc).isoformat(), fname, extraction, owner_email=owner)

            # 4. Enrichment in background (slow web research — don't block upload)
            background.add_task(worker.enrich_card_now, card_id)

            results.append({"ok": True, "id": card_id,
                            "name": extraction.get("full_name", ""),
                            "company": extraction.get("company", "")})
        except Exception as e:
            log.exception("Upload failed for %s", f.filename)
            results.append({"ok": False, "file": f.filename, "error": str(e)[:300]})
    return {"results": results}


@app.get("/api/cards")
def api_cards(request: Request):
    ident = _identity(request)
    if _is_admin(ident) or not auth.auth_enabled():
        cards = db.list_cards()          # admin sees everyone's cards, with owner
    else:
        cards = db.list_cards(owner=ident or "")   # each user sees only their own
    return {"cards": cards, "count": len(cards),
            "me": ident or "", "admin": _is_admin(ident),
            "generated_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/cards/{card_id}")
def api_card(card_id: int):
    card = db.get_card(card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    return card


@app.post("/api/cards/{card_id}/enrich")
def api_reenrich(card_id: int, background: BackgroundTasks):
    if not db.get_card(card_id):
        raise HTTPException(404, "Card not found")
    db.reset_for_reenrich(card_id)
    background.add_task(worker.enrich_card_now, card_id)
    return {"ok": True, "message": "Re-enrichment started"}


@app.delete("/api/cards/{card_id}")
def api_delete(card_id: int, request: Request):
    card = db.get_card(card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    ident = _identity(request)
    if auth.auth_enabled() and not _is_admin(ident) and card.get("owner_email") != ident:
        raise HTTPException(403, "You can only delete your own cards")
    img = os.path.join(UPLOAD_DIR, card["image_file"])
    if os.path.exists(img):
        try:
            os.remove(img)
        except OSError:
            pass
    db.delete_card(card_id)
    return {"ok": True}
