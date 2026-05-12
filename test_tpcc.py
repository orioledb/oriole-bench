#!/usr/bin/env python3
"""
Run TPC-C tests using go-tpc. Replaces test-tpcc.sh.

Per-warehouses data dirs let us reuse pre-loaded state across runs:
    <pgdata-base>/pgdata-<engine>-tpcc-w<W>
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
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
    ensure_dir,
    is_pgdata_initialized,
    log,
    now_str,
    parse_tpcc_tpm,
    pg_initdb,
    pg_psql,
    pg_restart,
    pg_start,
    positive_int,
    remove_dir,
    run,
    script_dir,
    stage,
    stop_pg_silent,
    write_engine_config,
)


go_tpc_binary = script_dir / "go-tpc" / "bin" / "go-tpc"

linear_conns = list(range(330, 0, -10)) + [1]
coarse_conns = [330, 220, 150, 100, 68, 47, 33, 22, 15, 10, 7, 5, 3, 2, 1]
default_warehouses = [470, 220, 100, 47, 22, 10, 5]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run TPC-C tests for one (engine, patch_id) point.",
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
                   help="Path to go-tpc binary.")
    p.add_argument("--stored-procs", action="store_true",
                   help="Use go-tpc's PL/pgSQL stored-procedure mode "
                        "(postgres driver only).")
    return p


def preflight(args: argparse.Namespace) -> None:
    pf = Preflight()
    pf.assert_engine(args.engine)
    for b in ("pg_ctl", "initdb", "psql", "killall"):
        pf.require_binary(b)
    if not Path(args.go_tpc).is_file():
        pf.err(f"go-tpc binary not found at {args.go_tpc}; run tests.py first.")
    for f in common.conf_files_required_for_tests["tpcc"]:
        pf.require_file(script_dir / f)
    if args.reuse_data and args.init_point:
        pf.err("--reuse-data and --init-point are mutually exclusive.")
    pf.finish()
    assert_pg_build_in_path()


def prepare_cluster(pgdatadir: Path, engine: str, memory_buffers: str,
                    undo_buffers: str, fsync: str, synchronous_commit: str,
                    warehouses: int, go_tpc: Path,
                    reuse_data: bool) -> bool:
    """Init/reuse PGDATA and run go-tpc prepare. Returns True if data was loaded."""
    cfg_args = dict(memory_buffers=memory_buffers, undo_buffers=undo_buffers,
                    fsync=fsync, synchronous_commit=synchronous_commit)
    if reuse_data and is_pgdata_initialized(pgdatadir):
        with stage(f"reuse pgdata {pgdatadir.name}"):
            stop_pg_silent(pgdatadir)
            write_engine_config(pgdatadir, engine, "tpcc", **cfg_args)
            pg_start(pgdatadir)
            pg_restart(pgdatadir)
        return False

    with stage(f"init pgdata {pgdatadir.name} w={warehouses}"):
        run(["sudo", "killall", "-9", "postgres"], allow_fail=True)
        stop_pg_silent(pgdatadir)
        remove_dir(pgdatadir)
        ensure_dir(pgdatadir.parent)
        time.sleep(10)
        pg_initdb(pgdatadir)
        pg_start(pgdatadir)

        write_engine_config(pgdatadir, engine, "tpcc", **cfg_args)
        if engine == "orioledb":
            pg_psql("create extension orioledb;")
        pg_restart(pgdatadir)
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
    return True


def run_tpcc_measure(
    *, go_tpc: Path, warehouses: int, conns: int, measure_time: str,
    monitor_path: Path | None, pgdatadir: Path, stored_procs: bool = False,
) -> int | None:
    cmd = [
        str(go_tpc), "tpcc",
        "--warehouses", str(warehouses), "run",
        "-d", "postgres", "-U", "ubuntu", "-p", "5432",
        "-D", "postgres", "-H", "127.0.0.1", "-P", "5432",
        "--conn-params", "sslmode=disable",
        "-T", str(conns),
        "--time", measure_time,
    ]
    if stored_procs:
        cmd.append("--stored-procs")
    cm = (
        ResourceMonitor(monitor_path, mount_point=pgdatadir,
                        pgdatadir=pgdatadir)
        if monitor_path is not None else contextlib.nullcontext()
    )
    with cm:
        proc = run(cmd, capture=True)
    return parse_tpcc_tpm(proc.stdout or "")


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
        measure_time = "5s"
        warehouses_list = [47, 22]
        fast_msg = "FAST RUN!"
    else:
        measure_time = "100s"
        warehouses_list = list(args.warehouses)
        fast_msg = ""

    ensure_dir(args.results_dir)
    result_file = args.results_dir / f"{args.engine}-{args.patch_id}-tpcc"
    monitor_dir = args.results_dir / f"{args.engine}-{args.patch_id}-tpcc-resources"
    if args.extended_logging:
        ensure_dir(monitor_dir)

    for w in warehouses_list:
        pgdatadir = data_dir_for(args.pgdata_base, engine=args.engine,
                                 patch_id=args.patch_id,
                                 test="tpcc", scale=f"w{w}")
        append_line(result_file, f"# {fast_msg} NEW SERIES warehouses = {w} {now_str()}")

        with stage(f"warehouses {w}"):
            if not args.init_point:
                prepare_cluster(pgdatadir, args.engine, memory_buffers,
                                args.undo_buffers, args.fsync,
                                args.synchronous_commit, w, args.go_tpc,
                                args.reuse_data)

            for a in conns_list:
                if args.init_point:
                    prepare_cluster(pgdatadir, args.engine, memory_buffers,
                                    args.undo_buffers, args.fsync,
                                    args.synchronous_commit, w, args.go_tpc,
                                    reuse_data=False)

                append_text(result_file, f"{w},{a},")
                pg_psql("checkpoint;")

                monitor_path = (
                    monitor_dir / f"w{w}-c{a}.jsonl"
                    if args.extended_logging else None
                )
                tpm = run_tpcc_measure(
                    go_tpc=args.go_tpc, warehouses=w, conns=a,
                    measure_time=measure_time,
                    monitor_path=monitor_path, pgdatadir=pgdatadir,
                    stored_procs=args.stored_procs,
                )
                if tpm is None:
                    log.warning("    w=%d conns=%d: no tpmTotal in output", w, a)
                    append_line(result_file, "ERROR")
                else:
                    log.info("    w=%d conns=%d tpm=%d", w, a, tpm)
                    append_line(result_file, str(tpm))

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
