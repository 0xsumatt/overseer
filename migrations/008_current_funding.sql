-- Venue-published current/indicative funding belongs with the point-in-time
-- market context that produced it. Settled payments remain in funding_rates.
ALTER TABLE liquidity
    ADD COLUMN IF NOT EXISTS index_price NUMERIC,
    ADD COLUMN IF NOT EXISTS current_funding_rate NUMERIC,
    ADD COLUMN IF NOT EXISTS funding_interval_hours INT,
    ADD COLUMN IF NOT EXISTS next_funding_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS funding_premium NUMERIC;

COMMENT ON COLUMN liquidity.index_price IS
    'Venue index/oracle price used as the funding reference, when published';
COMMENT ON COLUMN liquidity.current_funding_rate IS
    'Venue-published current/indicative rate for the next funding settlement';
COMMENT ON COLUMN liquidity.funding_interval_hours IS
    'Settlement interval applicable to current_funding_rate';
COMMENT ON COLUMN liquidity.next_funding_at IS
    'Venue-published or deterministic next settlement time';
COMMENT ON COLUMN liquidity.funding_premium IS
    'Venue-published premium input to funding, when exposed';
