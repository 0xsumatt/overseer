-- ---------------------------------------------------------------------------
-- Bullet funding as charged per settlement.
--
-- funding_rates.rate and liquidity.current_funding_rate hold what one
-- settlement charges, so APR = rate * 8760 / interval_hours. Bullet quotes
-- funding on an 8-hour basis but settles hourly at one-eighth of it
-- (https://docs.bullet.xyz/exchange/trading/funding). Its settled history was
-- therefore stored eight times too high from the start, and its current rate
-- has been since Bullet moved estimatedFundingRate from the hourly to the
-- 8-hour basis on 2026-10-27. The collector now converts; this rescales the
-- rows it stored before.
--
-- Apply once with the scheduler stopped, then start the fixed scheduler: rows
-- the old one writes afterwards would stay eight times too high. Applying it
-- again changes nothing. Bullet's interest-only rate, where crypto markets
-- often sit, is exactly 0.0001 on the 8-hour basis and 0.0000125 per hourly
-- settlement, so it shows which basis rows are on.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    rescaled BIGINT;
    switched TIMESTAMPTZ;
    hourly_until TIMESTAMPTZ;
BEGIN
    IF EXISTS (SELECT 1 FROM funding_rates
               WHERE exchange = 'bullet' AND rate = 0.0001) THEN
        UPDATE funding_rates SET rate = rate * interval_hours / 8
        WHERE exchange = 'bullet';
        GET DIAGNOSTICS rescaled = ROW_COUNT;
        RAISE NOTICE 'Bullet settled funding: % rows rescaled', rescaled;
    END IF;

    -- Current rates changed basis at the first snapshot quoting the 8-hour
    -- interest rate; starting a day early allows for the changelog's time zone.
    SELECT min(ts) INTO switched FROM liquidity
    WHERE exchange = 'bullet' AND ts >= '2026-10-26 00:00+00'
      AND current_funding_rate = 0.0001;
    IF switched IS NOT NULL THEN
        SELECT max(ts) INTO hourly_until FROM liquidity
        WHERE exchange = 'bullet' AND ts < switched
          AND current_funding_rate = 0.0000125;
        UPDATE liquidity
        SET current_funding_rate = current_funding_rate * funding_interval_hours / 8
        WHERE exchange = 'bullet' AND ts >= switched;
        GET DIAGNOSTICS rescaled = ROW_COUNT;
        RAISE NOTICE 'Bullet current funding: % snapshots from % rescaled (last hourly-basis snapshot %)',
            rescaled, switched, hourly_until;
    END IF;
END $$;
