#!/usr/bin/env python3
"""
Run ibench tests. Replaces test-ibench.sh.

Data directory is named per (engine, scale_mul) so concurrent scales coexist:
    <pgdata-base>/pgdata-<engine>-ibench-scale<N>

`--reuse-data` lets you re-run the same scale without rebuilding the cluster
(but the bench still runs every phase against the existing data).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import common
import run_ibench
from common import (
    BenchError,
    Preflight,
    add_common_test_args,
    append_line,
    assert_pg_build_in_path,
    data_dir_for,
    enable_pg_stat_statements,
    ensure_dir,
    is_pgdata_initialized,
    log,
    now_str,
    pg_initdb,
    pg_psql,
    pg_restart,
    pg_start,
    pgss_dump_report,
    pgss_reset,
    positive_int,
    remove_dir,
    script_dir,
    stage,
    stop_pg_silent,
    write_engine_config,
)


default_ibench_path = script_dir / "mdcallag-tools" / "bench" / "ibench" / "iibench.py"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run ibench tests for one (engine, patch_id) point.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_test_args(p)
    p.add_argument("--scale-mul", type=positive_int, default=None,
                   help="Scale multiplier (default 100, or 1 if --fast-run).")
    p.add_argument("--ibench-path", type=Path, default=default_ibench_path,
                   help="Path to mdcallag iibench.py.")
    p.add_argument("--conns", type=positive_int, default=20,
                   help="Number of parallel ibench workers per phase.")
    return p


def preflight(args: argparse.Namespace) -> None:
    pf = Preflight()
    pf.assert_engine(args.engine)
    for b in ("pg_ctl", "initdb", "psql", "python3", "du"):
        pf.require_binary(b)
    for f in common.conf_files_required_for_tests["ibench"]:
        pf.require_file(script_dir / f)
    if not Path(args.ibench_path).is_file():
        pf.err(
            f"iibench.py not found at {args.ibench_path}. "
            "Pass --ibench-path or clone mdcallag-tools."
        )
    pf.finish()
    assert_pg_build_in_path()


def prepare_cluster(pgdatadir: Path, engine: str, memory_buffers: str,
                    undo_buffers: str, fsync: str, synchronous_commit: str,
                    pg_stat_statements: bool,
                    reuse_data: bool) -> None:
    stop_pg_silent(pgdatadir)
    cfg_args = dict(memory_buffers=memory_buffers, undo_buffers=undo_buffers,
                    fsync=fsync, synchronous_commit=synchronous_commit,
                    pg_stat_statements=pg_stat_statements)
    if reuse_data and is_pgdata_initialized(pgdatadir):
        with stage(f"reuse pgdata {pgdatadir.name}"):
            write_engine_config(pgdatadir, engine, "ibench", **cfg_args)
            pg_start(pgdatadir)
            pg_restart(pgdatadir)
            if pg_stat_statements:
                enable_pg_stat_statements()
        return

    with stage(f"init pgdata {pgdatadir.name}"):
        remove_dir(pgdatadir)
        ensure_dir(pgdatadir.parent)
        pg_initdb(pgdatadir)
        pg_start(pgdatadir)
        write_engine_config(pgdatadir, engine, "ibench", **cfg_args)
        if engine == "orioledb":
            pg_psql("create extension orioledb;")
        pg_restart(pgdatadir)
        if pg_stat_statements:
            enable_pg_stat_statements()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    preflight(args)

    memory_buffers = args.memory_buffers or "70GB"
    if args.scale_mul is not None:
        scale_mul = args.scale_mul
    else:
        scale_mul = 1 if args.fast_run else 100
    fast_msg = "FAST RUN!" if args.fast_run else ""

    pgdatadir = data_dir_for(args.pgdata_base, engine=args.engine,
                             data_id=args.data_id or args.patch_id,
                             test="ibench", scale=f"scale{scale_mul}")
    prepare_cluster(pgdatadir, args.engine, memory_buffers,
                    args.undo_buffers, args.fsync, args.synchronous_commit,
                    args.pg_stat_statements, args.reuse_data)

    ensure_dir(args.results_dir)
    result_file = args.results_dir / f"{args.patch_id}-ibench-scale{scale_mul}"
    monitor_dir = (
        args.results_dir / f"{args.patch_id}-ibench-scale{scale_mul}-resources"
        if args.extended_logging else None
    )
    if monitor_dir is not None:
        ensure_dir(monitor_dir)

    append_line(result_file, f"# {fast_msg} {now_str()}")
    append_line(
        result_file,
        "# pgdata apparent, pgdata, pg_wal apparent, pg_wal, "
        "orioledb_data apparent, orioledb_data, orioledb_undo apparent, "
        "orioledb_undo, time, checkpoint time",
    )

    if args.pg_stat_statements:
        pgss_reset()

    run_ibench.run(
        engine=args.engine,
        patch_id=args.patch_id,
        pgdatadir=pgdatadir,
        ibench_path=Path(args.ibench_path),
        scale_mul=scale_mul,
        conns=args.conns,
        result_file=result_file,
        monitor_dir=monitor_dir,
    )

    if args.pg_stat_statements:
        pgss_dump_report(
            args.results_dir / f"{args.patch_id}-ibench-scale{scale_mul}-pgss.txt"
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
