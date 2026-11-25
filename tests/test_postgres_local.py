"""Run the suite in a NEW loopback-only, disposable local Postgres container.

Explicit invocation authorizes this temporary synthetic test environment only.
Prerequisites: local Docker, pre-pulled official postgres:17, optional postgres
Python dependency. No existing database, host data volume or cloud endpoint used.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

IMAGE = "postgres:17"
REPOSITORY = Path(__file__).resolve().parents[1]
SOCKET = Path("/var/run/docker.sock")


class LocalTestError(Exception):
    pass


def docker(arguments: list[str], env: dict, *, timeout: int = 60) -> str:
    result = subprocess.run(
        ["docker", "--host=unix://" + str(SOCKET), *arguments],
        env=env, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode:
        raise LocalTestError("Local Docker operation failed; raw diagnostics withheld.")
    return result.stdout.strip()


def wait_ready(container: str, env: dict) -> None:
    """Block on server logs, with a deadline; do not print raw server output."""
    process = subprocess.Popen(
        ["docker", "--host=unix://" + str(SOCKET), "logs", "--follow", container],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 60
    pending = b""
    initialized = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(timeout=max(0, remaining)):
                    raise LocalTestError("Local Postgres did not become ready within sixty seconds.")
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    raise LocalTestError("Local Postgres exited before readiness; raw logs withheld.")
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if b"PostgreSQL init process complete; ready for start up." in line:
                        initialized = True
                    if initialized and b"database system is ready to accept connections" in line:
                        return
                if len(pending) > 65536:
                    raise LocalTestError("Unexpected local server log size; raw logs withheld.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def run() -> int:
    try:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
    except ImportError:
        raise LocalTestError("Install the optional postgres dependency in your virtual environment first.") from None
    if not shutil.which("docker") or not SOCKET.is_socket():
        raise LocalTestError("A local Docker daemon at /var/run/docker.sock is required.")
    # Each invocation owns this unique container; it never stops an existing one.
    container = "growthos-pg-test-" + uuid.uuid4().hex
    database = "disposable_growthos_test"
    logins = {name: ("growthos_test_" + name.lower(), secrets.token_urlsafe(32)) for name in ("ADMIN", "WRITER", "READER")}
    with tempfile.TemporaryDirectory(prefix="growthos-pg-test-", dir="/tmp/opencode") as home:
        # Test credentials exist only in child environments/memory, not argv/files.
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "PYTHONIOENCODING": "utf-8"}
        image = docker(["image", "inspect", IMAGE, "--format", "{{index .RepoDigests 0}}"], env)
        if not image.startswith("postgres@sha256:"):
            raise LocalTestError("Pull the official postgres:17 image first; no image was started.")
        print("Local-only synthetic Postgres test; image: " + image, flush=True)
        started = False
        try:
            admin, password = logins["ADMIN"]
            docker([
                "run", "--detach", "--rm", "--name", container,
                "--label", "growthos.task=Task-1", "--restart=no",
                "--publish", "127.0.0.1::5432", "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
                "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,size=268435456",
                "--env", "POSTGRES_PASSWORD", "--env", "POSTGRES_USER=" + admin,
                "--env", "POSTGRES_DB=" + database,
                "--env", "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
                image,
            ], {**env, "POSTGRES_PASSWORD": password})
            started = True
            wait_ready(container, env)
            port = docker(["inspect", "--format", '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}', container], env)
            if not re.fullmatch(r"[0-9]{1,5}", port):
                raise LocalTestError("Unexpected loopback port assignment.")
            dsns = {
                name: make_conninfo(host="127.0.0.1", port=port, dbname=database, user=user, password=secret, sslmode="disable")
                for name, (user, secret) in logins.items()
            }
            with psycopg.connect(dsns["ADMIN"], autocommit=True, connect_timeout=5) as connection:
                version = connection.execute("SHOW server_version").fetchone()[0]
                print("Postgres " + version + "; psycopg " + psycopg.__version__ + "; three distinct test logins.", flush=True)
                for name in ("WRITER", "READER"):
                    user, secret = logins[name]
                    connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(sql.Identifier(user), sql.Literal(secret)))
            test_env = {**env, "GROWTHOS_TEST_ALLOW_DDL": "disposable-local-only"}
            test_env.update({"GROWTHOS_TEST_" + name + "_DSN": dsn for name, dsn in dsns.items()})
            return subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                cwd=REPOSITORY, env=test_env, timeout=180, check=False,
            ).returncode
        finally:
            if started:
                docker(["stop", "--time", "5", container], env, timeout=15)
                print("Owned test container stopped and removed; tmpfs database discarded.", flush=True)


def main() -> int:
    try:
        return run()
    except (LocalTestError, subprocess.TimeoutExpired):
        print("Local test setup failed or timed out; no cloud connection attempted. Check local Docker/image/dependency prerequisites.", file=sys.stderr)
        return 2
    except Exception:
        print("Local test runner failed; diagnostics withheld to protect ephemeral credentials.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
