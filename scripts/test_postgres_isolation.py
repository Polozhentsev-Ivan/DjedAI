"""Run real owned PostgreSQL acceptance in an isolated, disposable Docker container.

The official image's exact tag and multi-platform digest were verified against
https://hub.docker.com/v2/repositories/library/postgres/tags/17.11-bookworm
on 2026-10-22. Credentials are random, transient, and never command-line arguments
or report fields. Only this invocation's labelled container is removed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from cryptography.fernet import Fernet
from seed_demo_postgres import seed

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = (
    "postgres:17.11-bookworm@"
    "sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27"
)
TEST_PATHS = (
    "tests/integration/test_postgres_adapter.py",
    "tests/integration/test_owned_postgres.py",
    "tests/integration/test_postgres_owner_api.py",
)
LABEL = "org.djedai.postgres-acceptance"


class AcceptanceError(RuntimeError):
    """A failure whose message contains no command output or credentials."""


def _external_report(value: str) -> Path:
    path = Path(value).resolve()
    if path.is_relative_to(ROOT):
        raise argparse.ArgumentTypeError("Write acceptance reports outside the repository")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=_external_report, help="JSON report outside the checkout")
    args = parser.parse_args(argv)
    nonce = uuid4().hex
    name = f"djedai-pg-acceptance-{nonce}"
    passwords = {role: secrets.token_urlsafe(32) for role in ("postgres", "loader", "reader")}
    secret_values = list(passwords.values())
    started = time.monotonic()
    report: dict = {"status": "error", "image": POSTGRES_IMAGE, "tests": list(TEST_PATHS)}
    environment = os.environ.copy()
    environment["POSTGRES_PASSWORD"] = passwords["postgres"]
    phase = "docker"

    def sanitized(value: str) -> str:
        for secret in sorted(secret_values, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value

    def command(
        arguments: list[str], *, input_text: str | None = None, timeout: int = 300
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                arguments,
                input=input_text,
                env=environment,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AcceptanceError(f"{phase}: command unavailable or timed out") from None
        if result.returncode:
            # Keep useful infrastructure diagnostics, with all generated secrets removed.
            print(sanitized(result.stderr or result.stdout), file=sys.stderr, flush=True)
            raise AcceptanceError(f"{phase}: command failed")
        return result

    def sql(text: str, database: str = "postgres") -> str:
        return command(
            [
                "docker",
                "exec",
                "-i",
                name,
                "psql",
                "--no-psqlrc",
                "--username",
                "postgres",
                "--dbname",
                database,
                "--set",
                "ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
            ],
            input_text=text,
            timeout=60,
        ).stdout.strip()

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, terminate)
    try:
        command(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
        print("Pulling pinned PostgreSQL image...", flush=True)
        command(["docker", "pull", POSTGRES_IMAGE], timeout=600)
        phase = "start"
        command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--label",
                f"{LABEL}={nonce}",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_PASSWORD",
                POSTGRES_IMAGE,
            ]
        )
        deadline = time.monotonic() + 90
        while True:
            # The image's temporary init server only accepts Unix sockets.
            # TCP readiness waits for the final server after init has completed.
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "--host",
                    "127.0.0.1",
                    "--username",
                    "postgres",
                ],
                env=environment,
                capture_output=True,
                timeout=10,
            )
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise AcceptanceError("start: PostgreSQL did not become ready")
            time.sleep(0.5)
        binding = command(["docker", "port", name, "5432/tcp"], timeout=30).stdout.strip()
        match = re.fullmatch(r"127\.0\.0\.1:(\d+)", binding)
        if match is None:
            raise AcceptanceError("start: expected a localhost-only dynamic port")
        port = int(match.group(1))
        phase = "prepare"
        roles = (ROOT / "infra/postgres/roles.sql").read_text(encoding="utf-8")
        roles = roles.replace(":'loader_password'", f"'{passwords['loader']}'")
        roles = roles.replace(":'reader_password'", f"'{passwords['reader']}'")
        sql(roles)
        sql(
            "CREATE DATABASE djedai_demo_other OWNER djedai_loader;\n"
            "REVOKE ALL ON DATABASE djedai_demo_other FROM PUBLIC;\n"
            "GRANT CONNECT ON DATABASE djedai_demo_other TO djedai_loader, djedai_reader;"
        )
        sql(
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n"
            "ALTER SCHEMA public OWNER TO djedai_loader;\n"
            "GRANT USAGE ON SCHEMA public TO djedai_reader;\n"
            "ALTER DEFAULT PRIVILEGES FOR ROLE djedai_loader IN SCHEMA public "
            "GRANT SELECT ON TABLES TO djedai_reader;",
            "djedai_demo_other",
        )

        def dsn(role: str, database: str) -> str:
            value = (
                f"postgresql://djedai_{role}:{quote(passwords[role], safe='')}"
                f"@127.0.0.1:{port}/{database}"
            )
            secret_values.append(value)
            return value

        first_reader = dsn("reader", "djedai_demo")
        other_reader = dsn("reader", "djedai_demo_other")
        loader = dsn("loader", "djedai_demo")
        other_loader = dsn("loader", "djedai_demo_other")
        print("Seeding two PostgreSQL sources with distinct stores totals...", flush=True)
        seed(loader, ROOT / "tests/fixtures/synthetic")
        seed(other_loader, ROOT / "tests/fixtures/synthetic")
        sql(
            "DELETE FROM public.stores WHERE store_id <> (SELECT MIN(store_id) FROM public.stores)",
            "djedai_demo_other",
        )
        report["server_version"] = sql("SHOW server_version")
        environment.update(
            {
                "DJEDAI_TEST_PG_DSN": first_reader,
                "DJEDAI_TEST_PG_OTHER_DSN": other_reader,
                "DJEDAI_DEMO_PG_LOADER_DSN": loader,
                "SOURCE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            }
        )
        secret_values.append(environment["SOURCE_ENCRYPTION_KEY"])
        phase = "tests"
        print("Running real PostgreSQL adapter, owned-source and HTTP acceptance...", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--strict-markers", "--tb=short", "-q", *TEST_PATHS],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        output = sanitized(result.stdout + result.stderr)
        print(output, end="", flush=True)
        report["pytest_exit_code"] = result.returncode
        for status in ("passed", "failed", "skipped", "error"):
            counts = re.findall(rf"(\d+) {status}(?:s)?\b", output)
            report[status] = int(counts[-1]) if counts else 0
        if result.returncode or report["skipped"] or not report["passed"]:
            raise AcceptanceError("tests: acceptance must pass without skipped cases")
        report["status"] = "passed"
    except (AcceptanceError, KeyboardInterrupt, subprocess.TimeoutExpired):
        report["failed_phase"] = phase
        print(f"PostgreSQL acceptance failed during {phase}.", file=sys.stderr, flush=True)
    except Exception:
        # Source drivers may put credentials in exception chains; never print them.
        report["failed_phase"] = phase
        print(f"PostgreSQL acceptance failed during {phase}.", file=sys.stderr, flush=True)
    finally:
        phase = "cleanup"
        try:
            identity = subprocess.run(
                ["docker", "inspect", "--format", f'{{{{index .Config.Labels "{LABEL}"}}}}', name],
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if identity.returncode == 0 and identity.stdout.strip() == nonce:
                removed = subprocess.run(
                    ["docker", "rm", "--force", "--volumes", name],
                    env=environment,
                    capture_output=True,
                    timeout=60,
                )
                report["container_removed"] = removed.returncode == 0
                if removed.returncode:
                    report["status"] = "error"
                    report["failed_phase"] = phase
            else:
                # Failure to inspect is not evidence that removal succeeded.
                report["container_removed"] = False
                report["status"] = "error"
                report["failed_phase"] = phase
        except (OSError, subprocess.TimeoutExpired):
            report["container_removed"] = False
            report["status"] = "error"
            report["failed_phase"] = phase
        signal.signal(signal.SIGTERM, previous_sigterm)
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
