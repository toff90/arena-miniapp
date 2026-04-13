"""
🔥 Arena MiniApp API v2.6 — UX Fix & Energy Sync
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fixes:
  - Server now calculates Energy Regen (solves "No energy" 400 errors).
  - Removed harsh "No energy" rejection: taps are clamped to available energy.
  - Fixed Registration Race Condition (no more 409 Duplicate Key errors).
  - Removed rate limiting on Tap endpoint for fluid gameplay.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import logging
import datetime
import requests as req
import jwt
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
FRONTEND_ORIGINS    = os.getenv("FRONTEND_ORIGINS", "*").split(",") # Allow all for testing if needed
AVAX_RPC            = "https://api.avax.network/ext/bc/C/rpc"
ARENA_TOKEN_ADDR    = "0xb8d7710f7d8349a506b75dd184f05777c82dad0c"
BALANCEOF_SIG       = "0x70a08231"
MAX_ENERGY          = 100
ENERGY_REGEN_MIN    = 5

HEADERS = {
    "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json", "Prefer": "return=representation",
}

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
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day", "60 per minute"], storage_uri="memory://")

# ─── Helpers ───────────────────────────────────────────────────────────────────

def sb_get(table, params={}):
    try:
        r = req.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params, timeout=10)
        return r.json() if r.status_code == 200 else []
    except: return []

def sb_insert(table, data):
    try:
        r = req.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, json=data, timeout=10)
        if r.status_code not in (200, 201): return None, r.text
        res = r.json()
        return (res[0] if isinstance(res, list) and res else data), None
    except Exception as e: return None, str(e)

def sb_patch(table, params, data):
    try:
        r = req.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params, json=data, timeout=10)
        return data
    except: return {}

def normalize_address(addr): return (addr or "").strip().lower()

def calc_energy_regen(saved, last_iso):
    """Calcola l'energia attuale basandosi sul tempo trascorso."""
    if not last_iso: return saved or MAX_ENERGY
    try:
        last = datetime.datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
        if last.tzinfo is None: last = last.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        mins = (now - last).total_seconds() / 60
        return min(MAX_ENERGY, (saved or 0) + int(mins / ENERGY_REGEN_MIN))
    except: return saved or MAX_ENERGY

def get_user_by_wallet(wallet):
    rows = sb_get("users", {"wallet_address": f"eq.{normalize_address(wallet)}"})
    if not rows: return None
    u = rows[0]
    # IMPORTANTE: Aggiorna l'energia lato server prima di restituire l'utente
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

def get_onchain_balance(address):
    avax, arena = 0.0, 0.0
    try:
        r = req.post(AVAX_RPC, json={"jsonrpc":"2.0", "method":"eth_getBalance", "params":[address, "latest"], "id":1}, timeout=5)
        if r.ok: avax = int(r.json().get("result","0x0"), 16) / 1e18
    except: pass
    try:
        data = f"{BALANCEOF_SIG}{'0'*24}{address[2:].lower()}"
        r = req.post(AVAX_RPC, json={"jsonrpc":"2.0", "method":"eth_call", "params":[{"to":ARENA_TOKEN_ADDR, "data":data}, "latest"], "id":2}, timeout=5)
        if r.ok: arena = int(r.json().get("result","0x0"), 16) / 1e18
    except: pass
    return avax, arena

# ─── Auth ─────────────────────────────────────────────────────────────────────

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

# ─── Core Endpoints ───────────────────────────────────────────────────────────

@app.route("/")
def index(): return "⚔️ Arena API v2.6", 200

@app.route("/api/user/register", methods=["POST"])
def register_user():
    try:
        d = request.json
        wallet = normalize_address(d.get("wallet_address",""))
        if not wallet or len(wallet) < 10: return jsonify({"ok":False, "error":"Invalid wallet"}), 400
        
        # Check existing first
        user = get_user_by_wallet(wallet)
        if user:
            # Aggiorna username se fornito
            updates = {}
            un = str(d.get("username",""))[:30]
            fn = str(d.get("first_name",""))[:50]
            if un and un != user.get("username"): updates["username"] = un
            if fn and fn != user.get("first_name"): updates["first_name"] = fn
            if updates: sb_patch("users", {"wallet_address":f"eq.{wallet}"}, updates)
            return jsonify({"ok":True, "user":user, "is_new":False})

        # Create new
        ref = normalize_address(d.get("referral_code",""))
        new_u = {
            "wallet_address": wallet, 
            "username": d.get("username",""), 
            "first_name": d.get("first_name","Gladiator"),
            "coins": 500, "season_coins": 500, "sprint_coins": 0, 
            "tap_power": 1, "referral_count": 0, "streak": 0, 
            "energy": MAX_ENERGY, "last_energy_update": now_iso()
        }
        
        created, err = sb_insert("users", new_u)
        # Se fallisce per race condition (409), riprova a leggere
        if err and "23505" in str(err): 
            user = get_user_by_wallet(wallet)
            return jsonify({"ok":True, "user":user, "is_new":False})
        if err: return jsonify({"ok":False, "error":str(err)[:100]}), 500

        # Referral logic
        if ref and ref != wallet:
            ru = get_user_by_wallet(ref)
            if ru:
                sb_patch("users", {"wallet_address":f"eq.{ref}"}, {"coins":ru["coins"]+1000, "season_coins":ru.get("season_coins",0)+1000, "referral_count":ru.get("referral_count",0)+1})
        
        return jsonify({"ok":True, "user":created, "is_new":True}), 201
    except Exception as e:
        return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/tap", methods=["POST"])
# RIMOSSO LIMITER STRETTO PER TAP GAME
def record_taps():
    """
    Logic: Server calculates energy regen.
    If user sends more taps than energy, we count ONLY available energy (forgiving).
    """
    try:
        d = request.json
        wallet = normalize_address(d.get("wallet_address",""))
        taps_sent = int(d.get("taps", 0))
        
        if not wallet: return jsonify({"ok":False, "error":"Missing wallet"}), 400
        if taps_sent <= 0: return jsonify({"ok":False, "error":"Nothing to record"}), 400

        user = get_user_by_wallet(wallet) # Questo calcola già l'energia rigenerata
        if not user: return jsonify({"ok":False, "error":"User not found"}), 404

        current_energy = user.get("energy", 0)
        tap_power = user.get("tap_power", 1)

        # LOGICA PERMISSIVA: Se l'utente tappa più dell'energia che ha, usiamo l'energia rimanente.
        # Questo evita errori 400 frustranti se il frontend è leggermente sfasato.
        actual_taps = min(taps_sent, current_energy)
        
        if actual_taps <= 0:
             # Se davvero non c'è energia, restituiamo lo stato attuale senza errori
             return jsonify({"ok":True, "coins":user["coins"], "energy":0, "earned":0})

        coins_earned = actual_taps * tap_power
        new_energy = current_energy - actual_taps
        new_coins = user.get("coins",0) + coins_earned
        new_season = user.get("season_coins",0)
        new_sprint = user.get("sprint_coins",0)

        if get_active_status("seasons"): new_season += coins_earned
        if get_active_status("sprints"): new_sprint += coins_earned

        sb_patch("users", {"wallet_address":f"eq.{wallet}"}, {
            "coins": new_coins, "season_coins": new_season, "sprint_coins": new_sprint,
            "energy": new_energy, "last_energy_update": now_iso()
        })

        return jsonify({
            "ok": True, "coins": new_coins, "season_coins": new_season,
            "sprint_coins": new_sprint, "energy": new_energy, "earned": coins_earned
        })
    except Exception as e:
        logger.error(f"Tap error: {e}")
        return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/daily", methods=["POST"])
def claim_daily():
    try:
        d = request.json; wallet = normalize_address(d.get("wallet_address",""))
        if not wallet: return jsonify({"ok":False, "error":"Missing wallet"}), 400
        user = get_user_by_wallet(wallet)
        if not user: return jsonify({"ok":False, "error":"User not found"}), 404
        
        now = datetime.datetime.now(datetime.timezone.utc)
        streak = user.get("streak",0)
        last_raw = user.get("last_claim")
        
        if last_raw:
            last = datetime.datetime.fromisoformat(last_raw.replace("Z","+00:00"))
            if last.tzinfo is None: last = last.replace(tzinfo=datetime.timezone.utc)
            diff = (now - last).total_seconds() / 3600
            if diff < 24: return jsonify({"ok":False, "error":"already_claimed", "hours_left":round(24-diff,2), "streak":streak}), 200
            if diff > 48: streak = 0
        
        streak = min(streak+1, 7)
        bonus = STREAK_BONUS.get(streak, 1000)
        nc = user["coins"] + bonus
        ns = user.get("season_coins",0) + bonus
        
        sb_patch("users", {"wallet_address":f"eq.{wallet}"}, {"coins":nc, "season_coins":ns, "streak":streak, "last_claim":now.isoformat()})
        
        ref = user.get("referred_by")
        if ref:
            ru = get_user_by_wallet(ref)
            if ru:
                p = int(bonus*0.05)
                sb_patch("users", {"wallet_address":f"eq.{ref}"}, {"coins":ru["coins"]+p, "season_coins":ru.get("season_coins",0)+p})
        
        return jsonify({"ok":True, "coins_earned":bonus, "coins":nc, "streak":streak})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

# ... (RESTO DEGLI ENDPOINT INALTERATI: leaderboard, prize-pool, shop, admin) ...

@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    try:
        type = request.args.get("type","alltime")
        limit = min(int(request.args.get("limit",10)), 100)
        col = {"alltime":"coins", "season":"season_coins", "sprint":"sprint_coins"}.get(type,"coins")
        rows = sb_get("users", {"select":"wallet_address,username,first_name,coins,season_coins,sprint_coins,tap_power", "order":f"{col}.desc", "limit":str(limit)})
        pool, _ = get_onchain_balance(TREASURY_ADDR)
        return jsonify({"ok":True, "entries":rows, "prize_pool_avax":pool})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

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

# ... (PAYMENT & ADMIN ENDPOINTS SIMILARI A VERSIONE PRECEDENTE) ...

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
            sb_patch("users", {"wallet_address":f"eq.{wall}"}, {"energy":ne, "last_energy_update":now_iso()})
            return jsonify({"ok":True, "label":label, "energy":ne})
        elif act == "upgrade":
            if val <= u.get("tap_power",1): return jsonify({"ok":False, "error":"Owned"}), 409
            sb_patch("users", {"wallet_address":f"eq.{wall}"}, {"tap_power":val})
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
            sb_patch("users", {"wallet_address":f"eq.{wall}"}, {"energy":ne})
            return jsonify({"ok":True, "label":label, "energy":ne})
        elif act == "upgrade":
            if val <= u.get("tap_power",1): return jsonify({"ok":False, "error":"Owned"}), 409
            sb_patch("users", {"wallet_address":f"eq.{wall}"}, {"tap_power":val})
            return jsonify({"ok":True, "label":label, "tap_power":val})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

# ─── Admin Endpoints ───────────────────────────────────────────────────────────

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
        sb_patch("seasons", {"is_active":"eq.true"}, {"is_active":False})
        ns, err = sb_insert("seasons", {"name":d.get("season_name",f"Season {datetime.datetime.now().strftime('%B %Y')}"), "prize_description":d.get("prize_description",""), "starts_at":d.get("starts_at"), "ends_at":d.get("ends_at"), "is_active":True})
        if err: return jsonify({"ok":False, "error":err}), 500
        
        reset = {"season_coins":0, "sprint_coins":0}
        if d.get("reset_tap_power", True): reset["tap_power"]=1
        sb_patch("users", {"id":"gt.0"}, reset)
        return jsonify({"ok":True, "message":"Season reset", "ends_at":d.get("ends_at")})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/admin/sprint", methods=["POST"])
@require_admin
def new_sprint():
    try:
        d = request.json
        sb_patch("sprints", {"is_active":"eq.true"}, {"is_active":False})
        ns, err = sb_insert("sprints", {"name":d.get("sprint_name","Sprint"), "prize_description":d.get("prize_description",""), "starts_at":d.get("starts_at"), "ends_at":d.get("ends_at"), "is_active":True})
        if err: return jsonify({"ok":False, "error":err}), 500
        sb_patch("users", {"id":"gt.0"}, {"sprint_coins":0})
        return jsonify({"ok":True, "message":"Sprint started", "ends_at":d.get("ends_at")})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/api/admin/end-sprint", methods=["POST"])
@require_admin
def end_sprint():
    try:
        act = sb_get("sprints", {"is_active":"eq.true"})
        if act:
            sb_patch("sprints", {"id":f"eq.{act[0]['id']}"}, {"is_active":False, "ends_at":now_iso()})
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
        sb_patch("users", {"wallet_address":f"eq.{w}"}, {"coins":0, "season_coins":0, "sprint_coins":0, "tap_power":1, "energy":0})
        return jsonify({"ok":True, "banned":w})
    except Exception as e: return jsonify({"ok":False, "error":str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
