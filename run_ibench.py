"""
Ibench multi-phase workflow. Replaces run_ibench.sh.

The workflow is a sequence of phases:
    l.i0    -> initial load
    l.x     -> create secondary indexes
    l.i1    -> random inserts at 50 writes/commit
    l.i2    -> random inserts at  5 writes/commit
    qr100.L1, qp100.L2, qr500.L3, qp500.L4, qr1000.L5, qp1000.L6
                -> queries with background inserts/deletes

Each phase runs `conns` parallel iibench.py workers and waits for them all,
then a measurement row is appended to the result file.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from pathlib import Path

import report_ibench
from common import (
    BenchError,
    ResourceMonitor,
    log,
    log_dir,
    run_bg,
    stage,
    wait_all,
)


@dataclass(frozen=True)
class Phase:
    label: str            # e.g. "l.i0"
    max_rows_mul: int     # multiplied by scale_mul
    rows_per_commit: int
    inserts_per_second: int
    query_threads: int
    seed: int
    delete_per_insert: bool = False
    secondary_at_end: bool = False
    num_secondary_indexes: int = 0
    fixed_max_rows: int | None = None  # overrides max_rows_mul*scale_mul if set
    query_pk_only: bool = False


phases: tuple[Phase, ...] = (
    Phase(
        label="l.i0",
        max_rows_mul=100000,
        rows_per_commit=100,
        inserts_per_second=0,
        query_threads=0,
        seed=1733776768,
        num_secondary_indexes=0,
    ),
    Phase(
        label="l.ix",
        max_rows_mul=0,
        fixed_max_rows=5,
        rows_per_commit=100,
        inserts_per_second=0,
        query_threads=0,
        seed=1733776886,
        secondary_at_end=True,
        num_secondary_indexes=3,
    ),
    Phase(
        label="l.i1",
        max_rows_mul=160000,
        rows_per_commit=50,
        inserts_per_second=0,
        query_threads=0,
        seed=1733776944,
        delete_per_insert=True,
        num_secondary_indexes=3,
    ),
    Phase(
        label="l.i2",
        max_rows_mul=40000,
        rows_per_commit=5,
        inserts_per_second=0,
        query_threads=0,
        seed=1733777712,
        delete_per_insert=True,
        num_secondary_indexes=3,
    ),
    Phase(
        label="qr100.L1",
        max_rows_mul=1800,
        rows_per_commit=50,
        inserts_per_second=100,
        query_threads=1,
        seed=1733778656,
        delete_per_insert=True,
        num_secondary_indexes=3,
    ),
    Phase(
        label="qr100.L2",
        max_rows_mul=1800,
        rows_per_commit=50,
        inserts_per_second=100,
        query_threads=1,
        seed=1733780481,
        delete_per_insert=True,
        num_secondary_indexes=3,
        query_pk_only=True,
    ),
    Phase(
        label="qr500.L3",
        max_rows_mul=9000,
        rows_per_commit=50,
        inserts_per_second=500,
        query_threads=1,
        seed=1733782306,
        delete_per_insert=True,
        num_secondary_indexes=3,
    ),
    Phase(
        label="qr500.L4",
        max_rows_mul=9000,
        rows_per_commit=50,
        inserts_per_second=500,
        query_threads=1,
        seed=1733784214,
        delete_per_insert=True,
        num_secondary_indexes=3,
        query_pk_only=True,
    ),
    Phase(
        label="qr1000.L5",
        max_rows_mul=18000,
        rows_per_commit=50,
        inserts_per_second=1000,
        query_threads=1,
        seed=1733786203,
        delete_per_insert=True,
        num_secondary_indexes=3,
    ),
    Phase(
        label="qr1000.L6",
        max_rows_mul=18000,
        rows_per_commit=50,
        inserts_per_second=1000,
        query_threads=1,
        seed=1733789005,
        delete_per_insert=True,
        num_secondary_indexes=3,
        query_pk_only=True,
    ),
)


def _build_ibench_cmd(
    *,
    ibench_path: Path,
    p: Phase,
    scale_mul: int,
    table_name: str,
    my_id: int,
    engine: str,
    setup: bool,
) -> list[str]:
    if p.fixed_max_rows is not None:
        max_rows = p.fixed_max_rows
    else:
        max_rows = p.max_rows_mul * scale_mul

    if setup:
        # Initial load uses --engine_options="using <engine>" (literal "using")
        engine_options = f"using {engine}"
    else:
        engine_options = engine

    cmd = [
        "python3", str(ibench_path),
        "--dbms=postgres", "--db_name=postgres",
        "--secs_per_report=1",
        "--db_host=127.0.0.1", "--db_user=ubuntu",
        "--engine=pg",
        "--unique_checks=1", "--bulk_load=0",
    ]
    if p.delete_per_insert:
        cmd.append("--delete_per_insert")
    if p.query_pk_only:
        cmd.append("--query_pk_only")
    cmd += [
        f"--max_rows={max_rows}",
        f"--table_name={table_name}",
    ]
    if setup:
        cmd.append("--setup")
    if p.secondary_at_end:
        cmd.append("--secondary_at_end")
    cmd += [
        f"--num_secondary_indexes={p.num_secondary_indexes}",
        "--data_length_min=10", "--data_length_max=20",
        f"--rows_per_commit={p.rows_per_commit}",
        f"--inserts_per_second={p.inserts_per_second}",
        f"--query_threads={p.query_threads}",
        f"--seed={p.seed}",
        "--dbopt=none",
        f"--my_id={my_id}",
        "--use_prepared_query",
        f"--engine_options={engine_options}",
    ]
    return cmd


def _run_phase(
    *,
    p: Phase,
    ibench_path: Path,
    scale_mul: int,
    conns: int,
    engine: str,
    setup: bool,
    pgdatadir: Path,
    monitor_path: Path | None,
) -> float:
    start = time.monotonic()
    monitor_cm = (
        ResourceMonitor(monitor_path, mount_point=pgdatadir)
        if monitor_path is not None else contextlib.nullcontext()
    )
    with stage(f"ibench {p.label}"), monitor_cm:
        procs = []
        for n in range(1, conns + 1):
            cmd = _build_ibench_cmd(
                ibench_path=ibench_path,
                p=p,
                scale_mul=scale_mul,
                table_name=f"pi{n}",
                my_id=n,
                engine=engine,
                setup=setup,
            )
            # Each worker gets its own log file so the N parallel stdouts
            # don't interleave in one file.
            worker_log = log_dir / f"ibench-{p.label}-w{n:02d}.log"
            procs.append(run_bg(cmd, log_file=worker_log))

        wait_all(procs, label=f"ibench phase {p.label}")

    return time.monotonic() - start


def run(
    *,
    engine: str,
    patch_id: str,
    pgdatadir: Path,
    ibench_path: Path,
    scale_mul: int,
    conns: int,
    result_file: Path,
    monitor_dir: Path | None = None,
) -> None:
    if conns < 1:
        raise BenchError("conns must be >= 1 for ibench")

    for i, p in enumerate(phases):
        monitor_path = (
            monitor_dir / f"{p.label}.jsonl"
            if monitor_dir is not None else None
        )
        # Phase 0 (l.i0) creates the tables; that's the only setup phase.
        elapsed = _run_phase(
            p=p,
            ibench_path=ibench_path,
            scale_mul=scale_mul,
            conns=conns,
            engine=engine,
            setup=(i == 0),
            pgdatadir=pgdatadir,
            monitor_path=monitor_path,
        )
        report_ibench.report(
            test_name=p.label,
            elapsed=elapsed,
            pgdatadir=pgdatadir,
            result_file=result_file,
        )
