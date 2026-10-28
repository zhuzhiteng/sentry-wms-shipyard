-- 089: remove the obsolete distribution-demand, dispatch and delivery-note
-- slice. The first shipyard release uses the formal outbound manifest and
-- whole-ticket turnover flow instead.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

DROP TABLE IF EXISTS shipyard_delivery_lines CASCADE;
DROP TABLE IF EXISTS shipyard_delivery_notes CASCADE;
DROP TABLE IF EXISTS shipyard_dispatch_lines CASCADE;
DROP TABLE IF EXISTS shipyard_dispatch_orders CASCADE;
DROP TABLE IF EXISTS shipyard_distribution_lines CASCADE;
DROP TABLE IF EXISTS shipyard_distribution_files CASCADE;
DROP TABLE IF EXISTS shipyard_distribution_imports CASCADE;

COMMIT;
