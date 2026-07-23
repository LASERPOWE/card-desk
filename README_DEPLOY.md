# Card Desk — Deploy to a permanent https address

This bundle is your whole app, ready to put online. Once it's live at an
`https://…` address, the Google sign-in button, the QR contact-saving, and the
app itself all work on **any phone or computer, anywhere** — not just your Wi‑Fi.

Your existing 8 cards and their images are included (in `seed/`) and load
automatically on first start, so nothing is lost.

---

## Recommended host: Render.com

Render gives a permanent `https://your-app.onrender.com` address and, on the
Starter plan, a **persistent disk** so your scanned cards are never lost.

### What you do (the parts that need your accounts — I can't create accounts for you)

1. **Make a GitHub account** (if you don't have one): https://github.com/signup
2. **Create a new repository** (name it e.g. `card-desk`) and **upload every file
   in this folder** to it. On the new-repo page use *"uploading an existing file"*,
   then drag in everything here (including the `static/` and `seed/` folders).
3. **Make a Render account:** https://render.com  → sign in **with GitHub**.
4. In Render: **New → Blueprint**, pick your `card-desk` repo, click **Apply**.
   Render reads `render.yaml` and sets everything up.
5. When it asks for the two secret values, enter:
   - `OPENAI_API_KEY` — your OpenAI key (same one from your `.env`)
   - `APP_PASSWORD`   — the login password you want
6. Wait for the build to go green, then copy your new address:
   **`https://card-desk-xxxx.onrender.com`**

### What I do (send me that address)

- Register it on your Google client so **Continue with Google** works on phones.
- Wire it into the app and confirm the whole flow end‑to‑end.

That's it — after that, share the link and anyone can open it and sign in.

---

## Login
Username `admin`, password = whatever you set as `APP_PASSWORD`.

## Cost
The Starter plan (needed for the persistent disk that saves your cards) is about
**$7/month**. If you'd rather start free, tell me — I'll switch the config to
Render's free tier (the trade‑off: the app sleeps when idle and card data resets
on each redeploy, so it's fine for testing but not for real day‑to‑day use).

## Environment variables (reference)
| Key | Value |
|-----|-------|
| `OPENAI_API_KEY` | your OpenAI key (secret) |
| `APP_USERNAME` | `admin` |
| `APP_PASSWORD` | your chosen password (secret) |
| `DATA_DIR` | `/var/data` (set for you by `render.yaml`) |

## Files in this bundle
`main.py db.py auth.py ai.py worker.py` — the FastAPI app
`static/` — the website (index, login with Google button, user view, logos)
`seed/` — your existing cards.db + card images (loaded on first boot)
`render.yaml Dockerfile requirements.txt` — deploy config (Render, or any container host)
