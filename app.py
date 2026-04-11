"""
🔥 Arena MiniApp API v2.0 — Pure HTTP, Zero Telegram
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Flask REST API, no Telegram dependency
- User identity = wallet address (Arena SDK / WalletConnect)
- Supabase REST via httpx (Termux-safe, no Rust compilation)
- CORS configured for arena.social + GitHub Pages
- Deploy: Render free tier, kept alive by UptimeRobot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import logging
import datetime
import hashlib
import hmac
import requests as req
from flask import Flask, request, jsonify
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
SUPABASE_KEY      = os.getenv("SUPABASE_SERVICE_KEY", "")   # service role (server-side only)
TREASURY_ADDR     = os.getenv("TREASURY_ADDR", "").lower()
ADMIN_KEY         = os.getenv("ADMIN_KEY", "")              # secret for admin endpoints
FRONTEND_ORIGINS  = os.getenv("FRONTEND_ORIGINS",
    "https://arena.social,https://toff90.github.io,http://localhost:3000"
).split(",")
AVAX_RPC          = "https://api.avax.network/ext/bc/C/rpc"

# Supabase service-role headers (NEVER sent to client)
HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

# Shop catalogue: item_id → (avax_price, arena_price, label, action, value)
SHOP_ITEMS = {
    "energy_25":   (0.05,   500,  "⚡ Small Refill",  "energy",  25),
    "energy_100":  (0.15,  1500,  "⚡⚡ Full Refill",  "energy",  100),
    "upgrade_2":   (0.30,  3000,  "⚡ Power x2",       "upgrade", 2),
    "upgrade_5":   (0.80,  8000,  "🔥 Power x5",       "upgrade", 5),
    "upgrade_10":  (2.00, 20000,  "💎 Power x10",      "upgrade", 10),
    "upgrade_25":  (5.00, 50000,  "🚀 Power x25",      "upgrade", 25),
}

STREAK_BONUS = {1: 250, 2: 250, 3: 250, 4: 250, 5: 250, 6: 250, 7: 1000}
PRIZE_DIST   = [0.40, 0.20, 0.10, 0.05, 0.05, 0.05, 0.05, 0.03, 0.03, 0.04]

# ─── Flask Setup ───────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app,
     origins=FRONTEND_ORIGINS,
     supports_credentials=True,
     allow_headers=["Content-Type", "Authorization", "X-Admin-Key"],
     methods=["GET", "POST", "PATCH", "OPTIONS"])

# ─── Supabase Helpers (sync via requests) ──────────────────────────────────────

def sb_get(table: str, params: dict = {}) -> list:
    """SELECT from Supabase table."""
    try:
        r = req.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=HEADERS,
            params=params,
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"sb_get({table}): {e}")
        return []


def sb_insert(table: str, data: dict) -> dict:
    """INSERT into Supabase table, returns created row."""
    try:
        r = req.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=HEADERS,
            json=data,
            timeout=10
        )
        r.raise_for_status()
        res = r.json()
        return res[0] if isinstance(res, list) and res else data
    except Exception as e:
        logger.error(f"sb_insert({table}): {e}")
        return data


def sb_patch(table: str, params: dict, data: dict) -> dict:
    """UPDATE rows matching params."""
    try:
        r = req.patch(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=HEADERS,
            params=params,
            json=data,
            timeout=10
        )
        r.raise_for_status()
        res = r.json()
        return res[0] if isinstance(res, list) and res else {}
    except Exception as e:
        logger.error(f"sb_patch({table}): {e}")
        return {}


def sb_rpc(fn: str, payload: dict) -> dict:
    """Call a Supabase RPC function."""
    try:
        r = req.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{fn}",
            headers=HEADERS,
            json=payload,
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"sb_rpc({fn}): {e}")
        return {}


def normalize_address(addr: str) -> str:
    """Normalize wallet address to lowercase."""
    return (addr or "").strip().lower()


def get_user_by_wallet(wallet: str) -> dict | None:
    """Fetch user row by wallet_address."""
    rows = sb_get("users", {"wallet_address": f"eq.{normalize_address(wallet)}"})
    return rows[0] if rows else None


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sync_prize_pool(avax_amount: float):
    """Add AVAX to the prize pool atomically."""
    pool = sb_get("prize_pool", {"id": "eq.1"})
    if pool:
        current = float(pool[0].get("total_avax", 0))
        sb_patch("prize_pool", {"id": "eq.1"}, {
            "total_avax": current + avax_amount,
            "updated_at": now_iso()
        })


def require_admin(f):
    """Decorator: blocks requests without valid ADMIN_KEY."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Admin-Key") or request.json.get("admin_key", "")
        if not ADMIN_KEY or key != ADMIN_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ─── Public Endpoints ──────────────────────────────────────────────────────────

@app.route("/health")
def health():
    """UptimeRobot ping endpoint."""
    return jsonify({"status": "ok", "service": "arena-api", "version": "2.0"}), 200


@app.route("/api/user/register", methods=["POST"])
def register_user():
    """
    Register or login a user via wallet address.
    Body: { wallet_address, username?, first_name?, referral_code? }
    Returns: { ok, user, is_new }
    """
    try:
        data = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        if not wallet or not wallet.startswith("0x") or len(wallet) < 10:
            return jsonify({"ok": False, "error": "Invalid wallet address"}), 400

        username   = str(data.get("username", ""))[:30]
        first_name = str(data.get("first_name", "Gladiator"))[:50]
        referral   = normalize_address(data.get("referral_code", ""))

        existing = get_user_by_wallet(wallet)
        if existing:
            # Update username/first_name if changed
            updates = {}
            if username and username != existing.get("username"):
                updates["username"] = username
            if first_name and first_name != existing.get("first_name"):
                updates["first_name"] = first_name
            if updates:
                sb_patch("users", {"wallet_address": f"eq.{wallet}"}, updates)
                existing.update(updates)
            return jsonify({"ok": True, "user": existing, "is_new": False})

        # New user — welcome bonus
        new_user = {
            "wallet_address": wallet,
            "username":       username,
            "first_name":     first_name or "Gladiator",
            "coins":          500,
            "season_coins":   500,
            "tap_power":      1,
            "referral_count": 0,
            "streak":         0,
            "energy":         100,
            "last_energy_update": now_iso(),
        }

        # Handle referral
        if referral and referral != wallet:
            ref_user = get_user_by_wallet(referral)
            if ref_user:
                new_user["referred_by"] = referral
                sb_patch("users", {"wallet_address": f"eq.{referral}"}, {
                    "coins":          ref_user["coins"] + 1000,
                    "season_coins":   ref_user.get("season_coins", 0) + 1000,
                    "referral_count": ref_user.get("referral_count", 0) + 1,
                })

        created = sb_insert("users", new_user)
        return jsonify({"ok": True, "user": created, "is_new": True}), 201

    except Exception as e:
        logger.error(f"register_user: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/user/<wallet>", methods=["GET"])
def get_user(wallet: str):
    """
    Fetch user data by wallet address.
    Returns: { ok, user }
    """
    wallet = normalize_address(wallet)
    user = get_user_by_wallet(wallet)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    return jsonify({"ok": True, "user": user})


@app.route("/api/tap", methods=["POST"])
def record_taps():
    """
    Batch-record taps and persist coins.
    Body: { wallet_address, taps, coins_earned }
    Returns: { ok, coins, season_coins }
    """
    try:
        data   = request.get_json(force=True) or {}
        wallet = normalize_address(data.get("wallet_address", ""))
        taps   = int(data.get("taps", 0))
        coins  = int(data.get("coins_earned", 0))

        if not wallet:
            return jsonify({"ok": False, "error": "Missing wallet_address"}), 400
        if taps <= 0 or coins <= 0:
            return jsonify({"ok": False, "error": "Nothing to record"}), 400
        if coins > taps * 25:   # sanity guard: max x25 tap power
            return jsonify({"ok": False, "error": "Suspicious coin amount"}), 400

        user = get_user_by_wallet(wallet)
        if not user:
            return jsonify({"ok": False, "error": "User not found. Register first."}), 404

        new_coins  = user.get("coins", 0) + coins
        new_season = user.get("season_coins", 0) + coins
        new_sprint = user.get("sprint_coins", 0) + coins

        # Energy drain: 1 tap = 1 energy (clamped at 0)
        new_energy = max(0, user.get("energy", 100) - taps)

        sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
            "coins":        new_coins,
            "season_coins": new_season,
            "sprint_coins": new_sprint,
            "energy":       new_energy,
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
    """
    Claim daily reward with streak logic.
    Body: { wallet_address }
    Returns: { ok, coins_earned, streak, next_reward, hours_left? }
    """
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
            last_dt = datetime.datetime.fromisoformat(
                last_raw.replace("Z", "+00:00")
            )
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)

            diff_h = (now - last_dt).total_seconds() / 3600

            if diff_h < 24:
                hours_left = round(24 - diff_h, 1)
                return jsonify({
                    "ok":         False,
                    "error":      "already_claimed",
                    "hours_left": hours_left,
                    "streak":     streak,
                }), 200   # 200 so frontend can read body easily

            if diff_h > 48:
                streak = 0   # streak broken

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
        referrer_wallet = user.get("referred_by")
        if referrer_wallet:
            passive = int(bonus * 0.05)
            ref = get_user_by_wallet(referrer_wallet)
            if ref:
                sb_patch("users", {"wallet_address": f"eq.{referrer_wallet}"}, {
                    "coins":        ref["coins"] + passive,
                    "season_coins": ref.get("season_coins", 0) + passive,
                })

        next_reward = STREAK_BONUS.get(min(streak + 1, 7), 1000)
        return jsonify({
            "ok":          True,
            "coins_earned": bonus,
            "coins":       new_coins,
            "streak":      streak,
            "next_reward": next_reward,
        })

    except Exception as e:
        logger.error(f"claim_daily: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    """
    Fetch leaderboard.
    Query params: type=alltime|season|sprint, limit=10
    Returns: { ok, entries, prize_pool_avax }
    """
    try:
        lb_type = request.args.get("type", "alltime")
        limit   = min(int(request.args.get("limit", 10)), 50)

        order_col = {
            "alltime": "coins",
            "season":  "season_coins",
            "sprint":  "sprint_coins",
        }.get(lb_type, "coins")

        rows = sb_get("users", {
            "select": "wallet_address,username,first_name,coins,season_coins,sprint_coins,tap_power",
            "order":  f"{order_col}.desc",
            "limit":  str(limit),
        })
        # Python-side sort as safety fallback
        entries = sorted(rows, key=lambda x: x.get(order_col, 0), reverse=True)

        pool = sb_get("prize_pool", {"id": "eq.1"})
        pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0

        return jsonify({"ok": True, "entries": entries, "prize_pool_avax": pool_avax, "type": lb_type})

    except Exception as e:
        logger.error(f"leaderboard: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/prize-pool", methods=["GET"])
def prize_pool():
    """Return current prize pool and distribution."""
    try:
        pool = sb_get("prize_pool", {"id": "eq.1"})
        if not pool:
            return jsonify({"ok": True, "avax": 0, "arena": 0, "distribution": PRIZE_DIST})

        avax  = float(pool[0].get("total_avax", 0))
        arena = int(pool[0].get("total_arena", 0))

        dist = [
            {"rank": i + 1, "pct": pct, "avax": round(avax * pct, 4)}
            for i, pct in enumerate(PRIZE_DIST)
        ]
        return jsonify({
            "ok":          True,
            "avax":        avax,
            "arena":       arena,
            "distribution": dist,
        })

    except Exception as e:
        logger.error(f"prize_pool: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/squad/<wallet>", methods=["GET"])
def squad_info(wallet: str):
    """
    Return squad members and passive earnings for a wallet.
    Returns: { ok, members, total_coins, passive_5pct, referral_link }
    """
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
            "ok":         True,
            "members":    members,
            "member_count": len(members),
            "total_coins": total,
            "passive_5pct": passive,
            "referral_code": wallet,   # wallet addr IS the referral code
        })

    except Exception as e:
        logger.error(f"squad_info: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sprint/active", methods=["GET"])
def active_sprint():
    """Return the currently active sprint if any."""
    try:
        sprints = sb_get("sprints", {
            "is_active": "eq.true",
            "order":     "started_at.desc",
            "limit":     "1",
        })
        if not sprints:
            return jsonify({"ok": True, "sprint": None})

        s = sprints[0]
        ends = datetime.datetime.fromisoformat(s["ends_at"].replace("Z", "+00:00"))
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=datetime.timezone.utc)

        if datetime.datetime.now(datetime.timezone.utc) > ends:
            sb_patch("sprints", {"id": f"eq.{s['id']}"}, {"is_active": False})
            return jsonify({"ok": True, "sprint": None})

        remaining = ends - datetime.datetime.now(datetime.timezone.utc)
        h = int(remaining.total_seconds() // 3600)
        m = int((remaining.total_seconds() % 3600) // 60)

        return jsonify({
            "ok": True,
            "sprint": {
                **s,
                "remaining_h": h,
                "remaining_m": m,
                "remaining_label": f"{h}h {m}m",
            }
        })

    except Exception as e:
        logger.error(f"active_sprint: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/verify-payment", methods=["POST"])
def verify_payment():
    """
    Verify an on-chain AVAX payment and apply the shop item.
    Body: { tx_hash, item_id, wallet_address }
    Returns: { ok, item, ... }
    """
    try:
        data       = request.get_json(force=True) or {}
        tx_hash    = data.get("tx_hash", "").strip()
        item_id    = data.get("item_id", "").strip().lower()
        wallet     = normalize_address(data.get("wallet_address", ""))

        # Validation
        if not tx_hash or not item_id or not wallet:
            return jsonify({"ok": False, "error": "Missing fields"}), 400
        if item_id not in SHOP_ITEMS:
            return jsonify({"ok": False, "error": "Unknown item"}), 400
        if not tx_hash.startswith("0x") or len(tx_hash) < 40:
            return jsonify({"ok": False, "error": "Invalid TX hash format"}), 400

        # Duplicate TX check
        existing = sb_get("payments", {"tx_hash": f"eq.{tx_hash}"})
        if existing:
            return jsonify({"ok": False, "error": "TX already used"}), 409

        avax_price, _, label, action, value = SHOP_ITEMS[item_id]
        avax_wei = int(avax_price * 1e18)

        # ── On-chain verification ─────────────────────────────────────────────
        rpc_r = req.post(AVAX_RPC, json={
            "jsonrpc": "2.0", "method": "eth_getTransactionByHash",
            "params":  [tx_hash], "id": 1
        }, timeout=10)
        tx_data = rpc_r.json().get("result")

        if not tx_data:
            return jsonify({"ok": False, "error": "TX not found on chain"}), 404

        to_addr = (tx_data.get("to") or "").lower()
        if TREASURY_ADDR and to_addr != TREASURY_ADDR:
            return jsonify({"ok": False, "error": "Wrong destination address"}), 400

        tx_value = int(tx_data.get("value", "0x0"), 16)
        if tx_value < avax_wei * 0.99:
            return jsonify({
                "ok": False,
                "error": f"Insufficient amount. Expected ~{avax_price} AVAX"
            }), 400

        # ── Get user ──────────────────────────────────────────────────────────
        user = get_user_by_wallet(wallet)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404

        # ── Record payment ────────────────────────────────────────────────────
        sb_insert("payments", {
            "wallet_address": wallet,
            "tx_hash":        tx_hash,
            "amount_avax":    avax_price,
            "item":           item_id,
            "verified":       True,
        })

        _sync_prize_pool(avax_price)

        # ── Apply item ────────────────────────────────────────────────────────
        if action == "energy":
            new_energy = min(user.get("energy", 0) + value, 100)
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {
                "energy":            new_energy,
                "last_energy_update": now_iso(),
            })
            return jsonify({"ok": True, "item": item_id, "label": label, "energy": new_energy})

        elif action == "upgrade":
            current_power = user.get("tap_power", 1)
            if value <= current_power:
                return jsonify({
                    "ok":       False,
                    "error":    "Already owned or lower than current",
                    "tap_power": current_power,
                }), 409
            sb_patch("users", {"wallet_address": f"eq.{wallet}"}, {"tap_power": value})
            return jsonify({"ok": True, "item": item_id, "label": label, "tap_power": value})

    except Exception as e:
        logger.error(f"verify_payment: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

# ─── Admin Endpoints (protected by X-Admin-Key header) ────────────────────────

@app.route("/api/admin/new-season", methods=["POST"])
@require_admin
def new_season():
    """
    End current season, distribute prizes, reset season coins.
    Body: { season_name? } + X-Admin-Key header
    """
    try:
        data        = request.get_json(force=True) or {}
        season_name = data.get("season_name") or \
                      f"Season {datetime.datetime.now().strftime('%B %Y')}"

        top = sb_get("users", {
            "select": "wallet_address,username,first_name,season_coins",
            "order":  "season_coins.desc",
            "limit":  "10",
        })
        top = sorted(top, key=lambda x: x.get("season_coins", 0), reverse=True)

        pool = sb_get("prize_pool", {"id": "eq.1"})
        pool_avax = float(pool[0].get("total_avax", 0)) if pool else 0.0

        # Close active season
        sb_patch("seasons", {"is_active": "eq.true"}, {
            "is_active": False,
            "ended_at":  now_iso(),
        })

        # Create new season
        new_s = sb_insert("seasons", {"name": season_name, "is_active": True})

        # Record results & distribute
        results = []
        for i, u in enumerate(top):
            pct   = PRIZE_DIST[i] if i < len(PRIZE_DIST) else 0
            prize = pool_avax * pct
            sb_insert("season_results", {
                "season_id":    new_s.get("id", 0),
                "wallet_address": u.get("wallet_address", ""),
                "username":     u.get("username", ""),
                "first_name":   u.get("first_name", "Anon"),
                "final_coins":  u.get("season_coins", 0),
                "rank":         i + 1,
                "prize_avax":   prize,
            })
            results.append({
                "rank":    i + 1,
                "wallet":  u.get("wallet_address", ""),
                "name":    u.get("username") or u.get("first_name", "Anon"),
                "coins":   u.get("season_coins", 0),
                "prize":   prize,
            })

        # Reset all users: season_coins, sprint_coins, tap_power
        req.patch(
            f"{SUPABASE_URL}/rest/v1/users",
            headers={**HEADERS, "Prefer": "return=minimal"},
            params={"wallet_address": "neq.0x"},   # all rows
            json={"season_coins": 0, "sprint_coins": 0, "tap_power": 1},
            timeout=20
        )

        # Reset prize pool
        sb_patch("prize_pool", {"id": "eq.1"}, {
            "total_avax":  0,
            "total_arena": 0,
            "updated_at":  now_iso(),
        })

        return jsonify({
            "ok":          True,
            "season_name": season_name,
            "pool_avax":   pool_avax,
            "results":     results,
        })

    except Exception as e:
        logger.error(f"new_season: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/sprint", methods=["POST"])
@require_admin
def start_sprint():
    """
    Launch a new sprint.
    Body: { hours?, sprint_name? } + X-Admin-Key header
    """
    try:
        data         = request.get_json(force=True) or {}
        hours        = int(data.get("hours", 24))
        sprint_name  = data.get("sprint_name", "⚡ Arena Sprint")

        # End active sprints
        sb_patch("sprints", {"is_active": "eq.true"}, {"is_active": False})

        # Reset sprint_coins for all
        req.patch(
            f"{SUPABASE_URL}/rest/v1/users",
            headers={**HEADERS, "Prefer": "return=minimal"},
            params={"wallet_address": "neq.0x"},
            json={"sprint_coins": 0},
            timeout=20
        )

        ends_at = (
            datetime.datetime.now(datetime.timezone.utc) +
            datetime.timedelta(hours=hours)
        )
        sprint = sb_insert("sprints", {
            "name":              sprint_name,
            "ends_at":           ends_at.isoformat(),
            "is_active":         True,
            "prize_description": "Top 3 special rewards!",
        })

        return jsonify({
            "ok":          True,
            "sprint_name": sprint_name,
            "hours":       hours,
            "ends_at":     ends_at.isoformat(),
        })

    except Exception as e:
        logger.error(f"start_sprint: {e}", exc_info=True)
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
    port = int(os.environ.get("PORT", 10000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    logger.info(f"🔥 Arena API v2.0 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
# ─── Frontend Serving (Aggiunta) ───────────────────────────────────────────────
from flask import send_from_directory
import os

@app.route('/')
def serve_frontend():
    """Serve l'interfaccia utente web3 alla root."""
    return send_from_directory('.', 'index.html')

@app.route('/<path:filename>')
def serve_static_files(filename):
    """Serve asset statici, garantendo che le API continuino a funzionare."""
    if os.path.exists(filename):
        return send_from_directory('.', filename)
    # Lasciamo che le rotte API fallite vengano catturate dall'error handler 404
    return jsonify({"ok": False, "error": "Endpoint or file not found"}), 404
