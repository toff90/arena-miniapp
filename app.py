"""
🔥 Arena MiniApp API v2.1 — Pure HTTP, Zero Telegram
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fixes vs v2.0:
  - sb_insert/sb_get log the actual Supabase response body (not just 400)
  - register_user returns real error if Supabase insert fails
  - Admin endpoints return full diagnostic info
  - Better error propagation throughout
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import logging
import datetime
import requests as req
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY      = os.getenv("SUPABASE_SERVICE_KEY", "")
TREASURY_ADDR     = os.getenv("TREASURY_ADDR", "").lower()
ADMIN_KEY         = os.getenv("ADMIN_KEY", "")
FRONTEND_ORIGINS  = os.getenv(
    "FRONTEND_ORIGINS",
    "https://arena.social,https://toff90.github.io,http://localhost:3000,http://localhost:8080"
).split(",")
AVAX_RPC = "https://api.avax.network/ext/bc/C/rpc"

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

SHOP_ITEMS = {
    "energy_25":  (0.05,   500,  "⚡ Small Refill",  "energy",  25),
    "energy_100": (0.15,  1500,  "⚡⚡ Full Refill",  "energy",  100),
    "upgrade_2":  (0.30,  3000,  "⚡ Power x2",       "upgrade", 2),
    "upgrade_5":  (0.80,  8000,  "🔥 Power x5",       "upgrade", 5),
    "upgrade_10": (2.00, 20000,  "💎 Power x10",      "upgrade", 10),
    "upgrade_25": (5.00, 50000,  "🚀 Power x25",      "upgrade", 25),
}

STREAK_BONUS = {1: 250, 2: 250, 3: 250, 4: 250, 5: 250, 6: 250, 7: 1000}
PRIZE_DIST   = [0.40, 0.20, 0.10, 0.05, 0.05, 0.05, 0.05, 0.03, 0.03, 0.04]

# ─── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app,
     origins=FRONTEND_ORIGINS,
     supports_credentials=True,
     allow_headers=["Content-Type", "Authorization", "X-Admin-Key"],
     methods=["GET", "POST", "PATCH", "OPTIONS"])

# ─── Supabase Helpers ──────────────────────────────────────────────────────────

def sb_get(table: str, params: dict = {}) -> list:
    try:
        r = req.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=HEADERS, params=params, timeout=10
        )
        if r.status_code != 200:
            logger.error(f"sb_get({table}) {r.status_code}: {r.text[:300]}")
            return []
        return r.json()
    except Exception as e:
        logger.error(f"sb_get({table}) exception: {e}")
        return []


def sb_insert(table: str, data: dict) -> tuple[dict | None, str | None]:
    """
    Returns (row, error_msg).
    row is None on failure, error_msg is None on success.
    """
    try:
        r = req.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=HEADERS, json=data, timeout=10
        )
        if r.status_code not in (200, 201):
            err = r.text[:400]
            logger.error(f"sb_insert({table}) {r.status_code}: {err}")
            return None, err
        res = r.json()
        return (res[0] if isinstance(res, list) and res else data), None
    except Exception as e:
        logger.error(f"sb_insert({table}) exception: {e}")
        return None, str(e)


def sb_patch(table: str, params: dict, data: dict) -> dict:
    try:
        r = req.patch(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=HEADERS, params=params, json=data, timeout=10
        )
        if r.status_code not in (200, 204):
            logger.error(f"sb_patch({table}) {r.status_code}: {r.text[:300]}")
            return {}
        if r.status_code == 204 or not r.text.strip():
            return data
        res = r.json()
        return res[0] if isinstance(res, list) and res else {}
    except Exception as e:
        logger.error(f"sb_patch({table}) exception: {e}")
        return {}


def normalize_address(addr: str) -> str:
    return (addr or "").strip().lower()


def get_user_by_wallet(wallet: str) -> dict | None:
    rows = sb_get("users", {"wallet_address": f"eq.{normalize_address(wallet)}"})
    return rows[0] if rows else None


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sync_prize_pool(avax_amount: float):
    pool = sb_get("prize_pool", {"id": "eq.1"})
    if pool:
        current = float(pool[0].get("total_avax", 0))
        sb_patch("prize_pool", {"id": "eq.1"}, {
            "total_avax": round(current + avax_amount, 8),
            "updated_at": now_iso()
        })


def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Admin-Key", "") or (request.get_json(force=True, silent=True) or {}).get("admin_key", "")
        if not ADMIN_KEY or key != ADMIN_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ─── Public Endpoints ──────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":  "ok",
        "service": "arena-api",
        "version": "2.1",
        "supabase": bool(SUPABASE_URL and SUPABASE_KEY),
        "treasury": bool(TREASURY_ADDR),
    }), 200


@app.route("/admin")
@app.route("/admin.html")
def serve_admin():
    """Serve admin panel directly from Render — no GitHub needed."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, "admin.html")


@app.route("/api/user/register", methods=["POST"])
def register_user():
    """Register or login via wallet address."""
    try:
        data   = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))

        if not wallet or not wallet.startswith("0x") or len(wallet) < 10:
            return jsonify({"ok": False, "error": "Invalid wallet address"}), 400

        username   = str(data.get("username", ""))[:30]
        first_name = str(data.get("first_name", "Gladiator"))[:50]
        referral   = normalize_address(data.get("referral_code", ""))

        # ── Existing user ─────────────────────────────────────────────────────
        existing = get_user_by_wallet(wallet)
        if existing:
            updates = {}
            if username and username != existing.get("username"):
                updates["username"] = username
            if first_name and first_name != existing.get("first_name"):
                updates["first_name"] = first_name
            if updates:
                sb_patch("users", {"wallet_address": f"eq.{wallet}"}, updates)
                existing.update(updates)
            return jsonify({"ok": True, "user": existing, "is_new": False})

        # ── New user ──────────────────────────────────────────────────────────
        new_user = {
            "wallet_address":    wallet,
            "username":          username,
            "first_name":        first_name or "Gladiator",
            "coins":             500,
            "season_coins":      500,
            "tap_power":         1,
            "referral_count":    0,
            "streak":            0,
            "energy":            100,
            "last_energy_update": now_iso(),
        }

        # Referral bonus
        if referral and referral != wallet:
            ref_user = get_user_by_wallet(referral)
            if ref_user:
                new_user["referred_by"] = referral
                sb_patch("users", {"wallet_address": f"eq.{referral}"}, {
                    "coins":          ref_user["coins"] + 1000,
                    "season_coins":   ref_user.get("season_coins", 0) + 1000,
                    "referral_count": ref_user.get("referral_count", 0) + 1,
                })

        created, err = sb_insert("users", new_user)
        if err:
            # Return the error so the client knows something went wrong
            return jsonify({
                "ok": False,
                "error": f"Database error: {err[:200]}",
                "hint": "Run the migration SQL in Supabase — add wallet_address column"
            }), 500

        return jsonify({"ok": True, "user": created, "is_new": True}), 201

    except Exception as e:
        logger.error(f"register_user: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/user/<wallet>", methods=["GET"])
def get_user(wallet: str):
    wallet = normalize_address(wallet)
    user = get_user_by_wallet(wallet)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    return jsonify({"ok": True, "user": user})


@app.route("/api/tap", methods=["POST"])
def record_taps():
    """Batch-sync taps to DB."""
    try:
        data   = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        taps   = int(data.get("taps", 0))
        coins  = int(data.get("coins_earned", 0))

        if not wallet:
            return jsonify({"ok": False, "error": "Missing wallet_address"}), 400
        if taps <= 0 or coins <= 0:
            return jsonify({"ok": False, "error": "Nothing to record"}), 400
        if coins > taps * 25:
            return jsonify({"ok": False, "error": "Suspicious amount"}), 400

        user = get_user_by_wallet(wallet)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404

        new_coins  = user.get("coins", 0) + coins
        new_season = user.get("season_coins", 0) + coins
        new_sprint = user.get("sprint_coins", 0) + coins
        new_energy = max(0, user.get("energy", 100) - taps)

        sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
            "coins":              new_coins,
            "season_coins":       new_season,
            "sprint_coins":       new_sprint,
            "energy":             new_energy,
            "last_energy_update": now_iso(),
        })

        return jsonify({
            "ok":          True,
            "coins":       new_coins,
            "season_coins": new_season,
            "energy":      new_energy,
        })
    except Exception as e:
        logger.error(f"record_taps: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/daily", methods=["POST"])
def claim_daily():
    """Claim daily reward with 7-day streak logic."""
    try:
        data   = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        if not wallet:
            return jsonify({"ok": False, "error": "Missing wallet_address"}), 400

        user = get_user_by_wallet(wallet)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404

        now    = datetime.datetime.now(datetime.timezone.utc)
        streak = user.get("streak", 0)
        last_raw = user.get("last_claim")

        if last_raw:
            last_dt = datetime.datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
            diff_h = (now - last_dt).total_seconds() / 3600

            if diff_h < 24:
                hours_left = round(24 - diff_h, 2)
                return jsonify({
                    "ok":         False,
                    "error":      "already_claimed",
                    "hours_left": hours_left,
                    "streak":     streak,
                }), 200

            if diff_h > 48:
                streak = 0  # streak broken

        streak = min(streak + 1, 7)
        bonus  = STREAK_BONUS.get(streak, 1000)
        new_coins  = user.get("coins", 0) + bonus
        new_season = user.get("season_coins", 0) + bonus

        sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
            "coins":        new_coins,
            "season_coins": new_season,
            "streak":       streak,
            "last_claim":   now.isoformat(),
        })

        # 5% passive to referrer
        referrer = user.get("referred_by")
        if referrer:
            passive = int(bonus * 0.05)
            ref = get_user_by_wallet(referrer)
            if ref:
                sb_patch("users", {"wallet_address": f"eq.{referrer}"}, {
                    "coins":        ref["coins"] + passive,
                    "season_coins": ref.get("season_coins", 0) + passive,
                })

        return jsonify({
            "ok":           True,
            "coins_earned": bonus,
            "coins":        new_coins,
            "streak":       streak,
            "next_reward":  STREAK_BONUS.get(min(streak + 1, 7), 1000),
        })
    except Exception as e:
        logger.error(f"claim_daily: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    try:
        lb_type = request.args.get("type", "alltime")
        limit   = min(int(request.args.get("limit", 10)), 100)
        order_col = {"alltime": "coins", "season": "season_coins", "sprint": "sprint_coins"}.get(lb_type, "coins")

        rows = sb_get("users", {
            "select": "wallet_address,username,first_name,coins,season_coins,sprint_coins,tap_power",
            "order":  f"{order_col}.desc",
            "limit":  str(limit),
        })
        entries = sorted(rows, key=lambda x: x.get(order_col, 0), reverse=True)

        pool = sb_get("prize_pool", {"id": "eq.1"})
        pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0

        return jsonify({"ok": True, "entries": entries, "prize_pool_avax": pool_avax, "type": lb_type})
    except Exception as e:
        logger.error(f"leaderboard: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/prize-pool", methods=["GET"])
def prize_pool():
    try:
        pool = sb_get("prize_pool", {"id": "eq.1"})
        avax  = float(pool[0].get("total_avax", 0)) if pool else 0.0
        arena = int(pool[0].get("total_arena", 0)) if pool else 0
        dist  = [{"rank": i+1, "pct": p, "avax": round(avax*p, 4)} for i, p in enumerate(PRIZE_DIST)]
        return jsonify({"ok": True, "avax": avax, "arena": arena, "distribution": dist})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/squad/<wallet>", methods=["GET"])
def squad_info(wallet: str):
    try:
        wallet = normalize_address(wallet)
        user   = get_user_by_wallet(wallet)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404

        members = sb_get("users", {
            "referred_by": f"eq.{wallet}",
            "select":      "wallet_address,username,first_name,coins",
            "order":       "coins.desc",
            "limit":       "20",
        })
        total   = sum(m.get("coins", 0) for m in members)
        passive = int(total * 0.05)
        return jsonify({
            "ok":           True,
            "members":      members,
            "member_count": len(members),
            "total_coins":  total,
            "passive_5pct": passive,
            "referral_code": wallet,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sprint/active", methods=["GET"])
def active_sprint():
    try:
        sprints = sb_get("sprints", {"is_active": "eq.true", "order": "started_at.desc", "limit": "1"})
        if not sprints:
            return jsonify({"ok": True, "sprint": None})
        s = sprints[0]
        ends = datetime.datetime.fromisoformat(s["ends_at"].replace("Z", "+00:00"))
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=datetime.timezone.utc)
        if datetime.datetime.now(datetime.timezone.utc) > ends:
            sb_patch("sprints", {"id": f"eq.{s['id']}"}, {"is_active": False})
            return jsonify({"ok": True, "sprint": None})
        rem = ends - datetime.datetime.now(datetime.timezone.utc)
        h   = int(rem.total_seconds() // 3600)
        m   = int((rem.total_seconds() % 3600) // 60)
        return jsonify({"ok": True, "sprint": {**s, "remaining_label": f"{h}h {m}m"}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/verify-payment", methods=["POST"])
def verify_payment():
    try:
        data    = request.get_json(force=True) or {}
        tx_hash = data.get("tx_hash", "").strip()
        item_id = data.get("item_id", "").strip().lower()
        wallet  = normalize_address(data.get("wallet_address", ""))

        if not tx_hash or not item_id or not wallet:
            return jsonify({"ok": False, "error": "Missing fields"}), 400
        if item_id not in SHOP_ITEMS:
            return jsonify({"ok": False, "error": "Unknown item"}), 400
        if not tx_hash.startswith("0x") or len(tx_hash) < 40:
            return jsonify({"ok": False, "error": "Invalid TX hash"}), 400

        existing = sb_get("payments", {"tx_hash": f"eq.{tx_hash}"})
        if existing:
            return jsonify({"ok": False, "error": "TX already used"}), 409

        avax_price, _, label, action, value = SHOP_ITEMS[item_id]
        avax_wei = int(avax_price * 1e18)

        rpc_r = req.post(AVAX_RPC, json={
            "jsonrpc": "2.0", "method": "eth_getTransactionByHash",
            "params": [tx_hash], "id": 1
        }, timeout=10)
        tx_data = rpc_r.json().get("result")
        if not tx_data:
            return jsonify({"ok": False, "error": "TX not found on chain"}), 404

        to_addr = (tx_data.get("to") or "").lower()
        if TREASURY_ADDR and to_addr != TREASURY_ADDR:
            return jsonify({"ok": False, "error": "Wrong destination address"}), 400

        tx_value = int(tx_data.get("value", "0x0"), 16)
        if tx_value < avax_wei * 0.99:
            return jsonify({"ok": False, "error": f"Insufficient amount. Expected ~{avax_price} AVAX"}), 400

        user = get_user_by_wallet(wallet)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404

        sb_insert("payments", {
            "wallet_address": wallet, "tx_hash": tx_hash,
            "amount_avax": avax_price, "item": item_id, "verified": True,
        })
        _sync_prize_pool(avax_price)

        if action == "energy":
            new_energy = min(user.get("energy", 0) + value, 100)
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {"energy": new_energy, "last_energy_update": now_iso()})
            return jsonify({"ok": True, "item": item_id, "label": label, "energy": new_energy})

        elif action == "upgrade":
            current_power = user.get("tap_power", 1)
            if value <= current_power:
                return jsonify({"ok": False, "error": "Already owned", "tap_power": current_power}), 409
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {"tap_power": value})
            return jsonify({"ok": True, "item": item_id, "label": label, "tap_power": value})

    except Exception as e:
        logger.error(f"verify_payment: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/shop/buy-with-coins", methods=["POST"])
def buy_with_coins():
    """
    Purchase shop item using in-game coins (no blockchain tx).
    Body: { wallet_address, item_id }
    """
    # Coin prices mirror SHOP_ITEMS avax prices * 10,000
    COIN_PRICES = {
        "energy_25":   500,
        "energy_100":  1500,
        "upgrade_2":   3000,
        "upgrade_5":   8000,
        "upgrade_10":  20000,
        "upgrade_25":  50000,
    }
    try:
        data    = request.get_json(force=True) or {}
        wallet  = normalize_address(data.get("wallet_address", ""))
        item_id = data.get("item_id", "").strip().lower()

        if not wallet or not item_id:
            return jsonify({"ok": False, "error": "Missing fields"}), 400
        if item_id not in SHOP_ITEMS:
            return jsonify({"ok": False, "error": "Unknown item"}), 400
        if item_id not in COIN_PRICES:
            return jsonify({"ok": False, "error": "Item not available for coin purchase"}), 400

        coin_cost = COIN_PRICES[item_id]
        _, _, label, action, value = SHOP_ITEMS[item_id]

        user = get_user_by_wallet(wallet)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404

        current_coins = user.get("coins", 0)
        if current_coins < coin_cost:
            return jsonify({
                "ok":        False,
                "error":     f"Not enough coins. Need {coin_cost:,}, you have {current_coins:,}.",
                "coins":     current_coins,
                "coin_cost": coin_cost,
            }), 400

        new_coins  = current_coins - coin_cost
        new_season = max(0, user.get("season_coins", 0) - coin_cost)

        if action == "energy":
            new_energy = min(user.get("energy", 0) + value, 100)
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
                "coins":              new_coins,
                "season_coins":       new_season,
                "energy":             new_energy,
                "last_energy_update": now_iso(),
            })
            return jsonify({
                "ok":          True,
                "label":       label,
                "item":        item_id,
                "coins":       new_coins,
                "season_coins": new_season,
                "energy":      new_energy,
                "coin_cost":   coin_cost,
            })

        elif action == "upgrade":
            current_power = user.get("tap_power", 1)
            if value <= current_power:
                return jsonify({
                    "ok":       False,
                    "error":    "Already owned or lower than current power",
                    "tap_power": current_power,
                }), 409
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
                "coins":        new_coins,
                "season_coins": new_season,
                "tap_power":    value,
            })
            return jsonify({
                "ok":          True,
                "label":       label,
                "item":        item_id,
                "coins":       new_coins,
                "season_coins": new_season,
                "tap_power":   value,
                "coin_cost":   coin_cost,
            })

    except Exception as e:
        logger.error(f"buy_with_coins: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/verify-arena-payment", methods=["POST"])
def verify_arena_payment():
    """
    Verify an on-chain $ARENA ERC-20 transfer and apply the shop item.
    Body: { tx_hash, item_id, wallet_address }

    How verification works:
    - Call eth_getTransactionReceipt on Avalanche RPC
    - Check logs for ERC-20 Transfer event:
        topic[0] = Transfer sig = 0xddf252ad...
        topic[1] = from (buyer wallet)
        topic[2] = to (treasury)
        data     = amount in wei
    - Verify amount >= expected ARENA amount for the item
    """
    # $ARENA token contract address on Avalanche C-Chain
    ARENA_TOKEN = "0xb8d7710f7d8349a506b75dd184f05777c82dad0c"
    # ERC-20 Transfer event signature
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    # ARENA token amounts per item (integer, no decimals — actual tokens * 10^18 happens in wei)
    ARENA_PRICES = {
        "energy_25":   500,
        "energy_100":  1500,
        "upgrade_2":   3000,
        "upgrade_5":   8000,
        "upgrade_10":  20000,
        "upgrade_25":  50000,
    }

    try:
        data    = request.get_json(force=True) or {}
        tx_hash = data.get("tx_hash", "").strip()
        item_id = data.get("item_id", "").strip().lower()
        wallet  = normalize_address(data.get("wallet_address", ""))

        if not tx_hash or not item_id or not wallet:
            return jsonify({"ok": False, "error": "Missing fields"}), 400
        if item_id not in SHOP_ITEMS:
            return jsonify({"ok": False, "error": "Unknown item"}), 400
        if item_id not in ARENA_PRICES:
            return jsonify({"ok": False, "error": "Item not purchasable with $ARENA"}), 400
        if not tx_hash.startswith("0x") or len(tx_hash) < 40:
            return jsonify({"ok": False, "error": "Invalid TX hash"}), 400

        # Duplicate check
        existing = sb_get("payments", {"tx_hash": f"eq.{tx_hash}"})
        if existing:
            return jsonify({"ok": False, "error": "TX already used"}), 409

        arena_price = ARENA_PRICES[item_id]
        arena_wei   = arena_price * (10 ** 18)  # $ARENA has 18 decimals

        # ── Get receipt (has logs) ────────────────────────────────────────
        receipt_r = req.post(AVAX_RPC, json={
            "jsonrpc": "2.0",
            "method":  "eth_getTransactionReceipt",
            "params":  [tx_hash],
            "id":      1
        }, timeout=10)
        receipt = receipt_r.json().get("result")

        if not receipt:
            return jsonify({"ok": False, "error": "TX receipt not found — wait a few seconds and retry"}), 404

        if receipt.get("status") == "0x0":
            return jsonify({"ok": False, "error": "Transaction reverted on-chain"}), 400

        # ── Scan logs for Transfer event ──────────────────────────────────
        logs = receipt.get("logs", [])
        transfer_ok = False
        for log in logs:
            topics = log.get("topics", [])
            # Must be on ARENA token contract
            if log.get("address", "").lower() != ARENA_TOKEN.lower():
                continue
            # Must have 3 topics: Transfer sig + from + to
            if len(topics) < 3:
                continue
            # topic[0] = Transfer event signature
            if topics[0].lower() != TRANSFER_TOPIC.lower():
                continue
            # topic[1] = from address (padded to 32 bytes)
            from_addr = "0x" + topics[1][-40:]
            # topic[2] = to address
            to_addr   = "0x" + topics[2][-40:]
            # data = amount in hex wei
            amount_wei = int(log.get("data", "0x0"), 16)

            if (from_addr.lower() == wallet.lower() and
                to_addr.lower()   == TREASURY_ADDR.lower() and
                amount_wei        >= int(arena_wei * 0.99)):
                transfer_ok = True
                break

        if not transfer_ok:
            return jsonify({
                "ok":    False,
                "error": f"No valid $ARENA Transfer found. Expected {arena_price:,} $ARENA from {wallet[:10]}... to treasury."
            }), 400

        # ── Record payment ────────────────────────────────────────────────
        _, _, label, action, value = SHOP_ITEMS[item_id]
        sb_insert("payments", {
            "wallet_address": wallet,
            "tx_hash":        tx_hash,
            "amount_avax":    0,
            "item":           item_id,
            "verified":       True,
        })

        # ── Apply item ────────────────────────────────────────────────────
        user = get_user_by_wallet(wallet)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404

        if action == "energy":
            new_energy = min(user.get("energy", 0) + value, 100)
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
                "energy": new_energy, "last_energy_update": now_iso()
            })
            return jsonify({"ok": True, "item": item_id, "label": label, "energy": new_energy})

        elif action == "upgrade":
            current_power = user.get("tap_power", 1)
            if value <= current_power:
                return jsonify({"ok": False, "error": "Already owned", "tap_power": current_power}), 409
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {"tap_power": value})
            return jsonify({"ok": True, "item": item_id, "label": label, "tap_power": value})

    except Exception as e:
        logger.error(f"verify_arena_payment: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── Admin Endpoints ───────────────────────────────────────────────────────────

@app.route("/api/admin/status", methods=["GET", "POST"])
@require_admin
def admin_status():
    """Quick health-check for admin panel."""
    try:
        pool    = sb_get("prize_pool", {"id": "eq.1"})
        seasons = sb_get("seasons", {"order": "id.desc", "limit": "5"})
        sprints = sb_get("sprints", {"order": "id.desc", "limit": "3"})
        users_count = sb_get("users", {"select": "count"})

        return jsonify({
            "ok":           True,
            "prize_pool":   pool[0] if pool else {},
            "seasons":      seasons,
            "sprints":      sprints,
            "users":        users_count,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/new-season", methods=["POST"])
@require_admin
def new_season():
    """End current season and start a new one."""
    try:
        data        = request.get_json(force=True) or {}
        season_name = data.get("season_name") or f"Season {datetime.datetime.now().strftime('%B %Y')}"
        reset_power = data.get("reset_tap_power", True)

        top = sb_get("users", {
            "select": "wallet_address,username,first_name,season_coins",
            "order":  "season_coins.desc",
            "limit":  "10",
        })
        top = sorted(top, key=lambda x: x.get("season_coins", 0), reverse=True)

        pool      = sb_get("prize_pool", {"id": "eq.1"})
        pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0

        # Close active season
        sb_patch("seasons", {"is_active": "eq.true"}, {"is_active": False, "ended_at": now_iso()})

        # Create new season
        new_s, err = sb_insert("seasons", {"name": season_name, "is_active": True})
        if err:
            return jsonify({"ok": False, "error": f"Could not create season: {err}"}), 500

        results = []
        for i, u in enumerate(top):
            pct   = PRIZE_DIST[i] if i < len(PRIZE_DIST) else 0
            prize = round(pool_avax * pct, 4)
            sb_insert("season_results", {
                "season_id":      new_s.get("id", 0),
                "wallet_address": u.get("wallet_address", ""),
                "username":       u.get("username", ""),
                "first_name":     u.get("first_name", "Anon"),
                "final_coins":    u.get("season_coins", 0),
                "rank":           i + 1,
                "prize_avax":     prize,
            })
            results.append({"rank": i+1, "wallet": u.get("wallet_address",""), "coins": u.get("season_coins",0), "prize_avax": prize})

        # Reset season_coins and sprint_coins for all users
        reset_data = {"season_coins": 0, "sprint_coins": 0}
        if reset_power:
            reset_data["tap_power"] = 1

        req.patch(
            f"{SUPABASE_URL}/rest/v1/users",
            headers={**HEADERS, "Prefer": "return=minimal"},
            params={"wallet_address": "neq.0x0"},
            json=reset_data,
            timeout=30
        )

        # Reset prize pool
        sb_patch("prize_pool", {"id": "eq.1"}, {"total_avax": 0, "total_arena": 0, "updated_at": now_iso()})

        return jsonify({
            "ok":          True,
            "season_name": season_name,
            "pool_avax":   pool_avax,
            "reset_power": reset_power,
            "results":     results,
        })
    except Exception as e:
        logger.error(f"new_season: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/sprint", methods=["POST"])
@require_admin
def start_sprint():
    """Launch a new sprint."""
    try:
        data        = request.get_json(force=True) or {}
        hours       = int(data.get("hours", 24))
        sprint_name = data.get("sprint_name", "⚡ Arena Sprint")
        prize_desc  = data.get("prize_description", "Top 3 get special rewards!")

        # End active sprints
        sb_patch("sprints", {"is_active": "eq.true"}, {"is_active": False})

        # Reset sprint_coins
        req.patch(
            f"{SUPABASE_URL}/rest/v1/users",
            headers={**HEADERS, "Prefer": "return=minimal"},
            params={"wallet_address": "neq.0x0"},
            json={"sprint_coins": 0},
            timeout=30
        )

        ends_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
        sprint, err = sb_insert("sprints", {
            "name":              sprint_name,
            "ends_at":           ends_at.isoformat(),
            "is_active":         True,
            "prize_description": prize_desc,
        })
        if err:
            return jsonify({"ok": False, "error": err}), 500

        return jsonify({
            "ok":          True,
            "sprint_name": sprint_name,
            "hours":       hours,
            "ends_at":     ends_at.isoformat(),
            "prize_desc":  prize_desc,
        })
    except Exception as e:
        logger.error(f"start_sprint: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/end-sprint", methods=["POST"])
@require_admin
def end_sprint():
    """Manually end the active sprint."""
    try:
        sb_patch("sprints", {"is_active": "eq.true"}, {"is_active": False})
        return jsonify({"ok": True, "message": "Active sprint ended"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/set-prize-pool", methods=["POST"])
@require_admin
def set_prize_pool():
    """Manually set/adjust the prize pool (e.g. after manual AVAX transfer)."""
    try:
        data = request.get_json(force=True) or {}
        avax  = float(data.get("avax", 0))
        arena = int(data.get("arena", 0))
        mode  = data.get("mode", "set")  # "set" or "add"

        if mode == "add":
            pool = sb_get("prize_pool", {"id": "eq.1"})
            if pool:
                avax  = float(pool[0].get("total_avax", 0)) + avax
                arena = int(pool[0].get("total_arena", 0)) + arena

        sb_patch("prize_pool", {"id": "eq.1"}, {
            "total_avax":  round(avax, 8),
            "total_arena": arena,
            "updated_at":  now_iso(),
        })
        return jsonify({"ok": True, "avax": avax, "arena": arena})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/users", methods=["GET", "POST"])
@require_admin
def admin_users():
    """List top users. POST to filter."""
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        order = request.args.get("order", "coins")
        rows  = sb_get("users", {
            "select": "wallet_address,username,first_name,coins,season_coins,tap_power,streak,energy,referral_count,created_at",
            "order":  f"{order}.desc",
            "limit":  str(limit),
        })
        return jsonify({"ok": True, "users": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/ban-user", methods=["POST"])
@require_admin
def ban_user():
    """Zero out a user's coins (anti-cheat)."""
    try:
        data   = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        if not wallet:
            return jsonify({"ok": False, "error": "Missing wallet_address"}), 400
        sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
            "coins": 0, "season_coins": 0, "sprint_coins": 0, "tap_power": 1,
        })
        return jsonify({"ok": True, "banned": wallet})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── Error Handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({"ok": False, "error": "Endpoint not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"ok": False, "error": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"ok": False, "error": "Internal server error"}), 500

# ─── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 10000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    logger.info(f"🔥 Arena API v2.1 starting on :{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
