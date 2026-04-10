"""
🔥 Arena Bot v3.2 — Webhook Only (no polling)
- Flask con view sincrona che esegue asyncio.run()
- Webhook impostato all'avvio
- Nessun polling, nessun conflitto
"""

import os
import logging
import datetime
import threading
import asyncio
import httpx
from flask import Flask, request, jsonify
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

@flask_app.route('/health')
def health():
    return 'OK', 200

@flask_app.route('/api/verify-payment', methods=['POST'])
def verify_payment():
    try:
        data = request.get_json(force=True)
        tx_hash = data.get("tx_hash", "").strip()
        item_id = data.get("item_id", "").strip().lower()
        user_id = int(data.get("user_id", 0))

        if not tx_hash or not item_id or not user_id:
            return jsonify({"ok": False, "error": "Missing fields"}), 400
        if item_id not in SHOP_ITEMS:
            return jsonify({"ok": False, "error": "Unknown item"}), 400
        if not tx_hash.startswith("0x") or len(tx_hash) < 40:
            return jsonify({"ok": False, "error": "Invalid tx hash"}), 400

        import requests as req_lib

        r = req_lib.get(
            f"{SUPABASE_URL}/rest/v1/payments",
            headers=HEADERS,
            params={"tx_hash": f"eq.{tx_hash}"}
        )
        if r.ok and r.json():
            return jsonify({"ok": False, "error": "TX already used"}), 409

        avax_price, _, _, action, value = SHOP_ITEMS[item_id]
        avax_wei = int(avax_price * 1e18)

        rpc_payload = {
            "jsonrpc": "2.0", "method": "eth_getTransactionByHash",
            "params": [tx_hash], "id": 1
        }
        rpc_r = req_lib.post(AVAX_RPC, json=rpc_payload, timeout=10)
        tx_data = rpc_r.json().get("result")
        if not tx_data:
            return jsonify({"ok": False, "error": "TX not found on chain"}), 404

        to_addr = (tx_data.get("to") or "").lower()
        if to_addr != TREASURY_ADDR.lower():
            return jsonify({"ok": False, "error": "Wrong destination address"}), 400

        tx_value = int(tx_data.get("value", "0x0"), 16)
        if tx_value < avax_wei * 0.99:
            return jsonify({"ok": False, "error": f"Insufficient amount. Expected ~{avax_price} AVAX"}), 400

        req_lib.post(
            f"{SUPABASE_URL}/rest/v1/payments",
            headers=HEADERS,
            json={"user_id": user_id, "tx_hash": tx_hash,
                  "amount_avax": avax_price, "item": item_id, "verified": True}
        )

        user_r = req_lib.get(
            f"{SUPABASE_URL}/rest/v1/users",
            headers=HEADERS,
            params={"id": f"eq.{user_id}"}
        )
        if not user_r.ok or not user_r.json():
            return jsonify({"ok": False, "error": "User not found"}), 404
        user_data = user_r.json()[0]

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if action == "energy":
            new_energy = min(user_data.get("energy", 0) + value, 100)
            req_lib.patch(
                f"{SUPABASE_URL}/rest/v1/users?id=eq.{user_id}",
                headers=HEADERS,
                json={"energy": new_energy, "last_energy_update": now_iso}
            )
            _sync_prize_pool(req_lib, avax_price)
            return jsonify({"ok": True, "item": item_id, "energy": new_energy})

        elif action == "upgrade":
            current_power = user_data.get("tap_power", 1)
            if value <= current_power:
                return jsonify({"ok": False, "error": "Already owned", "tap_power": current_power}), 409
            req_lib.patch(
                f"{SUPABASE_URL}/rest/v1/users?id=eq.{user_id}",
                headers=HEADERS,
                json={"tap_power": value}
            )
            _sync_prize_pool(req_lib, avax_price)
            return jsonify({"ok": True, "item": item_id, "tap_power": value})

    except Exception as e:
        logger.error(f"verify-payment error: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

def _sync_prize_pool(req_lib, avax_amount: float):
    r = req_lib.get(f"{SUPABASE_URL}/rest/v1/prize_pool", headers=HEADERS, params={"id": "eq.1"})
    if r.ok and r.json():
        current = float(r.json()[0].get("total_avax", 0))
        req_lib.patch(
            f"{SUPABASE_URL}/rest/v1/prize_pool?id=eq.1",
            headers=HEADERS,
            json={"total_avax": current + avax_amount,
                  "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        )

# ═══════════════════════════════════════════════════════════
#  SUPABASE HELPERS (async)
# ═══════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════
#  USER HELPERS
# ═══════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════
#  HANDLER TELEGRAM (tutti async)
# ═══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref = None
    if context.args:
        try: ref = int(context.args[0].replace("ref_", ""))
        except: pass
    db_user, is_new = await ensure_user(user, ref)
    bot_name = (await context.bot.get_me()).username
    sprint = await get_sprint()

    text = (
        f"{'🎉 <b>Welcome to the Arena!</b>' if is_new else '👋 <b>Welcome back!</b>'}\n\n"
        f"💰 Total Coins: <b>{db_user.get('coins',0):,}</b>\n"
        f"🏁 Season Coins: <b>{db_user.get('season_coins',0):,}</b>\n"
        f"⚡ Tap Power: <b>x{db_user.get('tap_power',1)}</b>\n"
        f"👥 Squad: <b>{db_user.get('referral_count',0)}</b> members\n\n"
        f"🔗 Your invite link:\n"
        f"<code>https://t.me/{bot_name}?start=ref_{user.id}</code>\n\n"
        f"<i>Invite friends → +1,000 coins + 5% passive!</i>"
    )
    if sprint:
        text = f"⚡ <b>SPRINT: {sprint['name']}</b> — {remaining(sprint['ends_at'])} left!\n\n" + text

    await update.message.reply_text(text, parse_mode="HTML",
                                    reply_markup=main_keyboard(MINIAPP_URL, user.id))

async def lb_alltime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    top = await db_leaderboard("coins", 10)
    medals = ["🥇","🥈","🥉"] + ["🔥"]*7
    lines = ["<b>🌍 ALL-TIME LEADERBOARD</b>\n<i>Never resets</i>\n\n"]
    for i, u in enumerate(top):
        name = (u.get("username") or u.get("first_name") or "Anon")[:14]
        pw = f" ⚡x{u.get('tap_power',1)}" if u.get('tap_power',1) > 1 else ""
        lines.append(f"{medals[i]} <code>{name}</code> <b>{u.get('coins',0):,}</b>{pw}\n")
    await query.edit_message_text("".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏁 Season LB", callback_data="lb_season")],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ]))

async def lb_season_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    top = await db_leaderboard("season_coins", 10)
    pool = await db_get("prize_pool", {"id": "eq.1"})
    pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0
    seasons = await db_get("seasons", {"is_active": "eq.true", "order": "id.desc", "limit": "1"})
    sname = seasons[0]["name"] if seasons else "Season 1"
    sprint = await get_sprint()
    medals = ["🥇","🥈","🥉"] + ["🔥"]*7

    lines = [f"<b>🏁 {sname.upper()}</b>\n",
             f"🏆 Prize Pool: <b>{pool_avax:.2f} AVAX</b>\n",
             "<i>Resets monthly — personal coins kept</i>\n\n"]

    if sprint:
        sprint_top = await db_leaderboard("sprint_coins", 5)
        lines.append(f"⚡ <b>SPRINT: {sprint['name']}</b> — {remaining(sprint['ends_at'])}\n")
        for i, u in enumerate(sprint_top):
            name = (u.get("username") or u.get("first_name") or "Anon")[:14]
            lines.append(f"{medals[i]} <code>{name}</code> <b>{u.get('sprint_coins',0):,}</b> ⚡\n")
        lines.append("\n<b>Season Top 10:</b>\n")

    for i, u in enumerate(top):
        name = (u.get("username") or u.get("first_name") or "Anon")[:12]
        pw = f" ⚡x{u.get('tap_power',1)}" if u.get('tap_power',1) > 1 else ""
        pct = PRIZE_DIST[i] if i < len(PRIZE_DIST) else 0
        prize = f" ~{pool_avax*pct:.3f}A" if pool_avax > 0 else ""
        lines.append(f"{medals[i]} <code>{name}</code> <b>{u.get('season_coins',0):,}</b>{pw}{prize}\n")

    await query.edit_message_text("".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌍 All-Time LB", callback_data="lb_alltime")],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ]))

async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pool = await db_get("prize_pool", {"id": "eq.1"})
    pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0
    treasury = TREASURY_ADDR or "not configured"

    text = (
        f"💰 <b>Arena Shop</b>\n\n"
        f"🏆 Prize Pool: <b>{pool_avax:.2f} AVAX</b>\n"
        f"<i>All purchases fund the prize pool!</i>\n\n"
        f"📤 <b>Treasury Address:</b>\n<code>{treasury}</code>\n\n"
        f"⚡ <b>Energy Refills:</b>\n"
        f"• energy_25 — +25 Energy | <b>0.05 AVAX</b> / 500 $ARENA\n"
        f"• energy_100 — Full Tank | <b>0.15 AVAX</b> / 1,500 $ARENA\n\n"
        f"🚀 <b>Tap Power Upgrades</b> <i>(resets each season)</i>:\n"
        f"• upgrade_2 — Power x2 | <b>0.3 AVAX</b> / 3,000 $ARENA\n"
        f"• upgrade_5 — Power x5 | <b>0.8 AVAX</b> / 8,000 $ARENA\n"
        f"• upgrade_10 — Power x10 | <b>2.0 AVAX</b> / 20,000 $ARENA\n"
        f"• upgrade_25 — Power x25 | <b>5.0 AVAX</b> / 50,000 $ARENA\n\n"
        f"🎮 <b>Buy directly in the app</b> — tap PLAY NOW then Shop!\n\n"
        f"📲 <b>Manual payment:</b>\n"
        f"1. Send AVAX to treasury above\n"
        f"2. Copy TX hash\n"
        f"3. Send: <code>/pay TX_HASH ITEM_ID</code>"
    )
    await query.edit_message_text(text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Open Shop in App",
                                  web_app=WebAppInfo(url=f"{MINIAPP_URL}?uid=0&shop=1"))],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ]))

async def prize_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pool = await db_get("prize_pool", {"id": "eq.1"})
    avax = float(pool[0].get("total_avax", 0)) if pool else 0.0
    arena = int(pool[0].get("total_arena", 0)) if pool else 0
    await query.edit_message_text(
        f"🏆 <b>Arena Prize Pool</b>\n\n"
        f"💎 AVAX: <b>{avax:.4f}</b>\n"
        f"⚡ $ARENA: <b>{arena:,}</b>\n\n"
        f"<b>Season end distribution:</b>\n"
        f"🥇 1st — 40% (<b>{avax*0.40:.3f} AVAX</b>)\n"
        f"🥈 2nd — 20% (<b>{avax*0.20:.3f} AVAX</b>)\n"
        f"🥉 3rd — 10% (<b>{avax*0.10:.3f} AVAX</b>)\n"
        f"🔥 4th–10th — share 30%\n\n"
        f"<i>All shop purchases → prize pool.\nCoins accumulate forever → future $ARENA.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back")]]))

async def daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    db_user = await db_get("users", {"id": f"eq.{user.id}"})
    if not db_user:
        await query.answer("Use /start first!", show_alert=True); return

    data = db_user[0]
    now = datetime.datetime.now(datetime.timezone.utc)
    streak = data.get("streak", 0)
    last_raw = data.get("last_claim")

    if last_raw:
        last_dt = datetime.datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
        if last_dt.tzinfo is None: last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
        diff_h = (now - last_dt).total_seconds() / 3600
        if diff_h < 24:
            ore, mins = int(24-diff_h), int((24-diff_h-int(24-diff_h))*60)
            bar = "🔥"*streak + "⬜"*(7-streak)
            await query.edit_message_text(
                f"⏳ <b>Daily already claimed!</b>\n\nCome back in <b>{ore}h {mins}m</b>\n\n"
                f"📅 Streak: <b>{streak}/7</b>\n{bar}\n\n<i>Day 7 = 1,000 coins! ⭐</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back")]]))
            return
        elif diff_h > 48:
            streak = 0

    streak = min(streak + 1, 7)
    bonus = STREAK_BONUS.get(streak, 1000)
    await db_update("users", {"id": f"eq.{user.id}"}, {
        "coins": data["coins"]+bonus, "season_coins": data.get("season_coins",0)+bonus,
        "last_claim": now.isoformat(), "streak": streak
    })
    if data.get("referred_by"):
        passive = int(bonus * 0.05)
        ref = await db_get("users", {"id": f"eq.{data['referred_by']}"})
        if ref:
            await db_update("users", {"id": f"eq.{data['referred_by']}"}, {
                "coins": ref[0]["coins"]+passive,
                "season_coins": ref[0].get("season_coins",0)+passive
            })

    bar = "🔥"*streak + "⬜"*(7-streak)
    tomorrow = STREAK_BONUS.get(min(streak+1,7), 1000)
    extra = "\n⭐ <b>MAX STREAK!</b>" if streak == 7 else ""
    await query.edit_message_text(
        f"✅ <b>Daily reward claimed!</b>\n\n💰 +<b>{bonus:,}</b> coins!\n\n"
        f"📅 Streak: <b>{streak}/7</b>\n{bar}{extra}\n\nTomorrow: +<b>{tomorrow:,}</b> coins",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back")]]))

async def squad_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    bot_name = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_name}?start=ref_{user.id}"
    db_user = await db_get("users", {"id": f"eq.{user.id}"})
    data = db_user[0] if db_user else {}
    refs = await db_get("users", {"referred_by": f"eq.{user.id}", "order": "coins.desc", "limit": "10"})
    total = sum(r.get("coins",0) for r in refs)
    passive = int(total * 0.05)
    text = (
        f"👥 <b>Your Squad</b>\n\n"
        f"Members: <b>{len(refs)}</b>\n"
        f"Squad coins: <b>{total:,}</b>\n"
        f"Your passive (5%): <b>~{passive:,}</b>\n\n"
        f"🔗 Invite link:\n<code>{ref_link}</code>\n\n"
    )
    if refs:
        text += "<b>Members:</b>\n"
        for r in refs:
            name = (r.get("username") or r.get("first_name") or "Anon")[:15]
            text += f"• <code>{name}</code> — {r.get('coins',0):,} coins\n"
    else:
        text += "<i>No members yet — share your link!</i> 👆"
    await query.edit_message_text(text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Invite", url=f"https://t.me/share/url?url={ref_link}&text=🔥 Join Arena!")],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ]))

async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    db_user = await db_get("users", {"id": f"eq.{user.id}"})
    data = db_user[0] if db_user else {}
    bot_name = (await context.bot.get_me()).username
    sprint = await get_sprint()
    streak = data.get("streak", 0)
    s = "🔥"*min(streak,7) or "—"
    sp = f"\n⚡ <b>SPRINT: {sprint['name']}</b>\n" if sprint else ""
    text = (
        f"👋 <b>Welcome back, Gladiator!</b>{sp}\n"
        f"💰 Total Coins: <b>{data.get('coins',0):,}</b>\n"
        f"🏁 Season Coins: <b>{data.get('season_coins',0):,}</b>\n"
        f"⚡ Tap Power: <b>x{data.get('tap_power',1)}</b>\n"
        f"👥 Squad: <b>{data.get('referral_count',0)}</b> members\n"
        f"📅 Streak: {s} <b>{streak}/7</b>\n\n"
        f"🔗 Invite link:\n<code>https://t.me/{bot_name}?start=ref_{user.id}</code>\n\n"
        f"<i>Invite friends → +1,000 coins + 5% passive!</i>"
    )
    await query.edit_message_text(text, parse_mode="HTML",
                                  reply_markup=main_keyboard(MINIAPP_URL, user.id))

async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) < 2:
        items = "\n".join([f"• <code>{k}</code> — {v[2]} | <b>{v[0]} AVAX</b>"
                           for k, v in SHOP_ITEMS.items()])
        await update.message.reply_text(
            f"❌ <b>Usage:</b> <code>/pay TX_HASH ITEM_ID</code>\n\n"
            f"<b>Items:</b>\n{items}\n\n"
            f"📤 Treasury: <code>{TREASURY_ADDR}</code>",
            parse_mode="HTML"); return

    tx_hash = context.args[0].strip()
    item_id = context.args[1].strip().lower()

    if item_id not in SHOP_ITEMS:
        await update.message.reply_text(f"❌ Unknown item: <code>{item_id}</code>", parse_mode="HTML"); return
    if not tx_hash.startswith("0x") or len(tx_hash) < 40:
        await update.message.reply_text("❌ Invalid TX hash format.", parse_mode="HTML"); return

    existing = await db_get("payments", {"tx_hash": f"eq.{tx_hash}"})
    if existing:
        await update.message.reply_text("❌ TX already used!"); return

    avax_price, _, label, action, value = SHOP_ITEMS[item_id]
    db_user_data = await db_get("users", {"id": f"eq.{user.id}"})
    if not db_user_data:
        await update.message.reply_text("❌ Send /start first!"); return
    data = db_user_data[0]

    await db_insert("payments", {
        "user_id": user.id, "tx_hash": tx_hash,
        "amount_avax": avax_price, "item": item_id, "verified": False
    })
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if action == "energy":
        new_energy = min(data.get("energy", 0) + value, 100)
        await db_update("users", {"id": f"eq.{user.id}"}, {"energy": new_energy, "last_energy_update": now_iso})
        await db_update("payments", {"tx_hash": f"eq.{tx_hash}"}, {"verified": True})
        pool = await db_get("prize_pool", {"id": "eq.1"})
        if pool:
            await db_update("prize_pool", {"id": "eq.1"}, {
                "total_avax": float(pool[0].get("total_avax",0)) + avax_price,
                "updated_at": now_iso
            })
        msg = f"✅ <b>{label}</b>\n\n⚡ +{value} Energy → <b>{new_energy}/100</b>\n<code>{tx_hash[:24]}...</code>"

    elif action == "upgrade":
        current = data.get("tap_power", 1)
        if value <= current:
            await update.message.reply_text(f"ℹ️ Already own Power x{current} or higher."); return
        await db_update("users", {"id": f"eq.{user.id}"}, {"tap_power": value})
        await db_update("payments", {"tx_hash": f"eq.{tx_hash}"}, {"verified": True})
        pool = await db_get("prize_pool", {"id": "eq.1"})
        if pool:
            await db_update("prize_pool", {"id": "eq.1"}, {
                "total_avax": float(pool[0].get("total_avax",0)) + avax_price,
                "updated_at": now_iso
            })
        msg = f"✅ <b>{label}</b>\n\n🚀 Tap Power → <b>x{value}</b>\n<code>{tx_hash[:24]}...</code>"
    else:
        msg = "✅ Done!"

    await update.message.reply_text(msg + "\n\n<i>Purchase added to prize pool! 🏆</i>",
                                    parse_mode="HTML", reply_markup=main_keyboard(MINIAPP_URL, user.id))

async def prizepool_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool = await db_get("prize_pool", {"id": "eq.1"})
    avax  = float(pool[0].get("total_avax", 0)) if pool else 0.0
    arena = int(pool[0].get("total_arena", 0)) if pool else 0
    await update.message.reply_text(
        f"🏆 <b>Arena Prize Pool</b>\n\n"
        f"💎 AVAX: <b>{avax:.4f}</b>\n"
        f"⚡ $ARENA: <b>{arena:,}</b>\n\n"
        f"🥇 1st — 40% (<b>{avax*0.40:.3f} AVAX</b>)\n"
        f"🥈 2nd — 20% (<b>{avax*0.20:.3f} AVAX</b>)\n"
        f"🥉 3rd — 10% (<b>{avax*0.10:.3f} AVAX</b>)\n"
        f"🔥 4th–10th — share 30%",
        parse_mode="HTML")

async def new_season_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only."); return

    season_name = (" ".join(context.args) if context.args
                   else f"Season {datetime.datetime.now().strftime('%B %Y')}")

    top = await db_leaderboard("season_coins", 10)
    pool = await db_get("prize_pool", {"id": "eq.1"})
    pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0

    await db_update("seasons", {"is_active": "eq.true"}, {
        "is_active": False,
        "ended_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })

    new_s = await db_insert("seasons", {"name": season_name})

    medals = ["🥇","🥈","🥉"] + ["🔥"]*7
    recap = [f"🏁 <b>Season Ended!</b>\n\n"]
    for i, u in enumerate(top):
        pct = PRIZE_DIST[i] if i < len(PRIZE_DIST) else 0
        prize = pool_avax * pct
        await db_insert("season_results", {
            "season_id": new_s.get("id", 0), "user_id": u.get("id", 0),
            "username": u.get("username",""), "first_name": u.get("first_name","Anon"),
            "final_coins": u.get("season_coins",0), "rank": i+1, "prize_avax": prize
        })
        name = (u.get("username") or u.get("first_name") or "Anon")[:15]
        recap.append(f"{medals[i]} <code>{name}</code> {u.get('season_coins',0):,} → <b>{prize:.3f} AVAX</b>\n")

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.patch(
                f"{SUPABASE_URL}/rest/v1/users",
                headers={**HEADERS, "Prefer": "return=minimal"},
                params={"id": "neq.0"},
                json={"season_coins": 0, "sprint_coins": 0, "tap_power": 1}
            )
    except Exception as e:
        logger.error(f"Season reset error: {e}")

    await db_update("prize_pool", {"id": "eq.1"}, {
        "total_avax": 0, "total_arena": 0,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })

    recap.append(f"\n🆕 <b>{season_name} begins!</b>\n<i>Season coins + tap power reset. Personal coins KEPT.</i>")
    await update.message.reply_text("".join(recap), parse_mode="HTML")

async def sprint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only."); return

    hours = 24
    name = "⚡ Arena Sprint"
    if context.args:
        try: hours = int(context.args[0])
        except: pass
        if len(context.args) > 1: name = " ".join(context.args[1:])

    await db_update("sprints", {"is_active": "eq.true"}, {"is_active": False})
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.patch(f"{SUPABASE_URL}/rest/v1/users",
                          headers={**HEADERS, "Prefer": "return=minimal"},
                          params={"id": "neq.0"}, json={"sprint_coins": 0})
    except Exception as e:
        logger.error(f"Sprint reset: {e}")

    ends_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
    await db_insert("sprints", {
        "name": name, "ends_at": ends_at.isoformat(),
        "is_active": True, "prize_description": "Top 3 special rewards!"
    })
    await update.message.reply_text(
        f"🏁 <b>Sprint Launched!</b>\n\n📛 <b>{name}</b>\n"
        f"⏱ Duration: <b>{hours}h</b>\n"
        f"⌛ Ends: <code>{ends_at.strftime('%d %b %Y %H:%M UTC')}</code>",
        parse_mode="HTML")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning(f"Network error (ignored): {context.error}"); return
    logger.error(f"Unhandled: {context.error}", exc_info=context.error)

# ═══════════════════════════════════════════════════════════
#  WEBHOOK ENDPOINT (sincrono che esegue async)
# ═══════════════════════════════════════════════════════════

telegram_app = None

@flask_app.route('/telegram', methods=['POST'])
def telegram_webhook():
    """View sincrona che esegue l'handler asincrono."""
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

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port, threaded=True)

def main():
    global telegram_app

    # Avvia Flask in thread separato
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    # Crea Application Telegram
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_app = app

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

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(app.initialize())
        loop.run_until_complete(app.start())
        if WEBHOOK_URL:
            loop.run_until_complete(set_webhook(app))
            print(f"🔥 Arena Bot v3.2 — Webhook active at {WEBHOOK_URL}/telegram")
        else:
            raise RuntimeError("WEBHOOK_URL must be set!")
        # Mantiene il thread principale vivo per Flask (che gira nel thread separato)
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(app.stop())
        loop.close()

if __name__ == "__main__":
    main()
