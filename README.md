# 💣 Exodus SMS Bomber — render.com Setup

## One command runs everything
```
python Main.py
```
Flask API + Telegram bot — both start from this one command.

---

## Deploy on render.com

### Step 1 — Push to GitHub
Repo needs these files:
- Main.py, bot.py, requirements.txt
- render.yaml — render reads this automatically
- runtime.txt — sets Python 3.11
- Proxies.txt — optional (or paste in PROXY_LIST env var)

### Step 2 — Create Web Service
1. render.com → New → Web Service
2. Connect your GitHub repo
3. render auto-detects render.yaml:
   - Build: pip install -r requirements.txt
   - Start: python Main.py
4. Click Create Web Service

### Step 3 — Environment Variables
render dashboard → your service → Environment tab:

| Key | Value |
|---|---|
| BOT_TOKEN | from @BotFather |
| ADMIN_IDS | your Telegram numeric ID (from @userinfobot) |
| BASE_URL | https://your-app-name.onrender.com |
| PROXY_LIST | ip:port:user:pass,ip:port:user:pass,... |
| ADMIN_PASSWORD | your panel password |

Everything else has defaults in render.yaml.

### Step 4 — Wait for deploy
Console shows: EXODUS SMS BOMBER API STARTED

### Step 5 — Test
- Site: https://your-app.onrender.com
- Bot: /start in Telegram
- Admin: https://your-app.onrender.com/admin

---

## Proxies on render
No file upload on render. Use PROXY_LIST env var:
```
45.38.107.97:6014:user:pass,64.137.96.74:6641:user:pass
```
Paste all proxies comma-separated in one env var value.

---

## Notes
- SQLite resets on redeploy — use Firebase for persistence
- Free tier sleeps after 15 min inactivity — paid plan stays always on
- PORT is auto-set by render — never hardcode it
- SIGTERM handled cleanly — no crash on restart/redeploy

---

## Local run (Termux / VPS / CMD)
```bash
pip install flask requests firebase-admin urllib3 --break-system-packages
cp .env.example .env
# set BOT_TOKEN, ADMIN_IDS, BASE_URL in .env
python Main.py
```
