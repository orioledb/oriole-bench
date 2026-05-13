#!/usr/bin/env python3
"""
Run pgbench tests. Replaces test-pgbench.sh.

The PGDATA directory is named after the test+scale, so multiple data dirs can
coexist under --pgdata-base and `--reuse-data` reuses a populated one.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

import common
from common import (
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


# Pgbench scale is fixed to 1000 (matches the original bash).
pgbench_scale = 1000

precise_conns = [
    5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 18, 20, 22, 24, 27, 30, 33, 36, 39,
    43, 47, 51, 56, 62, 68, 75, 82, 91, 100, 110, 120, 130, 150, 160, 180,
    200, 220, 240, 270, 300, 330, 360, 390, 430, 470,
]
coarse_conns = [10, 15, 22, 33, 47, 68, 100, 150, 220, 330, 470]

default_subtests = [
    "select", "select_any9", "select_any30", "select_any50",
    "tpcb", "tpcb_procedure",
]

subtest_headers = {
    "select":           "# Random select test",
    "select_any9":      "# Select any random 9 test",
    "select_any30":     "# Select any random 30 test",
    "select_any50":     "# Select any random 50 test",
    "tpcb":             "# tpc-b test",
    "tpcb_procedure":   "# tpc-b in procedure test",
}

subtest_files = {
    "select_any9":   "orioledb-select-9.sql",
    "select_any30":  "orioledb-select-30.sql",
    "select_any50":  "orioledb-select-50.sql",
    "tpcb_procedure": "orioledb-tpcb-in-procedure.sql",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run pgbench tests for one (engine, patch_id) point.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_test_args(p)
    p.add_argument("--precise", action="store_true",
                   help="Use the dense connection list (overridden by --conns).")
    p.add_argument("--conns", nargs="+", type=positive_int, metavar="N",
                   help="Explicit list of connection counts (overrides --precise).")
    p.add_argument("--subtests", nargs="+", choices=default_subtests, metavar="TEST",
                   default=default_subtests,
                   help="Subtests to run (default: all).")
    return p


def preflight(args: argparse.Namespace) -> None:
    pf = Preflight()
    pf.assert_engine(args.engine)
    for b in ("pg_ctl", "initdb", "psql", "pgbench"):
        pf.require_binary(b)
    for f in common.conf_files_required_for_tests["pgbench"]:
        pf.require_file(script_dir / f)
    for t in args.subtests:
        sql = subtest_files.get(t)
        if sql:
            pf.require_file(script_dir / sql)
    pf.finish()
    assert_pg_build_in_path()


def prepare_cluster(pgdatadir: Path, engine: str, memory_buffers: str,
                    undo_buffers: str, fsync: str, synchronous_commit: str,
                    pg_stat_statements: bool,
                    reuse_data: bool) -> bool:
    """
    Initialize (or reuse) and start the PG cluster. Returns True if data needs
    to be (re)loaded with `pgbench -i`.
    """
    stop_pg_silent(pgdatadir)
    cfg_args = dict(memory_buffers=memory_buffers, undo_buffers=undo_buffers,
                    fsync=fsync, synchronous_commit=synchronous_commit,
                    pg_stat_statements=pg_stat_statements)
    if reuse_data and is_pgdata_initialized(pgdatadir):
        with stage(f"reuse pgdata {pgdatadir.name}"):
            ensure_dir(pgdatadir)
            write_engine_config(pgdatadir, engine, "pgbench", **cfg_args)
            pg_start(pgdatadir)
            pg_restart(pgdatadir)
            if pg_stat_statements:
                enable_pg_stat_statements()
        return False

    with stage(f"init pgdata {pgdatadir.name}"):
        remove_dir(pgdatadir)
        ensure_dir(pgdatadir.parent)
        pg_initdb(pgdatadir)
        pg_start(pgdatadir)
        write_engine_config(pgdatadir, engine, "pgbench", **cfg_args)
        if engine == "orioledb":
            pg_psql("create extension orioledb;")
        pg_restart(pgdatadir)
        if pg_stat_statements:
            enable_pg_stat_statements()
    return True


def run_pgbench_session(*, extra_args: list[str], conns: int, run_time: int,
                        monitor_path: Path | None,
                        pgdatadir: Path) -> int | None:
    cmd = [
        "pgbench", "postgres",
        *extra_args,
        f"-s{pgbench_scale}", "-M", "prepared",
        "-T", str(run_time),
        "-j", str(conns),
        "-c", str(conns),
    ]
    cm = (
        ResourceMonitor(monitor_path, mount_point=pgdatadir,
                        pgdatadir=pgdatadir)
        if monitor_path is not None else contextlib.nullcontext()
    )
    with cm:
        proc = run(cmd, capture=True)
    return parse_pgbench_tps(proc.stdout or "")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    preflight(args)

    memory_buffers = args.memory_buffers or "32GB"
    if args.conns:
        conns_list = list(args.conns)
    elif args.precise:
        conns_list = list(precise_conns)
    else:
        conns_list = list(coarse_conns)

    run_time = 5 if args.fast_run else 30
    fast_msg = "FAST RUN!" if args.fast_run else ""

    pgdatadir = data_dir_for(args.pgdata_base, engine=args.engine,
                             data_id=args.data_id or args.patch_id,
                             test="pgbench", scale=f"s{pgbench_scale}")
    needs_load = prepare_cluster(pgdatadir, args.engine, memory_buffers,
                                 args.undo_buffers, args.fsync,
                                 args.synchronous_commit,
                                 args.pg_stat_statements, args.reuse_data)
    if needs_load:
        with stage(f"load pgbench s={pgbench_scale}"):
            run(["pgbench", "postgres", "-i", f"-s{pgbench_scale}"])
            pg_psql_file(script_dir / "orioledb-prepare-function.sql")

    ensure_dir(args.results_dir)
    result_file = args.results_dir / f"{args.patch_id}-pgbench"
    monitor_dir = args.results_dir / f"{args.patch_id}-pgbench-resources"
    if args.extended_logging:
        ensure_dir(monitor_dir)

    append_line(result_file, f"# {fast_msg} {now_str()}")
    append_line(result_file, "# conns, tps")

    for t in args.subtests:
        with stage(f"subtest {t}"):
            append_line(result_file, subtest_headers[t])

            if t == "select":
                extra: list[str] = ["-S"]
            elif t == "tpcb":
                extra = []
            else:
                extra = ["-f", str(script_dir / subtest_files[t])]

            for c in conns_list:
                append_text(result_file, f"{c},")
                pg_psql("checkpoint;")
                if args.pg_stat_statements:
                    pgss_reset()

                monitor_path = (
                    monitor_dir / f"{t}-c{c}.jsonl"
                    if args.extended_logging else None
                )
                tps = run_pgbench_session(
                    extra_args=extra, conns=c, run_time=run_time,
                    monitor_path=monitor_path, pgdatadir=pgdatadir,
                )
                if tps is None:
                    log.warning("    conns=%d: pgbench produced no tps line", c)
                    append_line(result_file, "ERROR")
                else:
                    log.info("    conns=%d tps=%d", c, tps)
                    append_line(result_file, str(tps))

                if args.pg_stat_statements:
                    pgss_dump_report(
                        args.results_dir /
                        f"{args.patch_id}-pgbench-{t}-c{c}-pgss.txt"
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
