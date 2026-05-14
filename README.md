# oriole-bench: automated benchmarking for Postgres and OrioleDB

Drives end-to-end PG / OrioleDB benchmarks: bootstraps system + Python
dependencies on a fresh Ubuntu VM, clones and builds the requested PG and
OrioleDB refs, sets up the bench tools (`go-tpc`, optionally `HammerDB` and
`mdcallag/iibench`), and runs `pgbench` / TPC-C (go-tpc per-statement) /
TPC-C (pgbench + PL/pgSQL procedures) / TPC-C (HammerDB stored-procedure
mode) / `ibench`. Designed for large AWS instances (c7g/c7gd-class).
Mainly for internal use — use at your own risk.

## What's where

| File | Purpose |
|---|---|
| `tests.py` | Top-level orchestrator: bootstrap → build → prepare env → run tests |
| `test_pgbench.py` | Pgbench driver (select / select_any / tpc-b / tpc-b procedure) |
| `test_tpcc.py` | TPC-C driver via `go-tpc` (per-statement mode; `--stored-procs` flag also available) |
| `test_tpcc_pgb.py` | TPC-C driver via **pgbench + PL/pgSQL procedures** |
| `test_tpcc_hdb.py` | TPC-C driver via HammerDB in **stored-procedure mode** |
| `test_ibench.py`, `run_ibench.py`, `report_ibench.py` | mdcallag ibench multi-phase workflow |
| `common.py` | Shared utilities: `run()` wrapper, stage / log routing, `ResourceMonitor`, preflight, bootstrap |
| `aggregate_resources.py` | Summarises ResourceMonitor JSONL files (means, growth, wait events) |
| `postgresql.auto.conf.*` | Per-test, per-engine PG configs that are concatenated into the live `postgresql.auto.conf` |
| `orioledb-*.sql` | pgbench script files (`select_any9/30/50`, `tpc-b-procedure`, prepare function) |
| `tpcc-procs.sql` | Stored procedures (`tpcc_new_order / payment / order_status / delivery / stock_level` + `tpcc_c_last` helper) installed by `tpcc_pgb` |
| `tpcc-{neword,payment,order-status,delivery,stock-level}.sql` | pgbench scripts that `CALL` those procedures with the spec's 45/43/4/4/4 mix |

## Quick start

```bash
git clone https://github.com/orioledb/oriole-bench.git
cd oriole-bench

# Compare two postgres-master refs and one orioledb build, all suites,
# using the local NVMe disk for pgdata:
./tests.py --oriole-id beta15 --pg-id REL_17_8 master \
           --tests pgbench tpcc tpcc_pgb ibench \
           --nvme --extended-logging
```

At least one of `--oriole-id` / `--pg-id` must be given.

Result files land in `./results/<engine>-<patch_id>-<test>[-<suffix>]`. Each
file starts with a timestamp header and is appended (not overwritten) on
re-runs. Per-second resource samples — when `--extended-logging` is on —
live in `./results/<engine>-<patch_id>-<test>-resources/<...>.jsonl`. Use
`./aggregate_resources.py 'results/*-tpcc-resources/*.jsonl' --format csv`
to roll them up.

Subprocess noise (apt, pip, configure, make, pgbench, go-tpc, HammerDB,
ibench workers) goes into `./log/<stage>.log`. The console only sees
high-level stage transitions (`→ start`, `✓ ok (Xs)`, `✖ failed`). On
failure, the BenchError message includes the path to the relevant log.

## Top-level flags

```
--oriole-id REF [REF ...]   OrioleDB tags / commits / branches to compare
--pg-id REF [REF ...]       Postgres tags / commits / branches to compare
--tests TEST [TEST ...]     pgbench | tpcc | tpcc_pgb | tpcc_hdb | ibench
                            (default: all five)
--compiler CC [CC ...]      C compiler(s) to build PG with: clang (clang-17) or
                            gcc. Each (ref, compiler) pair is a separate build
                            under pgbin/<kind>-<ref>-<compiler>.
--fast-run                  Short benchmark runs (for debug, not real measurements)
--nvme                      Format and mount the local NVMe device on /ssd
                            (compatible with c7gd layout; skipped automatically
                             if /ssd is already a mount point)
--memory-buffers VAL        Override shared_buffers / orioledb.main_buffers
--undo-buffers VAL          orioledb.undo_buffers (orioledb engine only, default 1GB)
--fsync {on,off}            postgresql.conf 'fsync' (default off)
--synchronous-commit {on,off}  postgresql.conf 'synchronous_commit' (default off)
--pg-stat-statements        Enable pg_stat_statements with track=all and dump
                            a top-50 report per measurement point
                            (`results/<patch>-<test>-...-pgss.txt`)
--results-dir PATH          Where result files are written (default ./results)
--pgdata-base PATH          Parent dir for per-test PGDATA directories
                            (default /ssd; per-test dirs encode engine + patch_id
                             + test + scale, so they coexist on the same volume)

Behavior:
--reinitialize              Discard cached repos, go-tpc, HammerDB and pg builds
                            and rebuild from scratch. Default reuses everything.
--reuse-data                Reuse the per-test PGDATA dirs that already have a
                            valid cluster — skip initdb and the data-load step.
--skip-bootstrap            Don't run apt-get / pip install (use when the VM is
                            already provisioned).
--extended-logging          Sample CPU / disk IO / wait events once per second to
                            a JSONL file alongside each measurement.
```

Per-suite groups (see `./tests.py --help` for the full list):

* **pgbench**: `--precise-pgbench`, `--pgbench-conns`, `--pgbench-tests`,
  `--pgbench-scale` (default 1000)
* **tpcc** (go-tpc): `--linear-scale`, `--init-point`, `--warehouses`,
  `--tpcc-conns`, `--tpcc-stored-procs` (dispatches via PL/pgSQL inside go-tpc)
* **tpcc_pgb** (pgbench + procs): shares `--linear-scale`, `--init-point`,
  `--warehouses`, `--tpcc-conns` with the go-tpc suite
* **tpcc_hdb** (HammerDB): `--hdb-rampup-min`, `--hdb-duration-min`,
  `--hdb-build-vu`. Shares `--warehouses` and `--tpcc-conns` (= virtual
  users) with the go-tpc suite.
* **ibench**: `--ibench-scale-mul`, `--ibench-path`, `--ibench-conns`

## Per-suite notes

### pgbench

Schema is loaded once with `pgbench -i -s1000`. Subtests:
`select` (built-in `-S`), `select_any9 / select_any30 / select_any50` (random
multi-row lookups using `orioledb-select-{9,30,50}.sql`), `tpcb` (built-in
write workload), `tpcb_procedure` (TPC-B logic wrapped in a PL/pgSQL
procedure via `orioledb-tpcb-in-procedure.sql`). Default per-subtest run
time: 30s. `--fast-run` cuts that to 5s.

### TPC-C via go-tpc

Statement-mode TPC-C. Sweep over `--warehouses × --tpcc-conns`. Per
warehouses value, `go-tpc tpcc prepare` is run once, then go-tpc gets a
fresh `run` for each connection count (or per-point if `--init-point`).
Default measurement: 100s; `--fast-run`: 5s. `--tpcc-stored-procs`
switches go-tpc to dispatch each transaction as a single `CALL` to the
same five PL/pgSQL procedures used by `tpcc_pgb` (postgres driver only).

### TPC-C via pgbench (tpcc_pgb)

Same workload, different client driver: `go-tpc tpcc prepare` loads the
schema and data, then `tpcc-procs.sql` installs the five stored
procedures (`tpcc_new_order / payment / order_status / delivery /
stock_level` + the `tpcc_c_last(n)` NURand helper), and pgbench runs the
five `tpcc-*.sql` scripts with the 45/43/4/4/4 mix via `-f script@weight`.

* Result rows write **tpmTotal** (`tps × 60` across all five transaction
  types), matching the semantics of `test_tpcc.py`. tpmC (NEW_ORDER
  only) is approximately `0.45 × tpm`.
* Per-warehouses PGDATA: `pgdata-<engine>-<patch_id>-tpcc_pgb-w<W>`.
* `-M prepared` is used and `--max-tries=100` is passed to pgbench so
  the inevitable NEW_ORDER ↔ PAYMENT deadlocks retry instead of aborting
  the client.
* Useful as a third reference point against `tpcc` (go-tpc) and
  `tpcc_hdb` (HammerDB) — same engine-side code path as
  `tpcc --stored-procs`, but pgbench replaces Go runtime + lib/pq.

### TPC-C via HammerDB (stored-procedure mode)

Same sweep, but HammerDB BUILD creates five PL/pgSQL procedures
(`neword / payment / delivery / slev / ostat`) and virtual users call
them server-side via `SELECT neword(...)` etc. Substantially fewer
round-trips than go-tpc, surfacing pure engine throughput rather than
driver/protocol overhead.

* **amd64 only** for now (HammerDB ships an x86_64 binary; arm64 source
  build via Bawt is non-trivial because their bundled tclkit is x86_64).
  On arm64 the suite exits with a clear error pointing this out.
* On `engine=orioledb`, we `CREATE EXTENSION orioledb` in `template1`
  before HammerDB BUILDs, so the fresh `tpcc` database it creates
  inherits the extension; `default_table_access_method=orioledb` then
  takes care of every CREATE TABLE.
* Default rampup = 2 min, duration = 5 min per `(warehouses, vu)` point.
* HammerDB's BUILD with INSERT-per-row is much slower than go-tpc's bulk
  COPY — for 1000 warehouses on a c7gd-class host expect 1-2 hours per
  engine just for BUILD. Use `--reuse-data` on subsequent runs.

### ibench

Multi-phase write-heavy workload (`l.i0 → l.ix → l.i1 → l.i2 → qr100.L1 →
qr100.L2 → qr500.L3 → qr500.L4 → qr1000.L5 → qr1000.L6`). Each phase runs
`--ibench-conns` (default 20) parallel `iibench.py` workers and the result
row records `(test_name, du sizes for pgdata/pg_wal/orioledb_data/orioledb_undo,
elapsed, checkpoint time)`. Scale multiplier is 100 by default (a lot of
data — needs around 200 GB pgdata and several hours per engine), or 1 if
`--fast-run` is on. Requires
`mdcallag-tools/bench/ibench/iibench.py` (path tunable via `--ibench-path`).

## Per-test PGDATA naming and reuse

Each test allocates its own data directory under `--pgdata-base`, named:

```
pgdata-<engine>-<sanitized-patch_id>-<test>-<scale>
```

— e.g. `pgdata-orioledb-beta15-tpcc_hdb-w1000`. Includes the patch_id so
two builds never silently share an on-disk cluster (their file formats
may differ). `--reuse-data` checks for `PG_VERSION` in that directory and
skips initdb + data load when present.

## Build artifact reuse

By default `tests.py` only does what isn't already cached:

* Git checkouts (`orioledb/`, `postgres-oriole/`, `postgres-master/`,
  `go-tpc/`, `hammerdb-src/`) are reused — `git fetch` then `git checkout`
  the requested ref.
* PG builds in `pgbin/<ref>/` are reused if `pgbin/<ref>/bin/pg_ctl`
  exists.
* The `go-tpc` binary and the HammerDB tarball are reused if already
  installed.

Pass `--reinitialize` to blow these away and rebuild from scratch.

## Examples

All tests on a fresh c7gd VM, comparing one orioledb tag and two PG refs:
```bash
./tests.py --oriole-id beta15 --pg-id REL_17_8 master \
           --nvme --extended-logging
```

Pure-PG comparison (no `--oriole-id`):
```bash
./tests.py --pg-id REL_17_8 master --tests pgbench tpcc \
           --memory-buffers 100GB --nvme
```

TPC-C via HammerDB only, single warehouse value, custom VU sweep,
short measurements:
```bash
./tests.py --oriole-id beta15 --pg-id REL_17_8 --tests tpcc_hdb \
           --warehouses 1000 --tpcc-conns 330 100 33 10 \
           --hdb-rampup-min 1 --hdb-duration-min 3 \
           --extended-logging --nvme --reuse-data
```

Pgbench sweep on connections 10/50/100, only the most read/write-mixed
subtests, 100GB buffers:
```bash
./tests.py --oriole-id main --pg-id master --tests pgbench \
           --memory-buffers 100GB \
           --pgbench-tests select_any9 tpcb_procedure \
           --pgbench-conns 10 50 100
```

Three TPC-C reference points (go-tpc per-statement, pgbench + procs,
HammerDB stored-procs) side by side, with pg_stat_statements top-50s:
```bash
./tests.py --oriole-id main --tests tpcc tpcc_pgb tpcc_hdb \
           --warehouses 100 --tpcc-conns 100 50 \
           --pg-stat-statements --extended-logging
```

## Resource monitoring

`--extended-logging` enables per-second sampling via `psutil` and
`psycopg2`, modeled on `orioledb/ci/pgbench.py:run_pgbench`. Each sample
is a JSON line with:

```
time, disk_used, system, user, idle (CPU %),
read_count, write_count, read_bytes, write_bytes (disk IO delta/s),
waits  (jsonb_object_agg of wait_event counts from pg_stat_activity),
lsn    (pg_current_wal_lsn as 'X/Y' hex)
```

If psycopg2 isn't installed or PG can't be reached transiently (e.g. the
monitor opens during PG restart), the sampler keeps writing CPU/IO/disk
fields with `waits = lsn = null`; it retries the PG connection every 5s.

Aggregate with `aggregate_resources.py`:
```bash
./aggregate_resources.py 'results/*-tpcc-resources/*.jsonl' \
                         --format csv --out tpcc-summary.csv
```
Means for everything, last-value + average growth bytes/sec for
`disk_used` and `lsn`.

## Disk requirements

| Suite | Default scale | Pgdata size |
|---|---|---|
| pgbench | s=1000 (tunable via `--pgbench-scale`) | ~16 GB |
| tpcc (go-tpc) | 470 / 220 / 100 / 47 / 22 / 10 / 5 warehouses, separate dir each | up to ~80 GB for the biggest |
| tpcc_pgb (pgbench + procs) | same as tpcc | same as tpcc |
| tpcc_hdb | similar | similar |
| ibench | scale_mul = 100 | ~200 GB |

Multiple PGDATA dirs coexist when you sweep over warehouses or run
multiple `(engine, patch_id)` combinations — plan storage accordingly,
or sequence runs with `--reinitialize` to recycle.

## Limitations and caveats

* HammerDB suite (`tpcc_hdb`) is **amd64 only**.
* Defaults assume heavy hosts (lots of cores and RAM). On smaller
  machines tune `--memory-buffers`, `--warehouses`, `--tpcc-conns`,
  `--ibench-scale-mul`, the run-times, etc.
* `--nvme` runs `parted` / `mkfs.ext4` on the local NVMe device — only
  use on instances where you actually want a fresh filesystem there.
* `ibench` runs many parallel workers. If you kill `tests.py` mid-run,
  check for and kill any leftover `python3 iibench.py` processes.
* `--reuse-data` trusts that the existing PGDATA has the schema the test
  expects. After a failed data-load run, drop `--reuse-data` once so
  `tests.py` rebuilds.

## Acknowledgements

Mark Callaghan <https://github.com/mdcallag> for the ibench tests repo.
