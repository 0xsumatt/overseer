-- Positions watchlists now live in browser tab-session storage. Stop/redeploy
-- the old scheduler and web workers before applying this irreversible cutover.
-- Removes saved addresses/labels, encrypted tokens, snapshots and funding rows;
-- login accounts and unrelated market data are deliberately left intact.
-- Historical backups, WAL archives and logs are outside this migration's scope.
BEGIN;
DROP TABLE IF EXISTS position_funding_events;
DROP TABLE IF EXISTS position_accounts;
DROP TABLE IF EXISTS position_fx;
COMMIT;
