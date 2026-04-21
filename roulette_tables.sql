-- ═══════════════════════════════════════════════════════════════
-- REDFO ROULETTE - Supabase Tables
-- Run this in the Supabase SQL Editor
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS roulette_qualification (
    id              BIGSERIAL PRIMARY KEY,
    wallet_address  TEXT NOT NULL,
    qualified_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    spin_claimed    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS roulette_spins (
    id                  BIGSERIAL PRIMARY KEY,
    wallet_address      TEXT NOT NULL,
    prize_tier          TEXT NOT NULL,
    prize_label         TEXT NOT NULL,
    prize_amount_redfo  BIGINT NOT NULL DEFAULT 0,
    spun_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prize_sent          BOOLEAN NOT NULL DEFAULT FALSE,
    admin_note          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rq_wallet ON roulette_qualification(wallet_address);
CREATE INDEX IF NOT EXISTS idx_rs_wallet ON roulette_spins(wallet_address);
CREATE INDEX IF NOT EXISTS idx_rs_spun_at ON roulette_spins(spun_at DESC);
CREATE INDEX IF NOT EXISTS idx_rs_prize_sent ON roulette_spins(prize_sent);

ALTER TABLE roulette_qualification ENABLE ROW LEVEL SECURITY;
ALTER TABLE roulette_spins ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access qualification"
    ON roulette_qualification FOR ALL
    USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access spins"
    ON roulette_spins FOR ALL
    USING (auth.role() = 'service_role');

ALTER TABLE roulette_qualification
    ADD CONSTRAINT uq_rq_wallet UNIQUE (wallet_address);
