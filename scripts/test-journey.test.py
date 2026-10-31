"""Acceptance orchestration must isolate mutations and clean up after a failed phase."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("test-journey.sh")
FAKE = """#!/usr/bin/python3
import json, os, pathlib, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ['CALLS'], 'a') as log:
    log.write(json.dumps([name, args, os.environ['COMPOSE_PROJECT_NAME'],
                         os.environ['DJEDAI_ENV_FILE'], os.environ['DJEDAI_PORT']]) + '\\n')
if name == 'node' and '-e' in args:
    print('test-key')
elif name == 'docker' and 'port' in args:
    print('127.0.0.1:49172' if 'model' in args else '127.0.0.1:49171')
elif name == 'git':
    print('test-sha')
if name == 'node' and '--phase' in args:
    if args[args.index('--phase') + 1] == os.environ.get('FAIL'):
        sys.exit(7)
if name == 'docker' and 'down' in args and os.environ.get('FAIL') == 'cleanup':
    sys.exit(8)
"""


@unittest.skipIf(sys.platform == "win32", "Manual Compose orchestration uses Linux Bash")
class JourneyOrchestrationTests(unittest.TestCase):
    def execute(self, failure=""):
        with tempfile.TemporaryDirectory(prefix="journey-script-test-") as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copyfile(SCRIPT, root / "scripts/test-journey.sh")
            (root / ".env").write_text("working configuration must survive")
            (root / ".env.example").touch()
            commands = root / "bin"
            commands.mkdir()
            for name in ("docker", "node", "git"):
                target = commands / name
                target.write_text(FAKE)
                target.chmod(0o755)
            calls_file = root / "calls.jsonl"
            result = subprocess.run(
                ["bash", str(root / "scripts/test-journey.sh"), str(root / "evidence")],
                env={
                    **os.environ,
                    "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
                    "CALLS": str(calls_file),
                    "FAIL": failure,
                    "COMPOSE_PROJECT_NAME": "production",
                    "DJEDAI_ENV_FILE": "/production/.env",
                    "DJEDAI_PORT": "8080",
                    "COMPOSE_PROFILES": "demo",
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
            projects = {call[2] for call in calls}
            self.assertEqual(len(projects), 1)
            project = next(iter(projects))
            self.assertTrue(project.startswith("djedai-journey-"))
            for name, args, _, env_file, port in calls:
                self.assertEqual(env_file, str(root / ".env.example"))
                self.assertEqual(port, "0")
                if name == "docker":
                    self.assertEqual(args[args.index("-p") + 1], project)
            self.assertEqual((root / ".env").read_text(), "working configuration must survive")
            cleanup = [args for name, args, *_ in calls if name == "docker" and "down" in args]
            self.assertEqual(len(cleanup), 1)
            self.assertIn("--volumes", cleanup[0])
            return result, calls

    def test_all_phases_use_discovered_ports_and_only_their_postgres_is_mutated(self):
        result, calls = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        phases = [args for name, args, *_ in calls if name == "node" and "--phase" in args]
        self.assertEqual([args[-1] for args in phases], ["prepare", "verify", "partial"])
        for args in phases:
            self.assertEqual(args[args.index("--url") + 1], "http://127.0.0.1:49171")
            self.assertEqual(args[args.index("--model-url") + 1], "http://127.0.0.1:49172")
        mutations = [args for name, args, *_ in calls if name == "docker" and "exec" in args]
        self.assertEqual(len(mutations), 2)
        for args in mutations:
            self.assertIn("journey-postgres", args)
            self.assertIn("ON_ERROR_STOP=1", args)

    def test_failed_prepare_never_mutates_the_source_or_runs_later_phases(self):
        result, calls = self.execute("prepare")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(name == "docker" and "exec" in args for name, args, *_ in calls))
        phases = [args[-1] for name, args, *_ in calls if name == "node" and "--phase" in args]
        self.assertEqual(phases, ["prepare"])

    def test_failed_verify_never_injects_the_partial_failure(self):
        result, calls = self.execute("verify")
        self.assertNotEqual(result.returncode, 0)
        mutations = [args for name, args, *_ in calls if name == "docker" and "exec" in args]
        self.assertEqual(len(mutations), 1)
        self.assertNotIn("amount = -1", str(mutations))

    def test_failed_cleanup_is_not_success(self):
        result, _ = self.execute("cleanup")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
