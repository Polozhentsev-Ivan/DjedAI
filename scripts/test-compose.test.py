"""Exercise isolated Compose orchestration, including failure cleanup, without Docker."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("test-compose.sh")
FAKE_COMMAND = """#!/usr/bin/python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as log:
    log.write(json.dumps([name, args, os.environ.get('DJEDAI_PORT')]) + '\\n')
assert os.environ['DJEDAI_COMPOSE_ACCEPTANCE'] == (
    'true' if os.environ.get('GITHUB_ACTIONS') == 'true' else 'local'
)
if name == 'node':
    print('test-only-password')
if name == 'docker' and 'port' in args:
    counter = pathlib.Path(os.environ['CALLS'] + '.ports')
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    print(os.environ.get('BINDING', f'127.0.0.1:{49171 + count}'))
failure = os.environ.get('FAIL', '')
if (name == 'npm' and failure == 'smoke') or (name == 'docker' and failure in args):
    sys.exit(7)
"""


@unittest.skipIf(sys.platform == "win32", "The CI orchestration uses Linux Bash")
class ComposeSmokeTests(unittest.TestCase):
    def execute(self, **overrides):
        with tempfile.TemporaryDirectory(prefix="djedai-compose-test-") as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copyfile(SCRIPT, root / "scripts/test-compose.sh")
            (root / ".env").write_text("working application settings\n")
            (root / ".env.example").write_text("# safe defaults\n")
            commands = root / "bin"
            commands.mkdir()
            for name in ("docker", "node", "npm", "curl"):
                command = commands / name
                command.write_text(FAKE_COMMAND)
                command.chmod(0o755)
            calls_file = root / "calls.jsonl"
            environment = {
                **os.environ,
                "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
                "CALLS": str(calls_file),
                "GITHUB_RUN_ID": "4321",
                "GITHUB_RUN_ATTEMPT": "3",
                "COMPOSE_PROJECT_NAME": "djedai",
                "DJEDAI_PORT": "8080",
                "DJEDAI_ENV_FILE": "/production/.env",
                "DJEDAI_COMPOSE_ACCEPTANCE": "inherited-value",
                "GITHUB_ACTIONS": "false",
                **overrides,
            }
            result = subprocess.run(
                ["bash", str(root / "scripts/test-compose.sh"), str(root / "evidence")],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual((root / ".env").read_text(), "working application settings\n")
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
            for name, args, port in calls:
                self.assertEqual(port, "0")
                if name == "docker":
                    self.assertEqual(args[args.index("-p") + 1], "djedai-ci-4321-3")
                    self.assertEqual(args[args.index("--env-file") + 1], str(root / ".env.example"))
            self.assertEqual(sum(name == "docker" and "down" in args for name, args, _ in calls), 1)
            return result, calls

    def test_uses_allocated_port_for_both_smokes_and_health(self):
        result, calls = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        smokes = [args for name, args, _ in calls if name == "npm"]
        self.assertEqual(len(smokes), 2)
        for index, args in enumerate(smokes):
            self.assertEqual(args[args.index("--url") + 1], f"http://127.0.0.1:{49171 + index}")
        self.assertIn("--verify", smokes[1])
        self.assertIn(["curl", ["--fail", "http://127.0.0.1:49172/api/health"], "0"], calls)
        self.assertTrue(any(name == "docker" and "restart" in args for name, args, _ in calls))

    def test_failed_start_or_smoke_still_cleans_its_project(self):
        for failure in ("up", "smoke"):
            with self.subTest(failure=failure):
                result, calls = self.execute(FAIL=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(
                    any(name == "docker" and "restart" in args for name, args, _ in calls)
                )

    def test_cleanup_failure_is_not_reported_as_success(self):
        result, _ = self.execute(FAIL="down")
        self.assertNotEqual(result.returncode, 0)

    def test_ci_marks_resources_for_its_maintenance_hook(self):
        result, _ = self.execute(GITHUB_ACTIONS="true")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_binding_does_not_fall_back_to_working_application(self):
        result, calls = self.execute(BINDING="")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(name in {"npm", "curl"} for name, _, _ in calls))


if __name__ == "__main__":
    unittest.main()
