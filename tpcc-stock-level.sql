-- pgbench script: TPC-C STOCK_LEVEL via tpcc_stock_level(...).

\set w_id      random(1, :scale)
\set d_id      random(1, 10)
\set threshold random(10, 20)

CALL tpcc_stock_level(:w_id, :d_id, :threshold);
