"""Exercise runner cleanup and watchdog decisions without a live Docker daemon."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("local-runner-maintenance.sh")

FAKE_COMMAND = """#!/usr/bin/python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'docker':
    if os.environ.get('DOCKER_FAIL') == '1':
        sys.exit(124)
    if args[:2] == ['ps', '-aq']:
        if 'label=org.djedai.postgres-acceptance' in args:
            print('a' * 12)
        elif 'label=com.docker.compose.project=djedai' in args:
            print('b' * 12)
        elif not any(arg.startswith('label=') for arg in args):
            print('f' * 12)  # An unrelated container must never be selected.
    elif args[:3] == ['volume', 'ls', '-q']:
        print('djedai_app-data')
elif name == 'ps':
    print(os.environ.get('WORKER_AGES', ''))
elif name == 'systemctl' and args[0] == 'is-active':
    sys.exit(0 if os.environ.get('RUNNER_ACTIVE', '1') == '1' else 3)
"""


@unittest.skipIf(sys.platform == "win32", "The runner uses Linux Bash and process tools")
class RunnerMaintenanceTests(unittest.TestCase):
    def execute(self, mode, **overrides):
        with tempfile.TemporaryDirectory(prefix="djedai-maintenance-test-") as directory:
            root = Path(directory)
            calls_file = root / "calls.jsonl"
            for name in ("docker", "ps", "systemctl", "logger"):
                command = root / name
                command.write_text(FAKE_COMMAND, encoding="utf-8")
                command.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                "CALLS": str(calls_file),
                **overrides,
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), mode],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
            return result, calls

    def test_cleanup_removes_only_labelled_test_resources(self):
        result, calls = self.execute("cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        removals = [call for call in calls if call[:2] == ["docker", "rm"]]
        self.assertEqual(
            removals,
            [
                ["docker", "rm", "--force", "--volumes", "a" * 12],
                ["docker", "rm", "--force", "--volumes", "b" * 12],
            ],
        )
        self.assertIn(["docker", "volume", "rm", "djedai_app-data"], calls)
        for call in calls:
            if call[:3] == ["docker", "ps", "-aq"]:
                self.assertIn("--filter", call)
            if call[:4] == ["docker", "volume", "ls", "-q"]:
                self.assertIn("label=com.docker.compose.volume=app-data", call)

    def test_cleanup_does_not_hide_a_dead_docker_daemon(self):
        result, calls = self.execute("cleanup", DOCKER_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[:2] == ["docker", "rm"] for call in calls))

    def assert_no_restart(self, **overrides):
        result, calls = self.execute("watchdog", **overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(["systemctl", "restart", "djedai-ci"], calls)

    def test_idle_runner_is_not_restarted(self):
        self.assert_no_restart(WORKER_AGES="")

    def test_legitimate_jobs_are_not_restarted(self):
        self.assert_no_restart(WORKER_AGES="1499\n600")

    def test_deadline_includes_cancellation_grace(self):
        self.assert_no_restart(WORKER_AGES="2100")

    def test_manually_stopped_runner_stays_stopped(self):
        self.assert_no_restart(RUNNER_ACTIVE="0", WORKER_AGES="9999")

    def test_worker_stuck_past_all_job_deadlines_is_restarted(self):
        result, calls = self.execute("watchdog", WORKER_AGES="10\n2101")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count(["systemctl", "restart", "djedai-ci"]), 1)
        self.assertTrue(any(call[0] == "logger" for call in calls))


if __name__ == "__main__":
    unittest.main()
