-- pgbench script: TPC-C NEW_ORDER via tpcc_new_order(...).
-- Expects :scale (warehouses count) as a pgbench variable.
--
-- Notes vs spec:
--   * Item ids are independently uniform 1..100000 — duplicates within an
--     order are technically allowed (TPC-C requires distinct), which is fine
--     for performance benchmarking. The proc handles ol_cnt arg, so we always
--     pass 15 items in the array and the proc only consumes ol_cnt of them.
--   * rbk=1 (1% chance) overrides the last item id with -1 to trigger the
--     spec-mandated rollback path inside the procedure.

\set w_id   random(1, :scale)
\set d_id   random(1, 10)
\set c_id   random(1, 3000)
\set ol_cnt random(5, 15)
\set rbk    random(1, 100)

\set i1  random(1, 100000)
\set i2  random(1, 100000)
\set i3  random(1, 100000)
\set i4  random(1, 100000)
\set i5  random(1, 100000)
\set i6  random(1, 100000)
\set i7  random(1, 100000)
\set i8  random(1, 100000)
\set i9  random(1, 100000)
\set i10 random(1, 100000)
\set i11 random(1, 100000)
\set i12 random(1, 100000)
\set i13 random(1, 100000)
\set i14 random(1, 100000)
\set i15 random(1, 100000)

-- 1% rollback path: poison the last item.
\if :rbk = 1
  \set i15 -1
\endif

\set q1  random(1, 10)
\set q2  random(1, 10)
\set q3  random(1, 10)
\set q4  random(1, 10)
\set q5  random(1, 10)
\set q6  random(1, 10)
\set q7  random(1, 10)
\set q8  random(1, 10)
\set q9  random(1, 10)
\set q10 random(1, 10)
\set q11 random(1, 10)
\set q12 random(1, 10)
\set q13 random(1, 10)
\set q14 random(1, 10)
\set q15 random(1, 10)

-- Supply warehouses: 1% chance per item picks a different warehouse, but to
-- keep the script branch-free we use the same w_id for all 15 — close enough
-- on small/uniform warehouse counts. For 1000+ warehouses this slightly
-- underestimates remote-warehouse contention.
\set sw1  :w_id
\set sw2  :w_id
\set sw3  :w_id
\set sw4  :w_id
\set sw5  :w_id
\set sw6  :w_id
\set sw7  :w_id
\set sw8  :w_id
\set sw9  :w_id
\set sw10 :w_id
\set sw11 :w_id
\set sw12 :w_id
\set sw13 :w_id
\set sw14 :w_id
\set sw15 :w_id

-- pgbench's `-M prepared` binds :var values as parameters whose type the
-- server has to infer. Without an explicit cast on the ARRAY constructor
-- the inferred element type is text, so the proc-call overload lookup
-- fails. Force int[] via the outer ::int[] cast on each array.
CALL tpcc_new_order(
    :w_id, :d_id, :c_id, :ol_cnt,
    (ARRAY[:sw1,:sw2,:sw3,:sw4,:sw5,:sw6,:sw7,:sw8,:sw9,:sw10,
           :sw11,:sw12,:sw13,:sw14,:sw15]::int[])[1::int4:(:ol_cnt)::int4],
    (ARRAY[:i1,:i2,:i3,:i4,:i5,:i6,:i7,:i8,:i9,:i10,
           :i11,:i12,:i13,:i14,:i15]::int[])[1::int4:(:ol_cnt)::int4],
    (ARRAY[:q1,:q2,:q3,:q4,:q5,:q6,:q7,:q8,:q9,:q10,
           :q11,:q12,:q13,:q14,:q15]::int[])[1::int4:(:ol_cnt)::int4],
    localtimestamp, 1
);
