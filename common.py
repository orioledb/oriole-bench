"""
Common utilities for the oriole-bench Python scripts.

Centralizes subprocess execution, environment / preflight validation, paths,
parsing helpers, system bootstrap, and resource monitoring so that the test
scripts only contain the actual benchmark logic.
"""

from __future__ import annotations

import argparse
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
from typing import Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

script_dir = Path(__file__).resolve().parent
default_results_dir = script_dir / "results"
default_pgdata_base = Path("/ssd")

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
) -> subprocess.CompletedProcess:
    """
    Run a command synchronously with logging and strict error handling.
    Raises BenchError on non-zero exit unless allow_fail=True.
    """
    if isinstance(cmd, str) and not shell:
        cmd_for_run: Sequence[str] | str = shlex.split(cmd)
    else:
        cmd_for_run = cmd

    log.info("$ %s%s", _format_cmd(cmd), f"  (cwd={cwd})" if cwd else "")

    try:
        proc = subprocess.run(
            cmd_for_run,
            cwd=str(cwd) if cwd is not None else None,
            env=({**os.environ, **env} if env is not None else None),
            check=False,
            capture_output=capture,
            text=text,
            shell=shell,
            input=input_text,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise BenchError(f"Executable not found while running: {_format_cmd(cmd)} ({e})") from e
    except subprocess.TimeoutExpired as e:
        raise BenchError(f"Timeout {timeout}s expired for: {_format_cmd(cmd)}") from e
    except OSError as e:
        raise BenchError(f"OS error while running {_format_cmd(cmd)}: {e}") from e

    if check and not allow_fail and proc.returncode != 0:
        stderr_msg = ""
        if capture:
            stderr_msg = f"\n--- stderr ---\n{(proc.stderr or '').strip()}"
            stderr_msg += f"\n--- stdout ---\n{(proc.stdout or '').strip()}"
        raise BenchError(
            f"Command failed with code {proc.returncode}: {_format_cmd(cmd)}{stderr_msg}"
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
) -> subprocess.Popen:
    if isinstance(cmd, str) and not shell:
        cmd_for_run: Sequence[str] | str = shlex.split(cmd)
    else:
        cmd_for_run = cmd

    log.info("(bg) $ %s", _format_cmd(cmd))
    return subprocess.Popen(
        cmd_for_run,
        cwd=str(cwd) if cwd is not None else None,
        env=({**os.environ, **env} if env is not None else None),
        stdout=stdout,
        stderr=stderr,
        shell=shell,
        text=True,
    )


def wait_all(procs: Iterable[subprocess.Popen], *, label: str = "background job") -> None:
    procs_list = list(procs)
    failures: list[tuple[int, int]] = []
    for proc in procs_list:
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            for p in procs_list:
                if p.poll() is None:
                    try:
                        p.send_signal(signal.SIGTERM)
                    except OSError:
                        pass
            raise
        if rc != 0:
            failures.append((proc.pid, rc))
    if failures:
        details = ", ".join(f"pid={pid}->rc={rc}" for pid, rc in failures)
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
    """
    if is_git_repo(dest):
        log.info("Reusing existing git checkout: %s", dest)
        run(["git", "fetch", "--all", "--tags", "--prune"], cwd=dest)
    else:
        if dest.exists():
            raise BenchError(f"Path exists but is not a git repo: {dest}")
        run(["git", "clone", repo_url, str(dest)], cwd=dest.parent)


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
            import psycopg2  # noqa: F401
        except ImportError as e:
            raise BenchError(
                "Extended logging requires `psutil` and `psycopg2-binary`. "
                "Run tests.py once (it installs them) or `pip3 install psutil "
                "psycopg2-binary`."
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
            import psycopg2
        except ImportError as e:
            self._exc = BenchError(f"ResourceMonitor missing dep: {e}")
            return

        try:
            conn = psycopg2.connect(self.dsn)
            conn.autocommit = False
        except Exception as e:
            self._exc = BenchError(f"ResourceMonitor cannot connect to PG: {e}")
            return

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

                    cpu = psutil.cpu_times()
                    io = psutil.disk_io_counters()
                    try:
                        disk_used = shutil.disk_usage(self.mount_point).used
                    except OSError:
                        disk_used = None

                    waits, lsn = None, None
                    try:
                        with conn.cursor() as cur:
                            cur.execute(waits_sql)
                            row = cur.fetchone()
                        conn.commit()
                        if row is not None:
                            waits_json, lsn = row
                            waits = json.loads(waits_json) if waits_json else None
                    except Exception as e:  # noqa: BLE001
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        log.warning("ResourceMonitor: pg query failed: %s", e)

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
            self._exc = BenchError(f"ResourceMonitor crashed: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")
