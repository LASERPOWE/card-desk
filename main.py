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
PUBLIC_PATHS = {"/login", "/api/login", "/healthz"}


@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    if not auth.auth_enabled() or path in PUBLIC_PATHS:
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


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


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


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ── Pages & static ───────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/user")
def user_page():
    return FileResponse(os.path.join(STATIC_DIR, "user.html"))


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── API ──────────────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_cards(background: BackgroundTasks, files: list[UploadFile] = File(...)):
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
                datetime.now(timezone.utc).isoformat(), fname, extraction)

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
def api_cards():
    cards = db.list_cards()
    return {"cards": cards, "count": len(cards),
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
def api_delete(card_id: int):
    card = db.get_card(card_id)
    if not card:
        raise HTTPException(404, "Card not found")
    img = os.path.join(UPLOAD_DIR, card["image_file"])
    if os.path.exists(img):
        try:
            os.remove(img)
        except OSError:
            pass
    db.delete_card(card_id)
    return {"ok": True}
