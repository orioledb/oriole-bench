"""
Append a single result row to the ibench result file. Replaces report-ibench.sh.

Output columns:
    test_name,
    pgdata apparent, pgdata,
    pg_wal apparent, pg_wal,
    orioledb_data apparent, orioledb_data,
    orioledb_undo apparent, orioledb_undo,
    elapsed (sec, before checkpoint),
    checkpoint+report time (sec)
"""

from __future__ import annotations

import time
from pathlib import Path

from common import (
    BenchError,
    append_line,
    du_kb,
    log,
    pg_psql,
)


def report(
    *,
    test_name: str,
    elapsed: float,
    pgdatadir: Path,
    result_file: Path,
) -> None:
    if not pgdatadir.is_dir():
        raise BenchError(f"PGDATADIR does not exist: {pgdatadir}")

    log.info("Reporting phase %s (elapsed=%.1fs)", test_name, elapsed)
    report_start = time.monotonic()
    pg_psql("checkpoint;")

    pgdata = pgdatadir
    pg_wal = pgdatadir / "pg_wal"
    oriole_data = pgdatadir / "orioledb_data"
    oriole_undo = pgdatadir / "orioledb_undo"

    fields: list[str] = [test_name]
    for d in (pgdata, pg_wal, oriole_data, oriole_undo):
        fields.append(str(du_kb(d, apparent=True)))
        fields.append(str(du_kb(d, apparent=False)))

    fields.append(str(int(elapsed)))
    checkpoint_secs = int(time.monotonic() - report_start)
    fields.append(str(checkpoint_secs))

    append_line(result_file, ",".join(fields))
