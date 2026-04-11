"""
🔥 Arena Bot v3.5 — Stable Webhook Edition
- Nessun polling, nessuna modifica al loop
- Webhook endpoint sincrono che esegue asyncio.run()
- Avvio del bot e impostazione webhook in un thread separato
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
WEBHOOK_URL    = os.getenv("WEBHOOK_URL")
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
    # ... (invariato, lo stesso codice che hai già)
    pass  # sostituisci con la tua implementazione completa

# ─── Supabase helpers (async) ─────────────────────────────
# ... (tutte le funzioni db_get, db_insert, db_update, db_leaderboard invariati)

# ─── User helpers ────────────────────────────────────────
# ... (ensure_user, main_keyboard, get_sprint, remaining invariati)

# ─── Telegram Handlers ───────────────────────────────────
# ... (start, lb_alltime_callback, lb_season_callback, shop_callback, prize_callback,
#      daily_callback, squad_callback, back_callback, noop_callback,
#      pay_command, prizepool_command, new_season_command, sprint_command, error_handler)

# ─── Webhook endpoint (versione stabile) ─────────────────
telegram_app = None

@flask_app.route('/telegram', methods=['POST'])
def telegram_webhook():
    """Riceve gli update da Telegram e li processa in modo asincrono."""
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET not set. Accepting any request.")
    elif request.headers.get('X-Telegram-Bot-Api-Secret-Token') != WEBHOOK_SECRET:
        return 'Unauthorized', 401

    if telegram_app is None:
        return 'Bot not ready', 503

    update_data = request.get_json(force=True)

    async def process():
        update = Update.de_json(update_data, telegram_app.bot)
        await telegram_app.process_update(update)

    try:
        asyncio.run(process())
        return 'OK', 200
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return 'Error', 500

async def set_webhook(app):
    webhook_url = f"{WEBHOOK_URL}/telegram"
    await app.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    logger.info(f"Webhook set to {webhook_url}")

# ─── Inizializzazione bot (thread separato) ───────────────
def init_bot():
    global telegram_app

    app = Application.builder().token(BOT_TOKEN).build()
    telegram_app = app

    # Aggiungi tutti gli handler
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("pay",       pay_command))
    app.add_handler(CommandHandler("prizepool", prizepool_command))
    app.add_handler(CommandHandler("newseason", new_season_command))
    app.add_handler(CommandHandler("sprint",    sprint_command))

    app.add_handler(CallbackQueryHandler(lb_alltime_callback, pattern="^lb_alltime$"))
    app.add_handler(CallbackQueryHandler(lb_season_callback,  pattern="^lb_season$"))
    app.add_handler(CallbackQueryHandler(squad_callback,      pattern="^squad$"))
    app.add_handler(CallbackQueryHandler(shop_callback,       pattern="^shop$"))
    app.add_handler(CallbackQueryHandler(daily_callback,      pattern="^daily$"))
    app.add_handler(CallbackQueryHandler(prize_callback,      pattern="^prize$"))
    app.add_handler(CallbackQueryHandler(back_callback,       pattern="^back$"))
    app.add_handler(CallbackQueryHandler(noop_callback,       pattern="^noop$"))
    app.add_error_handler(error_handler)

    # Avvia il bot e imposta il webhook in un thread separato
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(app.initialize())
        loop.run_until_complete(app.start())
        if WEBHOOK_URL:
            loop.run_until_complete(set_webhook(app))
        # Non chiamare loop.run_forever() perché Gunicorn gestisce il server web,
        # ma il bot deve rimanere attivo per processare gli update.
        # Usiamo un semplice loop infinito che mantiene il thread vivo.
        import time
        while True:
            time.sleep(3600)

    threading.Thread(target=run, daemon=True).start()

# Avvia l'inizializzazione quando il modulo viene importato
init_bot()
