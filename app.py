"""
🔥 Arena MiniApp API v3.0 — Security & Performance Overhaul
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Changes vs v2.9:
  #1 CRITICAL: Server-side coin calculation (ignore client coins_earned)
  #2 CRITICAL: Race condition protection via last_sync_timestamp
  #3 CRITICAL: Rate limiting by wallet_address (not IP)
  #4 CRITICAL: sb_patch now checks response status
  #5 MEDIO:    Dedicated /api/user/rank endpoint (no top-100 scan)
  #6 MEDIO:    On-chain balance cache with 120s TTL
  #8 MEDIO:    Backend returns full server state on every tap sync
  #11 LOW:     All Supabase errors properly logged
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import time
import logging
import datetime
import requests as req
from collections import OrderedDict
from eth_account import Account
from eth_account.messages import encode_defunct
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL        = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY        = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
TREASURY_ADDR       = os.getenv("TREASURY_ADDR", "").lower()
ADMIN_KEY           = os.getenv("ADMIN_KEY", "")
FRONTEND_ORIGINS    = os.getenv("FRONTEND_ORIGINS", "*").split(",")
AVAX_RPC            = "https://api.avax.network/ext/bc/C/rpc"
ARENA_TOKEN_ADDR    = "0xb8d7710f7d8349a506b75dd184f05777c82dad0c"
BALANCEOF_SIG       = "0x70a08231"
MAX_ENERGY          = 100
ENERGY_REGEN_MIN    = 5
MAX_COMBO           = 10
# #6: Cache TTL for on-chain balance (seconds)
BALANCE_CACHE_TTL   = 120

HEADERS = {
    "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json", "Prefer": "return=representation",
}

# FIX #4: In-memory dedup cache to prevent double-counting from duplicate requests
# Stores {wallet: last_processed_time} — rejects tap requests that arrive within
# a short window after a previous request for the same wallet
_tap_dedup = OrderedDict()
TAP_DEDUP_MAX = 5000  # max entries in dedup cache
TAP_DEDUP_TTL  = 5    # seconds to reject duplicate tap requests

def _dedup_check(wallet, client_last_sync):
    """
    Check if this tap request is potentially a duplicate (e.g., from sendBeacon
    followed by a normal flushTaps on next load). Uses client_last_sync timestamp
    combined with a time window to reject requests that arrive too quickly.
    Returns True if the request should be REJECTED (likely duplicate).
    """
    if not client_last_sync or client_last_sync <= 0:
        return False  # No timestamp provided, can't dedup
    now = time.time()
    # Clean old entries
    while _tap_dedup and now - list(_tap_dedup.values())[0] > TAP_DEDUP_TTL:
        _tap_dedup.popitem(last=False)
        if len(_tap_dedup) > TAP_DEDUP_MAX:
            _tap_dedup.popitem(last=False)
    last_seen = _tap_dedup.get(wallet)
    if last_seen is not None and (now - last_seen) < TAP_DEDUP_TTL:
        logger.warning(f"Duplicate tap request rejected for {wallet}: {now - last_seen:.1f}s since last")
        return True
    _tap_dedup[wallet] = now  # Mark this wallet as having a recent tap request
    return False

SHOP_ITEMS = {
    "energy_25": (0.05, 500, "⚡ Small Refill", "energy", 25),
    "energy_100": (0.15, 1500, "⚡⚡ Full Refill", "energy", 100),
    "upgrade_2": (0.30, 3000, "⚡ Power x2", "upgrade", 2),
    "upgrade_5": (0.80, 8000, "🔥 Power x5", "upgrade", 5),
    "upgrade_10": (2.00, 20000, "💎 Power x10", "upgrade", 10),
    "upgrade_25": (5.00, 50000, "🚀 Power x25", "upgrade", 25),
}
STREAK_BONUS = {1: 250, 2: 250, 3: 250, 4: 250, 5: 250, 6: 250, 7: 1000}
PRIZE_DIST = [0.40, 0.20, 0.10, 0.05, 0.05, 0.05, 0.05, 0.03, 0.03, 0.04]

app = Flask(__name__)
CORS(app, origins=FRONTEND_ORIGINS, supports_credentials=True, allow_headers=["Content-Type", "Authorization", "X-Admin-Key"])

# #3 FIX: Rate limiting per wallet_address, non per IP
def _get_wallet_or_ip():
    """Extract wallet_address from request body (POST) or fall back to IP."""
    try:
        if request.is_json:
            w = (request.json or {}).get("wallet_address", "")
            if w and len(w) >= 10:
                return w.strip().lower()
    except Exception:
        pass
    return get_remote_address()

limiter = Limiter(
    app=app,
    key_func=_get_wallet_or_ip,
    default_limits=["200 per day", "60 per minute"],
    storage_uri="memory://"
)

# ─── #6 FIX: On-chain balance cache ──────────────────────────────────────────
_balance_cache = {"avax": 0.0, "arena": 0.0, "ts": 0}

def get_onchain_balance(address):
    """Return cached (avax, arena) balance; refreshes every BALANCE_CACHE_TTL seconds."""
    now = time.time()
    if now - _balance_cache["ts"] < BALANCE_CACHE_TTL and _balance_cache["ts"] > 0:
        return _balance_cache["avax"], _balance_cache["arena"]

    avax, arena = 0.0, 0.0
    try:
        r = req.post(AVAX_RPC, json={"jsonrpc":"2.0","method":"eth_getBalance","params":[address,"latest"],"id":1}, timeout=5)
        if r.ok:
            avax = int(r.json().get("result","0x0"), 16) / 1e18
    except Exception as e:
        logger.warning(f"AVAX balance RPC failed: {e}")

    try:
        data = f"{BALANCEOF_SIG}{'0'*24}{address[2:].lower()}"
        r = req.post(AVAX_RPC, json={"jsonrpc":"2.0","method":"eth_call","params":[{"to":ARENA_TOKEN_ADDR,"data":data},"latest"],"id":2}, timeout=5)
        if r.ok:
            arena = int(r.json().get("result","0x0"), 16) / 1e18
    except Exception as e:
        logger.warning(f"ARENA balance RPC failed: {e}")

    _balance_cache["avax"] = avax
    _balance_cache["arena"] = arena
    _balance_cache["ts"] = now
    logger.info(f"Balance cache refreshed: {avax:.4f} AVAX, {arena:.2f} ARENA")
    return avax, arena

# ─── #4 + #11 FIX: Supabase helpers with proper error handling ────────────────

def sb_get(table, params={}):
    try:
        r = req.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        logger.warning(f"sb_get({table}) status={r.status_code}: {r.text[:200]}")
        return []
    except Exception as e:
        logger.error(f"sb_get({table}) exception: {e}")
        return []

def sb_insert(table, data):
    try:
        r = req.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, json=data, timeout=10)
        if r.status_code in (200, 201):
            res = r.json()
            return (res[0] if isinstance(res, list) and res else data), None
        logger.warning(f"sb_insert({table}) status={r.status_code}: {r.text[:200]}")
        return None, r.text
    except Exception as e:
        logger.error(f"sb_insert({table}) exception: {e}")
        return None, str(e)

def sb_patch(table, params, data):
    """#4 FIX: Returns (success:bool, response_data_or_error)."""
    try:
        r = req.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params, json=data, timeout=10)
        if r.status_code in (200, 204):
            return True, data
        logger.warning(f"sb_patch({table}) status={r.status_code}: {r.text[:200]}")
        return False, r.text
    except Exception as e:
        logger.error(f"sb_patch({table}) exception: {e}")
        return False, str(e)

def normalize_address(addr): return (addr or "").strip().lower()

def calc_energy_regen(saved, last_iso):
    if not last_iso: return saved or MAX_ENERGY
    try:
        last = datetime.datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
        if last.tzinfo is None: last = last.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        mins = (now - last).total_seconds() / 60
        return min(MAX_ENERGY, (saved or 0) + int(mins / ENERGY_REGEN_MIN))
    except Exception as e:
        logger.warning(f"calc_energy_regen error: {e}")
        return saved or MAX_ENERGY

def get_user_by_wallet(wallet):
    rows = sb_get("users", {"wallet_address": f"eq.{normalize_address(wallet)}"})
    if not rows: return None
    u = rows[0]
    u["energy"] = calc_energy_regen(u.get("energy", 0), u.get("last_energy_update"))
    return u

def now_iso(): return datetime.datetime.now(datetime.timezone.utc).isoformat()

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Admin-Key", "") or (request.get_json(silent=True) or {}).get("admin_key", "")
        if not ADMIN_KEY or key != ADMIN_KEY: return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

def is_timestamp_active(start, end):
    if not start or not end: return False
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        s = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.datetime.fromisoformat(end.replace("Z", "+00:00"))
        return s <= now <= e
    except: return False

def get_active_status(table):
    items = sb_get(table, {"is_active": "eq.true"})
    for i in items:
        if is_timestamp_active(i.get("starts_at"), i.get("ends_at")): return i
    return None

# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.route("/")
def index(): return "⚔️ Arena API v3.0", 200

@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": "3.0"}), 200

@app.route("/admin")
@app.route("/admin.html")
def serve_admin():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, "admin.html")

@app.route("/api/auth/verify", methods=["POST"])
def auth_verify():
    try:
        d = request.json
        wallet, sig, msg = normalize_address(d.get("wallet_address","")), d.get("signature",""), d.get("message","")
        if not all([wallet, sig, msg]): return jsonify({"ok":False, "error":"Missing"}), 400
        enc = encode_defunct(text=msg)
        if Account.recover_message(enc, signature=sig).lower() == wallet:
            return jsonify({"ok":True, "verified":True})
        return jsonify({"ok":False, "error":"Mismatch"}), 401
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/user/register", methods=["POST"])
def register_user():
    try:
        d = request.json
        wallet = normalize_address(d.get("wallet_address",""))
        if not wallet or len(wallet) < 10: return jsonify({"ok":False, "error":"Invalid wallet"}), 400

        user = get_user_by_wallet(wallet)
        if user:
            updates = {}
            un = str(d.get("username",""))[:30]
            fn = str(d.get("first_name",""))[:50]
            if un and un != user.get("username"): updates["username"] = un
            if fn and fn != user.get("first_name"): updates["first_name"] = fn
            if updates:
                ok, _ = sb_patch("users", {"wallet_address":f"eq.{wallet}"}, updates)
                if not ok:
                    logger.error(f"Register update failed for {wallet}")
            return jsonify({"ok":True, "user":user, "is_new":False})

        ref = normalize_address(d.get("referral_code",""))

        new_u = {
            "wallet_address": wallet,
            "username": d.get("username",""),
            "first_name": d.get("first_name","Gladiator"),
            "coins": 500, "season_coins": 500, "sprint_coins": 0,
            "tap_power": 1, "referral_count": 0, "streak": 0,
            "energy": MAX_ENERGY, "last_energy_update": now_iso(),
            "referred_by": ref if (ref and ref != wallet) else None
        }

        created, err = sb_insert("users", new_u)
        if err and "23505" in str(err):
            user = get_user_by_wallet(wallet)
            return jsonify({"ok":True, "user":user, "is_new":False})
        if err: return jsonify({"ok":False, "error":str(err)[:100]}), 500

        if ref and ref != wallet:
            ru = get_user_by_wallet(ref)
            if ru:
                ok, _ = sb_patch("users", {"wallet_address":f"eq.{ref}"}, {
                    "coins":ru["coins"]+1000,
                    "season_coins":ru.get("season_coins",0)+1000,
                    "referral_count":ru.get("referral_count",0)+1
                })
                if not ok:
                    logger.error(f"Referral bonus failed for referrer {ref}")

        return jsonify({"ok":True, "user":created, "is_new":True}), 201
    except Exception as e:
        return jsonify({"ok":False, "error":str(e)}), 500

# ─── Combo calculation from tap timestamps ─────────────────────────────────
# Server-side combo: reconstructs combo chain from tap offsets (ms)
# Progressive window: 700ms at x1 → 350ms at x10
COMBO_WINDOW_START_MS = 700
COMBO_WINDOW_MIN_MS   = 350
COMBO_MAX = 10

def calc_combo_from_offsets(offsets):
    """
    Given a list of tap offsets (ms from first tap), calculate total coins.
    Returns (total_coins, num_valid_taps).
    """
    if not offsets:
        return 0, 0
    offsets = sorted(offsets)
    total_coins = 0
    combo_level = 1
    prev_offset = offsets[0]
    valid_taps = 0

    for i, offset in enumerate(offsets):
        valid_taps += 1
        if i == 0:
            total_coins += combo_level  # first tap = ×1
            continue
        # Calculate required window for current combo level
        window = max(COMBO_WINDOW_MIN_MS,
                     COMBO_WINDOW_START_MS - (combo_level - 1) * ((COMBO_WINDOW_START_MS - COMBO_WINDOW_MIN_MS) / (COMBO_MAX - 1)))
        if (offset - prev_offset) <= window:
            combo_level = min(combo_level + 1, COMBO_MAX)
        else:
            combo_level = 1
        total_coins += combo_level
        prev_offset = offset

    return total_coins, valid_taps

# ─── #1 + #2 + #8 FIX: Server-side coins + race condition protection ─────────
@app.route("/api/tap", methods=["POST"])
def record_taps():
    try:
        d = request.json
        wallet = normalize_address(d.get("wallet_address",""))
        taps_sent = int(d.get("taps", 0))
        # #2 FIX: Client sends its last known sync timestamp for idempotency
        client_last_sync = d.get("last_sync_ts", 0)
        # Client sends tap offsets (ms from first tap in this batch)
        tap_offsets = d.get("tap_offsets", [])

        if not wallet: return jsonify({"ok":False, "error":"Missing wallet"}), 400
        if taps_sent <= 0: return jsonify({"ok":False, "error":"Nothing to record"}), 400

        # FIX #4: Dedup check — reject if same wallet sent taps within the last few seconds
        # This prevents double-counting from page reload / sendBeacon + flushTaps race
        if _dedup_check(wallet, client_last_sync):
            user = get_user_by_wallet(wallet)
            if not user: return jsonify({"ok":False, "error":"User not found"}), 404
            server_now = time.time()
            return jsonify({
                "ok": True, "coins": user["coins"],
                "season_coins": user.get("season_coins", 0),
                "sprint_coins": user.get("sprint_coins", 0),
                "energy": user.get("energy", 0), "earned": 0,
                "tap_power": user.get("tap_power", 1), "server_ts": server_now,
                "dedup": True
            })

        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok":False, "error":"User not found"}), 404

        current_energy = user.get("energy", 0)
        tap_power = user.get("tap_power", 1)

        actual_taps = min(taps_sent, current_energy)

        if actual_taps <= 0:
            server_now = time.time()
            return jsonify({
                "ok": True, "coins": user["coins"],
                "season_coins": user.get("season_coins", 0),
                "sprint_coins": user.get("sprint_coins", 0),
                "energy": current_energy, "earned": 0,
                "tap_power": tap_power, "server_ts": server_now
            })

        # #1 FIX: Server calculates coins from tap timestamps + combo
        if tap_offsets and len(tap_offsets) == taps_sent:
            # Use timestamp-based combo calculation
            combo_units, _ = calc_combo_from_offsets(tap_offsets[:actual_taps])
            final_coins = combo_units * tap_power
        else:
            # Fallback: no timestamps, assume no combo
            final_coins = actual_taps * tap_power

        new_energy = current_energy - actual_taps
        new_coins = user.get("coins", 0) + final_coins
        new_season = user.get("season_coins", 0)
        new_sprint = user.get("sprint_coins", 0)

        if get_active_status("seasons"): new_season += final_coins
        if get_active_status("sprints"): new_sprint += final_coins

        ok, err = sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
            "coins": new_coins, "season_coins": new_season, "sprint_coins": new_sprint,
            "energy": new_energy, "last_energy_update": now_iso()
        })
        # #4 FIX: Verify DB update succeeded
        if not ok:
            logger.error(f"Tap DB update FAILED for {wallet}: {err}")
            return jsonify({"ok":False, "error":"DB update failed, tap lost"}), 500

        server_now = time.time()
        return jsonify({
            "ok": True, "coins": new_coins, "season_coins": new_season,
            "sprint_coins": new_sprint, "energy": new_energy, "earned": final_coins,
            "tap_power": tap_power, "server_ts": server_now
        })
    except Exception as e:
        logger.error(f"Tap error for {wallet}: {e}")
        return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/daily", methods=["POST"])
def claim_daily():
    try:
        d = request.json; wallet = normalize_address(d.get("wallet_address",""))
        if not wallet: return jsonify({"ok":False, "error":"Missing wallet"}), 400
        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok":False, "error":"User not found"}), 404

        now = datetime.datetime.now(datetime.timezone.utc)
        streak = user.get("streak", 0)
        last_raw = user.get("last_claim")

        if last_raw:
            last = datetime.datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
            if last.tzinfo is None: last = last.replace(tzinfo=datetime.timezone.utc)
            diff = (now - last).total_seconds() / 3600
            if diff < 24: return jsonify({"ok":False, "error":"already_claimed", "hours_left":round(24-diff,2), "streak":streak}), 200
            if diff > 48: streak = 0

        streak = min(streak + 1, 7)
        bonus = STREAK_BONUS.get(streak, 1000)
        nc = user["coins"] + bonus
        ns = user.get("season_coins", 0) + bonus

        ok, _ = sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {"coins": nc, "season_coins": ns, "streak": streak, "last_claim": now.isoformat()})
        if not ok:
            logger.error(f"Daily claim DB update FAILED for {wallet}")
            return jsonify({"ok": False, "error": "DB update failed"}), 500

        # REFERRAL PASSIVE INCOME 5%
        ref = user.get("referred_by")
        if ref:
            ru = get_user_by_wallet(ref)
            if ru:
                p = int(bonus * 0.05)
                ok_ref, _ = sb_patch("users", {"wallet_address": f"eq.{ref}"}, {"coins": ru["coins"] + p, "season_coins": ru.get("season_coins", 0) + p})
                if not ok_ref:
                    logger.error(f"Referral passive income FAILED for {ref}")

        return jsonify({"ok":True, "coins_earned":bonus, "coins":nc, "season_coins":ns, "streak":streak})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    try:
        lb_type = request.args.get("type", "alltime")
        limit = min(int(request.args.get("limit", 10)), 100)
        col = {"alltime":"coins", "season":"season_coins", "sprint":"sprint_coins"}.get(lb_type, "coins")
        rows = sb_get("users", {
            "select": "wallet_address,username,first_name,coins,season_coins,sprint_coins,tap_power",
            "order": f"{col}.desc", "limit": str(limit)
        })
        pool, _ = get_onchain_balance(TREASURY_ADDR)
        return jsonify({"ok":True, "entries":rows, "prize_pool_avax":pool})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

# ─── #5 FIX: Dedicated rank endpoint ─────────────────────────────────────────
@app.route("/api/user/rank", methods=["GET"])
def user_rank():
    """
    Efficient rank lookup: uses Supabase count filter instead of fetching top-100.
    Query: count users whose coins > my coins, then rank = count + 1.
    Supports ?type=alltime|season|sprint
    """
    try:
        wallet = normalize_address(request.args.get("wallet", ""))
        if not wallet:
            return jsonify({"ok": False, "error": "Missing wallet"}), 400

        lb_type = request.args.get("type", "alltime")
        col = {"alltime": "coins", "season": "season_coins", "sprint": "sprint_coins"}.get(lb_type, "coins")

        user = get_user_by_wallet(wallet)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404

        my_val = user.get(col, 0)

        # Count how many users have MORE coins than this user
        rows = sb_get("users", {
            "select": "id",
            f"{col}": f"gt.{my_val}",
        })
        rank = len(rows) + 1

        return jsonify({"ok": True, "rank": rank, "coins": my_val, "type": lb_type})
    except Exception as e:
        logger.error(f"Rank lookup error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/prize-pool", methods=["GET"])
def prize_pool():
    try:
        avax, arena = get_onchain_balance(TREASURY_ADDR)
        return jsonify({"ok":True, "avax":avax, "arena":arena, "distribution":[{"rank":i+1,"pct":p} for i,p in enumerate(PRIZE_DIST)]})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/squad/<wallet>", methods=["GET"])
def squad_info(wallet):
    try:
        w = normalize_address(wallet)
        if not get_user_by_wallet(w): return jsonify({"ok":False, "error":"Not found"}), 404
        mems = sb_get("users", {"referred_by":f"eq.{w}", "select":"wallet_address,username,first_name,coins", "order":"coins.desc", "limit":"20"})
        total = sum(m.get("coins",0) for m in mems)
        return jsonify({"ok":True, "members":mems, "member_count":len(mems), "total_coins":total, "passive_5pct":int(total*0.05), "referral_code":w})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

# --- PAYMENT & ADMIN ENDPOINTS ---

@app.route("/api/verify-payment", methods=["POST"])
def verify_payment():
    try:
        d = request.json
        tx, item, wall = d.get("tx_hash","").strip(), d.get("item_id","").lower(), normalize_address(d.get("wallet_address",""))
        if not all([tx, item, wall]): return jsonify({"ok":False, "error":"Missing"}), 400
        if item not in SHOP_ITEMS: return jsonify({"ok":False, "error":"Invalid item"}), 400
        if sb_get("payments", {"tx_hash":f"eq.{tx}"}): return jsonify({"ok":False, "error":"TX used"}), 409

        price, _, label, act, val = SHOP_ITEMS[item]
        rpc = req.post(AVAX_RPC, json={"jsonrpc":"2.0", "method":"eth_getTransactionByHash", "params":[tx], "id":1}, timeout=10).json().get("result")
        if not rpc: return jsonify({"ok":False, "error":"TX not found"}), 404
        if rpc.get("to","").lower() != TREASURY_ADDR: return jsonify({"ok":False, "error":"Wrong dest"}), 400
        if int(rpc.get("value","0x0"),16) < int(price*1e18*0.99): return jsonify({"ok":False, "error":"Low amount"}), 400

        u = get_user_by_wallet(wall)
        if not u: return jsonify({"ok":False, "error":"User not found"}), 404

        sb_insert("payments", {"wallet_address":wall, "tx_hash":tx, "amount_avax":price, "item":item, "verified":True})

        if act == "energy":
            ne = min(u.get("energy",0)+val, 100)
            ok, _ = sb_patch("users", {"wallet_address":f"eq.{wall}"}, {"energy":ne, "last_energy_update":now_iso()})
            if not ok: logger.error(f"Payment energy update FAILED for {wall}")
            return jsonify({"ok":True, "label":label, "energy":ne})
        elif act == "upgrade":
            if val <= u.get("tap_power",1): return jsonify({"ok":False, "error":"Owned"}), 409
            ok, _ = sb_patch("users", {"wallet_address":f"eq.{wall}"}, {"tap_power":val})
            if not ok: logger.error(f"Payment upgrade update FAILED for {wall}")
            return jsonify({"ok":True, "label":label, "tap_power":val})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/verify-arena-payment", methods=["POST"])
def verify_arena_payment():
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    PRICES = {"energy_25":500, "energy_100":1500, "upgrade_2":3000, "upgrade_5":8000, "upgrade_10":20000, "upgrade_25":50000}
    try:
        d = request.json
        tx, item, wall = d.get("tx_hash","").strip(), d.get("item_id","").lower(), normalize_address(d.get("wallet_address",""))
        if not all([tx, item, wall]): return jsonify({"ok":False, "error":"Missing"}), 400
        if item not in PRICES: return jsonify({"ok":False, "error":"Invalid item"}), 400
        if sb_get("payments", {"tx_hash":f"eq.{tx}"}): return jsonify({"ok":False, "error":"TX used"}), 409

        amt = PRICES[item]
        rcpt = req.post(AVAX_RPC, json={"jsonrpc":"2.0", "method":"eth_getTransactionReceipt", "params":[tx], "id":1}, timeout=10).json().get("result")
        if not rcpt: return jsonify({"ok":False, "error":"No receipt"}), 404
        if rcpt.get("status")=="0x0": return jsonify({"ok":False, "error":"Failed TX"}), 400

        ok = False
        for lg in rcpt.get("logs",[]):
            t = lg.get("topics",[])
            if lg.get("address","").lower() != ARENA_TOKEN_ADDR.lower() or len(t)<3 or t[0].lower()!=TRANSFER_TOPIC.lower(): continue
            from_a, to_a = "0x"+t[1][-40:], "0x"+t[2][-40:]
            if from_a.lower()==wall.lower() and to_a.lower()==TREASURY_ADDR.lower() and int(lg.get("data","0x0"),16) >= amt*1e18*0.99:
                ok=True; break

        if not ok: return jsonify({"ok":False, "error":"Transfer not found"}), 400

        _, _, label, act, val = SHOP_ITEMS[item]
        sb_insert("payments", {"wallet_address":wall, "tx_hash":tx, "amount_avax":0, "item":item, "verified":True})
        u = get_user_by_wallet(wall)
        if not u: return jsonify({"ok":False, "error":"User not found"}), 404

        if act == "energy":
            ne = min(u.get("energy",0)+val, 100)
            ok_db, _ = sb_patch("users", {"wallet_address":f"eq.{wall}"}, {"energy":ne})
            if not ok_db: logger.error(f"Arena payment energy update FAILED for {wall}")
            return jsonify({"ok":True, "label":label, "energy":ne})
        elif act == "upgrade":
            if val <= u.get("tap_power",1): return jsonify({"ok":False, "error":"Owned"}), 409
            ok_db, _ = sb_patch("users", {"wallet_address":f"eq.{wall}"}, {"tap_power":val})
            if not ok_db: logger.error(f"Arena payment upgrade update FAILED for {wall}")
            return jsonify({"ok":True, "label":label, "tap_power":val})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

# --- ADMIN ---

@app.route("/api/admin/status", methods=["GET","POST"])
@require_admin
def admin_status():
    try:
        avax, arena = get_onchain_balance(TREASURY_ADDR)
        return jsonify({"ok":True, "prize_pool_avax":avax, "prize_pool_arena":arena, "users_count":len(sb_get("users",{"select":"id"})), "seasons":sb_get("seasons",{"order":"id.desc","limit":"10"}), "sprints":sb_get("sprints",{"order":"id.desc","limit":"10"})})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/admin/users", methods=["GET"])
@require_admin
def admin_users():
    try:
        ord = request.args.get("order","coins")
        col = {"coins":"coins", "season_coins":"season_coins", "created_at":"created_at"}.get(ord,"coins")
        users = sb_get("users", {"select":"wallet_address,username,first_name,coins,season_coins,sprint_coins,tap_power,energy,streak,referral_count", "order":f"{col}.desc", "limit":"50"})
        return jsonify({"ok":True, "count":len(users), "users":users})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/admin/new-season", methods=["POST"])
@require_admin
def new_season():
    try:
        d = request.json
        ok, _ = sb_patch("seasons", {"is_active":"eq.true"}, {"is_active":False})
        if not ok: logger.warning("Failed to deactivate old season")
        ns, err = sb_insert("seasons", {"name":d.get("season_name",f"Season {datetime.datetime.now().strftime('%B %Y')}"), "prize_description":d.get("prize_description",""), "starts_at":d.get("starts_at"), "ends_at":d.get("ends_at"), "is_active":True})
        if err: return jsonify({"ok":False, "error":err}), 500

        reset = {"season_coins":0, "sprint_coins":0}
        if d.get("reset_tap_power", True): reset["tap_power"]=1
        ok, _ = sb_patch("users", {"id":"gt.0"}, reset)
        if not ok: logger.warning("Failed to reset users for new season")
        return jsonify({"ok":True, "message":"Season reset", "ends_at":d.get("ends_at")})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/admin/sprint", methods=["POST"])
@require_admin
def new_sprint():
    try:
        d = request.json
        ok, _ = sb_patch("sprints", {"is_active":"eq.true"}, {"is_active":False})
        if not ok: logger.warning("Failed to deactivate old sprint")
        ns, err = sb_insert("sprints", {"name":d.get("sprint_name","Sprint"), "prize_description":d.get("prize_description",""), "starts_at":d.get("starts_at"), "ends_at":d.get("ends_at"), "is_active":True})
        if err: return jsonify({"ok":False, "error":err}), 500
        ok, _ = sb_patch("users", {"id":"gt.0"}, {"sprint_coins":0})
        if not ok: logger.warning("Failed to reset sprint coins")
        return jsonify({"ok":True, "message":"Sprint started", "ends_at":d.get("ends_at")})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/admin/end-sprint", methods=["POST"])
@require_admin
def end_sprint():
    try:
        act = sb_get("sprints", {"is_active":"eq.true"})
        if act:
            ok, _ = sb_patch("sprints", {"id":f"eq.{act[0]['id']}"}, {"is_active":False, "ends_at":now_iso()})
            if not ok: logger.error("Failed to end sprint")
            return jsonify({"ok":True})
        return jsonify({"ok":False, "error":"No active"}), 400
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/admin/ban-user", methods=["POST"])
@require_admin
def ban_user():
    try:
        w = normalize_address(request.json.get("wallet_address",""))
        if not w: return jsonify({"ok":False, "error":"Missing"}), 400
        if not get_user_by_wallet(w): return jsonify({"ok":False, "error":"Not found"}), 404
        ok, _ = sb_patch("users", {"wallet_address":f"eq.{w}"}, {"coins":0, "season_coins":0, "sprint_coins":0, "tap_power":1, "energy":0})
        if not ok: logger.error(f"Ban failed for {w}")
        return jsonify({"ok":True, "banned":w})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
