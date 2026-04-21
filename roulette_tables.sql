-- ============================================================
-- ARENA TAP — ROULETTE TABLES
-- Execute this in Supabase SQL Editor
-- ============================================================

-- 1) Qualification table: tracks 30-day personal timer per wallet
CREATE TABLE IF NOT EXISTS roulette_qualification (
    id            BIGSERIAL PRIMARY KEY,
    wallet_address TEXT NOT NULL UNIQUE,
    qualify_start  TIMESTAMPTZ,          -- when the 30-day timer started (null = not qualifying)
    last_spun_at   TIMESTAMPTZ,          -- when the user last spun
    last_check     TIMESTAMPTZ,          -- last time we checked the balance
    last_redfo_balance NUMERIC(78,0) DEFAULT 0,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- 2) Spins table: records every spin with prize details
CREATE TABLE IF NOT EXISTS roulette_spins (
    id            BIGSERIAL PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    username      TEXT DEFAULT '',
    first_name    TEXT DEFAULT '',
    prize_id      TEXT NOT NULL,
    prize_name    TEXT NOT NULL,
    prize_type    TEXT NOT NULL CHECK (prize_type IN ('epic', 'consolation')),
    sent_status   TEXT NOT NULL DEFAULT 'pending' CHECK (sent_status IN ('pending', 'sent')),
    won_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_rqual_wallet  ON roulette_qualification(wallet_address);
CREATE INDEX IF NOT EXISTS idx_rspins_wallet ON roulette_spins(wallet_address);
CREATE INDEX IF NOT EXISTS idx_rspins_status ON roulette_spins(sent_status);
CREATE INDEX IF NOT EXISTS idx_rspins_type   ON roulette_spins(prize_type);
CREATE INDEX IF NOT EXISTS idx_rspins_won    ON roulette_spins(won_at DESC);
