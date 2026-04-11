# Arena MiniApp v2.0 ⚔️
**Pure HTTP API — No Telegram — Arena App Store ready**

---

## Architecture

```
index.html  (GitHub Pages / Render Static)
    │  user identity = wallet address (Arena SDK)
    │  API calls → BACKEND_URL
    ▼
app.py      (Render Web Service — Flask + gunicorn)
    │  httpx → Supabase REST API (service role)
    │  AVAX RPC → on-chain verification
    ▼
Supabase    (Postgres + RLS)
```

---

## Environment variables (Render)

Create a `.env` file locally (never commit it):

```env
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGci...   # service_role key — NEVER expose to frontend
TREASURY_ADDR=0xYOUR_AVAX_WALLET
ADMIN_KEY=your-secret-admin-key-here
FRONTEND_ORIGINS=https://arena.social,https://toff90.github.io,https://your-frontend.onrender.com
```

Set these in Render dashboard → your service → Environment.

---

## Deployment

### 1. Supabase — run migration
Open Supabase SQL editor and run `supabase_migration.sql`.

### 2. Render — backend API
- Service type: **Web Service**
- Runtime: **Python 3**
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT`
- Set env vars above

### 3. Frontend
Option A — **GitHub Pages**: push `index.html` to your repo, enable Pages.
Option B — **Render Static**: set `staticPublishPath: .` in render.yaml.

### 4. Update BACKEND_URL in index.html
```javascript
const BACKEND_URL = 'https://arena-api.onrender.com'  // your Render URL
```

### 5. UptimeRobot
Add HTTP monitor → `https://your-api.onrender.com/health` → every 5 minutes.
This keeps Render free tier alive.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET    | /health | Ping — UptimeRobot |
| POST   | /api/user/register | Register or login via wallet |
| GET    | /api/user/:wallet | Get user data |
| POST   | /api/tap | Batch-sync taps |
| POST   | /api/daily | Claim daily reward |
| GET    | /api/leaderboard?type=alltime\|season\|sprint | Leaderboard |
| GET    | /api/prize-pool | Prize pool + distribution |
| GET    | /api/squad/:wallet | Squad members + passive |
| GET    | /api/sprint/active | Current sprint |
| POST   | /api/verify-payment | Verify on-chain AVAX payment |
| POST   | /api/admin/new-season | **Admin** — end season |
| POST   | /api/admin/sprint | **Admin** — start sprint |

Admin endpoints require `X-Admin-Key: your-secret` header.

---

## User Identity

In v2.0, the user's **wallet address** is the primary identity (no Telegram user ID).

- Inside Arena app: Arena SDK auto-provides the wallet address
- Outside Arena: Reown/WalletConnect modal opens
- Referrals: `?ref=0xWALLET_ADDRESS` query param

---

## Arena App Store Submission

Requirements to submit:
1. ✅ Uses `@the-arena/arena-app-store-sdk`
2. ✅ WalletConnect via Reown AppKit (Project ID configured)
3. ✅ Works without Telegram
4. ✅ AVAX on-chain payments
5. Needs: Reown Project ID from https://cloud.reown.com
6. Needs: App icon 512×512, screenshots, description for store listing

---

## Referral System

- User A shares: `https://your-app.com?ref=0xADDRESS_A`
- User B opens link → on register, `referred_by = 0xADDRESS_A`
- User A gets +1,000 coins instantly
- Every time User B claims daily, User A gets 5% passive

---

## Admin Commands (curl examples)

```bash
# Start new season
curl -X POST https://your-api.onrender.com/api/admin/new-season \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: your-secret" \
  -d '{"season_name": "Season 2"}'

# Launch sprint (24h)
curl -X POST https://your-api.onrender.com/api/admin/sprint \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: your-secret" \
  -d '{"hours": 24, "sprint_name": "⚡ Weekend Sprint"}'
```
