"""Run real owned ClickHouse acceptance (#61) in an isolated, disposable Docker container.

The image is the exact tag and digest of ``demo-clickhouse`` in compose.yaml. Roles come
from infra/clickhouse/roles.sql; a second database with its own reader holds fewer stores
for the two-owner scenario. Credentials are random, transient, never command-line
arguments or report fields. Only this invocation's labelled container is removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from cryptography.fernet import Fernet
from seed_demo_clickhouse import ClickHouseError, ClickHouseHttp, seed

ROOT = Path(__file__).resolve().parents[1]
_IMAGE = re.search(
    r"demo-clickhouse:\s*\n\s*image:\s*(\S+)", (ROOT / "compose.yaml").read_text(encoding="utf-8")
)
CLICKHOUSE_IMAGE = _IMAGE[1] if _IMAGE else ""
TEST_PATHS = (
    "tests/integration/test_demo_databases.py::test_demo_database_matches_expectations[clickhouse]",
    "tests/integration/test_owned_clickhouse.py",
    "tests/integration/test_clickhouse_owner_api.py",
)
LABEL = "org.djedai.clickhouse-acceptance"
OTHER_DATABASE = "djedai_demo_other"


class AcceptanceError(RuntimeError):
    """A failure whose message contains no command output or credentials."""


def _external_report(value: str) -> Path:
    path = Path(value).resolve()
    if path.is_relative_to(ROOT):
        raise argparse.ArgumentTypeError("Write acceptance reports outside the repository")
    return path


def _statements(script: str) -> list[str]:
    """``roles.sql`` без комментариев, по одному оператору."""
    text = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("--"))
    return [statement.strip() for statement in text.split(";") if statement.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=_external_report, help="JSON report outside the checkout")
    args = parser.parse_args(argv)
    nonce = uuid4().hex
    name = f"djedai-ch-acceptance-{nonce}"
    passwords = {role: secrets.token_urlsafe(32) for role in ("admin", "loader", "reader", "other")}
    secret_values = list(passwords.values())
    started = time.monotonic()
    report: dict = {"status": "error", "image": CLICKHOUSE_IMAGE, "tests": list(TEST_PATHS)}
    environment = os.environ.copy()
    environment["CLICKHOUSE_PASSWORD"] = passwords["admin"]
    phase = "docker"

    def sanitized(value: str) -> str:
        for secret in sorted(secret_values, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value

    def command(arguments: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                arguments,
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
            print(sanitized(result.stderr or result.stdout), file=sys.stderr, flush=True)
            raise AcceptanceError(f"{phase}: command failed")
        return result

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, terminate)
    try:
        if not CLICKHOUSE_IMAGE:
            raise AcceptanceError("docker: demo-clickhouse image not found in compose.yaml")
        command(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
        print("Pulling pinned ClickHouse image...", flush=True)
        command(["docker", "pull", CLICKHOUSE_IMAGE], timeout=600)
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
                "127.0.0.1::8123",
                "--env",
                "CLICKHOUSE_USER=djedai_admin",
                "--env",
                "CLICKHOUSE_PASSWORD",
                "--env",
                "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
                "--ulimit",
                "nofile=262144:262144",
                CLICKHOUSE_IMAGE,
            ]
        )
        binding = command(["docker", "port", name, "8123/tcp"], timeout=30).stdout.strip()
        match = re.fullmatch(r"127\.0\.0\.1:(\d+)", binding.splitlines()[0] if binding else "")
        if match is None:
            raise AcceptanceError("start: expected a localhost-only dynamic port")
        port = int(match.group(1))
        admin = ClickHouseHttp("127.0.0.1", port, "djedai_admin", passwords["admin"])
        deadline = time.monotonic() + 90
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=5) as ping:
                    if ping.read().strip() == b"Ok.":
                        admin.query("SELECT 1")
                        break
            except (OSError, ClickHouseError):
                pass
            if time.monotonic() >= deadline:
                raise AcceptanceError("start: ClickHouse did not become ready")
            time.sleep(0.5)

        phase = "prepare"

        def sha256(role: str) -> str:
            return hashlib.sha256(passwords[role].encode()).hexdigest()

        roles = (ROOT / "infra/clickhouse/roles.sql").read_text(encoding="utf-8")
        roles = roles.replace("{loader_sha256}", sha256("loader"))
        roles = roles.replace("{reader_sha256}", sha256("reader"))
        for statement in _statements(roles):
            admin.query(statement)
        for statement in (
            f"CREATE DATABASE {OTHER_DATABASE}",
            f"GRANT ALL ON {OTHER_DATABASE}.* TO djedai_loader",
            "CREATE USER djedai_other_reader IDENTIFIED WITH sha256_hash BY "
            f"'{sha256('other')}' DEFAULT DATABASE {OTHER_DATABASE} SETTINGS readonly = 2 CONST",
            f"GRANT SELECT ON {OTHER_DATABASE}.* TO djedai_other_reader",
        ):
            admin.query(statement)

        def url(role: str, user: str, database: str) -> str:
            value = f"http://{user}:{quote(passwords[role], safe='')}@127.0.0.1:{port}/{database}"
            secret_values.append(value)
            return value

        first_reader = url("reader", "djedai_reader", "djedai_demo")
        other_reader = url("other", "djedai_other_reader", OTHER_DATABASE)
        print("Seeding two ClickHouse sources with distinct stores totals...", flush=True)
        for database in ("djedai_demo", OTHER_DATABASE):
            loader = ClickHouseHttp.from_url(url("loader", "djedai_loader", database))
            seed(loader, ROOT / "tests/fixtures/synthetic")
        admin.query(
            f"ALTER TABLE {OTHER_DATABASE}.stores DELETE WHERE store_id <> "
            f"(SELECT min(store_id) FROM {OTHER_DATABASE}.stores) SETTINGS mutations_sync = 2"
        )
        report["server_version"] = admin.query("SELECT version()").strip()
        environment.pop("CLICKHOUSE_PASSWORD")
        environment.update(
            {
                "DJEDAI_TEST_CH_URL": first_reader,
                "DJEDAI_TEST_CH_OTHER_URL": other_reader,
                "SOURCE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            }
        )
        secret_values.append(environment["SOURCE_ENCRYPTION_KEY"])
        phase = "tests"
        print("Running real ClickHouse catalog, aggregate and owner acceptance...", flush=True)
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
    except Exception:
        # Source errors and exception chains may carry credentials; never print them.
        report["failed_phase"] = phase
        print(f"ClickHouse acceptance failed during {phase}.", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        report["failed_phase"] = phase
        print(f"ClickHouse acceptance interrupted during {phase}.", file=sys.stderr, flush=True)
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
    sys.exit(main())
