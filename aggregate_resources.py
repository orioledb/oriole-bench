#!/usr/bin/env python3
"""
Aggregate ResourceMonitor JSONL files.

For each input file, compute:
  * mean of CPU percents (system/user/idle) and disk IO counters
    (read_count, write_count, read_bytes, write_bytes)
  * mean of each `waits` key (samples without the key contribute 0)
  * disk_used : last value + average growth bytes/sec
  * lsn       : last value + average growth bytes/sec

Inputs are JSONL paths or globs. Output is JSON to stdout (default) or CSV.

Examples:
  ./aggregate_resources.py results/heap-master-tpcc-resources/*.jsonl
  ./aggregate_resources.py 'results/*-tpcc-resources/*.jsonl' --format csv
  ./aggregate_resources.py logs/*.jsonl --out summary.json
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any


numeric_fields = (
    "system", "user", "idle",
    "read_count", "write_count",
    "read_bytes", "write_bytes",
)


def parse_lsn(s: Any) -> int | None:
    """Parse a Postgres LSN string 'X/Y' (hex/hex) into an integer byte offset."""
    if not isinstance(s, str):
        return None
    try:
        hi, lo = s.split("/", 1)
        return (int(hi, 16) << 32) | int(lo, 16)
    except (ValueError, AttributeError):
        return None


def format_lsn(value: int) -> str:
    return f"{value >> 32:X}/{value & 0xFFFFFFFF:X}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _per_second_growth(
    rows: list[dict[str, Any]],
    field: str,
    value_fn,
) -> tuple[Any, float | None, Any]:
    """
    For a value extracted with value_fn(row[field]), return:
      (final_value, growth_per_sec, sample_count)

    growth_per_sec uses the row's `time` field (seconds since monitor start)
    if available; otherwise falls back to sample index.
    """
    samples: list[tuple[float, Any]] = []
    for r in rows:
        v = value_fn(r.get(field))
        if v is None:
            continue
        t = r.get("time")
        if not isinstance(t, (int, float)):
            t = len(samples) + 1
        samples.append((float(t), v))
    if not samples:
        return None, None, 0
    if len(samples) == 1:
        return samples[-1][1], 0.0, 1
    t0, v0 = samples[0]
    tn, vn = samples[-1]
    dt = tn - t0
    growth = (vn - v0) / dt if dt > 0 else 0.0
    return vn, growth, len(samples)


def aggregate(path: Path) -> dict[str, Any] | None:
    rows = _read_jsonl(path)
    if not rows:
        return None

    out: dict[str, Any] = {"file": str(path), "samples": len(rows)}

    # Means of plain numeric fields
    for k in numeric_fields:
        vals = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        out[f"avg_{k}"] = mean(vals) if vals else None

    # Mean of waits per key. Samples that lack the key contribute 0; samples
    # where `waits` is null entirely are skipped (the monitor couldn't reach
    # PG that second).
    waits_sums: dict[str, float] = {}
    waits_seen = 0
    for r in rows:
        w = r.get("waits")
        if not isinstance(w, dict):
            continue
        waits_seen += 1
        for k, v in w.items():
            if isinstance(v, (int, float)):
                waits_sums[k] = waits_sums.get(k, 0.0) + float(v)
    avg_waits = (
        {k: total / waits_seen for k, total in waits_sums.items()}
        if waits_seen > 0 else {}
    )
    out["avg_waits"] = avg_waits
    out["waits_samples"] = waits_seen

    # disk_used: final + average growth (bytes/sec)
    final_disk, growth_disk, _ = _per_second_growth(
        rows, "disk_used",
        lambda v: v if isinstance(v, (int, float)) else None,
    )
    out["final_disk_used"] = final_disk
    out["avg_growth_disk_used_bps"] = growth_disk

    # lsn: parse 'X/Y' hex, then final + average growth (bytes/sec)
    final_lsn_int, growth_lsn, _ = _per_second_growth(
        rows, "lsn", parse_lsn,
    )
    out["final_lsn"] = format_lsn(final_lsn_int) if final_lsn_int is not None else None
    out["avg_growth_lsn_bps"] = growth_lsn

    return out


def expand_paths(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for p in patterns:
        if any(c in p for c in "*?["):
            matches = sorted(glob.glob(p, recursive=True))
            if not matches:
                print(f"warning: no files match: {p}", file=sys.stderr)
            paths.extend(Path(m) for m in matches)
        else:
            paths.append(Path(p))
    return paths


def to_csv(results: list[dict[str, Any]]) -> str:
    base_cols = ["file", "samples", "waits_samples"]
    base_cols += [f"avg_{k}" for k in numeric_fields]
    base_cols += [
        "final_disk_used", "avg_growth_disk_used_bps",
        "final_lsn", "avg_growth_lsn_bps",
    ]
    wait_keys = sorted({
        k for r in results for k in (r.get("avg_waits") or {}).keys()
    })
    cols = base_cols + [f"avg_waits.{k}" for k in wait_keys]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in results:
        row: list[Any] = []
        for c in cols:
            if c.startswith("avg_waits."):
                key = c[len("avg_waits."):]
                row.append((r.get("avg_waits") or {}).get(key, ""))
            else:
                v = r.get(c, "")
                row.append("" if v is None else v)
        w.writerow(row)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Aggregate ResourceMonitor JSONL files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("inputs", nargs="+",
                   help="JSONL files or glob patterns.")
    p.add_argument("--format", choices=("json", "csv"), default="json")
    p.add_argument("--out", type=Path, default=None,
                   help="Write to this file instead of stdout.")
    args = p.parse_args(argv)

    results: list[dict[str, Any]] = []
    for path in expand_paths(args.inputs):
        agg = aggregate(path)
        if agg is None:
            print(f"warning: no samples in {path}", file=sys.stderr)
            continue
        results.append(agg)

    if args.format == "json":
        text = json.dumps(results, indent=2) + "\n"
    else:
        text = to_csv(results)

    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
