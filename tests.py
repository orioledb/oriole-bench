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
import platform
import shutil
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
    log_router,
    pg_build_exists,
    remove_dir,
    repo_clone_or_fetch,
    run,
    script_dir,
    stage,
    valid_tests,
)


go_version = "1.25.0"

oriole_repo = "https://github.com/orioledb/orioledb"
postgres_oriole_repo = "https://github.com/orioledb/postgres"
postgres_master_repo = "https://github.com/postgres/postgres.git"

go_tpc_repo = "https://github.com/akorotkov/go-tpc.git"
go_tpc_ref = "master"
go_tpc_version_string = "master"
go_tpc_commit = "89aa038"
go_tpc_date = "2026-05-12"

hammerdb_version = "4.12"

benchbase_repo = "https://github.com/cmu-db/benchbase.git"
benchbase_ref = "main"
benchbase_jdk_apt = "openjdk-21-jdk"

# Map the user-facing --compiler choice to the CC value passed to configure.
# clang-17 is what orioledb/ci/prerequisites.sh installs on Ubuntu.
compiler_cc = {
    "clang": "clang-17",
    "gcc":   "gcc",
}
valid_compilers = tuple(compiler_cc.keys())


def build_id(kind: str, ref: str, compiler: str) -> str:
    """Identifier for one (kind, ref, compiler) build — used as patch_id."""
    return f"{kind}-{common._sanitize_log_name(ref)}-{compiler}"


def data_id(kind: str, ref: str) -> str:
    """
    Identifier for the data the build runs against — just the sanitised
    git ref. Compiler-agnostic so clang/gcc share a PGDATA; engine is
    already part of the path so we don't repeat the kind here either
    (e.g. orioledb engine + 'orioledb-' prefix → 'orioledb-orioledb-…').
    """
    del kind  # kept in signature for symmetry with build_id()
    return common._sanitize_log_name(ref)
hammerdb_binary_url = (
    f"https://github.com/TPC-Council/HammerDB/releases/download/"
    f"v{hammerdb_version}/HammerDB-{hammerdb_version}-Linux.tar.gz"
)


def detect_go_arch() -> str:
    """Map the running machine to a Go-style GOARCH name."""
    m = platform.machine().lower()
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "amd64"
    raise BenchError(f"Unsupported architecture for Go binary download: {m}")


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
        "--compiler", nargs="+", default=["clang"], choices=valid_compilers,
        help="C compiler(s) to build PG with. Each (ref, compiler) pair "
             "becomes its own build under pgbin/<kind>-<ref>-<compiler>.",
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
        "--undo-buffers", default="1GB",
        help="orioledb.undo_buffers value (orioledb engine only).",
    )
    p.add_argument(
        "--fsync", choices=("on", "off"), default="off",
        help="postgresql.conf 'fsync' value.",
    )
    p.add_argument(
        "--synchronous-commit", choices=("on", "off"), default="off",
        help="postgresql.conf 'synchronous_commit' value.",
    )
    p.add_argument(
        "--pg-stat-statements", action="store_true",
        help="Enable pg_stat_statements during the run and dump a "
             "top-50 report alongside each test's result file.",
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
    pg.add_argument("--pgbench-scale", type=common.positive_int, default=1000,
                    help="Pgbench scale factor (-s) for init and run.")

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
    tp.add_argument("--tpcc-stored-procs", action="store_true",
                    help="TPC-C (go-tpc): dispatch transactions as PL/pgSQL "
                         "stored procedures (postgres driver only).")

    ib = p.add_argument_group("ibench")
    ib.add_argument("--ibench-scale-mul", type=common.positive_int,
                    help="Ibench: scale multiplier (default 100, or 1 if --fast-run).")
    ib.add_argument("--ibench-path", type=Path,
                    default=script_dir / "mdcallag-tools" / "bench" / "ibench" / "iibench.py",
                    help="Path to mdcallag iibench.py.")
    ib.add_argument("--ibench-conns", type=common.positive_int, default=20,
                    help="Ibench: number of parallel workers per phase.")

    hdb = p.add_argument_group("tpcc_hdb (HammerDB stored-procedure TPC-C)")
    hdb.add_argument("--hdb-rampup-min", type=common.positive_int, default=2,
                     help="HammerDB: rampup time in minutes (ignored on --fast-run).")
    hdb.add_argument("--hdb-duration-min", type=common.positive_int, default=5,
                     help="HammerDB: measurement duration in minutes "
                          "(ignored on --fast-run).")
    hdb.add_argument("--hdb-build-vu", type=common.positive_int, default=16,
                     help="HammerDB: virtual users used for schema BUILD.")

    bb = p.add_argument_group("tpcc_bb (BenchBase TPC-C)")
    bb.add_argument("--bb-rampup-min", type=common.positive_int, default=1,
                    help="BenchBase: warmup time in minutes (ignored on "
                         "--fast-run).")
    bb.add_argument("--bb-duration-min", type=common.positive_int, default=3,
                    help="BenchBase: measurement duration in minutes "
                         "(ignored on --fast-run).")

    return p


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def preflight(args: argparse.Namespace) -> None:
    pf = Preflight()

    if not args.oriole_id and not args.pg_id:
        pf.err("At least one of --oriole-id / --pg-id must be set.")

    # `sudo` is invoked by every code path (bootstrap, /ssd setup, NVMe
    # mount), so it has to exist before we do anything.
    pf.require_binary("sudo")

    # Bootstrap installs git/make/wget/tar/python3/pip3/parted/etc. via apt.
    # When the user opts out with --skip-bootstrap, validate up front;
    # otherwise trust bootstrap to provide them.
    if args.skip_bootstrap:
        bootstrap_bins = ["git", "make", "wget", "tar",
                          "python3", "pip3"]
        for b in bootstrap_bins:
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
    with stage("clean workspace"):
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


def build_orioledb(ref: str, compiler: str, bid: str, *, force: bool) -> None:
    workspace = script_dir / "pgbin" / bid
    if pg_build_exists(workspace) and not force:
        log.info("Reusing PG/orioledb build %s at %s", bid, workspace)
        return

    cc = compiler_cc[compiler]
    with stage(f"build {bid}"):
        ensure_dir(workspace)
        bin_dir = workspace / "bin"

        orioledb_dir = script_dir / "orioledb"
        pg_oriole_dir = script_dir / "postgres-oriole"

        run(["git", "checkout", ref], cwd=orioledb_dir)
        patchset = read_pg_patchset_for_oriole(orioledb_dir)
        run(["git", "checkout", *patchset.split()], cwd=pg_oriole_dir)
        # Wipe build artifacts left by previous configurations — pg_config.h
        # changes from a fresh ./configure don't always propagate through
        # make's incremental rebuild, leading to .o files compiled against
        # the old config (notably pg_crc32c.o missing the runtime-check
        # function pointer). Cheaper than chasing partial-rebuild bugs.
        run(["git", "clean", "-fdx"], cwd=pg_oriole_dir)
        run(["git", "clean", "-fdx"], cwd=orioledb_dir)

        overlay_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_WORKSPACE": str(workspace),
        }
        nproc = str(cpu_count())

        run(["./configure", "--enable-debug", "--disable-cassert",
             "--enable-tap-tests", "--with-icu",
             f"--prefix={workspace}", f"CC={cc}", "CFLAGS=-O3"],
            cwd=pg_oriole_dir, env=overlay_env)
        run(["make", "-j", nproc, "-s"], cwd=pg_oriole_dir, env=overlay_env)
        run(["make", "-j", nproc, "-s", "install"], cwd=pg_oriole_dir, env=overlay_env)
        run(["make", "-C", "src/bin/pgbench", "-j", nproc, "-s"],
            cwd=pg_oriole_dir, env=overlay_env)
        run(["make", "-C", "src/bin/pgbench", "-j", nproc, "-s", "install"],
            cwd=pg_oriole_dir, env=overlay_env)

        # contrib provides pg_stat_statements (and friends). Built the same
        # way as in build_pg_master so the same extensions are available
        # regardless of which engine we test against.
        run(["make", "-C", "contrib", "-j", nproc, "-s"],
            cwd=pg_oriole_dir, env=overlay_env)
        run(["make", "-C", "contrib", "-j", nproc, "-s", "install"],
            cwd=pg_oriole_dir, env=overlay_env)

        run(["make", "-j", nproc, "USE_PGXS=1", "IS_DEV=1"],
            cwd=orioledb_dir, env=overlay_env)
        run(["make", "-j", nproc, "USE_PGXS=1", "IS_DEV=1", "install"],
            cwd=orioledb_dir, env=overlay_env)
        run(["make", "-j", nproc, "USE_PGXS=1", "IS_DEV=1", "clean"],
            cwd=orioledb_dir, env=overlay_env)


def build_pg_master(ref: str, compiler: str, bid: str, *, force: bool) -> None:
    workspace = script_dir / "pgbin" / bid
    if pg_build_exists(workspace) and not force:
        log.info("Reusing PG master build %s at %s", bid, workspace)
        return

    cc = compiler_cc[compiler]
    with stage(f"build {bid}"):
        ensure_dir(workspace)
        bin_dir = workspace / "bin"
        pg_dir = script_dir / "postgres-master"

        overlay_env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_WORKSPACE": str(workspace),
        }

        run(["git", "checkout", ref], cwd=pg_dir)
        # See build_orioledb() for the reasoning behind the unconditional
        # tree wipe — stale .o files from a previous configure can survive
        # incremental rebuilds and break extension dlopen.
        run(["git", "clean", "-fdx"], cwd=pg_dir)
        nproc = str(cpu_count())
        run(["./configure", "--enable-debug", "--disable-cassert",
             "--enable-tap-tests", "--with-icu",
             f"--prefix={workspace}", f"CC={cc}", "CFLAGS=-O3"],
            cwd=pg_dir, env=overlay_env)
        run(["make", "-j", nproc, "-s"], cwd=pg_dir, env=overlay_env)
        run(["make", "-j", nproc, "-s", "install"], cwd=pg_dir, env=overlay_env)
        run(["make", "-C", "contrib", "-j", nproc, "-s"], cwd=pg_dir, env=overlay_env)
        run(["make", "-C", "contrib", "-j", nproc, "-s", "install"],
            cwd=pg_dir, env=overlay_env)


def oriole_builds(args: argparse.Namespace) -> list[tuple[str, str, str, str]]:
    """
    Cartesian product of --oriole-id × --compiler, each as
    (ref, compiler, build_id, data_id).
    """
    return [
        (ref, c, build_id("orioledb", ref, c), data_id("orioledb", ref))
        for ref in args.oriole_id for c in args.compiler
    ]


def pg_builds(args: argparse.Namespace) -> list[tuple[str, str, str, str]]:
    """Cartesian product of --pg-id × --compiler."""
    return [
        (ref, c, build_id("pg", ref, c), data_id("pg", ref))
        for ref in args.pg_id for c in args.compiler
    ]


def build_phase(args: argparse.Namespace) -> None:
    with stage("build"):
        if args.reinitialize:
            clean_workspace_full()

        if args.oriole_id:
            with stage("clone sources (orioledb)"):
                repo_clone_or_fetch(oriole_repo, script_dir / "orioledb")
                repo_clone_or_fetch(postgres_oriole_repo, script_dir / "postgres-oriole")
            with stage("orioledb prerequisites"):
                ensure_orioledb_prerequisites(args)
            for ref, compiler, bid, _did in oriole_builds(args):
                build_orioledb(ref, compiler, bid, force=args.reinitialize)

        if args.pg_id:
            with stage("clone sources (pg master)"):
                repo_clone_or_fetch(postgres_master_repo, script_dir / "postgres-master")
            for ref, compiler, bid, _did in pg_builds(args):
                build_pg_master(ref, compiler, bid, force=args.reinitialize)


# ---------------------------------------------------------------------------
# Tools setup (go, go-tpc, NVMe, /ssd)
# ---------------------------------------------------------------------------

def install_go(*, force: bool) -> None:
    go_arch = detect_go_arch()
    tarball_name = f"go{go_version}.linux-{go_arch}.tar.gz"
    go_url = f"https://dl.google.com/go/{tarball_name}"

    if not force and Path("/usr/local/go/bin/go").is_file():
        log.info("Reusing existing Go installation at /usr/local/go.")
    else:
        with stage("install go"):
            tarball = script_dir / tarball_name
            run(["wget", "-q", go_url, "-O", str(tarball)], cwd=script_dir)
            run(["sudo", "rm", "-rf", "/usr/local/go"])
            run(["sudo", "tar", "-C", "/usr/local", "-xzf", str(tarball)])
            try:
                tarball.unlink()
            except FileNotFoundError:
                pass

    profile = Path.home() / ".profile"
    snippet_marker = "/usr/local/go/bin"
    if not (profile.is_file() and snippet_marker in profile.read_text()):
        with open(profile, "a") as f:
            f.write("\nexport PATH=$PATH:/usr/local/go/bin:$HOME/go/bin\n")
            f.write("export GOPATH=$HOME/go\n")

    gopath = str(Path.home() / "go")
    os.environ.setdefault("GOPATH", gopath)
    gobin = f"{gopath}/bin"
    path = os.environ.get("PATH", "")
    extras = [p for p in ("/usr/local/go/bin", gobin) if p not in path]
    if extras:
        os.environ["PATH"] = path + ":" + ":".join(extras)


def install_hammerdb(*, force: bool, needed: bool) -> None:
    """
    Make sure ./hammerdb is a runnable HammerDB tree.

    Only amd64 is supported for now — HammerDB ships an x86_64 binary tarball
    and the source build pipeline (Bawt + bundled tclkit-Linux64) is also
    x86_64-only. On arm64 we error out clearly; arm64 support would require
    either qemu-user-static emulation or a hand-rolled system-tclsh setup
    (build Pgtcl from source, vendor a missing ticklecharts module, etc.) —
    not worth the complexity for now.
    """
    if not needed:
        return

    target = script_dir / "hammerdb"
    if not force and (target / "hammerdbcli").is_file():
        log.info("Reusing existing HammerDB at %s", target)
        return

    arch = detect_go_arch()
    if arch != "amd64":
        raise BenchError(
            f"HammerDB integration currently supports amd64 only "
            f"(detected: {arch}). Run --tests tpcc_hdb on an x86_64 host, "
            f"or drop tpcc_hdb from --tests."
        )

    with stage(f"install hammerdb {hammerdb_version}"):
        tarball_name = f"HammerDB-{hammerdb_version}-Linux.tar.gz"
        tarball = script_dir / tarball_name
        run(["wget", "-q", hammerdb_binary_url, "-O", str(tarball)],
            cwd=script_dir)
        if target.exists():
            remove_dir(target)
        run(["tar", "-xzf", str(tarball), "-C", str(script_dir)])
        extracted = script_dir / f"HammerDB-{hammerdb_version}"
        if not extracted.is_dir():
            raise BenchError(
                f"After extraction, expected {extracted} — tarball layout "
                f"changed?"
            )
        extracted.rename(target)
        try:
            tarball.unlink()
        except FileNotFoundError:
            pass


def build_go_tpc(*, force: bool) -> None:
    go_tpc_dir = script_dir / "go-tpc"
    binary = go_tpc_dir / "bin" / "go-tpc"
    if binary.is_file() and not force:
        log.info("Reusing existing go-tpc binary at %s", binary)
        return

    with stage("build go-tpc"):
        if force or not (go_tpc_dir / ".git").exists():
            remove_dir(go_tpc_dir)
            run(["git", "clone", go_tpc_repo], cwd=script_dir)
        else:
            run(["git", "fetch", "--all", "--tags", "--prune"], cwd=go_tpc_dir)

        # Lock to the branch/tag we want to advertise via ldflags so the
        # source matches the build info.
        run(["git", "checkout", go_tpc_ref], cwd=go_tpc_dir)
        run(["git", "pull", "--ff-only"], cwd=go_tpc_dir, allow_fail=True)

        go_arch = detect_go_arch()
        ldflags = (
            f'-X "main.version={go_tpc_version_string}" '
            f'-X "main.commit={go_tpc_commit}" '
            f'-X "main.date={go_tpc_date}"'
        )
        env = {
            "CGO_ENABLED": "0",
            "GOARCH": go_arch,
            "GO111MODULE": "on",
            # The fork imports encoding/json/v2 from Go 1.25, which needs the
            # jsonv2 experiment toggled on at compile time.
            "GOEXPERIMENT": "jsonv2",
            # Use the public Go module proxy and skip checksum DB to avoid
            # network/policy surprises on CI machines.
            "GOPROXY": "https://proxy.golang.org,direct",
            "GOSUMDB": "off",
        }
        run(["go", "mod", "download"], cwd=go_tpc_dir, env=env)
        # `./cmd/go-tpc` (with the `./` prefix) makes Go treat it as a local
        # package path rather than a stdlib import — otherwise newer Go
        # toolchains may misinterpret it as `cmd/go-tpc` from std.
        run(["go", "build", "-ldflags", ldflags, "-o", "./bin/go-tpc",
             "./cmd/go-tpc"],
            cwd=go_tpc_dir, env=env)
        if not binary.is_file():
            raise BenchError(f"go-tpc binary not produced at {binary}")


def install_jdk(*, needed: bool) -> None:
    """Install OpenJDK 21 (BenchBase needs Java 21+). No-op if `java` is
    already on PATH — we trust whatever the user has."""
    if not needed:
        return
    if shutil.which("java") is not None:
        log.info("Reusing existing Java toolchain (java in PATH).")
        return
    with stage(f"install {benchbase_jdk_apt}"):
        common.apt_install([benchbase_jdk_apt])


def build_benchbase(*, force: bool, needed: bool) -> None:
    """
    Clone + build BenchBase, extract benchbase-postgres.tgz to ./benchbase/.

    The Maven build downloads ~hundreds of MB of plugins the first time, so
    it's gated by `force` like every other tools build. Layout after this:
        benchbase-src/   — git checkout, Maven build tree
        benchbase/       — extracted distribution, contains benchbase.jar
    """
    if not needed:
        return

    src_dir = script_dir / "benchbase-src"
    dist_dir = script_dir / "benchbase"
    if (dist_dir / "benchbase.jar").is_file() and not force:
        log.info("Reusing existing BenchBase at %s", dist_dir)
        return

    with stage("build benchbase"):
        if force or not (src_dir / ".git").exists():
            remove_dir(src_dir)
            run(["git", "clone", benchbase_repo, str(src_dir)],
                cwd=script_dir)
        else:
            run(["git", "fetch", "--all", "--tags", "--prune"], cwd=src_dir)
        run(["git", "checkout", benchbase_ref], cwd=src_dir)
        run(["git", "pull", "--ff-only"], cwd=src_dir, allow_fail=True)

        # mvnw is a Maven wrapper bundled in the repo; uses ./mvnw via bash
        # so we don't have to chmod +x it (the file may be 644 on a fresh
        # checkout, and chmod would show up as a dirty working tree).
        run(["bash", "./mvnw", "-P", "postgres", "clean", "package",
             "-DskipTests"], cwd=src_dir)

        tgz = src_dir / "target" / "benchbase-postgres.tgz"
        if not tgz.is_file():
            raise BenchError(
                f"BenchBase build finished but {tgz} was not produced — "
                f"check the Maven log."
            )
        remove_dir(dist_dir)
        ensure_dir(dist_dir)
        run(["tar", "-xzf", str(tgz), "--strip-components=1",
             "-C", str(dist_dir)])
        if not (dist_dir / "benchbase.jar").is_file():
            raise BenchError(
                f"After extraction, benchbase.jar not found in {dist_dir}"
            )


def mount_nvme() -> None:
    if os.path.ismount("/ssd"):
        log.info("Reusing existing /ssd mount (skipping NVMe format).")
        return

    with stage("mount nvme"):
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

        run(["sudo", "parted", nvme_vol, "mklabel", "gpt", "-s"])
        run(["sudo", "parted", nvme_vol, "mkpart", "ext4", "0%", "100%", "-s"])
        partition = f"{nvme_vol}p1"
        run(["sudo", "mkfs.ext4", partition])
        run(["sudo", "mount", "-t", "ext4", "-o", "defaults,nocheck", partition, "/ssd"])
        run(["sudo", "chmod", "0777", "/ssd"])


def setup_test_environment(args: argparse.Namespace) -> None:
    with stage("prepare environment"):
        # tpcc_pgb also uses go-tpc, but only for the data-load step
        # (`go-tpc tpcc prepare`); pgbench drives the actual measurement.
        need_go_tpc = "tpcc" in args.tests or "tpcc_pgb" in args.tests
        need_hammerdb = "tpcc_hdb" in args.tests
        need_benchbase = "tpcc_bb" in args.tests
        if need_go_tpc:
            install_go(force=args.reinitialize)
            build_go_tpc(force=args.reinitialize)
        install_hammerdb(force=args.reinitialize, needed=need_hammerdb)
        install_jdk(needed=need_benchbase)
        build_benchbase(force=args.reinitialize, needed=need_benchbase)

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
                   engine: str, patch_id: str, data_id: str) -> list[str]:
    cli: list[str] = [
        "--patch-id", patch_id,
        "--data-id", data_id,
        "--engine", engine,
        "--pgdata-base", str(args.pgdata_base),
        "--results-dir", str(args.results_dir),
    ]
    if args.memory_buffers is not None:
        cli += ["--memory-buffers", args.memory_buffers]
    if args.undo_buffers is not None:
        cli += ["--undo-buffers", args.undo_buffers]
    cli += ["--fsync", args.fsync,
            "--synchronous-commit", args.synchronous_commit]
    if args.pg_stat_statements:
        cli.append("--pg-stat-statements")
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
        cli += ["--scale", str(args.pgbench_scale)]
    elif test_name == "tpcc":
        if args.linear_scale:
            cli.append("--linear-scale")
        if args.init_point:
            cli.append("--init-point")
        if args.warehouses:
            cli += ["--warehouses", *(str(w) for w in args.warehouses)]
        if args.tpcc_conns:
            cli += ["--conns", *(str(c) for c in args.tpcc_conns)]
        if args.tpcc_stored_procs:
            cli.append("--stored-procs")
    elif test_name == "tpcc_pgb":
        if args.linear_scale:
            cli.append("--linear-scale")
        if args.init_point:
            cli.append("--init-point")
        if args.warehouses:
            cli += ["--warehouses", *(str(w) for w in args.warehouses)]
        if args.tpcc_conns:
            cli += ["--conns", *(str(c) for c in args.tpcc_conns)]
    elif test_name == "tpcc_hdb":
        if args.warehouses:
            cli += ["--warehouses", *(str(w) for w in args.warehouses)]
        if args.tpcc_conns:
            cli += ["--vu", *(str(c) for c in args.tpcc_conns)]
        cli += [
            "--hammerdb", str(script_dir / "hammerdb"),
            "--rampup-min", str(args.hdb_rampup_min),
            "--duration-min", str(args.hdb_duration_min),
            "--build-vu", str(args.hdb_build_vu),
        ]
    elif test_name == "tpcc_bb":
        if args.warehouses:
            cli += ["--warehouses", *(str(w) for w in args.warehouses)]
        if args.tpcc_conns:
            cli += ["--terminals", *(str(c) for c in args.tpcc_conns)]
        cli += [
            "--benchbase", str(script_dir / "benchbase"),
            "--rampup-min", str(args.bb_rampup_min),
            "--duration-min", str(args.bb_duration_min),
        ]
    elif test_name == "ibench":
        if args.ibench_scale_mul is not None:
            cli += ["--scale-mul", str(args.ibench_scale_mul)]
        if args.ibench_path is not None:
            cli += ["--ibench-path", str(args.ibench_path)]
        if args.ibench_conns is not None:
            cli += ["--conns", str(args.ibench_conns)]
    return cli


def run_test(test_name: str, *, args: argparse.Namespace,
             engine: str, patch_id: str, data_id: str,
             prefix_path: Path) -> None:
    test_script = script_dir / f"test_{test_name}.py"
    if not test_script.is_file():
        raise BenchError(f"Test script missing: {test_script}")

    # Forward our current stage depth so the child's "→ ..." lines align
    # visually with the parent's.
    overlay_env = {
        "PATH": f"{prefix_path / 'bin'}:{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(prefix_path),
        "ORIOLE_BENCH_LOG_DEPTH": str(log_router.depth()),
    }
    cli = [sys.executable, "-u", str(test_script)] + child_args_for(
        test_name, args=args, engine=engine,
        patch_id=patch_id, data_id=data_id,
    )
    # inherit_io so the child's per-measurement log lines reach the console
    # live; the child manages its own log/<...>.log files for subprocess noise.
    run(cli, env=overlay_env, inherit_io=True)


def test_phase(args: argparse.Namespace) -> None:
    with stage("tests"):
        if args.fast_run:
            log.info("FAST RUN mode is on")

        for ref, compiler, bid, did in oriole_builds(args):
            prefix = script_dir / "pgbin" / bid
            if not pg_build_exists(prefix):
                raise BenchError(f"Missing PG binary build: {prefix}")
            for t in args.tests:
                with stage(f"{t} {bid}"):
                    run_test(t, args=args, engine="orioledb",
                             patch_id=bid, data_id=did, prefix_path=prefix)

        for ref, compiler, bid, did in pg_builds(args):
            prefix = script_dir / "pgbin" / bid
            if not pg_build_exists(prefix):
                raise BenchError(f"Missing PG binary build: {prefix}")
            for t in args.tests:
                with stage(f"{t} {bid}"):
                    run_test(t, args=args, engine="heap",
                             patch_id=bid, data_id=did, prefix_path=prefix)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    check_linux()
    preflight(args)

    try:
        if not args.skip_bootstrap:
            with stage("bootstrap"):
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
