-- pgbench script: TPC-C DELIVERY via tpcc_delivery(...).

\set w_id       random(1, :scale)
\set carrier_id random(1, 10)

CALL tpcc_delivery(:w_id, :carrier_id, localtimestamp);
