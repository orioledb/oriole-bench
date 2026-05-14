-- pgbench script: TPC-C ORDER_STATUS via tpcc_order_status(...).
-- 60% by c_last, 40% by c_id.

\set w_id    random(1, :scale)
\set d_id    random(1, 10)
\set by_name random(1, 100)

\if :by_name <= 60
  \set n random(0, 999)
  CALL tpcc_order_status(:w_id, :d_id, NULL, tpcc_c_last(:n));
\else
  \set c_id random(1, 3000)
  CALL tpcc_order_status(:w_id, :d_id, :c_id, NULL);
\endif
