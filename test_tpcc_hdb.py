#!/usr/bin/env python3
"""
Run TPC-C via HammerDB in stored-procedure mode.

Compared to test_tpcc.py (which uses go-tpc with per-statement SQL), this
driver builds 5 PL/pgSQL functions (neword/payment/delivery/slev/ostat)
during BUILD and the virtual users invoke them directly. The result is
much higher throughput and a cleaner picture of pure engine performance.

Per-warehouse PGDATA naming:
    <pgdata-base>/pgdata-<engine>-<patch_id>-tpcc_hdb-w<W>
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
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
    ensure_dir,
    is_pgdata_initialized,
    log,
    log_dir,
    now_str,
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


default_warehouses = [470, 220, 100, 47, 22, 10, 5]
default_vu = [330, 220, 150, 100, 68, 47, 33, 22, 15, 10, 7, 5, 3, 2, 1]

# HammerDB uses a dedicated PG user. With trust auth on local/127.0.0.1 the
# password is ignored, but HammerDB still wants non-empty strings.
hdb_pg_user = "tpcc"
hdb_pg_pass = "tpcc"
hdb_pg_dbase = "tpcc"
hdb_pg_defaultdbase = "postgres"


# Regex matches several historical HammerDB output formats:
#   "System achieved 12345 NOPM from 28912 PostgreSQL TPM"
#   "System achieved 12345 NOPM from a PostgreSQL TPM of 28912"
nopm_re = re.compile(
    r"System achieved\s+(\d+)\s+NOPM\s+from\s+(?:a\s+PostgreSQL\s+TPM\s+of\s+(\d+)|(\d+)\s+PostgreSQL)",
    re.IGNORECASE,
)


def parse_hdb_result(output: str) -> tuple[int, int] | None:
    """Return (nopm, tpm) from a HammerDB run log, or None."""
    last: tuple[int, int] | None = None
    for line in output.splitlines():
        m = nopm_re.search(line)
        if m:
            nopm = int(m.group(1))
            tpm = int(m.group(2) or m.group(3))
            last = (nopm, tpm)
    return last


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run TPC-C via HammerDB (stored-procedure mode) for one "
                    "(engine, patch_id) point.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_test_args(p)
    p.add_argument("--warehouses", nargs="+", type=positive_int, metavar="N",
                   default=default_warehouses,
                   help="Warehouses values to run.")
    p.add_argument("--vu", nargs="+", type=positive_int, metavar="N",
                   default=default_vu,
                   help="Virtual-user counts to run (HammerDB analog of tpcc conns).")
    p.add_argument("--rampup-min", type=positive_int, default=2,
                   help="Rampup time in minutes (ignored on --fast-run).")
    p.add_argument("--duration-min", type=positive_int, default=5,
                   help="Measured duration in minutes (ignored on --fast-run).")
    p.add_argument("--build-vu", type=positive_int, default=16,
                   help="Virtual users used for schema BUILD/load.")
    p.add_argument("--hammerdb", type=Path,
                   default=script_dir / "hammerdb",
                   help="Path to the HammerDB install tree.")
    return p


def preflight(args: argparse.Namespace) -> None:
    pf = Preflight()
    pf.assert_engine(args.engine)
    for b in ("pg_ctl", "initdb", "psql", "killall"):
        pf.require_binary(b)

    hdb_cli = Path(args.hammerdb) / "hammerdbcli"
    if not hdb_cli.is_file():
        pf.err(
            f"hammerdbcli not found at {hdb_cli}. "
            f"Run tests.py first (it installs HammerDB) or pass --hammerdb."
        )

    for f in common.conf_files_required_for_tests["tpcc_hdb"]:
        pf.require_file(script_dir / f)
    pf.finish()
    assert_pg_build_in_path()


# ---------------------------------------------------------------------------
# Cluster init
# ---------------------------------------------------------------------------

def prepare_cluster(pgdatadir: Path, engine: str, memory_buffers: str,
                    undo_buffers: str, fsync: str, synchronous_commit: str,
                    reuse_data: bool) -> bool:
    """
    Init/reuse PGDATA. Returns True if HammerDB BUILD still needs to run.

    For engine=orioledb we install the extension into template1 so the tpcc
    database that HammerDB CREATE-DATABASE's later automatically inherits it
    (HammerDB itself doesn't know about orioledb).
    """
    cfg_args = dict(memory_buffers=memory_buffers, undo_buffers=undo_buffers,
                    fsync=fsync, synchronous_commit=synchronous_commit)
    if reuse_data and is_pgdata_initialized(pgdatadir):
        with stage(f"reuse pgdata {pgdatadir.name}"):
            stop_pg_silent(pgdatadir)
            write_engine_config(pgdatadir, engine, "tpcc_hdb", **cfg_args)
            pg_start(pgdatadir)
            pg_restart(pgdatadir)
        return False

    with stage(f"init pgdata {pgdatadir.name}"):
        run(["sudo", "killall", "-9", "postgres"], allow_fail=True)
        stop_pg_silent(pgdatadir)
        remove_dir(pgdatadir)
        ensure_dir(pgdatadir.parent)
        time.sleep(5)
        pg_initdb(pgdatadir)
        pg_start(pgdatadir)
        write_engine_config(pgdatadir, engine, "tpcc_hdb", **cfg_args)
        if engine == "orioledb":
            # Install the extension into template1 so the database HammerDB
            # creates inherits it. We restart afterwards to pick up
            # shared_preload_libraries=orioledb from the auto.conf.
            pg_restart(pgdatadir)
            run(["psql", "-dtemplate1", "-c", "create extension orioledb;"])
        pg_restart(pgdatadir)
        if engine == "orioledb":
            pg_psql("show shared_buffers; show orioledb.main_buffers; "
                    "show default_table_access_method;")
        else:
            pg_psql("show shared_buffers; show default_table_access_method;")
    return True


# ---------------------------------------------------------------------------
# HammerDB driving
# ---------------------------------------------------------------------------

def _tcl_quote(s: str) -> str:
    """TCL-style quote for a string used in `diset ... "value"`."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _hammerdb_run(tcl_path: Path, hammerdb: Path, *,
                  output_path: Path) -> str:
    """
    Invoke hammerdbcli in auto mode. HammerDB's stdout/stderr stream into
    `output_path` live (so you can `tail -f` it during long runs). Returns
    the captured output as a string for parsing.
    """
    hdb_cli = hammerdb / "hammerdbcli"
    cmd = [str(hdb_cli), "auto", str(tcl_path)]

    # HammerDB ships libpgtcl2.1.1.so under ./lib/pgtcl2.1.1/. Pgtcl is
    # dynamically linked against libpq.so.5, which we deliberately don't
    # install system-wide — our libpq lives in the per-ref build under
    # $GITHUB_WORKSPACE/lib. Tell the linker where to find it; the
    # hammerdbcli wrapper itself prepends ./lib, so our value is appended
    # after that and Pgtcl resolves cleanly.
    env: dict[str, str] = {}
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        pg_lib = str(Path(workspace) / "lib")
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = (
            pg_lib + (":" + existing if existing else "")
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("")  # truncate any leftover from a prior run
    run(cmd, cwd=hammerdb, env=env, log_file=output_path)
    return output_path.read_text()


def _build_tcl(warehouses: int, build_vu: int, superuser: str) -> str:
    return f"""\
dbset db pg
dbset bm TPC-C
diset connection pg_host 127.0.0.1
diset connection pg_port 5432
diset connection pg_sslmode disable
diset tpcc pg_superuser "{_tcl_quote(superuser)}"
diset tpcc pg_superuserpass "bench"
diset tpcc pg_defaultdbase {hdb_pg_defaultdbase}
diset tpcc pg_user {hdb_pg_user}
diset tpcc pg_pass {hdb_pg_pass}
diset tpcc pg_dbase {hdb_pg_dbase}
diset tpcc pg_count_ware {warehouses}
diset tpcc pg_num_vu {build_vu}
diset tpcc pg_storedprocs true
buildschema
waittocomplete
vudestroy
exit
"""


def _run_tcl(vu: int, rampup_min: int, duration_min: int,
             superuser: str) -> str:
    # pg_total_iterations is the upper bound on TX count per virtual user —
    # even in timed mode, setting it to 0 makes every worker do zero
    # transactions and exit immediately. Use a large number so timed mode
    # actually keeps the workers looping until rampup+duration elapses.
    return f"""\
dbset db pg
dbset bm TPC-C
diset connection pg_host 127.0.0.1
diset connection pg_port 5432
diset connection pg_sslmode disable
diset tpcc pg_superuser "{_tcl_quote(superuser)}"
diset tpcc pg_superuserpass "bench"
diset tpcc pg_user {hdb_pg_user}
diset tpcc pg_pass {hdb_pg_pass}
diset tpcc pg_dbase {hdb_pg_dbase}
diset tpcc pg_driver timed
diset tpcc pg_rampup {rampup_min}
diset tpcc pg_duration {duration_min}
diset tpcc pg_total_iterations 10000000
diset tpcc pg_storedprocs true
diset tpcc pg_allwarehouse true
diset tpcc pg_raiseerror true
diset tpcc pg_user_delay 5
diset tpcc pg_repeat_delay 0
loadscript
vuset vu {vu}
vucreate
vurun
vudestroy
exit
"""


def hdb_build(*, hammerdb: Path, warehouses: int, build_vu: int,
              superuser: str) -> None:
    ensure_dir(log_dir)
    tcl = log_dir / f"hdb-build-w{warehouses}.tcl"
    output = log_dir / f"hdb-build-w{warehouses}.log"
    tcl.write_text(_build_tcl(warehouses, build_vu, superuser))
    with stage(f"hdb build w={warehouses}"):
        _hammerdb_run(tcl, hammerdb, output_path=output)


def hdb_run_one(*, hammerdb: Path, vu: int, rampup_min: int,
                duration_min: int, superuser: str,
                monitor_path: Path | None, pgdatadir: Path,
                warehouses: int) -> tuple[int, int] | None:
    ensure_dir(log_dir)
    tcl = log_dir / f"hdb-run-w{warehouses}-vu{vu}.tcl"
    output = log_dir / f"hdb-run-w{warehouses}-vu{vu}.log"
    tcl.write_text(_run_tcl(vu, rampup_min, duration_min, superuser))

    cm = (
        ResourceMonitor(monitor_path, mount_point=pgdatadir,
                        pgdatadir=pgdatadir,
                        dsn=(f"host=127.0.0.1 port=5432 "
                             f"dbname={hdb_pg_dbase} user={hdb_pg_user}"))
        if monitor_path is not None else contextlib.nullcontext()
    )
    with stage(f"hdb run w={warehouses} vu={vu}"), cm:
        raw = _hammerdb_run(tcl, hammerdb, output_path=output)
    return parse_hdb_result(raw)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    preflight(args)

    memory_buffers = args.memory_buffers or "20GB"
    superuser = getpass.getuser()

    if args.fast_run:
        rampup_min, duration_min = 1, 1
        warehouses_list = [22, 10]
        vu_list = [10, 4]
        fast_msg = "FAST RUN!"
    else:
        rampup_min = args.rampup_min
        duration_min = args.duration_min
        warehouses_list = list(args.warehouses)
        vu_list = list(args.vu)
        fast_msg = ""

    ensure_dir(args.results_dir)
    result_file = args.results_dir / f"{args.engine}-{args.patch_id}-tpcc_hdb"
    monitor_dir = (
        args.results_dir / f"{args.engine}-{args.patch_id}-tpcc_hdb-resources"
        if args.extended_logging else None
    )
    if monitor_dir is not None:
        ensure_dir(monitor_dir)

    append_line(result_file, f"# {fast_msg} {now_str()}")
    append_line(result_file, "# warehouses, vu, nopm, tpm")

    for w in warehouses_list:
        pgdatadir = data_dir_for(args.pgdata_base, engine=args.engine,
                                 patch_id=args.patch_id,
                                 test="tpcc_hdb", scale=f"w{w}")
        append_line(result_file,
                    f"# {fast_msg} NEW SERIES warehouses = {w} {now_str()}")

        with stage(f"warehouses {w}"):
            needs_build = prepare_cluster(pgdatadir, args.engine,
                                          memory_buffers, args.undo_buffers,
                                          args.fsync, args.synchronous_commit,
                                          args.reuse_data)
            if needs_build:
                hdb_build(hammerdb=args.hammerdb, warehouses=w,
                          build_vu=args.build_vu, superuser=superuser)

            for vu in vu_list:
                append_text(result_file, f"{w},{vu},")
                pg_psql("checkpoint;")

                monitor_path = (
                    monitor_dir / f"w{w}-vu{vu}.jsonl"
                    if args.extended_logging else None
                )
                result = hdb_run_one(
                    hammerdb=args.hammerdb, vu=vu,
                    rampup_min=rampup_min, duration_min=duration_min,
                    superuser=superuser,
                    monitor_path=monitor_path, pgdatadir=pgdatadir,
                    warehouses=w,
                )
                if result is None:
                    log.warning("    w=%d vu=%d: no NOPM/TPM in HammerDB output", w, vu)
                    append_line(result_file, "ERROR,ERROR")
                else:
                    nopm, tpm = result
                    log.info("    w=%d vu=%d nopm=%d tpm=%d", w, vu, nopm, tpm)
                    append_line(result_file, f"{nopm},{tpm}")

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
