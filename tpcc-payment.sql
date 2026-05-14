-- pgbench script: TPC-C PAYMENT via tpcc_payment(...).
-- 60% by-c_last, 40% by-c_id. 85% local, 15% remote warehouse.

\set w_id     random(1, :scale)
\set d_id     random(1, 10)
\set h_amount random(100, 500000)
-- h_amount is stored as NUMERIC; the proc accepts NUMERIC, so we pass the
-- raw integer (it'll be coerced). Spec says NUMERIC(6,2) value 1.00..5000.00,
-- we send "cents", proc-side it lands as the same effective magnitude — for
-- benchmark fidelity this is OK.

\set by_name  random(1, 100)
\set local    random(1, 100)

-- Customer warehouse / district.
\if :local <= 85
  \set c_w_id :w_id
  \set c_d_id :d_id
\else
  \set c_w_id random(1, :scale)
  \set c_d_id random(1, 10)
\endif

\if :by_name <= 60
  -- by c_last: pick a random NURand-style name index.
  \set n random(0, 999)
  CALL tpcc_payment(
      :w_id, :d_id, :c_w_id, :c_d_id,
      (:h_amount::numeric) / 100,
      NULL, tpcc_c_last(:n), localtimestamp
  );
\else
  -- by c_id
  \set c_id random(1, 3000)
  CALL tpcc_payment(
      :w_id, :d_id, :c_w_id, :c_d_id,
      (:h_amount::numeric) / 100,
      :c_id, NULL, localtimestamp
  );
\endif
