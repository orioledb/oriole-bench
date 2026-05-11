#!/usr/bin/env python3
"""
Top-level orchestrator: bootstraps system deps, builds OrioleDB / Postgres
(or reuses prior artifacts) and runs all configured benchmarks.

Usage examples:
    # Pure-PG comparison between two postgres-master refs.
    ./tests.py --pg-id master REL_17_STABLE

    # OrioleDB + PG comparison, 100GB buffers, tpcc + pgbench only.
    ./tests.py --oriole-id main beta9 --pg-id master \
               --memory-buffers 100GB --tests pgbench tpcc

    # Re-run on existing data without rebuilding and without re-loading.
    ./tests.py --pg-id master --reuse-data

    # Force a fresh build of everything.
    ./tests.py --oriole-id main --reinitialize
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import common
from common import (
    BenchError,
    Preflight,
    bootstrap_system,
    check_linux,
    cpu_count,
    default_pgdata_base,
    default_results_dir,
    ensure_dir,
    log,
    pg_build_exists,
    remove_dir,
    repo_clone_or_fetch,
    run,
    script_dir,
    valid_tests,
)


go_version = "1.21.1"
go_tarball = f"go{go_version}.linux-arm64.tar.gz"
go_url = f"https://dl.google.com/go/{go_tarball}"

oriole_repo = "https://github.com/orioledb/orioledb"
postgres_oriole_repo = "https://github.com/orioledb/postgres"
postgres_master_repo = "https://github.com/postgres/postgres.git"

go_tpc_repo = "https://github.com/pingcap/go-tpc.git"
go_tpc_version = "v1.0.10"
go_tpc_commit = "01c06538227a49fa8f0953cfdf3146a95b4a34a3"
go_tpc_date = "2024-10-29 03:01:30"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run oriole-bench end-to-end: bootstrap, build, prepare "
                    "and benchmark.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--oriole-id", nargs="*", default=[], metavar="REF",
        help="One or more orioledb commit hashes / tags / branches to compare. "
             "Optional — pass --pg-id only for a pure-PG comparison.",
    )
    p.add_argument(
        "--pg-id", nargs="*", default=[], metavar="REF",
        help="One or more postgres commit hashes / tags / branches "
             "to compare (PG-only tests).",
    )
    p.add_argument(
        "--tests", nargs="+", default=list(valid_tests),
        choices=valid_tests, metavar="TEST",
        help=f"Test suites to run (default: {' '.join(valid_tests)}).",
    )
    p.add_argument(
        "--fast-run", action="store_true",
        help="Run fast for debug; not for actual measurements.",
    )
    p.add_argument(
        "--nvme", action="store_true",
        help="Create+mount the local NVMe volume on /ssd (c7gd-style instances).",
    )
    p.add_argument(
        "--memory-buffers", default=None,
        help="Override shared_buffers / orioledb.main_buffers value.",
    )
    p.add_argument(
        "--results-dir", type=Path, default=default_results_dir,
        help="Where result files are written.",
    )
    p.add_argument(
        "--pgdata-base", type=Path, default=default_pgdata_base,
        help="Parent directory under which per-test PGDATA dirs live.",
    )

    behavior = p.add_argument_group("behavior")
    behavior.add_argument(
        "--reinitialize", action="store_true",
        help="Discard cached repos / go-tpc / pg builds and rebuild from "
             "scratch. Default: reuse everything that's already on disk.",
    )
    behavior.add_argument(
        "--reuse-data", action="store_true",
        help="Reuse per-test data directories (skip initdb + data load) when "
             "they already exist. Propagated to all child tests.",
    )
    behavior.add_argument(
        "--skip-bootstrap", action="store_true",
        help="Skip the apt-get + pip3 install step (use when the VM is already "
             "provisioned).",
    )

    pg = p.add_argument_group("pgbench")
    pg.add_argument("--precise-pgbench", action="store_true",
                    help="Pgbench: dense connection list.")
    pg.add_argument("--pgbench-conns", nargs="+", type=common.positive_int,
                    metavar="N", help="Pgbench: explicit connection list "
                                      "(overrides --precise-pgbench).")
    pg.add_argument("--pgbench-tests", nargs="+", metavar="TEST",
                    help="Pgbench subtests to run (default: all).")

    tp = p.add_argument_group("tpcc")
    tp.add_argument("--linear-scale", action="store_true",
                    help="TPC-C: use a linear connection list.")
    tp.add_argument("--init-point", action="store_true",
                    help="TPC-C: re-init cluster before each measurement point.")
    tp.add_argument("--warehouses", nargs="+", type=common.positive_int,
                    metavar="N", help="TPC-C: explicit warehouses list.")
    tp.add_argument("--tpcc-conns", nargs="+", type=common.positive_int,
                    metavar="N", help="TPC-C: explicit connection list "
                                      "(overrides --linear-scale).")
    tp.add_argument("--extended-logging", action="store_true",
                    help="Collect per-second psutil + pg_stat_activity samples "
                         "to a JSONL file (applied to all test types).")

    ib = p.add_argument_group("ibench")
    ib.add_argument("--ibench-scale-mul", type=common.positive_int,
                    help="Ibench: scale multiplier (default 100, or 1 if --fast-run).")
    ib.add_argument("--ibench-path", type=Path,
                    default=script_dir / "mdcallag-tools" / "bench" / "ibench" / "iibench.py",
                    help="Path to mdcallag iibench.py.")
    ib.add_argument("--ibench-conns", type=common.positive_int, default=20,
                    help="Ibench: number of parallel workers per phase.")

    return p


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def preflight(args: argparse.Namespace) -> None:
    pf = Preflight()

    if not args.oriole_id and not args.pg_id:
        pf.err("At least one of --oriole-id / --pg-id must be set.")

    required_bins = ["git", "make", "sudo", "chmod", "rm", "mkdir"]
    for b in required_bins:
        pf.require_binary(b)
    if args.nvme:
        for b in ("parted", "mkfs.ext4", "mount"):
            pf.require_binary(b)

    pf.require_writable(script_dir)

    needed_configs: set[str] = set()
    for t in args.tests:
        needed_configs.update(common.conf_files_required_for_tests.get(t, []))
    for f in sorted(needed_configs):
        pf.require_file(script_dir / f)

    for t in args.tests:
        pf.require_file(script_dir / f"test_{t}.py")
    if "ibench" in args.tests:
        pf.require_file(script_dir / "run_ibench.py")
        pf.require_file(script_dir / "report_ibench.py")

    pf.finish()
    log.info("Plan: oriole_ids=%s pg_ids=%s tests=%s reinitialize=%s reuse_data=%s",
             args.oriole_id, args.pg_id, args.tests,
             args.reinitialize, args.reuse_data)


# ---------------------------------------------------------------------------
# Build phase
# ---------------------------------------------------------------------------

def clean_workspace_full() -> None:
    log.info("--reinitialize: removing all cached source trees and builds.")
    for d in ("orioledb", "postgres-oriole", "postgres-master", "pgbin", "go-tpc"):
        remove_dir(script_dir / d)


def ensure_orioledb_prerequisites(args: argparse.Namespace) -> None:
    """Run orioledb's CI prerequisites.sh. Idempotent."""
    prereq = script_dir / "orioledb" / "ci" / "prerequisites.sh"
    if not prereq.is_file():
        raise BenchError(f"orioledb prerequisites script missing: {prereq}")
    env = {
        "COMPILER": "clang",
        "LLVM_VER": "17",
        "CC": "clang-17",
        "CHECK_TYPE": "normal",
        "GITHUB_ENV": "tmp",
    }
    # Invoke via `bash` so we don't have to chmod +x the upstream file
    # (which would otherwise be seen as a dirty working-tree change).
    run(["bash", str(prereq)], cwd=script_dir, env=env)


def read_pg_patchset_for_oriole(orioledb_dir: Path) -> str:
    pgtags = orioledb_dir / ".pgtags"
    if not pgtags.is_file():
        raise BenchError(f"Cannot find {pgtags} for current orioledb checkout.")
    with open(pgtags) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            tokens = line.split()
            if tokens and "17" in tokens[0]:
                return " ".join(tokens[1:]).strip()
    raise BenchError(f"No PG17 patchset entry found in {pgtags}")


def build_orioledb(oriole_id: str, *, force: bool) -> None:
    workspace = script_dir / "pgbin" / oriole_id
    if pg_build_exists(workspace) and not force:
        log.info("Reusing PG/orioledb build for %s at %s", oriole_id, workspace)
        return

    log.info("=== Building OrioleDB stack for %s ===", oriole_id)
    ensure_dir(workspace)
    bin_dir = workspace / "bin"

    orioledb_dir = script_dir / "orioledb"
    pg_oriole_dir = script_dir / "postgres-oriole"

    run(["git", "checkout", oriole_id], cwd=orioledb_dir)
    patchset = read_pg_patchset_for_oriole(orioledb_dir)
    log.info("checkout patchset: %s", patchset)
    run(["git", "checkout", *patchset.split()], cwd=pg_oriole_dir)

    overlay_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(workspace),
    }
    nproc = str(cpu_count())

    run(["./configure", "--enable-debug", "--disable-cassert",
         "--enable-tap-tests", "--with-icu",
         f"--prefix={workspace}", "CFLAGS=-O3"],
        cwd=pg_oriole_dir, env=overlay_env)
    run(["make", "-j", nproc, "-s"], cwd=pg_oriole_dir, env=overlay_env)
    run(["make", "-j", nproc, "-s", "install"], cwd=pg_oriole_dir, env=overlay_env)
    run(["make", "-C", "src/bin/pgbench", "-j", nproc, "-s"],
        cwd=pg_oriole_dir, env=overlay_env)
    run(["make", "-C", "src/bin/pgbench", "-j", nproc, "-s", "install"],
        cwd=pg_oriole_dir, env=overlay_env)

    run(["make", "-j", nproc, "USE_PGXS=1", "IS_DEV=1"],
        cwd=orioledb_dir, env=overlay_env)
    run(["make", "-j", nproc, "USE_PGXS=1", "IS_DEV=1", "install"],
        cwd=orioledb_dir, env=overlay_env)
    run(["make", "-j", nproc, "USE_PGXS=1", "IS_DEV=1", "clean"],
        cwd=orioledb_dir, env=overlay_env)


def build_pg_master(pg_id: str, *, force: bool) -> None:
    workspace = script_dir / "pgbin" / pg_id
    if pg_build_exists(workspace) and not force:
        log.info("Reusing PG master build for %s at %s", pg_id, workspace)
        return

    log.info("=== Building Postgres master %s ===", pg_id)
    ensure_dir(workspace)
    bin_dir = workspace / "bin"
    pg_dir = script_dir / "postgres-master"

    overlay_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(workspace),
    }

    run(["git", "checkout", pg_id], cwd=pg_dir)
    nproc = str(cpu_count())
    run(["./configure", "--enable-debug", "--disable-cassert",
         "--enable-tap-tests", "--with-icu",
         f"--prefix={workspace}", "CFLAGS=-O3"],
        cwd=pg_dir, env=overlay_env)
    run(["make", "-j", nproc, "-s"], cwd=pg_dir, env=overlay_env)
    run(["make", "-j", nproc, "-s", "install"], cwd=pg_dir, env=overlay_env)
    run(["make", "-C", "contrib", "-j", nproc, "-s"], cwd=pg_dir, env=overlay_env)
    run(["make", "-C", "contrib", "-j", nproc, "-s", "install"],
        cwd=pg_dir, env=overlay_env)


def build_phase(args: argparse.Namespace) -> None:
    log.info("==== BUILD PHASE ====")
    if args.reinitialize:
        clean_workspace_full()

    if args.oriole_id:
        repo_clone_or_fetch(oriole_repo, script_dir / "orioledb")
        repo_clone_or_fetch(postgres_oriole_repo, script_dir / "postgres-oriole")
        ensure_orioledb_prerequisites(args)
        for oid in args.oriole_id:
            build_orioledb(oid, force=args.reinitialize)

    if args.pg_id:
        repo_clone_or_fetch(postgres_master_repo, script_dir / "postgres-master")
        for pgid in args.pg_id:
            build_pg_master(pgid, force=args.reinitialize)


# ---------------------------------------------------------------------------
# Tools setup (go, go-tpc, NVMe, /ssd)
# ---------------------------------------------------------------------------

def install_go(*, force: bool) -> None:
    if not force and (Path("/usr/local/go/bin/go").is_file() or
                      common.script_dir.joinpath("go-tpc/bin/go-tpc").is_file()):
        log.info("Reusing existing Go installation.")
    else:
        log.info("Installing Go %s", go_version)
        tarball = script_dir / go_tarball
        run(["wget", "-q", go_url, "-O", str(tarball)], cwd=script_dir)
        run(["sudo", "rm", "-rf", "/usr/local/go"])
        run(["sudo", "tar", "-C", "/usr/local", "-xzf", str(tarball)])
        try:
            tarball.unlink()
        except FileNotFoundError:
            pass

    profile = Path.home() / ".profile"
    snippet_marker = "/usr/local/go/bin"
    if profile.is_file() and snippet_marker in profile.read_text():
        log.info("Go PATH already set in %s", profile)
    else:
        log.info("Setting up Go PATH in %s", profile)
        with open(profile, "a") as f:
            f.write("\nexport PATH=$PATH:/usr/local/go/bin\n")
            f.write("export GOPATH=$HOME/go\n")

    if "/usr/local/go/bin" not in os.environ.get("PATH", ""):
        os.environ["PATH"] = os.environ["PATH"] + ":/usr/local/go/bin"


def build_go_tpc(*, force: bool) -> None:
    go_tpc_dir = script_dir / "go-tpc"
    binary = go_tpc_dir / "bin" / "go-tpc"
    if binary.is_file() and not force:
        log.info("Reusing existing go-tpc binary at %s", binary)
        return

    if force or not (go_tpc_dir / ".git").exists():
        remove_dir(go_tpc_dir)
        run(["git", "clone", go_tpc_repo], cwd=script_dir)
    else:
        run(["git", "fetch", "--all", "--tags", "--prune"], cwd=go_tpc_dir)

    ldflags = (
        f'-X "main.version={go_tpc_version}" '
        f'-X "main.commit={go_tpc_commit}" '
        f'-X "main.date={go_tpc_date}"'
    )
    env = {
        "GO15VENDOREXPERIMENT": "1",
        "CGO_ENABLED": "0",
        "GOARCH": "arm64",
        "GO111MODULE": "on",
    }
    run(["go", "build", "-ldflags", ldflags, "-o", "./bin/go-tpc",
         "cmd/go-tpc/"],
        cwd=go_tpc_dir, env=env)
    if not binary.is_file():
        raise BenchError(f"go-tpc binary not produced at {binary}")


def mount_nvme() -> None:
    log.info("Mounting NVME volume...")
    proc = run(["sudo", "parted", "-l", "-m"], capture=True, allow_fail=True)
    if proc.returncode != 0:
        raise BenchError("Failed to query disks via parted; try without --nvme.")

    nvme_vol: str | None = None
    for line in (proc.stdout or "").splitlines():
        if "Amazon EC2 NVMe Instance Storage" in line:
            nvme_vol = line.split(":", 1)[0]
            break
    if not nvme_vol:
        raise BenchError(
            "NVME volume not found in 'parted -l -m'. Try without --nvme."
        )
    log.info("NVME device: %s", nvme_vol)

    run(["sudo", "parted", nvme_vol, "mklabel", "gpt", "-s"])
    run(["sudo", "parted", nvme_vol, "mkpart", "ext4", "0%", "100%", "-s"])
    partition = f"{nvme_vol}p1"
    run(["sudo", "mkfs.ext4", partition])
    run(["sudo", "mount", "-t", "ext4", "-o", "defaults,nocheck", partition, "/ssd"])
    run(["sudo", "chmod", "0777", "/ssd"])
    log.info("Successfully mounted NVME volume.")


def setup_test_environment(args: argparse.Namespace) -> None:
    log.info("==== PREPARE TESTS PHASE ====")
    install_go(force=args.reinitialize)
    build_go_tpc(force=args.reinitialize)

    run(["sudo", "mkdir", "-p", str(args.pgdata_base)])
    if args.nvme:
        mount_nvme()
    run(["sudo", "chmod", "0777", str(args.pgdata_base)])

    run(["sudo", "mkdir", "-p", str(args.results_dir)])
    run(["sudo", "chmod", "0777", str(args.results_dir)])


# ---------------------------------------------------------------------------
# Test phase
# ---------------------------------------------------------------------------

def child_args_for(test_name: str, *, args: argparse.Namespace,
                   engine: str, patch_id: str) -> list[str]:
    cli: list[str] = [
        "--patch-id", patch_id,
        "--engine", engine,
        "--pgdata-base", str(args.pgdata_base),
        "--results-dir", str(args.results_dir),
    ]
    if args.memory_buffers is not None:
        cli += ["--memory-buffers", args.memory_buffers]
    if args.fast_run:
        cli.append("--fast-run")
    if args.reuse_data:
        cli.append("--reuse-data")
    if args.extended_logging:
        cli.append("--extended-logging")

    if test_name == "pgbench":
        if args.precise_pgbench:
            cli.append("--precise")
        if args.pgbench_conns:
            cli += ["--conns", *(str(c) for c in args.pgbench_conns)]
        if args.pgbench_tests:
            cli += ["--subtests", *args.pgbench_tests]
    elif test_name == "tpcc":
        if args.linear_scale:
            cli.append("--linear-scale")
        if args.init_point:
            cli.append("--init-point")
        if args.warehouses:
            cli += ["--warehouses", *(str(w) for w in args.warehouses)]
        if args.tpcc_conns:
            cli += ["--conns", *(str(c) for c in args.tpcc_conns)]
    elif test_name == "ibench":
        if args.ibench_scale_mul is not None:
            cli += ["--scale-mul", str(args.ibench_scale_mul)]
        if args.ibench_path is not None:
            cli += ["--ibench-path", str(args.ibench_path)]
        if args.ibench_conns is not None:
            cli += ["--conns", str(args.ibench_conns)]
    return cli


def run_test(test_name: str, *, args: argparse.Namespace,
             engine: str, patch_id: str, prefix_path: Path) -> None:
    test_script = script_dir / f"test_{test_name}.py"
    if not test_script.is_file():
        raise BenchError(f"Test script missing: {test_script}")

    overlay_env = {
        "PATH": f"{prefix_path / 'bin'}:{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(prefix_path),
    }
    cli = [sys.executable, str(test_script)] + child_args_for(
        test_name, args=args, engine=engine, patch_id=patch_id,
    )
    run(cli, env=overlay_env)


def test_phase(args: argparse.Namespace) -> None:
    log.info("==== TEST PHASE ====")
    if args.fast_run:
        log.info("FAST RUN mode is on")

    for oid in args.oriole_id:
        prefix = script_dir / "pgbin" / oid
        if not pg_build_exists(prefix):
            raise BenchError(f"Missing PG binary build: {prefix}")
        for t in args.tests:
            log.info("RUN OrioleDB %s test_%s.py", oid, t)
            run_test(t, args=args, engine="orioledb",
                     patch_id=oid, prefix_path=prefix)

    for pgid in args.pg_id:
        prefix = script_dir / "pgbin" / pgid
        if not pg_build_exists(prefix):
            raise BenchError(f"Missing PG binary build: {prefix}")
        for t in args.tests:
            log.info("RUN heap %s test_%s.py", pgid, t)
            run_test(t, args=args, engine="heap",
                     patch_id=pgid, prefix_path=prefix)

    log.info("Oriole-bench tests finished")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    check_linux()
    preflight(args)

    try:
        if not args.skip_bootstrap:
            bootstrap_system()
        else:
            log.info("--skip-bootstrap: not installing apt/pip packages.")
        build_phase(args)
        setup_test_environment(args)
        test_phase(args)
    except BenchError as e:
        log.error("Bench error: %s", e)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted by user")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
