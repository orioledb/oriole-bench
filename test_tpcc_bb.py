#!/usr/bin/env python3
"""
Run TPC-C via BenchBase (cmu-db/benchbase).

BenchBase is per-statement, JDBC-driven, similar in spirit to test_tpcc.py
(go-tpc per-statement) but in Java. It loads data through
`benchbase.jar -b tpcc --create --load`, then for each terminal count we
re-invoke benchbase.jar with `--execute` against the same loaded schema.

The bench connects to a dedicated database named `benchbase` (created in
prepare_cluster); for engine=orioledb the orioledb extension is installed
into template1 so that database inherits it on creation.

Per-warehouses PGDATA naming:
    <pgdata-base>/pgdata-<engine>-<patch_id>-tpcc_bb-w<W>
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import re
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
    enable_pg_stat_statements,
    ensure_dir,
    is_pgdata_initialized,
    log,
    log_dir,
    now_str,
    pg_initdb,
    pg_psql,
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


default_benchbase_dir = script_dir / "benchbase"
bb_db = "benchbase"

default_warehouses = [470, 220, 100, 47, 22, 10, 5]
default_terminals = [330, 220, 150, 100, 68, 47, 33, 22, 15, 10, 7, 5, 3, 2, 1]


# BenchBase prints e.g. "Throughput (requests/second): 1234.56" near the
# tail of stdout for the measured phase.
throughput_re = re.compile(
    r"Throughput\s*\(requests/second\)\s*:\s*([\d.]+)",
    re.IGNORECASE,
)


def parse_bb_throughput(output: str) -> float | None:
    last: float | None = None
    for line in output.splitlines():
        m = throughput_re.search(line)
        if m:
            try:
                last = float(m.group(1))
            except ValueError:
                pass
    return last


_config_template = """\
<?xml version="1.0"?>
<parameters>
    <type>POSTGRES</type>
    <driver>org.postgresql.Driver</driver>
    <url>jdbc:postgresql://127.0.0.1:5432/{db}?sslmode=disable&amp;reWriteBatchedInserts=true</url>
    <username>{user}</username>
    <password>bench</password>
    <isolation>TRANSACTION_SERIALIZABLE</isolation>
    <batchsize>128</batchsize>

    <scalefactor>{warehouses}</scalefactor>
    <terminals>{terminals}</terminals>

    <works>
        <work>
            <warmup>{rampup_sec}</warmup>
            <time>{duration_sec}</time>
            <rate>unlimited</rate>
            <weights>45,43,4,4,4</weights>
        </work>
    </works>

    <transactiontypes>
        <transactiontype><name>NewOrder</name></transactiontype>
        <transactiontype><name>Payment</name></transactiontype>
        <transactiontype><name>OrderStatus</name></transactiontype>
        <transactiontype><name>Delivery</name></transactiontype>
        <transactiontype><name>StockLevel</name></transactiontype>
    </transactiontypes>
</parameters>
"""


def render_config(*, warehouses: int, terminals: int, rampup_sec: int,
                  duration_sec: int, user: str) -> str:
    return _config_template.format(
        db=bb_db, user=user,
        warehouses=warehouses, terminals=terminals,
        rampup_sec=rampup_sec, duration_sec=duration_sec,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run TPC-C via BenchBase for one (engine, patch_id) point.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_test_args(p)
    p.add_argument("--warehouses", nargs="+", type=positive_int, metavar="N",
                   default=default_warehouses,
                   help="Warehouses values (BenchBase scalefactor).")
    p.add_argument("--terminals", nargs="+", type=positive_int, metavar="N",
                   default=default_terminals,
                   help="Terminal counts (BenchBase analog of tpcc conns).")
    p.add_argument("--rampup-min", type=positive_int, default=1,
                   help="Warmup time in minutes (ignored on --fast-run).")
    p.add_argument("--duration-min", type=positive_int, default=3,
                   help="Measured duration in minutes (ignored on --fast-run).")
    p.add_argument("--benchbase", type=Path, default=default_benchbase_dir,
                   help="Path to the extracted benchbase-postgres tree "
                        "(contains benchbase.jar).")
    return p


def preflight(args: argparse.Namespace) -> None:
    pf = Preflight()
    pf.assert_engine(args.engine)
    for b in ("pg_ctl", "initdb", "psql", "killall", "java"):
        pf.require_binary(b)
    jar = Path(args.benchbase) / "benchbase.jar"
    if not jar.is_file():
        pf.err(
            f"benchbase.jar not found at {jar}. "
            f"Run tests.py first (it builds BenchBase) or pass --benchbase."
        )
    for f in common.conf_files_required_for_tests["tpcc_bb"]:
        pf.require_file(script_dir / f)
    pf.finish()
    assert_pg_build_in_path()


def prepare_cluster(pgdatadir: Path, engine: str, memory_buffers: str,
                    undo_buffers: str, fsync: str, synchronous_commit: str,
                    pg_stat_statements: bool,
                    reuse_data: bool) -> bool:
    """
    Init/reuse PGDATA and create the `benchbase` database. Returns True if
    BenchBase --create/--load still needs to run (i.e. on fresh PGDATA).

    For engine=orioledb (and for pg_stat_statements) the extensions are
    installed into template1 so the freshly-created `benchbase` database
    inherits them, matching how test_tpcc_hdb.py handles the HammerDB
    workflow.
    """
    cfg_args = dict(memory_buffers=memory_buffers, undo_buffers=undo_buffers,
                    fsync=fsync, synchronous_commit=synchronous_commit,
                    pg_stat_statements=pg_stat_statements)
    if reuse_data and is_pgdata_initialized(pgdatadir):
        with stage(f"reuse pgdata {pgdatadir.name}"):
            stop_pg_silent(pgdatadir)
            write_engine_config(pgdatadir, engine, "tpcc_bb", **cfg_args)
            pg_start(pgdatadir)
            pg_restart(pgdatadir)
            if pg_stat_statements:
                enable_pg_stat_statements(db=bb_db)
        return False

    with stage(f"init pgdata {pgdatadir.name}"):
        run(["sudo", "killall", "-9", "postgres"], allow_fail=True)
        stop_pg_silent(pgdatadir)
        remove_dir(pgdatadir)
        ensure_dir(pgdatadir.parent)
        time.sleep(5)
        pg_initdb(pgdatadir)
        pg_start(pgdatadir)
        write_engine_config(pgdatadir, engine, "tpcc_bb", **cfg_args)
        if engine == "orioledb" or pg_stat_statements:
            pg_restart(pgdatadir)
            if engine == "orioledb":
                run(["psql", "-dtemplate1", "-c", "create extension orioledb;"])
            if pg_stat_statements:
                run(["psql", "-dtemplate1", "-c",
                     "create extension pg_stat_statements;"])
        pg_restart(pgdatadir)
        pg_psql(f"CREATE DATABASE {bb_db};")
        if engine == "orioledb":
            pg_psql("show shared_buffers; show orioledb.main_buffers; "
                    "show default_table_access_method;")
        else:
            pg_psql("show shared_buffers; show default_table_access_method;")
    return True


def _bb_run(*, benchbase: Path, config: Path, output_dir: Path,
            create: bool, load: bool, execute: bool,
            capture: bool = False) -> str | None:
    """Invoke benchbase.jar with the chosen action flags. Returns stdout
    when capture=True (used to parse Throughput on the execute phase)."""
    cmd = [
        "java", "-jar", str(benchbase / "benchbase.jar"),
        "-b", "tpcc",
        "-c", str(config),
        "-d", str(output_dir),
        f"--create={'true' if create else 'false'}",
        f"--load={'true' if load else 'false'}",
        f"--execute={'true' if execute else 'false'}",
    ]
    proc = run(cmd, cwd=benchbase, capture=capture)
    return proc.stdout if capture else None


def bb_load(*, benchbase: Path, warehouses: int, user: str,
            output_dir: Path) -> None:
    cfg_path = log_dir / f"bb-load-w{warehouses}.xml"
    # Terminals + times only matter to --execute, but BenchBase still parses
    # them on --load; use minimal placeholders.
    cfg_path.write_text(render_config(
        warehouses=warehouses, terminals=1,
        rampup_sec=0, duration_sec=1, user=user,
    ))
    with stage(f"bb load w={warehouses}"):
        _bb_run(benchbase=benchbase, config=cfg_path, output_dir=output_dir,
                create=True, load=True, execute=False)


def bb_run_one(*, benchbase: Path, warehouses: int, terminals: int,
               rampup_sec: int, duration_sec: int, user: str,
               output_dir: Path,
               monitor_path: Path | None, pgdatadir: Path,
               ) -> float | None:
    cfg_path = log_dir / f"bb-run-w{warehouses}-t{terminals}.xml"
    cfg_path.write_text(render_config(
        warehouses=warehouses, terminals=terminals,
        rampup_sec=rampup_sec, duration_sec=duration_sec, user=user,
    ))
    cm = (
        ResourceMonitor(
            monitor_path, mount_point=pgdatadir, pgdatadir=pgdatadir,
            dsn=f"host=127.0.0.1 port=5432 dbname={bb_db} user={user}",
        )
        if monitor_path is not None else contextlib.nullcontext()
    )
    with stage(f"bb run w={warehouses} t={terminals}"), cm:
        out = _bb_run(benchbase=benchbase, config=cfg_path,
                      output_dir=output_dir,
                      create=False, load=False, execute=True,
                      capture=True)
    return parse_bb_throughput(out or "")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    preflight(args)

    memory_buffers = args.memory_buffers or "20GB"
    user = getpass.getuser()

    if args.fast_run:
        rampup_sec, duration_sec = 5, 10
        warehouses_list = [22, 10]
        terminals_list = [10, 4]
        fast_msg = "FAST RUN!"
    else:
        rampup_sec = args.rampup_min * 60
        duration_sec = args.duration_min * 60
        warehouses_list = list(args.warehouses)
        terminals_list = list(args.terminals)
        fast_msg = ""

    ensure_dir(args.results_dir)
    result_file = args.results_dir / f"{args.patch_id}-tpcc_bb"
    monitor_dir = (
        args.results_dir / f"{args.patch_id}-tpcc_bb-resources"
        if args.extended_logging else None
    )
    if monitor_dir is not None:
        ensure_dir(monitor_dir)

    # BenchBase writes per-run JSON/CSV files into <output_dir>. Keep them
    # separate from our result files but under results_dir for traceability.
    bb_out_dir = args.results_dir / f"{args.patch_id}-tpcc_bb-benchbase-out"
    ensure_dir(bb_out_dir)

    append_line(result_file, f"# {fast_msg} {now_str()}")
    append_line(result_file, "# warehouses, terminals, tps, tpm")

    for w in warehouses_list:
        pgdatadir = data_dir_for(args.pgdata_base, engine=args.engine,
                                 data_id=args.data_id or args.patch_id,
                                 test="tpcc_bb", scale=f"w{w}")
        append_line(result_file,
                    f"# {fast_msg} NEW SERIES warehouses = {w} {now_str()}")

        with stage(f"warehouses {w}"):
            needs_load = prepare_cluster(pgdatadir, args.engine,
                                         memory_buffers, args.undo_buffers,
                                         args.fsync, args.synchronous_commit,
                                         args.pg_stat_statements,
                                         args.reuse_data)
            if needs_load:
                bb_load(benchbase=args.benchbase, warehouses=w, user=user,
                        output_dir=bb_out_dir)

            for t in terminals_list:
                append_text(result_file, f"{w},{t},")
                pg_psql("checkpoint;")
                if args.pg_stat_statements:
                    pgss_reset(db=bb_db)

                monitor_path = (
                    monitor_dir / f"w{w}-t{t}.jsonl"
                    if monitor_dir is not None else None
                )
                tps = bb_run_one(
                    benchbase=args.benchbase, warehouses=w, terminals=t,
                    rampup_sec=rampup_sec, duration_sec=duration_sec,
                    user=user, output_dir=bb_out_dir,
                    monitor_path=monitor_path, pgdatadir=pgdatadir,
                )
                if tps is None:
                    log.warning("    w=%d terminals=%d: no Throughput line "
                                "in BenchBase output", w, t)
                    append_line(result_file, "ERROR,ERROR")
                else:
                    tpm = int(round(tps * 60))
                    log.info("    w=%d terminals=%d tps=%.1f tpm=%d",
                             w, t, tps, tpm)
                    append_line(result_file, f"{tps:.1f},{tpm}")

                if args.pg_stat_statements:
                    pgss_dump_report(
                        args.results_dir /
                        f"{args.patch_id}-tpcc_bb-w{w}-t{t}-pgss.txt",
                        db=bb_db,
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
