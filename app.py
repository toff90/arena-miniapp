"""
Arena MiniApp API v3.4 - FULLY Atomic Tap (No Race Condition Possible)
======================================================================
Changes vs v3.3:
  #1 CRITICAL: ALL energy logic moved INSIDE SQL function with FOR UPDATE
               row-level locking. Rapid tapping CANNOT cause energy loss
               or free taps anymore.
  #2 CRITICAL: SQL function now calculates: energy regen + taps accepted +
               coins earned — all in one locked transaction.
  #3 MEDIO:    Python simplified — just sends taps + pre-calculated coins,
               SQL handles everything atomically. No fallback needed.

HOW IT FIXES RAPID TAPPING:
  Before (v3.3): Python reads energy → calculates new_energy → sends to SQL
                  Two rapid taps both read energy=84, both send 74, both SET 74
                  Race = free taps!

  Now (v3.4):  SQL does SELECT ... FOR UPDATE (locks row) → calculates regen
                  → subtracts taps → writes back. Second tap WAITS for lock.
                  No race possible. Period.
"""

import os
import time
import logging
import datetime
import requests as req
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
BALANCE_CACHE_TTL   = 120

HEADERS = {
    "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json", "Prefer": "return=representation",
}

SHOP_ITEMS = {
    "energy_25": (0.05, 500, "Small Refill", "energy", 25),
    "energy_100": (0.15, 1500, "Full Refill", "energy", 100),
    "upgrade_2": (0.30, 3000, "Power x2", "upgrade", 2),
    "upgrade_5": (0.80, 8000, "Power x5", "upgrade", 5),
    "upgrade_10": (2.00, 20000, "Power x10", "upgrade", 10),
    "upgrade_25": (5.00, 50000, "Power x25", "upgrade", 25),
}
STREAK_BONUS = {1: 250, 2: 250, 3: 250, 4: 250, 5: 250, 6: 250, 7: 1000}
PRIZE_DIST = [0.40, 0.20, 0.10, 0.05, 0.05, 0.05, 0.05, 0.03, 0.03, 0.04]

app = Flask(__name__)
CORS(app, origins=FRONTEND_ORIGINS, supports_credentials=True, allow_headers=["Content-Type", "Authorization", "X-Admin-Key"])

# Rate limiting per wallet_address, not per IP
def _get_wallet_or_ip():
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

# ─── On-chain balance cache ──────────────────────────────────────────────────
_balance_cache = {"avax": 0.0, "arena": 0.0, "ts": 0}

def get_onchain_balance(address):
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

# ─── Supabase helpers ────────────────────────────────────────────────────────

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
    try:
        r = req.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params, json=data, timeout=10)
        if r.status_code in (200, 204):
            return True, data
        logger.warning(f"sb_patch({table}) status={r.status_code}: {r.text[:200]}")
        return False, r.text
    except Exception as e:
        logger.error(f"sb_patch({table}) exception: {e}")
        return False, str(e)

# ─── Atomic RPC helper ───────────────────────────────────────────────────────
def sb_rpc(function_name, params):
    """
    Call a Supabase RPC (PostgreSQL) function atomically.
    Returns the response data on success, or None on failure.
    """
    try:
        url = f"{SUPABASE_URL}/rest/v1/rpc/{function_name}"
        r = req.post(url, headers=HEADERS, json=params, timeout=10)
        if r.status_code in (200, 201):
            return r.json()
        logger.warning(f"sb_rpc({function_name}) status={r.status_code}: {r.text[:300]}")
        return None
    except Exception as e:
        logger.error(f"sb_rpc({function_name}) exception: {e}")
        return None

def normalize_address(addr): return (addr or "").strip().lower()

def get_user_by_wallet(wallet):
    """Fetch user WITHOUT energy regen — SQL handles regen now."""
    rows = sb_get("users", {"wallet_address": f"eq.{normalize_address(wallet)}"})
    if not rows: return None
    return rows[0]

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

# ─── Combo calculation from tap timestamps ─────────────────────────────────
COMBO_WINDOW_START_MS = 700
COMBO_WINDOW_MIN_MS   = 350
COMBO_MAX = 10

def calc_combo_from_offsets(offsets):
    """
    Given a list of tap offsets (ms from first tap), calculate total combo coins.
    Returns (total_combo_units, num_valid_taps).
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
            total_coins += combo_level
            continue
        window = max(COMBO_WINDOW_MIN_MS,
                     COMBO_WINDOW_START_MS - (combo_level - 1) * ((COMBO_WINDOW_START_MS - COMBO_WINDOW_MIN_MS) / (COMBO_MAX - 1)))
        if (offset - prev_offset) <= window:
            combo_level = min(combo_level + 1, COMBO_MAX)
        else:
            combo_level = 1
        total_coins += combo_level
        prev_offset = offset

    return total_coins, valid_taps

# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return "Arena API v3.4 - Fully Atomic Taps", 200

@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": "3.4", "atomic_rpc": True, "row_locking": True}), 200

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
                ref_ok = sb_rpc("add_referral_passive", {
                    "p_wallet": ref,
                    "p_amount": 1000
                })
                if ref_ok:
                    logger.info(f"[REGISTER] Referral bonus 1000 coins for {ref} (RPC atomic)")
                else:
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

# ─── v3.4 FULLY ATOMIC: /api/tap ────────────────────────────────────────────
# ALL energy logic is inside the SQL function now.
# SQL uses SELECT ... FOR UPDATE which LOCKS the row.
# Second concurrent tap WAITS for the lock → no race possible.
@app.route("/api/tap", methods=["POST"])
def record_taps():
    try:
        d = request.json
        wallet = normalize_address(d.get("wallet_address", ""))
        taps_sent = int(d.get("taps", 0))
        tap_offsets = d.get("tap_offsets", [])

        if not wallet: return jsonify({"ok": False, "error": "Missing wallet"}), 400
        if taps_sent <= 0: return jsonify({"ok": False, "error": "Nothing to record"}), 400

        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok": False, "error": "User not found"}), 404

        tap_power = user.get("tap_power", 1)

        # Pre-calculate combo coins (assuming ALL taps accepted)
        if tap_offsets and len(tap_offsets) == taps_sent:
            combo_units, _ = calc_combo_from_offsets(tap_offsets)
            pre_coins = combo_units * tap_power
        else:
            pre_coins = taps_sent * tap_power

        # Check if season/sprint is active
        season_earned = pre_coins if get_active_status("seasons") else 0
        sprint_earned = pre_coins if get_active_status("sprints") else 0

        # ─── v3.4: FULLY ATOMIC RPC ────────────────────────────────────
        # SQL function handles: energy regen + taps accepted + coins update
        # Uses FOR UPDATE → row locked → no race condition possible
        rpc_result = sb_rpc("tap_earn_atomic", {
            "p_wallet": wallet,
            "p_taps_requested": taps_sent,
            "p_total_coins": pre_coins,
            "p_season_earned": season_earned,
            "p_sprint_earned": sprint_earned
        })

        if rpc_result is None:
            logger.error(f"[TAP] {wallet[:8]}... RPC FAILED")
            return jsonify({"ok": False, "error": "Server error, retry"}), 500

        # Extract results from atomic SQL function
        taps_accepted = rpc_result.get("taps_accepted", 0)
        coins_earned = rpc_result.get("coins_earned", 0)
        new_energy = rpc_result.get("energy_after", 0)
        updated_user = rpc_result.get("user", {})
        energy_before = rpc_result.get("energy_before", 0)

        logger.info(f"[TAP] {wallet[:8]}... +{coins_earned} coins, taps={taps_accepted}/{taps_sent}, energy {energy_before}->{new_energy} (v3.4 LOCKED)")

        server_now = time.time()
        response = {
            "ok": True,
            "coins": updated_user.get("coins", 0),
            "season_coins": updated_user.get("season_coins", 0),
            "sprint_coins": updated_user.get("sprint_coins", 0),
            "energy": new_energy,
            "earned": coins_earned,
            "taps_accepted": taps_accepted,
            "tap_power": tap_power,
            "server_ts": server_now,
            "method": "atomic_v4"
        }

        # If no energy, add the flag
        if taps_accepted == 0:
            response["no_energy"] = True
            logger.info(f"[TAP] {wallet[:8]}... REJECTED no energy (0/{taps_sent})")

        return jsonify(response)

    except Exception as e:
        logger.error(f"Tap error for {wallet}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── v3.1 ATOMIC: /api/daily with RPC ────────────────────────────────────────
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

        rpc_result = sb_rpc("claim_daily_atomic", {
            "p_wallet": wallet,
            "p_bonus": bonus,
            "p_streak": streak
        })

        if rpc_result is not None:
            logger.info(f"[DAILY] {wallet[:8]}... +{bonus} coins, streak={streak} (RPC atomic)")
            nc = user["coins"] + bonus
            ns = user.get("season_coins", 0) + bonus
            return jsonify({
                "ok": True,
                "coins_earned": bonus,
                "coins": nc,
                "season_coins": ns,
                "streak": streak,
                "method": "atomic_rpc"
            })

        # Fallback
        logger.warning(f"[DAILY] {wallet[:8]}... RPC claim_daily_atomic failed, using fallback")
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
                ref_rpc = sb_rpc("add_referral_passive", {
                    "p_wallet": ref,
                    "p_amount": p
                })
                if ref_rpc:
                    logger.info(f"[DAILY] Referral passive {p} coins for {ref[:8]}... (RPC atomic)")
                else:
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

@app.route("/api/user/rank", methods=["GET"])
def user_rank():
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

# ══════════════════════════════════════════════════════════════════════════════
# REDFO ROULETTE — Aggiunto, codice esistente NON modificato
# ══════════════════════════════════════════════════════════════════════════════
REDFO_TOKEN_ADDR   = "0xA479796434C668DCE1881ed1E0d88Fe87aAdADB8"
REDFO_THRESHOLD    = 69_000_000
REDFO_QUALIFY_DAYS = 30
REDFO_CONSOLATION  = 10_000

ROULETTE_SEGMENTS = [
    {"id":"nft_redpop",    "label":"NFT Red Popcorn",      "tier":"epic",        "weight":4},
    {"id":"1m_paradise",   "label":"1M PARADISE",           "tier":"epic",        "weight":4},
    {"id":"1m_coffma",     "label":"1M COFFMA",             "tier":"epic",        "weight":4},
    {"id":"1m_redpopcorn", "label":"1M REDPOPCORN",         "tier":"epic",        "weight":4},
    {"id":"1m_redfo",      "label":"1M REDFO",              "tier":"epic",        "weight":4},
    {"id":"1k_rpepe",      "label":"1K RPEPE",              "tier":"epic",        "weight":4},
    {"id":"nft_cir",       "label":"NFT Creations In Red",  "tier":"epic",        "weight":4},
    {"id":"1m_rave",       "label":"1M RAVE",               "tier":"epic",        "weight":4},
    {"id":"consolation",   "label":"10,000 REDFO",          "tier":"consolation",  "weight":68},
]

def get_redfo_balance(address):
    try:
        data = f"{BALANCEOF_SIG}{'0'*24}{address[2:].lower()}"
        r = req.post(AVAX_RPC, json={"jsonrpc":"2.0","method":"eth_call","params":[{"to":REDFO_TOKEN_ADDR,"data":data},"latest"],"id":3}, timeout=5)
        if r.ok: return int(r.json().get("result","0x0"),16) / 1e18
    except Exception as e: logger.warning(f"REDFO balance RPC failed: {e}")
    return 0

def roulette_pick_prize():
    import random
    epics = [s for s in ROULETTE_SEGMENTS if s["tier"]=="epic"]
    roll = random.randint(1,100)
    if roll <= 32:
        p = random.choice(epics)
        return p["id"], p["tier"], p["label"], p["weight"], 0
    return "consolation", "consolation", "10,000 REDFO", 68, REDFO_CONSOLATION

@app.route("/api/roulette/check", methods=["POST"])
def roulette_check():
    try:
        d = request.json; wallet = normalize_address(d.get("wallet_address",""))
        if not wallet: return jsonify({"ok":False,"error":"Missing wallet"}),400
        redfo_bal = get_redfo_balance(wallet)
        qualified = redfo_bal >= REDFO_THRESHOLD
        if not qualified:
            rows = sb_get("roulette_qualification", {"wallet_address":f"eq.{wallet}"})
            if rows: sb_patch("roulette_qualification", {"wallet_address":f"eq.{wallet}"}, {"spin_claimed":False,"qualified_at":now_iso()})
            return jsonify({"ok":True,"qualified":False,"redfo_balance":redfo_bal,"redfo_needed":REDFO_THRESHOLD,"message":f"Hold {REDFO_THRESHOLD//1_000_000}M REDFO to qualify"})
        rows = sb_get("roulette_qualification", {"wallet_address":f"eq.{wallet}"})
        if not rows:
            sb_insert("roulette_qualification", {"wallet_address":wallet,"qualified_at":now_iso(),"spin_claimed":False})
            return jsonify({"ok":True,"qualified":True,"spin_available":False,"redfo_balance":redfo_bal,"days_elapsed":0,"days_remaining":REDFO_QUALIFY_DAYS,"message":f"Qualification started! {REDFO_QUALIFY_DAYS} days to go"})
        row = rows[0]
        if row.get("spin_claimed"):
            sb_patch("roulette_qualification", {"wallet_address":f"eq.{wallet}"}, {"qualified_at":now_iso(),"spin_claimed":False})
            return jsonify({"ok":True,"qualified":True,"spin_available":False,"redfo_balance":redfo_bal,"days_elapsed":0,"days_remaining":REDFO_QUALIFY_DAYS,"message":f"New period started! {REDFO_QUALIFY_DAYS} days to go"})
        qualified_at = datetime.datetime.fromisoformat(row["qualified_at"].replace("Z","+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        days_elapsed = (now - qualified_at).total_seconds() / 86400
        days_remaining = max(0, REDFO_QUALIFY_DAYS - days_elapsed)
        spin_available = days_elapsed >= REDFO_QUALIFY_DAYS
        return jsonify({"ok":True,"qualified":True,"spin_available":spin_available,"redfo_balance":redfo_bal,"days_elapsed":round(days_elapsed,1),"days_remaining":round(days_remaining,1),"message":"Spin available!" if spin_available else f"{round(days_remaining,1)} days remaining"})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/roulette/spin", methods=["POST"])
def roulette_spin():
    try:
        d = request.json; wallet = normalize_address(d.get("wallet_address",""))
        if not wallet: return jsonify({"ok":False,"error":"Missing wallet"}),400
        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok":False,"error":"User not found"}),404
        redfo_bal = get_redfo_balance(wallet)
        if redfo_bal < REDFO_THRESHOLD: return jsonify({"ok":False,"error":"Not enough REDFO"}),403
        rows = sb_get("roulette_qualification", {"wallet_address":f"eq.{wallet}"})
        if not rows: return jsonify({"ok":False,"error":"Not qualified yet"}),403
        row = rows[0]
        if row.get("spin_claimed"): return jsonify({"ok":False,"error":"Already spun this period"}),403
        qualified_at = datetime.datetime.fromisoformat(row["qualified_at"].replace("Z","+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        days_elapsed = (now - qualified_at).total_seconds() / 86400
        if days_elapsed < REDFO_QUALIFY_DAYS: return jsonify({"ok":False,"error":"Not ready yet","days_remaining":round(REDFO_QUALIFY_DAYS-days_elapsed,1)}),403
        prize_id, tier, label, weight, amount = roulette_pick_prize()
        sb_insert("roulette_spins", {"wallet_address":wallet,"prize_tier":tier,"prize_label":label,"prize_amount_redfo":amount,"spun_at":now_iso(),"prize_sent":False})
        sb_patch("roulette_qualification", {"wallet_address":f"eq.{wallet}"}, {"spin_claimed":True})
        logger.info(f"[ROULETTE] {wallet[:8]}... spun -> {tier}: {label}")
        return jsonify({"ok":True,"prize_id":prize_id,"prize_tier":tier,"prize_label":label,"prize_amount_redfo":amount,"redfo_balance":redfo_bal,"note":"Epic prize - admin sends manually within 24h" if tier=="epic" else "Consolation prize"})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/roulette/admin/spins", methods=["GET"])
@require_admin
def roulette_admin_spins():
    try:
        status = request.args.get("status","all")
        params = {"order":"spun_at.desc","limit":"100"}
        if status=="pending": params["prize_sent"]="eq.false"
        elif status=="sent": params["prize_sent"]="eq.true"
        spins = sb_get("roulette_spins", params)
        return jsonify({"ok":True,"count":len(spins),"spins":spins})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/roulette/admin/mark-sent", methods=["POST"])
@require_admin
def roulette_admin_mark_sent():
    try:
        d = request.json; spin_id = d.get("spin_id"); note = d.get("admin_note","")
        if not spin_id: return jsonify({"ok":False,"error":"Missing spin_id"}),400
        data = {"prize_sent":True}
        if note: data["admin_note"] = note
        ok, _ = sb_patch("roulette_spins", {"id":f"eq.{spin_id}"}, data)
        if not ok: return jsonify({"ok":False,"error":"Update failed"}),500
        return jsonify({"ok":True,"message":"Prize marked as sent"})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

# ─── Startup banner ──────────────────────────────────────────────────────────
print("=" * 60)
print("  Arena API v3.4 - FULLY Atomic Taps (Row Locking)")
print("  RPC: tap_earn_atomic (FOR UPDATE + energy regen)")
print("       claim_daily_atomic, add_referral_passive")
print("  FIX: Rapid tapping race condition ELIMINATED")
print("  + REDFO Roulette: /api/roulette/check, /api/roulette/spin")
print("=" * 60)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
