"""
🔥 Arena MiniApp API v2.5 — Security & Auth Fix
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Features:
  - Secure Wallet Login (Personal Sign Verification).
  - Admin Panel Fixes (Dates, Endpoints).
  - Username Modal support (no more blocked prompts).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import logging
import datetime
import requests as req
import jwt  # PyJWT
from eth_account import Account
from eth_account.messages import encode_defunct
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL        = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY        = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
TREASURY_ADDR       = os.getenv("TREASURY_ADDR", "").lower()
ADMIN_KEY           = os.getenv("ADMIN_KEY", "")
FRONTEND_ORIGINS    = os.getenv(
    "FRONTEND_ORIGINS",
    "https://arena.social,https://toff90.github.io,http://localhost:3000,http://localhost:8080"
).split(",")
AVAX_RPC            = "https://api.avax.network/ext/bc/C/rpc"

ARENA_TOKEN_ADDR    = "0xb8d7710f7d8349a506b75dd184f05777c82dad0c"
BALANCEOF_SIG       = "0x70a08231"

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

# ─── Flask & Limiter ───────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app,
     origins=FRONTEND_ORIGINS,
     supports_credentials=True,
     allow_headers=["Content-Type", "Authorization", "X-Admin-Key"],
     methods=["GET", "POST", "PATCH", "OPTIONS"])

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# ─── Helpers ───────────────────────────────────────────────────────────────────

def sb_get(table: str, params: dict = {}) -> list:
    try:
        r = req.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params, timeout=10)
        if r.status_code != 200:
            logger.error(f"sb_get({table}) {r.status_code}: {r.text[:300]}")
            return []
        return r.json()
    except Exception as e:
        logger.error(f"sb_get({table}) exception: {e}")
        return []

def sb_insert(table: str, data: dict) -> tuple[dict | None, str | None]:
    try:
        r = req.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, json=data, timeout=10)
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
        r = req.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params, json=data, timeout=10)
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

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Admin-Key", "") or (request.get_json(force=True, silent=True) or {}).get("admin_key", "")
        if not ADMIN_KEY or key != ADMIN_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

def is_timestamp_active(iso_str_start, iso_str_end):
    if not iso_str_start or not iso_str_end:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        start = datetime.datetime.fromisoformat(iso_str_start.replace("Z", "+00:00"))
        end = datetime.datetime.fromisoformat(iso_str_end.replace("Z", "+00:00"))
        return start <= now <= end
    except:
        return False

def get_active_status(table_name: str):
    items = sb_get(table_name, {"is_active": "eq.true"})
    for item in items:
        if is_timestamp_active(item.get("starts_at"), item.get("ends_at")):
            return item
    return None

# ─── Blockchain Helpers ────────────────────────────────────────────────────────

def get_onchain_balance(address: str) -> tuple[float, float]:
    avax_bal = 0.0
    arena_bal = 0.0
    try:
        resp = req.post(AVAX_RPC, json={
            "jsonrpc": "2.0", "method": "eth_getBalance",
            "params": [address, "latest"], "id": 1
        }, timeout=10)
        if resp.ok:
            res = resp.json().get("result", "0x0")
            avax_bal = int(res, 16) / 1e18
    except Exception as e:
        logger.error(f"RPC AVAX balance error: {e}")

    try:
        padded_addr = "000000000000000000000000" + address[2:].lower()
        data = f"{BALANCEOF_SIG}{padded_addr}"
        resp = req.post(AVAX_RPC, json={
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": ARENA_TOKEN_ADDR, "data": data}, "latest"], "id": 2
        }, timeout=10)
        if resp.ok:
            res = resp.json().get("result", "0x0")
            arena_bal = int(res, 16) / 1e18
    except Exception as e:
        logger.error(f"RPC ARENA balance error: {e}")

    return avax_bal, arena_bal

# ─── Security: Auth Endpoint ───────────────────────────────────────────────────

@app.route("/api/auth/verify", methods=["POST"])
def auth_verify():
    """
    Verifica la firma del wallet per garantire che l'utente possieda l'indirizzo.
    """
    try:
        data = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        signature = data.get("signature", "")
        message = data.get("message", "")

        if not wallet or not signature or not message:
            return jsonify({"ok": False, "error": "Missing fields"}), 400

        # Recupera l'indirizzo che ha firmato il messaggio
        encoded_msg = encode_defunct(text=message)
        recovered_addr = Account.recover_message(encoded_msg, signature=signature)

        if recovered_addr.lower() == wallet:
            return jsonify({"ok": True, "verified": True})
        else:
            return jsonify({"ok": False, "error": "Signature mismatch"}), 401

    except Exception as e:
        logger.error(f"Auth verify error: {e}")
        return jsonify({"ok": False, "error": "Verification failed"}), 500

# ─── Public Endpoints ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return "⚔️ Arena MiniApp API is Live (Secure v2.5)", 200

@app.route("/health")
def health():
    return jsonify({
        "status":  "ok", "service": "arena-api", "version": "2.5",
        "supabase": bool(SUPABASE_URL and SUPABASE_KEY),
        "treasury": bool(TREASURY_ADDR),
    }), 200

@app.route("/admin")
@app.route("/admin.html")
def serve_admin():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, "admin.html")

@app.route("/api/user/register", methods=["POST"])
def register_user():
    try:
        data   = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        if not wallet or not wallet.startswith("0x") or len(wallet) < 10:
            return jsonify({"ok": False, "error": "Invalid wallet address"}), 400

        username   = str(data.get("username", ""))[:30]
        first_name = str(data.get("first_name", "Gladiator"))[:50]
        referral   = normalize_address(data.get("referral_code", ""))

        existing = get_user_by_wallet(wallet)
        if existing:
            updates = {}
            if username and username != existing.get("username"): updates["username"] = username
            if first_name and first_name != existing.get("first_name"): updates["first_name"] = first_name
            if updates:
                sb_patch("users", {"wallet_address": f"eq.{wallet}"}, updates)
                existing.update(updates)
            return jsonify({"ok": True, "user": existing, "is_new": False})

        new_user = {
            "wallet_address": wallet, "username": username, "first_name": first_name or "Gladiator",
            "coins": 500, "season_coins": 500, "sprint_coins": 0, "tap_power": 1, "referral_count": 0,
            "streak": 0, "energy": 100, "last_energy_update": now_iso(),
        }
        if referral and referral != wallet:
            ref_user = get_user_by_wallet(referral)
            if ref_user:
                new_user["referred_by"] = referral
                sb_patch("users", {"wallet_address": f"eq.{referral}"}, {
                    "coins": ref_user["coins"] + 1000,
                    "season_coins": ref_user.get("season_coins", 0) + 1000,
                    "referral_count": ref_user.get("referral_count", 0) + 1,
                })

        created, err = sb_insert("users", new_user)
        if err: return jsonify({"ok": False, "error": f"Database error: {err[:200]}"}), 500
        return jsonify({"ok": True, "user": created, "is_new": True}), 201

    except Exception as e:
        logger.error(f"register_user: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/user/<wallet>", methods=["GET"])
def get_user(wallet: str):
    user = get_user_by_wallet(wallet)
    if not user: return jsonify({"ok": False, "error": "User not found"}), 404
    return jsonify({"ok": True, "user": user})

@app.route("/api/tap", methods=["POST"])
@limiter.limit("30 per second")
def record_taps():
    try:
        data   = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        
        if not wallet: return jsonify({"ok": False, "error": "Missing wallet_address"}), 400

        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok": False, "error": "User not found"}), 404

        current_energy = user.get("energy", 100)
        tap_power      = user.get("tap_power", 1)
        
        taps_requested = int(data.get("taps", 0))
        if taps_requested <= 0:
             return jsonify({"ok": False, "error": "Nothing to record"}), 400

        actual_taps = min(taps_requested, current_energy)
        
        if actual_taps == 0:
             return jsonify({"ok": False, "error": "No energy", "energy": 0}), 400

        coins_earned = actual_taps * tap_power
        
        new_energy = current_energy - actual_taps
        new_coins  = user.get("coins", 0) + coins_earned
        
        new_season = user.get("season_coins", 0)
        new_sprint = user.get("sprint_coins", 0)

        active_season = get_active_status("seasons")
        if active_season: new_season += coins_earned

        active_sprint = get_active_status("sprints")
        if active_sprint: new_sprint += coins_earned

        sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
            "coins": new_coins, 
            "season_coins": new_season, 
            "sprint_coins": new_sprint,
            "energy": new_energy, 
            "last_energy_update": now_iso(),
        })
        
        return jsonify({
            "ok": True, 
            "coins": new_coins, 
            "season_coins": new_season, 
            "sprint_coins": new_sprint, 
            "energy": new_energy,
            "earned": coins_earned
        })
    except Exception as e:
        logger.error(f"record_taps: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/daily", methods=["POST"])
@limiter.limit("10 per minute")
def claim_daily():
    try:
        data   = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        if not wallet: return jsonify({"ok": False, "error": "Missing wallet_address"}), 400

        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok": False, "error": "User not found"}), 404

        now = datetime.datetime.now(datetime.timezone.utc)
        streak = user.get("streak", 0)
        last_raw = user.get("last_claim")

        if last_raw:
            last_dt = datetime.datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
            if last_dt.tzinfo is None: last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
            diff_h = (now - last_dt).total_seconds() / 3600
            if diff_h < 24:
                return jsonify({"ok": False, "error": "already_claimed", "hours_left": round(24 - diff_h, 2), "streak": streak}), 200
            if diff_h > 48: streak = 0

        streak = min(streak + 1, 7)
        bonus  = STREAK_BONUS.get(streak, 1000)
        new_coins  = user.get("coins", 0) + bonus
        new_season = user.get("season_coins", 0) + bonus

        sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
            "coins": new_coins, "season_coins": new_season, "streak": streak, "last_claim": now.isoformat()
        })

        referrer = user.get("referred_by")
        if referrer:
            passive = int(bonus * 0.05)
            ref = get_user_by_wallet(referrer)
            if ref:
                sb_patch("users", {"wallet_address": f"eq.{referrer}"}, {
                    "coins": ref["coins"] + passive, "season_coins": ref.get("season_coins", 0) + passive
                })

        return jsonify({"ok": True, "coins_earned": bonus, "coins": new_coins, "streak": streak})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    try:
        lb_type = request.args.get("type", "alltime")
        limit   = min(int(request.args.get("limit", 10)), 100)
        order_col = {"alltime": "coins", "season": "season_coins", "sprint": "sprint_coins"}.get(lb_type, "coins")
        rows = sb_get("users", {
            "select": "wallet_address,username,first_name,coins,season_coins,sprint_coins,tap_power",
            "order": f"{order_col}.desc", "limit": str(limit)
        })
        entries = sorted(rows, key=lambda x: x.get(order_col, 0), reverse=True)
        pool_avax, _ = get_onchain_balance(TREASURY_ADDR)
        return jsonify({"ok": True, "entries": entries, "prize_pool_avax": pool_avax, "type": lb_type})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/prize-pool", methods=["GET"])
def prize_pool():
    try:
        avax_bal, arena_bal = get_onchain_balance(TREASURY_ADDR)
        dist = [{"rank": i+1, "pct": p, "avax": round(avax_bal*p, 4)} for i, p in enumerate(PRIZE_DIST)]
        return jsonify({"ok": True, "avax": avax_bal, "arena": arena_bal, "distribution": dist})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/squad/<wallet>", methods=["GET"])
def squad_info(wallet: str):
    try:
        wallet = normalize_address(wallet)
        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok": False, "error": "User not found"}), 404

        members = sb_get("users", {"referred_by": f"eq.{wallet}", "select": "wallet_address,username,first_name,coins", "order": "coins.desc", "limit": "20"})
        total = sum(m.get("coins", 0) for m in members)
        passive = int(total * 0.05)
        return jsonify({"ok": True, "members": members, "member_count": len(members), "total_coins": total, "passive_5pct": passive, "referral_code": wallet})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── Payment Verification ─────────────────────────────────────────────────────

@app.route("/api/verify-payment", methods=["POST"])
@limiter.limit("10 per minute")
def verify_payment():
    try:
        data    = request.get_json(force=True) or {}
        tx_hash = data.get("tx_hash", "").strip()
        item_id = data.get("item_id", "").strip().lower()
        wallet  = normalize_address(data.get("wallet_address", ""))

        if not tx_hash or not item_id or not wallet: return jsonify({"ok": False, "error": "Missing fields"}), 400
        if item_id not in SHOP_ITEMS: return jsonify({"ok": False, "error": "Unknown item"}), 400

        existing = sb_get("payments", {"tx_hash": f"eq.{tx_hash}"})
        if existing: return jsonify({"ok": False, "error": "TX already used"}), 409

        avax_price, _, label, action, value = SHOP_ITEMS[item_id]
        avax_wei = int(avax_price * 1e18)

        rpc_r = req.post(AVAX_RPC, json={"jsonrpc": "2.0", "method": "eth_getTransactionByHash", "params": [tx_hash], "id": 1}, timeout=10)
        tx_data = rpc_r.json().get("result")
        if not tx_data: return jsonify({"ok": False, "error": "TX not found on chain"}), 404

        to_addr = (tx_data.get("to") or "").lower()
        if TREASURY_ADDR and to_addr != TREASURY_ADDR: return jsonify({"ok": False, "error": "Wrong destination"}), 400

        tx_value = int(tx_data.get("value", "0x0"), 16)
        if tx_value < avax_wei * 0.99: return jsonify({"ok": False, "error": f"Insufficient amount. Expected ~{avax_price} AVAX"}), 400

        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok": False, "error": "User not found"}), 404

        sb_insert("payments", {"wallet_address": wallet, "tx_hash": tx_hash, "amount_avax": avax_price, "item": item_id, "verified": True})

        if action == "energy":
            new_energy = min(user.get("energy", 0) + value, 100)
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {"energy": new_energy, "last_energy_update": now_iso()})
            return jsonify({"ok": True, "item": item_id, "label": label, "energy": new_energy})
        elif action == "upgrade":
            current_power = user.get("tap_power", 1)
            if value <= current_power: return jsonify({"ok": False, "error": "Already owned", "tap_power": current_power}), 409
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {"tap_power": value})
            return jsonify({"ok": True, "item": item_id, "label": label, "tap_power": value})
    except Exception as e:
        logger.error(f"verify_payment: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/verify-arena-payment", methods=["POST"])
@limiter.limit("10 per minute")
def verify_arena_payment():
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    ARENA_PRICES = {
        "energy_25": 500, "energy_100": 1500, "upgrade_2": 3000, "upgrade_5": 8000,
        "upgrade_10": 20000, "upgrade_25": 50000,
    }
    try:
        data    = request.get_json(force=True) or {}
        tx_hash = data.get("tx_hash", "").strip()
        item_id = data.get("item_id", "").strip().lower()
        wallet  = normalize_address(data.get("wallet_address", ""))

        if not tx_hash or not item_id or not wallet: return jsonify({"ok": False, "error": "Missing fields"}), 400
        if item_id not in ARENA_PRICES: return jsonify({"ok": False, "error": "Item not purchasable with $ARENA"}), 400

        existing = sb_get("payments", {"tx_hash": f"eq.{tx_hash}"})
        if existing: return jsonify({"ok": False, "error": "TX already used"}), 409

        arena_price = ARENA_PRICES[item_id]
        arena_wei   = arena_price * (10 ** 18)

        receipt_r = req.post(AVAX_RPC, json={"jsonrpc": "2.0", "method": "eth_getTransactionReceipt", "params": [tx_hash], "id": 1}, timeout=10)
        receipt = receipt_r.json().get("result")
        if not receipt: return jsonify({"ok": False, "error": "TX receipt not found"}), 404
        if receipt.get("status") == "0x0": return jsonify({"ok": False, "error": "Transaction reverted"}), 400

        logs = receipt.get("logs", [])
        transfer_ok = False
        for log in logs:
            topics = log.get("topics", [])
            if log.get("address", "").lower() != ARENA_TOKEN_ADDR.lower(): continue
            if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC.lower(): continue
            
            from_addr = "0x" + topics[1][-40:]
            to_addr   = "0x" + topics[2][-40:]
            amount_wei = int(log.get("data", "0x0"), 16)

            if (from_addr.lower() == wallet.lower() and to_addr.lower() == TREASURY_ADDR.lower() and amount_wei >= int(arena_wei * 0.99)):
                transfer_ok = True
                break

        if not transfer_ok: return jsonify({"ok": False, "error": "No valid $ARENA Transfer found"}), 400

        _, _, label, action, value = SHOP_ITEMS[item_id]
        sb_insert("payments", {"wallet_address": wallet, "tx_hash": tx_hash, "amount_avax": 0, "item": item_id, "verified": True})
        
        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok": False, "error": "User not found"}), 404

        if action == "energy":
            new_energy = min(user.get("energy", 0) + value, 100)
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {"energy": new_energy, "last_energy_update": now_iso()})
            return jsonify({"ok": True, "item": item_id, "label": label, "energy": new_energy})
        elif action == "upgrade":
            current_power = user.get("tap_power", 1)
            if value <= current_power: return jsonify({"ok": False, "error": "Already owned"}), 409
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {"tap_power": value})
            return jsonify({"ok": True, "item": item_id, "label": label, "tap_power": value})

    except Exception as e:
        logger.error(f"verify_arena_payment: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── Admin Endpoints ───────────────────────────────────────────────────────────

@app.route("/api/admin/status", methods=["GET", "POST"])
@require_admin
def admin_status():
    try:
        avax, arena = get_onchain_balance(TREASURY_ADDR)
        seasons = sb_get("seasons", {"order": "id.desc", "limit": "10"})
        sprints = sb_get("sprints", {"order": "id.desc", "limit": "10"})
        
        return jsonify({
            "ok": True, 
            "prize_pool_avax": avax, 
            "prize_pool_arena": arena,
            "users_count": len(sb_get("users", {"select": "id"})),
            "seasons": seasons,
            "sprints": sprints
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/admin/users", methods=["GET"])
@require_admin
def admin_users():
    try:
        order = request.args.get("order", "coins")
        limit = request.args.get("limit", "50")
        col_map = {"coins": "coins", "season_coins": "season_coins", "created_at": "created_at"}
        order_col = col_map.get(order, "coins")
        
        users = sb_get("users", {
            "select": "wallet_address,username,first_name,coins,season_coins,sprint_coins,tap_power,energy,streak,referral_count",
            "order": f"{order_col}.desc",
            "limit": limit
        })
        return jsonify({"ok": True, "count": len(users), "users": users})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/admin/new-season", methods=["POST"])
@require_admin
def new_season():
    try:
        data = request.get_json(force=True) or {}
        season_name = data.get("season_name") or f"Season {datetime.datetime.now().strftime('%B %Y')}"
        reset_power = data.get("reset_tap_power", True)
        prize_desc  = data.get("prize_description", "")
        starts_at = data.get("starts_at")
        ends_at   = data.get("ends_at")
        
        sb_patch("seasons", {"is_active": "eq.true"}, {"is_active": False})
        
        new_season_data = {
            "name": season_name,
            "prize_description": prize_desc,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "is_active": True
        }
        inserted, err = sb_insert("seasons", new_season_data)
        if err: return jsonify({"ok": False, "error": f"Failed to create season: {err}"}), 500

        reset_data = {"season_coins": 0, "sprint_coins": 0}
        if reset_power: reset_data["tap_power"] = 1
        sb_patch("users", {"id": "gt.0"}, reset_data)

        return jsonify({"ok": True, "message": "Season reset triggered", "ends_at": ends_at})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/admin/sprint", methods=["POST"])
@require_admin
def new_sprint():
    try:
        data = request.get_json(force=True) or {}
        sprint_name = data.get("sprint_name", "Arena Sprint")
        prize_desc  = data.get("prize_description", "")
        starts_at   = data.get("starts_at")
        ends_at     = data.get("ends_at")
        
        sb_patch("sprints", {"is_active": "eq.true"}, {"is_active": False})
        
        new_sprint_data = {
            "name": sprint_name,
            "prize_description": prize_desc,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "is_active": True
        }
        inserted, err = sb_insert("sprints", new_sprint_data)
        if err: return jsonify({"ok": False, "error": f"Failed to create sprint: {err}"}), 500
        
        sb_patch("users", {"id": "gt.0"}, {"sprint_coins": 0})

        return jsonify({"ok": True, "message": "Sprint started", "ends_at": ends_at, "sprint_name": sprint_name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/admin/end-sprint", methods=["POST"])
@require_admin
def end_sprint():
    try:
        active = sb_get("sprints", {"is_active": "eq.true"})
        if active:
            sid = active[0]["id"]
            sb_patch("sprints", {"id": f"eq.{sid}"}, {"is_active": False, "ends_at": now_iso()})
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "No active sprint"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/admin/ban-user", methods=["POST"])
@require_admin
def ban_user():
    try:
        data = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        if not wallet: return jsonify({"ok": False, "error": "Wallet required"}), 400
        
        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok": False, "error": "User not found"}), 404
        
        sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
            "coins": 0, "season_coins": 0, "sprint_coins": 0, "tap_power": 1, "energy": 0
        })
        return jsonify({"ok": True, "banned": wallet})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── Error Handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e): return jsonify({"ok": False, "error": "Endpoint not found"}), 404

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"ok": False, "error": "Rate limit exceeded"}), 429

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 10000))
    logger.info(f"🔥 Arena API v2.5 starting on :{port}")
    app.run(host="0.0.0.0", port=port)
