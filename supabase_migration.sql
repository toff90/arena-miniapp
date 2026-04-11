-- ============================================================
-- Arena MiniApp v2.0 — Supabase Migration
-- Run this in the Supabase SQL Editor
-- ============================================================

-- 1. Add wallet_address column to users (if upgrading from v1)
ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_address TEXT UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS energy INTEGER DEFAULT 100;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_energy_update TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE users ADD COLUMN IF NOT EXISTS season_coins INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS sprint_coins INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by TEXT;   -- wallet address of referrer

-- 2. Create users table (fresh install)
CREATE TABLE IF NOT EXISTS users (
  id                  BIGSERIAL PRIMARY KEY,
  wallet_address      TEXT UNIQUE NOT NULL,
  username            TEXT DEFAULT '',
  first_name          TEXT DEFAULT 'Gladiator',
  coins               INTEGER DEFAULT 500,
  season_coins        INTEGER DEFAULT 0,
  sprint_coins        INTEGER DEFAULT 0,
  tap_power           INTEGER DEFAULT 1,
  energy              INTEGER DEFAULT 100,
  last_energy_update  TIMESTAMPTZ DEFAULT NOW(),
  streak              INTEGER DEFAULT 0,
  last_claim          TIMESTAMPTZ,
  referred_by         TEXT,         -- wallet_address of referrer
  referral_count      INTEGER DEFAULT 0,
  created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Payments table (wallet-based)
CREATE TABLE IF NOT EXISTS payments (
  id              BIGSERIAL PRIMARY KEY,
  wallet_address  TEXT NOT NULL,
  tx_hash         TEXT UNIQUE NOT NULL,
  amount_avax     NUMERIC(18, 8) NOT NULL,
  item            TEXT NOT NULL,
  verified        BOOLEAN DEFAULT FALSE,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 4. Prize pool
CREATE TABLE IF NOT EXISTS prize_pool (
  id           INTEGER PRIMARY KEY DEFAULT 1,
  total_avax   NUMERIC(18, 8) DEFAULT 0,
  total_arena  BIGINT DEFAULT 0,
  updated_at   TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO prize_pool (id, total_avax, total_arena) VALUES (1, 0, 0)
ON CONFLICT (id) DO NOTHING;

-- 5. Seasons
CREATE TABLE IF NOT EXISTS seasons (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  is_active   BOOLEAN DEFAULT TRUE,
  started_at  TIMESTAMPTZ DEFAULT NOW(),
  ended_at    TIMESTAMPTZ
);
INSERT INTO seasons (name, is_active) VALUES ('Season 1', TRUE)
ON CONFLICT DO NOTHING;

-- 6. Season results (wallet-based)
CREATE TABLE IF NOT EXISTS season_results (
  id              BIGSERIAL PRIMARY KEY,
  season_id       INTEGER,
  wallet_address  TEXT NOT NULL,
  username        TEXT,
  first_name      TEXT,
  final_coins     INTEGER DEFAULT 0,
  rank            INTEGER,
  prize_avax      NUMERIC(18, 8) DEFAULT 0,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 7. Sprints
CREATE TABLE IF NOT EXISTS sprints (
  id                  BIGSERIAL PRIMARY KEY,
  name                TEXT NOT NULL,
  ends_at             TIMESTAMPTZ NOT NULL,
  is_active           BOOLEAN DEFAULT TRUE,
  prize_description   TEXT,
  started_at          TIMESTAMPTZ DEFAULT NOW()
);

-- 8. Row Level Security (RLS)
-- Enable RLS on users table
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;

-- Anon can only READ users (for leaderboard from frontend if needed)
CREATE POLICY "public_read_users" ON users
  FOR SELECT USING (true);

-- Only service_role can INSERT/UPDATE/DELETE users
CREATE POLICY "service_write_users" ON users
  FOR ALL USING (auth.role() = 'service_role');

-- Anon can read payments? No — service_role only
CREATE POLICY "service_all_payments" ON payments
  FOR ALL USING (auth.role() = 'service_role');

-- 9. add_coins RPC (for atomic frontend increments — optional, kept for backward compat)
CREATE OR REPLACE FUNCTION add_coins(user_id BIGINT, amount INTEGER)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  UPDATE users SET coins = coins + amount WHERE id = user_id;
END;
$$;

-- 10. Indexes for performance
CREATE INDEX IF NOT EXISTS idx_users_wallet ON users (wallet_address);
CREATE INDEX IF NOT EXISTS idx_users_coins ON users (coins DESC);
CREATE INDEX IF NOT EXISTS idx_users_season_coins ON users (season_coins DESC);
CREATE INDEX IF NOT EXISTS idx_payments_tx_hash ON payments (tx_hash);
CREATE INDEX IF NOT EXISTS idx_payments_wallet ON payments (wallet_address);
