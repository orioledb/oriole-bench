#!/usr/bin/env python3
"""
Run TPC-C tests driven by pgbench (instead of go-tpc) against PL/pgSQL
stored procedures. Sister mode to test_tpcc.py — same schema and procedures,
different client driver.

Data is loaded once via `go-tpc tpcc prepare` (same as test_tpcc.py), then
tpcc-procs.sql installs the 5 stored procedures + tpcc_c_last helper, and
pgbench runs the 5 transaction scripts (-f tpcc-{neword,payment,order-status,
delivery,stock-level}.sql) weighted to match the TPC-C transaction mix.

Per-warehouses data dirs let us reuse pre-loaded state across runs:
    <pgdata-base>/pgdata-<engine>-tpcc_pgb-w<W>
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from pathlib import Path

import common
from common import (
    BackendSampler,
    BenchError,
    Preflight,
    ResourceMonitor,
    add_common_test_args,
    append_line,
    append_text,
    assert_pg_build_in_path,
    data_dir_for,
    enable_pg_stat_statements,
    ensure_dir,
    is_pgdata_initialized,
    log,
    now_str,
    parse_pgbench_tps,
    pg_initdb,
    pg_psql,
    pg_psql_file,
    pg_restart,
    pg_start,
    pgss_dump_report,
    pgss_reset,
    positive_int,
    remove_dir,
    run,
    script_dir,
    stage,
    stop_pg_silent,
    write_engine_config,
)


go_tpc_binary = script_dir / "go-tpc" / "bin" / "go-tpc"

# TPC-C transaction mix (weights, like the spec):
#   NEW_ORDER    45%
#   PAYMENT      43%
#   ORDER_STATUS  4%
#   DELIVERY      4%
#   STOCK_LEVEL   4%
# pgbench picks one script per transaction with probability weight/sum(weights).
pgb_scripts = [
    ("tpcc-neword.sql",       45),
    ("tpcc-payment.sql",      43),
    ("tpcc-order-status.sql",  4),
    ("tpcc-delivery.sql",      4),
    ("tpcc-stock-level.sql",   4),
]

linear_conns = list(range(330, 0, -10)) + [1]
coarse_conns = [330, 220, 150, 100, 68, 47, 33, 22, 15, 10, 7, 5, 3, 2, 1]
default_warehouses = [470, 220, 100, 47, 22, 10, 5]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run TPC-C via pgbench + PL/pgSQL procedures for one "
                    "(engine, patch_id) point.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_test_args(p)
    p.add_argument("--linear-scale", action="store_true",
                   help="Use the linear connection list (overridden by --conns).")
    p.add_argument("--init-point", action="store_true",
                   help="Re-init cluster before each (warehouses, conns) point.")
    p.add_argument("--warehouses", nargs="+", type=positive_int, metavar="N",
                   default=default_warehouses,
                   help="Warehouses values to run.")
    p.add_argument("--conns", nargs="+", type=positive_int, metavar="N",
                   help="Connection counts (overrides --linear-scale).")
    p.add_argument("--go-tpc", type=Path, default=go_tpc_binary,
                   help="Path to go-tpc binary (used only to load schema/data).")
    p.add_argument("--duration-sec", type=positive_int, default=100,
                   help="Measurement duration in seconds (ignored on --fast-run).")
    return p


def preflight(args: argparse.Namespace) -> None:
    pf = Preflight()
    pf.assert_engine(args.engine)
    for b in ("pg_ctl", "initdb", "psql", "pgbench", "killall"):
        pf.require_binary(b)
    if not Path(args.go_tpc).is_file():
        pf.err(f"go-tpc binary not found at {args.go_tpc}; run tests.py first.")
    for f in common.conf_files_required_for_tests["tpcc_pgb"]:
        pf.require_file(script_dir / f)
    if args.reuse_data and args.init_point:
        pf.err("--reuse-data and --init-point are mutually exclusive.")
    pf.finish()
    assert_pg_build_in_path()


def prepare_cluster(pgdatadir: Path, engine: str, memory_buffers: str,
                    undo_buffers: str, fsync: str, synchronous_commit: str,
                    pg_stat_statements: bool,
                    warehouses: int, go_tpc: Path,
                    reuse_data: bool) -> bool:
    """Init/reuse PGDATA, run go-tpc prepare, install procs. Returns True if
    data was (re)loaded."""
    cfg_args = dict(memory_buffers=memory_buffers, undo_buffers=undo_buffers,
                    fsync=fsync, synchronous_commit=synchronous_commit,
                    pg_stat_statements=pg_stat_statements)
    if reuse_data and is_pgdata_initialized(pgdatadir):
        with stage(f"reuse pgdata {pgdatadir.name}"):
            stop_pg_silent(pgdatadir)
            write_engine_config(pgdatadir, engine, "tpcc_pgb", **cfg_args)
            pg_start(pgdatadir)
            pg_restart(pgdatadir)
            if pg_stat_statements:
                enable_pg_stat_statements()
            # Procedures live in the data dir, but `go-tpc prepare` recreates
            # the schema (drops public.* objects). Re-install procs to be
            # safe — CREATE OR REPLACE makes this idempotent.
            pg_psql_file(script_dir / "tpcc-procs.sql")
        return False

    with stage(f"init pgdata {pgdatadir.name} w={warehouses}"):
        run(["sudo", "killall", "-9", "postgres"], allow_fail=True)
        stop_pg_silent(pgdatadir)
        remove_dir(pgdatadir)
        ensure_dir(pgdatadir.parent)
        time.sleep(10)
        pg_initdb(pgdatadir)
        pg_start(pgdatadir)

        write_engine_config(pgdatadir, engine, "tpcc_pgb", **cfg_args)
        if engine == "orioledb":
            pg_psql("create extension orioledb;")
        pg_restart(pgdatadir)
        if pg_stat_statements:
            enable_pg_stat_statements()
        if engine == "orioledb":
            pg_psql("show shared_buffers; show orioledb.main_buffers; "
                    "show default_table_access_method;")
        else:
            pg_psql("show shared_buffers; show default_table_access_method;")

    with stage(f"go-tpc prepare w={warehouses}"):
        run([
            str(go_tpc), "tpcc",
            "--warehouses", str(warehouses),
            "prepare", "-T", "100",
            "-d", "postgres", "-U", "ubuntu", "-p", "5432",
            "-D", "postgres", "-H", "127.0.0.1", "-P", "5432",
            "--conn-params", "sslmode=disable",
            "--no-check",
        ])

    with stage("install tpcc procs"):
        pg_psql_file(script_dir / "tpcc-procs.sql")

    return True


def run_pgbench_measure(
    *, warehouses: int, conns: int, run_time: int,
    monitor_path: Path | None, pgdatadir: Path,
    flamegraph: str | None = None, fg_out: Path | None = None,
    fg_scripts_dir: Path | None = None, fg_name: str = "sample",
) -> int | None:
    """Run pgbench with the 5 weighted -f scripts. Returns total tps."""
    # TPC-C is intrinsically deadlock-prone (NEW_ORDER + PAYMENT both touch
    # the same warehouse/district/customer rows under concurrency). Without
    # retries pgbench aborts the offending client on first deadlock; with
    # --max-tries pgbench just re-runs the script and the count shows up as
    # a 'retried' transaction. The retried-tx tps is reported as part of the
    # normal total.
    cmd = [
        "pgbench", "postgres",
        "-M", "prepared",
        "--max-tries", "100",
        "-T", str(run_time),
        "-j", str(conns),
        "-c", str(conns),
        "-D", f"scale={warehouses}",
    ]
    for sql, weight in pgb_scripts:
        cmd += ["-f", f"{script_dir / sql}@{weight}"]

    rm = (
        ResourceMonitor(monitor_path, mount_point=pgdatadir,
                        pgdatadir=pgdatadir)
        if monitor_path is not None else contextlib.nullcontext()
    )
    fg = (
        BackendSampler(flamegraph, fg_out, fg_name,
                       duration_sec=run_time,
                       fg_scripts_dir=fg_scripts_dir)
        if flamegraph is not None and fg_out is not None
        else contextlib.nullcontext()
    )
    with rm, fg:
        proc = run(cmd, capture=True)
    return parse_pgbench_tps(proc.stdout or "")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    preflight(args)

    memory_buffers = args.memory_buffers or "20GB"

    if args.conns:
        conns_list = list(args.conns)
    elif args.linear_scale:
        conns_list = list(linear_conns)
    else:
        conns_list = list(coarse_conns)

    if args.fast_run:
        run_time = 5
        warehouses_list = [47, 22]
        fast_msg = "FAST RUN!"
    else:
        run_time = args.duration_sec
        warehouses_list = list(args.warehouses)
        fast_msg = ""

    ensure_dir(args.results_dir)
    result_file = args.results_dir / f"{args.patch_id}-tpcc_pgb"
    monitor_dir = args.results_dir / f"{args.patch_id}-tpcc_pgb-resources"
    if args.extended_logging:
        ensure_dir(monitor_dir)
    fg_dir = (args.results_dir / f"{args.patch_id}-tpcc_pgb-flamegraphs"
              if args.flamegraph else None)
    if fg_dir is not None:
        ensure_dir(fg_dir)

    for w in warehouses_list:
        pgdatadir = data_dir_for(args.pgdata_base, engine=args.engine,
                                 data_id=args.data_id or args.patch_id,
                                 test="tpcc_pgb", scale=f"w{w}")
        append_line(result_file, f"# {fast_msg} NEW SERIES warehouses = {w} {now_str()}")

        with stage(f"warehouses {w}"):
            if not args.init_point:
                prepare_cluster(pgdatadir, args.engine, memory_buffers,
                                args.undo_buffers, args.fsync,
                                args.synchronous_commit,
                                args.pg_stat_statements,
                                w, args.go_tpc, args.reuse_data)

            for a in conns_list:
                if args.init_point:
                    prepare_cluster(pgdatadir, args.engine, memory_buffers,
                                    args.undo_buffers, args.fsync,
                                    args.synchronous_commit,
                                    args.pg_stat_statements,
                                    w, args.go_tpc, reuse_data=False)

                append_text(result_file, f"{w},{a},")
                pg_psql("checkpoint;")
                if args.pg_stat_statements:
                    pgss_reset()

                monitor_path = (
                    monitor_dir / f"w{w}-c{a}.jsonl"
                    if args.extended_logging else None
                )
                tps = run_pgbench_measure(
                    warehouses=w, conns=a, run_time=run_time,
                    monitor_path=monitor_path, pgdatadir=pgdatadir,
                    flamegraph=args.flamegraph,
                    fg_out=fg_dir,
                    fg_scripts_dir=args.flamegraph_fg_dir,
                    fg_name=f"w{w}-c{a}",
                )
                if tps is None:
                    log.warning("    w=%d conns=%d: no tps line in pgbench output",
                                w, a)
                    append_line(result_file, "ERROR")
                else:
                    # Report tpm (matches test_tpcc.py's column semantics):
                    # pgbench's tps counts all 5 transaction types, mirroring
                    # go-tpc's tpmTotal.
                    tpm = tps * 60
                    log.info("    w=%d conns=%d tps=%d tpm=%d", w, a, tps, tpm)
                    append_line(result_file, str(tpm))

                if args.pg_stat_statements:
                    pgss_dump_report(
                        args.results_dir /
                        f"{args.patch_id}-tpcc_pgb-w{w}-c{a}-pgss.txt"
                    )

            stop_pg_silent(pgdatadir)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BenchError as e:
        log.error("Bench error: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        log.error("Interrupted by user")
        sys.exit(130)
