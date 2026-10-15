import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { checkRepository, requiredFiles, requiredDirectories, markdownLinks, requiredResult } from './check-repository.mjs';

const script = fileURLToPath(new URL('./check-repository.mjs', import.meta.url));

function fixture(t) {
  const parent = path.resolve(os.tmpdir());
  const root = fs.mkdtempSync(path.join(parent, 'djedai-ci-test-'));
  const tracked = new Set();
  t.after(() => {
    const target = path.resolve(root);
    assert.equal(path.dirname(target), parent);
    assert.ok(path.basename(target).startsWith('djedai-ci-test-'));
    fs.rmSync(target, { recursive: true, force: true });
  });
  const add = (file, contents = '# Example\n', track = true) => {
    const destination = path.join(root, file);
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, contents);
    if (track) tracked.add(file);
  };
  for (const file of requiredFiles) add(file, file === '.env.example' ? 'APP_ENV=development\nSESSION_SECRET=\n' : '# Example\n');
  for (const directory of requiredDirectories) add(`${directory}/README.md`);
  return { root, tracked, add, report: () => checkRepository(root, [...tracked]) };
}

function enablePython(f, version = '3.12.8') {
  f.add('src/djedai/example.py', 'def example():\n    return 1\n');
  f.add('tests/unit/test_example.py', 'def test_example():\n    assert True\n');
  f.add('pyproject.toml', '[project]\nname = "example"\nversion = "0.1.0"\n');
  f.add('uv.lock', 'version = 1\n');
  f.add('.python-version', `${version}\n`);
}

test('the scaffold is valid and does not claim Python testing', t => {
  const report = fixture(t).report();
  assert.deepEqual(report.errors, []);
  assert.equal(report.ok, true);
  assert.equal(report.pythonEnabled, false);
  assert.equal(report.applicationFiles, 0);
});

test('a required file existing only locally is not accepted', t => {
  const f = fixture(t);
  f.tracked.delete('CONTRIBUTING.md');
  assert.ok(f.report().errors.some(e => e.includes('CONTRIBUTING.md')));
});

test('empty or untracked component directories are detected', t => {
  const f = fixture(t);
  f.tracked.delete('src/djedai/data/README.md');
  assert.ok(f.report().errors.some(e => e.includes('src/djedai/data/')));
});

test('tracked local files are rejected even with a gitignore', async t => {
  for (const file of ['.env', '.env.local', '.env_example', '.venv/lib/example.py', 'var/private.csv', 'models/weights.bin', 'artifacts/report.json', 'model.gguf', '.coverage']) {
    await t.test(file, child => {
      const f = fixture(child);
      f.add('.gitignore', '.env\n.venv/\n/var/\n/models/\n/artifacts/\n');
      f.add(file, 'local data\n');
      assert.ok(f.report().errors.some(e => e.includes(file)));
    });
  }
});

test('valid relative, encoded, angle and reference links resolve in tracked files', t => {
  const f = fixture(t);
  f.add('docs/a file(1).md');
  f.add('docs/guide.md', '[Home](../README.md)\n');
  f.add('README.md', '[Space](docs/a%20file(1).md)\n[Angle](<docs/a file(1).md>)\n[Directory](docs/)\n[Root](.)\n[Guide][g]\n[g]: docs/guide.md\n');
  assert.deepEqual(f.report().errors, []);
  assert.equal(f.report().markdownLinks, 6);
});

test('links in fenced/inline examples and comments are ignored', () => {
  const text = ['```markdown', '[fake](missing.md)', '```', '`[fake](missing.md)`', '<!-- [fake](missing.md) -->', '[real](README.md)', '~~~', '[fake](missing.md)', '~~~'].join('\n');
  assert.deepEqual(markdownLinks(text), ['README.md']);
});

test('broken local links do not pass because an untracked file exists', t => {
  const f = fixture(t);
  f.add('README.md', '[Missing](untracked.md)\n');
  f.add('untracked.md', '# Not committed\n', false);
  assert.ok(f.report().errors.some(e => e.includes('untracked.md')));
});

test('external URLs and anchors are not fetched', t => {
  const f = fixture(t);
  f.add('README.md', '[External](https://example.invalid/a)\n[Mail](mailto:example@example.invalid)\n[Here](#title)\n');
  assert.equal(f.report().ok, true);
  assert.equal(f.report().markdownLinks, 0);
});

test('escaping and malformed encoded links give actionable errors', async t => {
  for (const destination of ['../outside.md', '%ZZ.md']) {
    await t.test(destination, child => {
      const f = fixture(child);
      f.add('README.md', `[Link](${destination})\n`);
      assert.ok(f.report().errors.some(e => e.includes('Некорректная локальная ссылка')));
    });
  }
});

test('invalid UTF-8 in owned source or documentation fails', t => {
  const f = fixture(t);
  f.add('README.md', Buffer.from([0xff, 0xfe, 0x80]));
  assert.ok(f.report().errors.some(e => e.includes('UTF-8 файл README.md')));
});

test('dirty fixtures may use other encodings and malformed contents', t => {
  const f = fixture(t);
  f.add('tests/fixtures/cp1251.csv', Buffer.from([0xcf, 0xf0, 0xe8, 0xe2, 0xe5, 0xf2]));
  f.add('tests/fixtures/invalid.json', Buffer.from([0xff]));
  assert.equal(f.report().ok, true);
});

test('nonempty sensitive examples fail without printing their values', t => {
  const f = fixture(t);
  f.add('.env.example', 'SESSION_SECRET=do-not-print-this-value\n');
  const report = f.report();
  assert.ok(report.errors.some(e => e.includes('SESSION_SECRET')));
  assert.ok(!JSON.stringify(report).includes('do-not-print-this-value'));
});

test('safe environment defaults and blank quoted secrets pass', t => {
  const f = fixture(t);
  f.add('.env.example', 'APP_ENV=development\nLOG_LEVEL=INFO\nSESSION_SECRET=""\nAPP_DATABASE_URL=\n');
  assert.equal(f.report().ok, true);
});

test('Python source cannot silently bypass absent metadata and tests', t => {
  const f = fixture(t);
  f.add('src/djedai/example.py', 'value = 1\n');
  const report = f.report();
  assert.equal(report.pythonEnabled, false);
  for (const name of ['pyproject.toml', 'uv.lock', '.python-version', 'tests/']) assert.ok(report.errors.some(e => e.includes(name)));
});

test('a complete Python scaffold requests the separate Python job', t => {
  const f = fixture(t);
  enablePython(f);
  const report = f.report();
  assert.deepEqual(report.errors, []);
  assert.equal(report.pythonEnabled, true);
  assert.equal(report.applicationFiles, 1);
  assert.equal(report.testFiles, 1);
});

test('the Python runtime must be pinned to an exact patch version', t => {
  const f = fixture(t);
  enablePython(f, '3.12');
  assert.ok(f.report().errors.some(e => e.includes('точная версия Python')));
});

test('metadata alone is not reported as tested application code', t => {
  const f = fixture(t);
  enablePython(f);
  f.tracked.delete('src/djedai/example.py');
  assert.ok(f.report().errors.some(e => e.includes('без кода приложения')));
});

test('the final gate accepts only the expected completed checks', () => {
  assert.equal(requiredResult('success', 'false', 'skipped'), true);
  assert.equal(requiredResult('success', 'true', 'success'), true);
  for (const [repository, expected, python] of [
    ['failure', 'false', 'skipped'], ['cancelled', 'false', 'skipped'],
    ['success', 'true', 'failure'], ['success', 'true', 'skipped'],
    ['success', 'true', 'cancelled'], ['success', 'true', ''],
    ['success', '', 'skipped'], ['success', 'false', 'success'],
  ]) assert.equal(requiredResult(repository, expected, python), false);
});

test('CLI uses Git inventory and emits explicit GitHub outputs and summary', t => {
  const f = fixture(t);
  execFileSync('git', ['init', '--quiet'], { cwd: f.root });
  execFileSync('git', ['-c', 'core.autocrlf=false', 'add', '--all'], { cwd: f.root });
  const output = path.join(f.root, 'github-output');
  const summary = path.join(f.root, 'github-summary');
  const run = spawnSync(process.execPath, [script], { cwd: f.root, encoding: 'utf8', env: { ...process.env, GITHUB_OUTPUT: output, GITHUB_STEP_SUMMARY: summary } });
  assert.equal(run.status, 0, run.stderr);
  assert.equal(JSON.parse(run.stdout).pythonEnabled, false);
  assert.match(fs.readFileSync(output, 'utf8'), /python-enabled=false/);
  assert.match(fs.readFileSync(summary, 'utf8'), /tests\/coverage not claimed/);
});

test('CLI final gate fails on a required Python job being skipped', t => {
  const f = fixture(t);
  const run = spawnSync(process.execPath, [script, '--required'], {
    cwd: f.root, encoding: 'utf8',
    env: { ...process.env, REPOSITORY_RESULT: 'success', PYTHON_EXPECTED: 'true', PYTHON_RESULT: 'skipped' },
  });
  assert.equal(run.status, 1);
  assert.match(run.stdout, /Required CI: failed/);
});
