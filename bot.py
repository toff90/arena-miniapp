"""
🔥 Arena Bot v7.0 — Webhook Definitive Edition
- Singolo processo (python bot.py)
- Flask nel main thread
- Bot in thread separato con webhook (nessun polling)
- Nessun conflitto, stabile su Render
"""

import os
import logging
import datetime
import threading
import asyncio
import httpx
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import NetworkError, TimedOut

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_SERVICE_KEY")
MINIAPP_URL    = os.getenv("MINIAPP_URL")
TREASURY_ADDR  = os.getenv("TREASURY_ADDR", "")
ADMIN_IDS      = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
WEBHOOK_URL    = os.getenv("WEBHOOK_URL")          # https://arena-tap.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
AVAX_RPC       = "https://api.avax.network/ext/bc/C/rpc"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

SHOP_ITEMS = {
    "energy_25":  (0.05,   500, "⚡ Small Refill",  "energy",  25),
    "energy_100": (0.15,  1500, "⚡⚡ Full Refill",  "energy",  100),
    "upgrade_2":  (0.30,  3000, "⚡ Power x2",       "upgrade", 2),
    "upgrade_5":  (0.80,  8000, "🔥 Power x5",       "upgrade", 5),
    "upgrade_10": (2.00, 20000, "💎 Power x10",      "upgrade", 10),
    "upgrade_25": (5.00, 50000, "🚀 Power x25",      "upgrade", 25),
}

STREAK_BONUS = {1:250, 2:250, 3:250, 4:250, 5:250, 6:250, 7:1000}
PRIZE_DIST   = [0.40, 0.20, 0.10, 0.05, 0.05, 0.05, 0.05, 0.03, 0.03, 0.04]

# ─── Flask App ─────────────────────────────────────────────
flask_app = Flask(__name__)
CORS(flask_app, origins=["https://arena.social", "https://toff90.github.io"], supports_credentials=True)

@flask_app.route('/health')
def health():
    return 'OK', 200

@flask_app.route('/api/verify-payment', methods=['POST'])
def verify_payment():
    # ... (invariato, vedi file precedenti)
    pass  # copia la tua implementazione completa

# ─── Supabase helpers (async) ─────────────────────────────
async def db_get(table: str, filters: dict = {}) -> list:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=filters)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.error(f"db_get: {e}"); return []

async def db_insert(table: str, data: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, json=data)
        res = r.json()
        return res[0] if isinstance(res, list) and res else data
    except Exception as e:
        logger.error(f"db_insert: {e}"); return data

async def db_update(table: str, filters: dict, data: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=filters, json=data)
        res = r.json()
        return res[0] if isinstance(res, list) and res else {}
    except Exception as e:
        logger.error(f"db_update: {e}"); return {}

async def db_leaderboard(order_by: str, limit: int = 10) -> list:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{SUPABASE_URL}/rest/v1/users", headers=HEADERS,
                            params={"select": "username,first_name,coins,season_coins,sprint_coins,tap_power",
                                    "order": f"{order_by}.desc", "limit": str(limit)})
        data = r.json() if r.status_code == 200 else []
        return sorted(data, key=lambda x: x.get(order_by, 0), reverse=True)
    except Exception as e:
        logger.error(f"leaderboard: {e}"); return []

# ─── User helpers ────────────────────────────────────────
async def ensure_user(user, referred_by=None):
    existing = await db_get("users", {"id": f"eq.{user.id}"})
    if not existing:
        new_user = {
            "id": user.id, "username": user.username or "",
            "first_name": user.first_name or "Anon",
            "coins": 500, "season_coins": 500,
            "tap_power": 1, "referral_count": 0, "streak": 0, "energy": 100,
        }
        if referred_by and referred_by != user.id:
            new_user["referred_by"] = referred_by
            ref = await db_get("users", {"id": f"eq.{referred_by}"})
            if ref:
                await db_update("users", {"id": f"eq.{referred_by}"}, {
                    "coins": ref[0]["coins"] + 1000,
                    "season_coins": ref[0].get("season_coins", 0) + 1000,
                    "referral_count": ref[0].get("referral_count", 0) + 1
                })
        result = await db_insert("users", new_user)
        return result or new_user, True
    return existing[0], False

def main_keyboard(miniapp_url, user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 PLAY NOW", web_app=WebAppInfo(url=f"{miniapp_url}?uid={user_id}"))],
        [InlineKeyboardButton("🌍 All-Time",  callback_data="lb_alltime"),
         InlineKeyboardButton("🏁 Season",    callback_data="lb_season")],
        [InlineKeyboardButton("👥 Squad",     callback_data="squad"),
         InlineKeyboardButton("📅 Daily",     callback_data="daily")],
        [InlineKeyboardButton("💰 Shop",      callback_data="shop"),
         InlineKeyboardButton("🏆 Prize",     callback_data="prize")],
    ])

async def get_sprint():
    sp = await db_get("sprints", {"is_active": "eq.true", "order": "started_at.desc", "limit": "1"})
    if not sp: return None
    s = sp[0]
    try:
        ends = datetime.datetime.fromisoformat(s["ends_at"].replace("Z", "+00:00"))
        if ends.tzinfo is None: ends = ends.replace(tzinfo=datetime.timezone.utc)
        if datetime.datetime.now(datetime.timezone.utc) > ends:
            await db_update("sprints", {"id": f"eq.{s['id']}"}, {"is_active": False})
            return None
    except: return None
    return s

def remaining(ends_str):
    try:
        e = datetime.datetime.fromisoformat(ends_str.replace("Z", "+00:00"))
        if e.tzinfo is None: e = e.replace(tzinfo=datetime.timezone.utc)
        r = e - datetime.datetime.now(datetime.timezone.utc)
        if r.total_seconds() <= 0: return "ended"
        h, m = int(r.total_seconds()//3600), int((r.total_seconds()%3600)//60)
        return f"{h}h {m}m"
    except: return "?"

# ─── Telegram Handlers (TUTTI INCLUSI) ─────────────────────
# ... incolla qui tutti gli handler: start, lb_alltime_callback, lb_season_callback,
# shop_callback, prize_callback, daily_callback, squad_callback, back_callback,
# noop_callback, pay_command, prizepool_command, new_season_command, sprint_command,
# error_handler
# (sono esattamente gli stessi del file precedente, non li ripeto per brevità)

# ─── Webhook endpoint ────────────────────────────────────
telegram_app = None

@flask_app.route('/telegram', methods=['POST'])
def telegram_webhook():
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET not set.")
    elif request.headers.get('X-Telegram-Bot-Api-Secret-Token') != WEBHOOK_SECRET:
        return 'Unauthorized', 401

    if telegram_app is None:
        return 'Bot not ready', 503

    update_data = request.get_json(force=True)
    async def process():
        update = Update.de_json(update_data, telegram_app.bot)
        await telegram_app.process_update(update)

    try:
        # Crea un nuovo event loop per questa richiesta (evita problemi di loop chiusi)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(process())
        loop.close()
        return 'OK', 200
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return 'Error', 500

async def set_webhook(app):
    webhook_url = f"{WEBHOOK_URL}/telegram"
    await app.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    logger.info(f"Webhook set to {webhook_url}")

# ─── Inizializzazione del bot in un thread separato ───────
def init_bot():
    global telegram_app
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_app = app

    # Aggiungi tutti gli handler (invariati)
    # ...

    # Avvia il bot e imposta il webhook
    async def startup():
        await app.initialize()
        await app.start()
        if WEBHOOK_URL:
            await set_webhook(app)

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(startup())
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()

# Avvia il bot all'importazione
init_bot()

# ─── Avvio Flask nel main thread ──────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
