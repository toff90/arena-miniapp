"""
🔥 Arena Bot v2.2
- Aggiunto server HTTP per Render (UptimeRobot)
- Fix: season_coins sincronizzati, due leaderboard separate,
       gestione NetworkError, /pay sistema pagamenti AVAX
"""

import os
import logging
import datetime
import httpx
import threading                     # ─── AGGIUNTO PER RENDER ───
from flask import Flask             # ─── AGGIUNTO PER RENDER ───
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton,
    InlineKeyboardMarkup, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes
)
from telegram.error import NetworkError, TimedOut

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN")
SUPABASE_URL  = os.getenv("SUPABASE_URL")
SUPABASE_KEY  = os.getenv("SUPABASE_SERVICE_KEY")
MINIAPP_URL   = os.getenv("MINIAPP_URL")
TREASURY_ADDR = os.getenv("TREASURY_ADDR", "0xTUO_WALLET_AVAX")
ADMIN_IDS     = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

# ─── Shop items ─────────────────────────────────────────────
SHOP_ITEMS = {
    "energy_25":  (0.05,   500, "⚡ Small Refill",  "+25 Energy",        "energy",  25),
    "energy_100": (0.15,  1500, "⚡⚡ Full Refill",  "Full tank 100/100", "energy",  100),
    "upgrade_2":  (0.30,  3000, "⚡ Power x2",       "2x coins per tap",  "upgrade", 2),
    "upgrade_5":  (0.80,  8000, "🔥 Power x5",       "5x coins per tap",  "upgrade", 5),
    "upgrade_10": (2.00, 20000, "💎 Power x10",      "10x — dominate",    "upgrade", 10),
    "upgrade_25": (5.00, 50000, "🚀 Power x25",      "Arena Legend",      "upgrade", 25),
}

STREAK_BONUS = {1:250, 2:250, 3:250, 4:250, 5:250, 6:250, 7:1000}
PRIZE_DIST   = [0.40, 0.20, 0.10, 0.05, 0.05, 0.05, 0.05, 0.03, 0.03, 0.04]


# ═══════════════════════════════════════════════════════════
#  HEALTH SERVER (per Render / UptimeRobot)
# ═══════════════════════════════════════════════════════════

health_app = Flask(__name__)

@health_app.route('/health')
def health():
    return 'OK', 200

def run_health_server():
    port = int(os.environ.get('PORT', 10000))
    health_app.run(host='0.0.0.0', port=port, threaded=True)


# ═══════════════════════════════════════════════════════════
#  SUPABASE HELPERS
# ═══════════════════════════════════════════════════════════

async def db_get(table: str, filters: dict = {}) -> list:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers=HEADERS, params=filters
            )
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.error(f"db_get error: {e}")
        return []


async def db_insert(table: str, data: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers=HEADERS, json=data
            )
        res = r.json()
        return res[0] if isinstance(res, list) and res else data
    except Exception as e:
        logger.error(f"db_insert error: {e}")
        return data


async def db_update(table: str, filters: dict, data: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.patch(
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers=HEADERS, params=filters, json=data
            )
        res = r.json()
        return res[0] if isinstance(res, list) and res else {}
    except Exception as e:
        logger.error(f"db_update error: {e}")
        return {}


async def db_get_leaderboard(order_by: str, limit: int = 10) -> list:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/users",
                headers=HEADERS,
                params={
                    "select": "username,first_name,coins,season_coins,sprint_coins,tap_power",
                    "order":  f"{order_by}.desc",
                    "limit":  str(limit)
                }
            )
        data = r.json() if r.status_code == 200 else []
        return sorted(data, key=lambda x: x.get(order_by, 0), reverse=True)
    except Exception as e:
        logger.error(f"leaderboard error: {e}")
        return []


# ═══════════════════════════════════════════════════════════
#  USER HELPERS
# ═══════════════════════════════════════════════════════════

async def ensure_user(user, referred_by: int = None):
    existing = await db_get("users", {"id": f"eq.{user.id}"})
    if not existing:
        new_user = {
            "id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "Anon",
            "coins": 500,
            "season_coins": 500,
            "tap_power": 1,
            "referral_count": 0,
            "streak": 0,
            "energy": 100,
        }
        if referred_by and referred_by != user.id:
            new_user["referred_by"] = referred_by
            referrer = await db_get("users", {"id": f"eq.{referred_by}"})
            if referrer:
                r = referrer[0]
                await db_update("users", {"id": f"eq.{referred_by}"}, {
                    "coins":          r["coins"] + 1_000,
                    "season_coins":   r.get("season_coins", 0) + 1_000,
                    "referral_count": r.get("referral_count", 0) + 1
                })
        result = await db_insert("users", new_user)
        return result or new_user, True
    return existing[0], False


def main_keyboard(miniapp_url: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🎮 PLAY NOW",
            web_app=WebAppInfo(url=f"{miniapp_url}?uid={user_id}")
        )],
        [
            InlineKeyboardButton("🌍 All-Time",  callback_data="lb_alltime"),
            InlineKeyboardButton("🏁 Season",    callback_data="lb_season"),
        ],
        [
            InlineKeyboardButton("👥 Squad",     callback_data="squad"),
            InlineKeyboardButton("📅 Daily",     callback_data="daily"),
        ],
        [
            InlineKeyboardButton("💰 Shop",      callback_data="shop"),
            InlineKeyboardButton("🏆 Prize",     callback_data="prize"),
        ],
    ])


def home_text(data: dict, ref_link: str, sprint: dict = None) -> str:
    streak = data.get("streak", 0)
    streak_str = "🔥" * min(streak, 7) if streak > 0 else "—"
    sprint_line = ""
    if sprint:
        sprint_line = f"\n⚡ *SPRINT ACTIVE: {sprint['name']}*\n"
    return (
        f"👋 *Welcome back, Gladiator!*{sprint_line}\n"
        f"💰 Total Coins: *{data.get('coins', 0):,}*\n"
        f"🏁 Season Coins: *{data.get('season_coins', 0):,}*\n"
        f"⚡ Tap Power: *x{data.get('tap_power', 1)}*\n"
        f"👥 Squad: *{data.get('referral_count', 0)}* members\n"
        f"📅 Streak: {streak_str} *{streak}/7*\n\n"
        f"🔗 Your invite link:\n`{ref_link}`\n\n"
        f"_Invite friends → +1,000 coins + 5% passive!_"
    )


async def get_active_sprint() -> dict | None:
    sprints = await db_get("sprints", {
        "is_active": "eq.true",
        "order": "started_at.desc",
        "limit": "1"
    })
    if not sprints:
        return None
    s = sprints[0]
    try:
        ends_at = datetime.datetime.fromisoformat(s["ends_at"].replace("Z", "+00:00"))
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=datetime.timezone.utc)
        if datetime.datetime.now(datetime.timezone.utc) > ends_at:
            await db_update("sprints", {"id": f"eq.{s['id']}"}, {"is_active": False})
            return None
    except Exception:
        return None
    return s


def _time_remaining(ends_at_str: str) -> str:
    try:
        ends_at = datetime.datetime.fromisoformat(ends_at_str.replace("Z", "+00:00"))
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=datetime.timezone.utc)
        remaining = ends_at - datetime.datetime.now(datetime.timezone.utc)
        if remaining.total_seconds() <= 0:
            return "ended"
        h = int(remaining.total_seconds() // 3600)
        m = int((remaining.total_seconds() % 3600) // 60)
        return f"{h}h {m}m"
    except Exception:
        return "?"


# ═══════════════════════════════════════════════════════════
#  /start
# ═══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referred_by = None
    if context.args:
        try:
            referred_by = int(context.args[0].replace("ref_", ""))
        except ValueError:
            pass

    db_user, is_new = await ensure_user(user, referred_by)
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    sprint = await get_active_sprint()

    text = (
        f"{'🎉 *Welcome to the Arena!*' if is_new else '👋 *Welcome back!*'}\n\n"
        f"💰 Total Coins: *{db_user.get('coins', 0):,}*\n"
        f"🏁 Season Coins: *{db_user.get('season_coins', 0):,}*\n"
        f"⚡ Tap Power: *x{db_user.get('tap_power', 1)}*\n"
        f"👥 Squad: *{db_user.get('referral_count', 0)}* members\n\n"
        f"🔗 Your invite link:\n`{ref_link}`\n\n"
        f"_Invite friends → +1,000 coins + 5% passive!_"
    )

    if sprint:
        remaining = _time_remaining(sprint["ends_at"])
        text = f"⚡ *SPRINT: {sprint['name']}* — {remaining} left!\n\n" + text

    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=main_keyboard(MINIAPP_URL, user.id)
    )


# ═══════════════════════════════════════════════════════════
#  LEADERBOARD ALL-TIME
# ═══════════════════════════════════════════════════════════

async def lb_alltime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    top = await db_get_leaderboard("coins", 10)
    medals = ["🥇","🥈","🥉"] + ["🔥"]*7
    lines = ["*🌍 ALL-TIME LEADERBOARD*\n",
             "_Total coins ever earned — never resets_\n\n"]

    for i, u in enumerate(top):
        name = u.get("username") or u.get("first_name") or "Anon"
        power = u.get("tap_power", 1)
        ps = f" ⚡x{power}" if power > 1 else ""
        lines.append(f"{medals[i]} `{name[:14]}` *{u.get('coins',0):,}*{ps}\n")

    await query.edit_message_text(
        "".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏁 Season LB", callback_data="lb_season")],
            [InlineKeyboardButton("🔙 Back",       callback_data="back")]
        ])
    )


# ═══════════════════════════════════════════════════════════
#  LEADERBOARD STAGIONE
# ═══════════════════════════════════════════════════════════

async def lb_season_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    top    = await db_get_leaderboard("season_coins", 10)
    pool   = await db_get("prize_pool", {"id": "eq.1"})
    pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0

    seasons = await db_get("seasons", {"is_active": "eq.true", "order": "id.desc", "limit": "1"})
    season_name = seasons[0]["name"] if seasons else "Season 1"
    sprint = await get_active_sprint()

    medals = ["🥇","🥈","🥉"] + ["🔥"]*7
    lines = [
        f"*🏁 {season_name.upper()}*\n",
        f"🏆 Prize Pool: *{pool_avax:.2f} AVAX*\n",
        "_Resets monthly — personal coins kept_\n\n",
    ]

    if sprint:
        sprint_top = await db_get_leaderboard("sprint_coins", 5)
        remaining = _time_remaining(sprint["ends_at"])
        lines.append(f"⚡ *SPRINT: {sprint['name']}* — {remaining}\n")
        for i, u in enumerate(sprint_top):
            name = u.get("username") or u.get("first_name") or "Anon"
            lines.append(f"{medals[i]} `{name[:14]}` *{u.get('sprint_coins',0):,}* ⚡\n")
        lines.append("\n*Season Top 10:*\n")

    for i, u in enumerate(top):
        name = u.get("username") or u.get("first_name") or "Anon"
        power = u.get("tap_power", 1)
        ps = f" ⚡x{power}" if power > 1 else ""
        pct = PRIZE_DIST[i] if i < len(PRIZE_DIST) else 0
        prize_str = f" ~{pool_avax*pct:.3f}A" if pool_avax > 0 else ""
        lines.append(
            f"{medals[i]} `{name[:12]}` *{u.get('season_coins',0):,}*{ps}{prize_str}\n"
        )

    await query.edit_message_text(
        "".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌍 All-Time LB", callback_data="lb_alltime")],
            [InlineKeyboardButton("🔙 Back",         callback_data="back")]
        ])
    )


# ═══════════════════════════════════════════════════════════
#  PRIZE POOL
# ═══════════════════════════════════════════════════════════

async def prize_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pool = await db_get("prize_pool", {"id": "eq.1"})
    avax  = float(pool[0].get("total_avax", 0))  if pool else 0.0
    arena = int(pool[0].get("total_arena", 0))    if pool else 0

    lines = [
        "🏆 *Arena Prize Pool*\n\n",
        f"💎 AVAX: *{avax:.4f}*\n",
        f"⚡ $ARENA: *{arena:,}*\n\n",
        "*Season end distribution:*\n",
        f"🥇 1st — 40% (*{avax*0.40:.3f} AVAX*)\n",
        f"🥈 2nd — 20% (*{avax*0.20:.3f} AVAX*)\n",
        f"🥉 3rd — 10% (*{avax*0.10:.3f} AVAX*)\n",
        f"🔥 4th–10th — share 30%\n\n",
        "_All AVAX shop purchases → prize pool_\n",
        "_Your coins accumulate forever → future $ARENA_",
    ]

    await query.edit_message_text(
        "".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])
    )


# ═══════════════════════════════════════════════════════════
#  /pay
# ═══════════════════════════════════════════════════════════

async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if len(context.args) < 2:
        items_list = "\n".join(
            [f"• `{k}` — {v[2]} | *{v[0]} AVAX* / {v[1]:,} $ARENA"
             for k, v in SHOP_ITEMS.items()]
        )
        await update.message.reply_text(
            f"❌ *Usage:* `/pay TX\\_HASH ITEM`\n\n"
            f"Example:\n`/pay 0xabc123... energy\\_100`\n\n"
            f"*Available items:*\n{items_list}\n\n"
            f"📤 Send to: `{TREASURY_ADDR}`",
            parse_mode="Markdown"
        )
        return

    tx_hash = context.args[0].strip()
    item_id = context.args[1].strip().lower()

    if item_id not in SHOP_ITEMS:
        await update.message.reply_text(
            f"❌ Unknown item: `{item_id}`\n"
            f"Valid: {', '.join([f'`{k}`' for k in SHOP_ITEMS])}",
            parse_mode="Markdown"
        )
        return

    if not tx_hash.startswith("0x") or len(tx_hash) < 40:
        await update.message.reply_text(
            "❌ Invalid TX hash. Must start with `0x` and be 66 chars.",
            parse_mode="Markdown"
        )
        return

    existing = await db_get("payments", {"tx_hash": f"eq.{tx_hash}"})
    if existing:
        await update.message.reply_text("❌ This TX hash has already been used!")
        return

    item = SHOP_ITEMS[item_id]
    avax_price, arena_price, label, desc, action, value = item

    db_user_data = await db_get("users", {"id": f"eq.{user.id}"})
    if not db_user_data:
        await update.message.reply_text("❌ Send /start first!")
        return
    data = db_user_data[0]

    await db_insert("payments", {
        "user_id":     user.id,
        "tx_hash":     tx_hash,
        "amount_avax": avax_price,
        "item":        item_id,
        "verified":    False
    })

    msg = await _apply_item(user.id, data, action, value, tx_hash, avax_price, label)
    await update.message.reply_text(
        msg, parse_mode="Markdown",
        reply_markup=main_keyboard(MINIAPP_URL, user.id)
    )


async def _apply_item(user_id, data, action, value, tx_hash, avax_price, label) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if action == "energy":
        new_energy = min(data.get("energy", 0) + value, 100)
        await db_update("users", {"id": f"eq.{user_id}"}, {
            "energy": new_energy,
            "last_energy_update": now
        })
        await db_update("payments", {"tx_hash": f"eq.{tx_hash}"}, {"verified": True})
        await _add_to_prize_pool(avax_price)
        return (
            f"✅ *{label} Confirmed!*\n\n"
            f"⚡ +{value} Energy → *{new_energy}/100*\n"
            f"TX: `{tx_hash[:24]}...`\n\n"
            f"_Purchase added to prize pool! 🏆_"
        )

    elif action == "upgrade":
        current = data.get("tap_power", 1)
        if value <= current:
            await db_update("payments", {"tx_hash": f"eq.{tx_hash}"}, {"verified": True})
            return f"ℹ️ You already own *Power x{current}* or higher!\n_TX logged — contact admin for refund._"
        await db_update("users", {"id": f"eq.{user_id}"}, {"tap_power": value})
        await db_update("payments", {"tx_hash": f"eq.{tx_hash}"}, {"verified": True})
        await _add_to_prize_pool(avax_price)
        return (
            f"✅ *{label} Confirmed!*\n\n"
            f"🚀 Tap Power → *x{value}*\n"
            f"Each tap earns *{value}x* more coins!\n"
            f"TX: `{tx_hash[:24]}...`\n\n"
            f"_Purchase added to prize pool! 🏆_"
        )
    return "✅ Done!"


async def _add_to_prize_pool(avax: float):
    pool = await db_get("prize_pool", {"id": "eq.1"})
    if pool:
        current = float(pool[0].get("total_avax", 0))
        await db_update("prize_pool", {"id": "eq.1"}, {
            "total_avax": current + avax,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        })


# ═══════════════════════════════════════════════════════════
#  DAILY REWARD + STREAK
# ═══════════════════════════════════════════════════════════

async def daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    db_user = await db_get("users", {"id": f"eq.{user.id}"})
    if not db_user:
        await query.answer("Use /start first!", show_alert=True)
        return

    data   = db_user[0]
    now    = datetime.datetime.now(datetime.timezone.utc)
    streak = data.get("streak", 0)
    last_raw = data.get("last_claim")

    if last_raw:
        last_dt = datetime.datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
        diff_h = (now - last_dt).total_seconds() / 3600

        if diff_h < 24:
            ore  = int(24 - diff_h)
            mins = int((24 - diff_h - ore) * 60)
            bar  = "🔥" * streak + "⬜" * (7 - streak)
            await query.edit_message_text(
                f"⏳ *Daily already claimed!*\n\n"
                f"Come back in *{ore}h {mins}m*\n\n"
                f"📅 Streak: *{streak}/7*\n{bar}\n\n"
                f"_Day 7 = 1,000 coins!_ ⭐",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="back")]
                ])
            )
            return
        elif diff_h > 48:
            streak = 0

    streak = min(streak + 1, 7)
    bonus  = STREAK_BONUS.get(streak, 1000)

    await db_update("users", {"id": f"eq.{user.id}"}, {
        "coins":        data["coins"] + bonus,
        "season_coins": data.get("season_coins", 0) + bonus,
        "last_claim":   now.isoformat(),
        "streak":       streak
    })

    if data.get("referred_by"):
        passive = int(bonus * 0.05)
        ref = await db_get("users", {"id": f"eq.{data['referred_by']}"})
        if ref:
            await db_update("users", {"id": f"eq.{data['referred_by']}"}, {
                "coins":        ref[0]["coins"] + passive,
                "season_coins": ref[0].get("season_coins", 0) + passive
            })

    bar      = "🔥" * streak + "⬜" * (7 - streak)
    tomorrow = STREAK_BONUS.get(min(streak + 1, 7), 1000)
    extra    = "\n⭐ *MAX STREAK! Bonus week!*" if streak == 7 else ""

    await query.edit_message_text(
        f"✅ *Daily reward claimed!*\n\n"
        f"💰 +*{bonus:,}* coins!\n\n"
        f"📅 Streak: *{streak}/7*\n{bar}{extra}\n\n"
        f"Tomorrow: +*{tomorrow:,}* coins\n"
        f"_Keep it alive!_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])
    )


# ═══════════════════════════════════════════════════════════
#  SHOP
# ═══════════════════════════════════════════════════════════

async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pool = await db_get("prize_pool", {"id": "eq.1"})
    pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0

    lines = [
        "💰 *Arena Shop*\n\n",
        f"🏆 Prize Pool: *{pool_avax:.2f} AVAX*\n",
        "_All purchases fund the prize pool!_\n\n",
        f"📤 *Treasury:* `{TREASURY_ADDR}`\n\n",
        "⚡ *Energy Refills:*\n",
        "• `energy_25` — +25 Energy | *0.05 AVAX* / 500 $ARENA\n",
        "• `energy_100` — Full Tank | *0.15 AVAX* / 1,500 $ARENA\n\n",
        "🚀 *Tap Power Upgrades* _(permanent, one-time)_:\n",
        "• `upgrade_2` — Power x2 | *0.3 AVAX* / 3,000 $ARENA\n",
        "• `upgrade_5` — Power x5 | *0.8 AVAX* / 8,000 $ARENA\n",
        "• `upgrade_10` — Power x10 | *2.0 AVAX* / 20,000 $ARENA\n",
        "• `upgrade_25` — Power x25 | *5.0 AVAX* / 50,000 $ARENA\n\n",
        "📲 *How to buy:*\n",
        "1. Send AVAX to the treasury address above\n",
        "2. Copy the TX hash from your wallet\n",
        "3. Send: `/pay TX_HASH ITEM_ID`\n\n",
        "_Example: `/pay 0xabc... energy_100`_",
    ]

    await query.edit_message_text(
        "".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])
    )


# ═══════════════════════════════════════════════════════════
#  SQUAD
# ═══════════════════════════════════════════════════════════

async def squad_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    db_user = await db_get("users", {"id": f"eq.{user.id}"})
    data = db_user[0] if db_user else {}

    refs = await db_get("users", {
        "referred_by": f"eq.{user.id}",
        "order": "coins.desc", "limit": "10"
    })

    total   = sum(r.get("coins", 0) for r in refs)
    passive = int(total * 0.05)

    text = (
        f"👥 *Your Squad*\n\n"
        f"Members: *{len(refs)}*\n"
        f"Squad total coins: *{total:,}*\n"
        f"Your passive (5%): *~{passive:,}* coins\n\n"
        f"🔗 Invite link:\n`{ref_link}`\n\n"
    )
    if refs:
        text += "*Members:*\n"
        for r in refs:
            name = r.get("username") or r.get("first_name") or "Anon"
            text += f"• `{name[:15]}` — {r.get('coins', 0):,} coins\n"
    else:
        text += "_No members yet — share your link!_ 👆"

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📤 Invite friends",
                url=f"https://t.me/share/url?url={ref_link}&text=🔥 Join my Arena squad!"
            )],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])
    )


# ═══════════════════════════════════════════════════════════
#  BACK
# ═══════════════════════════════════════════════════════════

async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    db_user = await db_get("users", {"id": f"eq.{user.id}"})
    data = db_user[0] if db_user else {}
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    sprint = await get_active_sprint()

    await query.edit_message_text(
        home_text(data, ref_link, sprint),
        parse_mode="Markdown",
        reply_markup=main_keyboard(MINIAPP_URL, user.id)
    )


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ═══════════════════════════════════════════════════════════
#  ADMIN — /newseason
# ═══════════════════════════════════════════════════════════

async def new_season_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return

    season_name = (" ".join(context.args) if context.args
                   else f"Season {datetime.datetime.now().strftime('%B %Y')}")

    top = await db_get_leaderboard("season_coins", 10)
    pool = await db_get("prize_pool", {"id": "eq.1"})
    pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0

    await db_update("seasons", {"is_active": "eq.true"}, {
        "is_active": False,
        "ended_at":  datetime.datetime.now(datetime.timezone.utc).isoformat()
    })

    new_s = await db_insert("seasons", {"name": season_name})

    medals = ["🥇","🥈","🥉"] + ["🔥"]*7
    recap  = [f"🏁 *Season Ended!*\n\n"]
    for i, u in enumerate(top):
        pct   = PRIZE_DIST[i] if i < len(PRIZE_DIST) else 0
        prize = pool_avax * pct
        await db_insert("season_results", {
            "season_id":   new_s.get("id", 0),
            "user_id":     u.get("id", 0),
            "username":    u.get("username", ""),
            "first_name":  u.get("first_name", "Anon"),
            "final_coins": u.get("season_coins", 0),
            "rank":        i + 1,
            "prize_avax":  prize
        })
        name = u.get("username") or u.get("first_name") or "Anon"
        recap.append(f"{medals[i]} `{name}` {u.get('season_coins',0):,} → *{prize:.3f} AVAX*\n")

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.patch(
                f"{SUPABASE_URL}/rest/v1/users",
                headers={**HEADERS, "Prefer": "return=minimal"},
                params={"id": "neq.0"},
                json={"season_coins": 0, "sprint_coins": 0}
            )
    except Exception as e:
        logger.error(f"Reset error: {e}")

    await db_update("prize_pool", {"id": "eq.1"}, {
        "total_avax": 0, "total_arena": 0,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })

    recap.append(f"\n🆕 *{season_name} begins!*\n_Season coins reset. Personal coins KEPT._")
    await update.message.reply_text("".join(recap), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════
#  ADMIN — /sprint
# ═══════════════════════════════════════════════════════════

async def sprint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return

    hours     = 24
    name      = "⚡ Arena Sprint"
    prize_desc = "Top 3 get special rewards!"

    if context.args:
        try:
            hours = int(context.args[0])
        except ValueError:
            pass
        if len(context.args) > 1:
            name = " ".join(context.args[1:])

    await db_update("sprints", {"is_active": "eq.true"}, {"is_active": False})
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.patch(
                f"{SUPABASE_URL}/rest/v1/users",
                headers={**HEADERS, "Prefer": "return=minimal"},
                params={"id": "neq.0"},
                json={"sprint_coins": 0}
            )
    except Exception as e:
        logger.error(f"Sprint reset error: {e}")

    ends_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
    await db_insert("sprints", {
        "name":             name,
        "ends_at":          ends_at.isoformat(),
        "is_active":        True,
        "prize_description": prize_desc
    })

    await update.message.reply_text(
        f"🏁 *Sprint Launched!*\n\n"
        f"📛 *{name}*\n"
        f"⏱ Duration: *{hours}h*\n"
        f"⌛ Ends: `{ends_at.strftime('%d %b %Y %H:%M UTC')}`\n\n"
        f"_Sprint coins are separate — only count during this event._",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════
#  /prizepool
# ═══════════════════════════════════════════════════════════

async def prizepool_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool  = await db_get("prize_pool", {"id": "eq.1"})
    avax  = float(pool[0].get("total_avax", 0)) if pool else 0.0
    arena = int(pool[0].get("total_arena", 0))   if pool else 0

    await update.message.reply_text(
        f"🏆 *Arena Prize Pool*\n\n"
        f"💎 AVAX: *{avax:.4f}*\n"
        f"⚡ $ARENA: *{arena:,}*\n\n"
        f"*Season end distribution:*\n"
        f"🥇 1st — 40% (*{avax*0.40:.3f} AVAX*)\n"
        f"🥈 2nd — 20% (*{avax*0.20:.3f} AVAX*)\n"
        f"🥉 3rd — 10% (*{avax*0.10:.3f} AVAX*)\n"
        f"🔥 4th–10th — share 30%\n\n"
        f"_All purchases → prize pool_\n"
        f"_Coins → future $ARENA allocation_",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════
#  ERROR HANDLER
# ═══════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning(f"Network error (ignored): {context.error}")
        return
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main():
    # Avvia il server HTTP in un thread separato (per Render)
    threading.Thread(target=run_health_server, daemon=True).start()

    # Configura e avvia il bot Telegram
    app = Application.builder().token(BOT_TOKEN).build()

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

    print("🔥 Arena Bot v2.2 + Health Server running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
