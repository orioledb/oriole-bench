"""
Common utilities for the oriole-bench Python scripts.

Centralizes subprocess execution, environment / preflight validation, paths,
parsing helpers, system bootstrap, and resource monitoring so that the test
scripts only contain the actual benchmark logic.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

script_dir = Path(__file__).resolve().parent
default_results_dir = script_dir / "results"
default_pgdata_base = Path("/ssd")
log_dir = script_dir / "log"

valid_engines = ("orioledb", "heap")
valid_tests = ("pgbench", "tpcc", "ibench")

conf_files_required_for_tests = {
    "pgbench": [
        "postgresql.auto.conf.pgbench",
        "postgresql.auto.conf.orioledb.pgbench",
        "postgresql.auto.conf.heap.pgbench",
        "orioledb-prepare-function.sql",
        "orioledb-select-9.sql",
        "orioledb-tpcb-in-procedure.sql",
    ],
    "tpcc": [
        "postgresql.auto.conf.tpcc",
        "postgresql.auto.conf.orioledb.tpcc",
        "postgresql.auto.conf.heap.tpcc",
    ],
    "ibench": [
        "postgresql.auto.conf.ibench",
        "postgresql.auto.conf.orioledb.ibench",
        "postgresql.auto.conf.heap.ibench",
    ],
}

# Packages installed by bootstrap_ubuntu() on a fresh VM. The orioledb
# prerequisites.sh covers the LLVM/clang side, so this list focuses on
# build-essentials and runtime tools needed before we can even clone+build.
apt_packages = [
    "build-essential",
    "git", "wget", "curl", "ca-certificates", "sudo",
    "parted", "e2fsprogs",
    "python3", "python3-pip", "python3-dev", "python3-venv",
    "libicu-dev", "libreadline-dev", "zlib1g-dev", "libssl-dev",
    "libxml2-dev", "libxslt1-dev",
    "flex", "bison", "pkg-config",
    "libipc-run-perl",
    "sysstat",  # iostat
    "psmisc",   # killall
]

pip_packages = [
    "psycopg2-binary",
    "psutil",
    "six",
    "testgres",
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("oriole-bench")
    if logger.handlers:
        logger.setLevel(level)
        return logger
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


log = setup_logging()


class BenchError(RuntimeError):
    """Raised on any unrecoverable benchmark error."""


def die(msg: str, code: int = 1) -> "NoReturn":  # noqa: F821 - typing only
    log.error(msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Stage / log routing
#
# Each subprocess we run sends its stdout+stderr to a "current" log file under
# log/. Top-level code wraps phases in `with stage("name"):` and the console
# only sees stage transitions (→ start, ✓ ok, ✖ failed). All the noisy output
# (apt, pip, configure, make, pgbench, go-tpc, ...) lands in log/<name>.log.
# ---------------------------------------------------------------------------

def _sanitize_log_name(name: str) -> str:
    safe = []
    for ch in name:
        safe.append(ch if (ch.isalnum() or ch in "-_.") else "-")
    cleaned = "".join(safe).strip("-")
    return cleaned or "stage"


class _LogRouter:
    """Stack of log paths. The top of the stack receives subprocess output."""

    def __init__(self) -> None:
        self._stack: list[Path] = []
        self._default = log_dir / "run.log"
        # Parent processes pass their indentation depth via env so nested
        # stage messages from child scripts line up visually.
        try:
            self._base_depth = int(os.environ.get("ORIOLE_BENCH_LOG_DEPTH", "0"))
        except ValueError:
            self._base_depth = 0

    @property
    def current(self) -> Path:
        return self._stack[-1] if self._stack else self._default

    def push(self, path: Path) -> None:
        self._stack.append(path)

    def pop(self) -> None:
        if self._stack:
            self._stack.pop()

    def depth(self) -> int:
        return self._base_depth + len(self._stack)


log_router = _LogRouter()


@contextlib.contextmanager
def stage(name: str, *, file_name: str | None = None) -> Iterator[Path]:
    """
    Wrap a logical phase. The console sees one start + one finish line; all
    subprocess output produced inside the block goes to log/<name>.log.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{_sanitize_log_name(file_name or name)}.log"

    indent = "  " * log_router.depth()
    log.info("%s→ %s", indent, name)
    started = time.monotonic()

    with open(path, "a") as f:
        f.write(f"\n{'=' * 60}\n{name}  (start: {now_str()})\n{'=' * 60}\n")

    log_router.push(path)
    try:
        yield path
    except BaseException as e:
        elapsed = time.monotonic() - started
        with open(path, "a") as f:
            f.write(f"\n!!! FAILED after {elapsed:.1f}s: {e}\n")
        log.error("%s✖ %s (%.1fs) — see %s", indent, name, elapsed, path)
        raise
    else:
        elapsed = time.monotonic() - started
        with open(path, "a") as f:
            f.write(f"\n--- OK ({elapsed:.1f}s) ---\n")
        log.info("%s✓ %s (%.1fs)", indent, name, elapsed)
    finally:
        log_router.pop()


def _append_log(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a") as f:
        f.write(text)


def _tail(text: str, lines: int = 20, max_chars: int = 2000) -> str:
    if not text:
        return ""
    out = "\n".join(text.splitlines()[-lines:])
    return out[-max_chars:]


# ---------------------------------------------------------------------------
# Subprocess execution with strict error handling
# ---------------------------------------------------------------------------

def _format_cmd(cmd: Sequence[str] | str) -> str:
    if isinstance(cmd, str):
        return cmd
    return " ".join(shlex.quote(p) for p in cmd)


def run(
    cmd: Sequence[str] | str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
    text: bool = True,
    shell: bool = False,
    input_text: str | None = None,
    timeout: float | None = None,
    allow_fail: bool = False,
    log_file: Path | None = None,
    inherit_io: bool = False,
) -> subprocess.CompletedProcess:
    """
    Run a command synchronously. stdout+stderr are redirected to the active
    stage's log file (or `log_file=` override) so the console stays clean.
    Raises BenchError on non-zero exit unless allow_fail=True.

    capture=True still returns stdout/stderr in the CompletedProcess for
    parsing — the same text is *also* appended to the log file.

    inherit_io=True passes parent's stdin/stdout/stderr through to the child
    (used when invoking nested oriole-bench scripts whose own log lines should
    reach the console).
    """
    if isinstance(cmd, str) and not shell:
        cmd_for_run: Sequence[str] | str = shlex.split(cmd)
    else:
        cmd_for_run = cmd

    target = log_file if log_file is not None else log_router.current
    cwd_str = str(cwd) if cwd is not None else None
    banner = f"$ {_format_cmd(cmd)}" + (f"  (cwd={cwd_str})" if cwd_str else "") + "\n"
    _append_log(target, banner)

    try:
        if inherit_io:
            proc = subprocess.run(
                cmd_for_run, cwd=cwd_str,
                env=({**os.environ, **env} if env is not None else None),
                check=False, text=text,
                shell=shell, input=input_text, timeout=timeout,
            )
        elif capture:
            proc = subprocess.run(
                cmd_for_run, cwd=cwd_str,
                env=({**os.environ, **env} if env is not None else None),
                check=False, capture_output=True, text=text,
                shell=shell, input=input_text, timeout=timeout,
            )
            tail_parts = []
            if proc.stdout:
                tail_parts.append(proc.stdout if proc.stdout.endswith("\n")
                                  else proc.stdout + "\n")
            if proc.stderr:
                tail_parts.append("--- stderr ---\n")
                tail_parts.append(proc.stderr if proc.stderr.endswith("\n")
                                  else proc.stderr + "\n")
            if tail_parts:
                _append_log(target, "".join(tail_parts))
        else:
            with open(target, "a") as f:
                f.flush()
                proc = subprocess.run(
                    cmd_for_run, cwd=cwd_str,
                    env=({**os.environ, **env} if env is not None else None),
                    check=False, stdout=f, stderr=subprocess.STDOUT, text=text,
                    shell=shell, input=input_text, timeout=timeout,
                )
    except FileNotFoundError as e:
        raise BenchError(
            f"Executable not found while running: {_format_cmd(cmd)} ({e})"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise BenchError(
            f"Timeout {timeout}s expired for: {_format_cmd(cmd)}\nSee log: {target}"
        ) from e
    except OSError as e:
        raise BenchError(
            f"OS error while running {_format_cmd(cmd)}: {e}\nSee log: {target}"
        ) from e

    if check and not allow_fail and proc.returncode != 0:
        snippet = ""
        if capture and proc.stderr:
            snippet = f"\n--- last stderr ---\n{_tail(proc.stderr)}"
        raise BenchError(
            f"Command failed with code {proc.returncode}: {_format_cmd(cmd)}"
            f"\nSee log: {target}{snippet}"
        )

    return proc


def run_bg(
    cmd: Sequence[str] | str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdout=None,
    stderr=None,
    shell: bool = False,
    log_file: Path | None = None,
) -> subprocess.Popen:
    """
    Start a background process. If neither stdout nor stderr is specified, the
    process inherits an exclusive log file (the stage's log by default, or
    `log_file=`). wait_all() closes the handle.
    """
    if isinstance(cmd, str) and not shell:
        cmd_for_run: Sequence[str] | str = shlex.split(cmd)
    else:
        cmd_for_run = cmd

    log_fh = None
    if stdout is None and stderr is None:
        target = log_file if log_file is not None else log_router.current
        target.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(target, "a")
        log_fh.write(f"(bg) $ {_format_cmd(cmd)}\n")
        log_fh.flush()
        stdout = log_fh
        stderr = subprocess.STDOUT
        log_path: Path | None = target
    else:
        log_path = None

    proc = subprocess.Popen(
        cmd_for_run,
        cwd=str(cwd) if cwd is not None else None,
        env=({**os.environ, **env} if env is not None else None),
        stdout=stdout,
        stderr=stderr,
        shell=shell,
        text=True,
    )
    proc._log_fh = log_fh  # type: ignore[attr-defined]
    proc._log_path = log_path  # type: ignore[attr-defined]
    return proc


def wait_all(procs: Iterable[subprocess.Popen], *, label: str = "background job") -> None:
    procs_list = list(procs)
    failures: list[tuple[int, int, Path | None]] = []
    try:
        for proc in procs_list:
            try:
                rc = proc.wait()
            finally:
                fh = getattr(proc, "_log_fh", None)
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
            if rc != 0:
                failures.append((proc.pid, rc, getattr(proc, "_log_path", None)))
    except KeyboardInterrupt:
        for p in procs_list:
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGTERM)
                except OSError:
                    pass
            fh = getattr(p, "_log_fh", None)
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        raise

    if failures:
        details = ", ".join(
            f"pid={pid}->rc={rc}" + (f" (log: {path})" if path else "")
            for pid, rc, path in failures
        )
        raise BenchError(f"One or more {label} processes failed: {details}")


def stop_pg_silent(pgdatadir: str | os.PathLike[str]) -> None:
    try:
        run(["pg_ctl", "-D", str(pgdatadir), "-l", "logfile", "stop"], allow_fail=True)
    except BenchError:
        pass


# ---------------------------------------------------------------------------
# Path / binary discovery
# ---------------------------------------------------------------------------

def assert_pg_build_in_path() -> None:
    pg_ctl = shutil.which("pg_ctl")
    if pg_ctl is None:
        die("pg_ctl is not in PATH. Make sure your custom PG bin/ is the first PATH entry.")
    if pg_ctl == "/usr/local/pgsql/bin/pg_ctl":
        die(
            "USING DEFAULT PG BINARIES. CHECK THAT bin DIRECTORY OF YOUR PATCHSET "
            "IS SET ON A FIRST POSITION IN PATH"
        )


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

@dataclass
class Preflight:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def require_binary(self, name: str) -> None:
        if shutil.which(name) is None:
            self.err(f"Required executable not found in PATH: {name}")

    def require_file(self, path: Path | str) -> None:
        p = Path(path)
        if not p.is_file():
            self.err(f"Required file is missing: {p}")

    def require_dir(self, path: Path | str) -> None:
        p = Path(path)
        if not p.is_dir():
            self.err(f"Required directory is missing: {p}")

    def require_writable(self, path: Path | str) -> None:
        p = Path(path)
        if not p.exists():
            self.err(f"Path does not exist: {p}")
            return
        if not os.access(p, os.W_OK):
            self.err(f"Path is not writable: {p}")

    def assert_engine(self, engine: str | None) -> None:
        if engine is None:
            self.err("--engine is not set; expected one of: orioledb|heap")
        elif engine not in valid_engines:
            self.err(f"--engine has unknown value '{engine}'; expected one of: {valid_engines}")

    def finish(self) -> None:
        for w in self.warnings:
            log.warning(w)
        if self.errors:
            log.error("Preflight failed with %d error(s):", len(self.errors))
            for e in self.errors:
                log.error("  - %s", e)
            sys.exit(1)
        log.info("Preflight checks passed.")


def check_linux() -> None:
    if sys.platform != "linux":
        log.warning(
            "Detected non-Linux platform (%s). The bench scripts assume Linux/AWS; "
            "some operations (parted, mount, sudo, du --apparent-size) will fail.",
            sys.platform,
        )


# ---------------------------------------------------------------------------
# Argparse helpers
# ---------------------------------------------------------------------------

def add_common_test_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--patch-id", required=True,
        help="Commit hash / tag / branch identifying the build under test.",
    )
    parser.add_argument(
        "--engine", required=True, choices=valid_engines,
        help="Storage engine: orioledb or heap.",
    )
    parser.add_argument(
        "--pgdata-base", required=True, type=Path,
        help="Parent directory under which per-test data directories live "
             "(e.g. /ssd).",
    )
    parser.add_argument(
        "--memory-buffers", default=None,
        help="Override shared_buffers / orioledb.main_buffers value.",
    )
    parser.add_argument(
        "--fast-run", action="store_true",
        help="Short benchmark runs (debug only).",
    )
    parser.add_argument(
        "--results-dir", default=default_results_dir, type=Path,
        help="Where to write result files (default: ./results).",
    )
    parser.add_argument(
        "--reuse-data", action="store_true",
        help="Reuse the existing per-test data directory if present "
             "(skip initdb / data load).",
    )
    parser.add_argument(
        "--extended-logging", action="store_true",
        help="Collect per-second resource stats (CPU, disk IO, wait events) "
             "to a JSONL file next to the result file.",
    )


def positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected integer, got {value!r}") from e
    if n <= 0:
        raise argparse.ArgumentTypeError(f"expected positive integer, got {n}")
    return n


# ---------------------------------------------------------------------------
# Data-directory naming
# ---------------------------------------------------------------------------

def data_dir_for(base: Path, *, engine: str, test: str, scale: str | int) -> Path:
    """
    A per-(engine, test, scale) data directory. The path is uniquely defined by
    its inputs, so multiple data dirs can coexist on the same volume.
    """
    return Path(base) / f"pgdata-{engine}-{test}-{scale}"


# ---------------------------------------------------------------------------
# File / config helpers
# ---------------------------------------------------------------------------

def append_file_to(file_src: Path | str, file_dst: Path | str) -> None:
    src = Path(file_src)
    dst = Path(file_dst)
    if not src.is_file():
        raise BenchError(f"Cannot append, source file does not exist: {src}")
    with open(src, "r") as fsrc, open(dst, "a") as fdst:
        fdst.write(fsrc.read())


def append_line(file_dst: Path | str, line: str) -> None:
    with open(file_dst, "a") as f:
        if not line.endswith("\n"):
            line += "\n"
        f.write(line)


def append_text(file_dst: Path | str, text: str) -> None:
    with open(file_dst, "a") as f:
        f.write(text)


def ensure_dir(path: Path | str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def remove_dir(path: Path | str) -> None:
    p = Path(path)
    if p.exists():
        if p.is_symlink() or p.is_file():
            p.unlink()
        else:
            shutil.rmtree(p)


def cpu_count() -> int:
    """
    Number of CPUs available to this process, matching what `nproc` reports
    on Linux: respects cgroup / taskset CPU affinity. Falls back to the total
    CPU count on platforms without sched_getaffinity.
    """
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        try:
            n = len(getaffinity(0))
            if n > 0:
                return n
        except OSError:
            pass
    return max(os.cpu_count() or 1, 1)


# ---------------------------------------------------------------------------
# Bootstrap (Ubuntu/Debian)
# ---------------------------------------------------------------------------

def detect_debian_family() -> bool:
    """Returns True for Ubuntu/Debian derivatives (where apt-get is available)."""
    osr = Path("/etc/os-release")
    if not osr.is_file():
        return False
    try:
        text = osr.read_text()
    except OSError:
        return False
    return any(marker in text for marker in (
        "ID=ubuntu", "ID=debian", "ID_LIKE=debian", "ID_LIKE=ubuntu"
    ))


def apt_install(packages: Sequence[str]) -> None:
    if not packages:
        return
    # DEBIAN_FRONTEND=noninteractive   silences debconf prompts.
    # DEBIAN_PRIORITY=critical         only ask on truly critical questions.
    # NEEDRESTART_MODE=a               auto-restart services without the
    #                                  curses 'Daemons using outdated libs' UI
    #                                  that ships on Ubuntu 22.04+.
    # NEEDRESTART_SUSPEND=1            backstop in case needrestart still tries.
    env = {
        "DEBIAN_FRONTEND": "noninteractive",
        "DEBIAN_PRIORITY": "critical",
        "NEEDRESTART_MODE": "a",
        "NEEDRESTART_SUSPEND": "1",
    }
    # -o Dpkg::Options::=--force-confold|--force-confdef keeps existing config
    # files on conflict instead of asking; Dpkg::Use-Pty=0 disables the pty UI.
    apt_opts = [
        "-y", "-q",
        "-o", "Dpkg::Options::=--force-confdef",
        "-o", "Dpkg::Options::=--force-confold",
        "-o", "Dpkg::Use-Pty=0",
    ]
    # `sudo -E` preserves the DEBIAN_FRONTEND etc. env across sudo's filter.
    run(["sudo", "-E", "apt-get", "update", *apt_opts], env=env)
    run(["sudo", "-E", "apt-get", "install", *apt_opts, *packages], env=env)


def pip_install(packages: Sequence[str]) -> None:
    """
    pip3 install -U <pkgs>, retrying with --break-system-packages if the host
    enforces PEP 668.
    """
    if not packages:
        return
    cmd = ["pip3", "install", "-U", *packages]
    proc = run(cmd, capture=True, allow_fail=True)
    if proc.returncode == 0:
        return
    combined = (proc.stderr or "") + (proc.stdout or "")
    if "externally-managed-environment" in combined or "PEP 668" in combined:
        log.warning("pip refused (PEP 668); retrying with --break-system-packages")
        run(["pip3", "install", "-U", "--break-system-packages", *packages])
        return
    raise BenchError(
        f"pip install failed (rc={proc.returncode}): {combined.strip()[:400]}"
    )


def bootstrap_system() -> None:
    """Install system + python deps on a fresh Ubuntu VM (idempotent)."""
    log.info("==== BOOTSTRAP PHASE ====")
    if not detect_debian_family():
        log.warning("Not Ubuntu/Debian — skipping apt-get install; relying on "
                    "preinstalled packages.")
    else:
        apt_install(apt_packages)
    pip_install(pip_packages)


# ---------------------------------------------------------------------------
# Repository / build artifact reuse
# ---------------------------------------------------------------------------

def is_git_repo(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def repo_clone_or_fetch(repo_url: str, dest: Path) -> None:
    """
    If dest exists and is a git repo, fetch all refs. Otherwise, clone.
    Caller is responsible for git-checkout-ing the desired ref afterwards.

    Sets core.fileMode=false on the local clone so executable-bit changes
    (e.g. chmod +x ci/prerequisites.sh) are not seen as dirty working-tree
    changes that would block subsequent `git checkout <ref>`.
    """
    if is_git_repo(dest):
        log.info("Reusing existing git checkout: %s", dest)
        run(["git", "fetch", "--all", "--tags", "--prune"], cwd=dest)
    else:
        if dest.exists():
            raise BenchError(f"Path exists but is not a git repo: {dest}")
        run(["git", "clone", repo_url, str(dest)], cwd=dest.parent)
    run(["git", "config", "core.fileMode", "false"], cwd=dest)


def pg_build_exists(prefix: Path) -> bool:
    return (prefix / "bin" / "pg_ctl").is_file()


# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------

def pg_psql(sql: str, *, db: str = "postgres", capture: bool = False) -> subprocess.CompletedProcess:
    return run(["psql", f"-d{db}", "-c", sql], capture=capture)


def pg_psql_file(path: Path | str, *, db: str = "postgres") -> subprocess.CompletedProcess:
    return run(["psql", f"-d{db}", "-f", str(path)])


def pg_initdb(pgdatadir: Path | str) -> None:
    run(["initdb", str(pgdatadir), "--no-locale"])


def pg_start(pgdatadir: Path | str, logfile: str = "logfile") -> None:
    run(["pg_ctl", "-D", str(pgdatadir), "-l", logfile, "start"])


def pg_restart(pgdatadir: Path | str, logfile: str = "logfile") -> None:
    run(["pg_ctl", "-D", str(pgdatadir), "-l", logfile, "restart"])


def pg_stop(pgdatadir: Path | str, logfile: str = "logfile", *, allow_fail: bool = False) -> None:
    run(["pg_ctl", "-D", str(pgdatadir), "-l", logfile, "stop"], allow_fail=allow_fail)


def is_pgdata_initialized(pgdatadir: Path) -> bool:
    """A directory is a usable PGDATA if it contains PG_VERSION."""
    return pgdatadir.is_dir() and (pgdatadir / "PG_VERSION").is_file()


def write_engine_config(
    pgdatadir: Path,
    engine: str,
    test: str,
    memory_buffers: str,
) -> None:
    """Write postgresql.auto.conf for the given engine + test."""
    auto_conf = pgdatadir / "postgresql.auto.conf"
    base = script_dir / f"postgresql.auto.conf.{test}"
    if not base.is_file():
        raise BenchError(f"Base config not found: {base}")
    shutil.copyfile(base, auto_conf)

    if engine == "orioledb":
        engine_conf = script_dir / f"postgresql.auto.conf.orioledb.{test}"
        if not engine_conf.is_file():
            raise BenchError(f"OrioleDB config not found: {engine_conf}")
        append_file_to(engine_conf, auto_conf)
        append_line(auto_conf, f"orioledb.main_buffers = {memory_buffers}")
    elif engine == "heap":
        engine_conf = script_dir / f"postgresql.auto.conf.heap.{test}"
        if not engine_conf.is_file():
            raise BenchError(f"Heap config not found: {engine_conf}")
        append_file_to(engine_conf, auto_conf)
        append_line(auto_conf, f"shared_buffers = {memory_buffers}")
    else:
        raise BenchError(f"Unknown engine: {engine}")


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def parse_pgbench_tps(output: str) -> int | None:
    for line in output.splitlines():
        if "tps = " in line and "(without initial connection time)" in line:
            parts = line.split()
            if len(parts) >= 3:
                int_part = parts[2].split(".")[0]
                try:
                    return int(int_part)
                except ValueError:
                    return None
    return None


def parse_tpcc_tpm(output: str) -> int | None:
    last = None
    for line in output.splitlines():
        if "tpmTotal" in line:
            parts = line.split()
            if len(parts) >= 2:
                last = parts[1]
    if last is None:
        return None
    try:
        return int(last.split(".")[0])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Disk-usage helpers
# ---------------------------------------------------------------------------

def du_kb(path: Path | str, *, apparent: bool = False) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    cmd = ["du", "-s"]
    if apparent:
        cmd.append("--apparent-size")
    cmd.append(str(p))
    proc = run(cmd, capture=True, allow_fail=True)
    if proc.returncode != 0:
        log.warning("du failed for %s: %s", p, (proc.stderr or "").strip())
        return 0
    out = (proc.stdout or "").strip()
    if not out:
        return 0
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# ResourceMonitor — psutil + psycopg2 sampling, JSONL output.
# Mirrors run_pgbench() in orioledb/ci/pgbench.py.
# ---------------------------------------------------------------------------

class ResourceMonitor:
    """
    Context manager that samples CPU / disk IO / disk usage / pg wait events
    once per second while the context is active. Output is JSON lines, one
    sample per line.

    Lazy-imports psutil and psycopg2 so that scripts that don't use extended
    logging can run without these deps installed.
    """

    def __init__(
        self,
        output_path: Path,
        *,
        mount_point: Path,
        dsn: str = "dbname=postgres",
        interval: float = 1.0,
    ) -> None:
        self.output_path = Path(output_path)
        self.mount_point = Path(mount_point)
        self.dsn = dsn
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._exc: BaseException | None = None

    def __enter__(self) -> "ResourceMonitor":
        try:
            import psutil  # noqa: F401
        except ImportError as e:
            raise BenchError(
                "Extended logging requires `psutil`. "
                "Run tests.py once (it installs deps) or `pip3 install psutil`."
            ) from e

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._sample_loop, name="resource-monitor", daemon=True
        )
        log.info("ResourceMonitor -> %s", self.output_path)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval * 3 + 5)
        if self._exc is not None and exc_type is None:
            raise self._exc

    def _sample_loop(self) -> None:
        try:
            import psutil
        except ImportError as e:
            self._exc = BenchError(f"ResourceMonitor missing psutil: {e}")
            return

        # psycopg2 is optional. If it isn't installed, or the connection
        # cannot be established (e.g. Postgres isn't running yet / has been
        # stopped), we just keep sampling CPU/IO/disk and leave waits+lsn null.
        try:
            import psycopg2  # type: ignore
        except ImportError:
            log.warning(
                "ResourceMonitor: psycopg2 not available; "
                "pg wait-event sampling will be skipped."
            )
            psycopg2 = None  # type: ignore[assignment]

        conn = None
        if psycopg2 is not None:
            try:
                conn = psycopg2.connect(self.dsn)
                conn.autocommit = False
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ResourceMonitor: cannot connect to PG (%s); "
                    "continuing with CPU/IO/disk sampling only.", e,
                )
                conn = None

        waits_sql = (
            "SELECT jsonb_object_agg(k, v)::text waits, "
            "       pg_current_wal_lsn()::text lsn "
            "FROM (SELECT coalesce(wait_event, 'CPU') k, count(*) v "
            "      FROM pg_stat_activity GROUP BY wait_event) x"
        )

        try:
            with open(self.output_path, "w") as out_file:
                prev_cpu = psutil.cpu_times()
                prev_io = psutil.disk_io_counters()
                cpus = psutil.cpu_count() or 1
                started_at = time.time()
                tick = 0

                while not self._stop_event.is_set():
                    tick += 1
                    target = started_at + tick * self.interval
                    delay = max(target - time.time(), 0.0)
                    if self._stop_event.wait(timeout=delay):
                        break

                    try:
                        cpu = psutil.cpu_times()
                        io = psutil.disk_io_counters()
                    except Exception as e:  # noqa: BLE001
                        log.warning("ResourceMonitor: psutil sample failed: %s", e)
                        continue

                    try:
                        disk_used = shutil.disk_usage(self.mount_point).used
                    except OSError:
                        disk_used = None

                    waits, lsn = None, None
                    if conn is not None:
                        try:
                            with conn.cursor() as cur:
                                cur.execute(waits_sql)
                                row = cur.fetchone()
                            conn.commit()
                            if row is not None:
                                waits_json, lsn = row
                                waits = (json.loads(waits_json)
                                         if waits_json else None)
                        except Exception as e:  # noqa: BLE001
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            log.warning(
                                "ResourceMonitor: pg query failed (%s); "
                                "disabling pg sampling for the rest of "
                                "this run.", e,
                            )
                            try:
                                conn.close()
                            except Exception:
                                pass
                            conn = None

                    sample = {
                        "time": tick,
                        "disk_used": disk_used,
                        "system": (cpu.system - prev_cpu.system) / cpus * 100.0,
                        "user":   (cpu.user   - prev_cpu.user)   / cpus * 100.0,
                        "idle":   (cpu.idle   - prev_cpu.idle)   / cpus * 100.0,
                        "read_count":  io.read_count  - prev_io.read_count,
                        "write_count": io.write_count - prev_io.write_count,
                        "read_bytes":  io.read_bytes  - prev_io.read_bytes,
                        "write_bytes": io.write_bytes - prev_io.write_bytes,
                        "waits": waits,
                        "lsn": lsn,
                    }
                    prev_cpu = cpu
                    prev_io = io
                    out_file.write(json.dumps(sample) + "\n")
                    out_file.flush()
        except Exception as e:  # noqa: BLE001
            # File-write errors and similar are genuine — surface them.
            self._exc = BenchError(f"ResourceMonitor crashed: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")
