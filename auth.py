"""Simple session-cookie authentication.

Credentials come from .env:
  APP_USERNAME=admin
  APP_PASSWORD=your-strong-password

Tokens are HMAC-signed (stdlib only, no extra dependencies) and stay valid
for SESSION_DAYS or until the password is changed.
"""
import base64
import hashlib
import hmac
import os
import time

COOKIE_NAME = "cardesk_session"
SESSION_DAYS = 30


def _creds():
    u = os.environ.get("APP_USERNAME", "").strip()
    p = os.environ.get("APP_PASSWORD", "").strip()
    return u, p


def auth_enabled():
    u, p = _creds()
    return bool(u and p)


def _secret():
    u, p = _creds()
    return hashlib.sha256(("cardesk::" + u + "::" + p).encode()).digest()


def check_login(username, password):
    u, p = _creds()
    return bool(u) and hmac.compare_digest(username or "", u) and hmac.compare_digest(password or "", p)


def create_token(username):
    payload = "{}|{}".format(username, int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig


def verify_token(token):
    if not token or "." not in token:
        return False
    b64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(b64.encode()).decode()
    except Exception:
        return False
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        _, expiry = payload.split("|", 1)
        return int(expiry) > time.time()
    except Exception:
        return False
