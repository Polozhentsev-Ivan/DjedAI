import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { isDocumentation, selectChecks } from './check-ci-changes.mjs';
import { requiredResult } from './check-repository.mjs';

const script = fileURLToPath(new URL('./check-ci-changes.mjs', import.meta.url));

function fixture(t) {
  const parent = path.resolve(os.tmpdir());
  const root = fs.mkdtempSync(path.join(parent, 'djedai-ci-changes-'));
  t.after(() => {
    const target = path.resolve(root);
    assert.equal(path.dirname(target), parent);
    assert.ok(path.basename(target).startsWith('djedai-ci-changes-'));
    fs.rmSync(target, { recursive: true, force: true });
  });
  const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  git('init', '--initial-branch=main');
  git('config', 'user.name', 'CI test');
  git('config', 'user.email', 'ci-test@example.invalid');
  git('config', 'core.autocrlf', 'false');
  const write = (file, contents) => {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.writeFileSync(path.join(root, file), contents);
  };
  const commit = () => {
    git('add', '--all');
    git('commit', '-m', 'Test change');
    return git('rev-parse', 'HEAD');
  };
  write('README.md', '# Project\n');
  write('src/application.py', 'print("application")\n');
  const base = commit();
  const push = (before = base) => selectChecks(root, 'push', { before, after: git('rev-parse', 'HEAD') });
  return { root, git, write, commit, base, push };
}

test('documentation allowlist excludes runtime prompts and unknown/code/config files', () => {
  for (const file of ['README.md', 'CONTRIBUTING.md', 'src/djedai/README.md', 'docs/guide.md', 'docs/source/spec.docx', 'docs/screen shot.png']) {
    assert.equal(isDocumentation(file), true, file);
  }
  for (const file of ['prompts/understand.md', 'prompts/nested/plan.md', '.github/workflows/ci.yml', 'uv.lock', '.python-version', 'frontend/package-lock.json', 'src/app.py', 'docs/example.py', 'docs/ui-design/support.js', 'frontend/public/chart.svg']) {
    assert.equal(isDocumentation(file), false, file);
  }
});

test('docs-only push skips application checks and leaves the required gate valid', t => {
  const f = fixture(t);
  f.write('README.md', '# Updated project\n');
  f.write('docs/usage.md', '# Usage\n');
  f.commit();
  const report = f.push();
  assert.equal(report.runTests, false);
  assert.deepEqual(report.changedFiles, ['README.md', 'docs/usage.md']);
  assert.equal(requiredResult('success', 'false', 'skipped', 'false', 'skipped'), true);
  assert.equal(requiredResult('failure', 'false', 'skipped', 'false', 'skipped'), false);
});

test('a push containing code followed by a docs commit still runs the full suite', t => {
  const f = fixture(t);
  f.write('src/application.py', 'print("changed")\n');
  f.commit();
  f.write('README.md', '# Updated docs\n');
  f.commit();
  assert.equal(f.push().runTests, true);
});

test('PR compares all branch changes from the merge base, ignoring unrelated main changes', t => {
  const f = fixture(t);
  f.git('checkout', '-b', 'docs');
  f.write('README.md', '# PR docs\n');
  const head = f.commit();
  f.git('checkout', 'main');
  f.write('src/application.py', 'print("unrelated main change")\n');
  const base = f.commit();
  assert.equal(selectChecks(f.root, 'pull_request', { pull_request: { base: { sha: base }, head: { sha: head } } }).runTests, false);
});

test('a code change earlier in a PR is not hidden by the final documentation commit', t => {
  const f = fixture(t);
  f.write('src/application.py', 'print("PR change")\n');
  f.commit();
  f.write('README.md', '# PR docs\n');
  const head = f.commit();
  assert.equal(selectChecks(f.root, 'pull_request', { pull_request: { base: { sha: f.base }, head: { sha: head } } }).runTests, true);
});

test('deleting application code or renaming it to documentation runs the full suite', async t => {
  for (const rename of [false, true]) {
    await t.test(rename ? 'rename' : 'delete', child => {
      const f = fixture(child);
      if (rename) f.git('mv', 'src/application.py', 'example.md');
      else fs.unlinkSync(path.join(f.root, 'src/application.py'));
      f.commit();
      const report = f.push();
      assert.equal(report.runTests, true);
      assert.ok(report.changedFiles.includes('src/application.py'));
    });
  }
});

test('deleting documentation can skip application checks', t => {
  const f = fixture(t);
  fs.unlinkSync(path.join(f.root, 'README.md'));
  f.commit();
  assert.equal(f.push().runTests, false);
  // The separate repository job still validates required files and Markdown links.
});

test('changing a runtime Markdown prompt runs application checks', t => {
  const f = fixture(t);
  f.write('prompts/understand.md', 'Updated runtime instructions\n');
  f.commit();
  assert.equal(f.push().runTests, true);
});

test('manual events, new branches, unavailable history and empty diffs run all checks', t => {
  const f = fixture(t);
  const head = f.base;
  for (const [eventName, event] of [
    ['workflow_dispatch', {}],
    ['schedule', {}],
    ['push', { before: '0'.repeat(40), after: head }],
    ['push', { before: 'a'.repeat(40), after: head }],
    ['push', { before: head, after: head }],
    ['pull_request', {}],
    ['push', { before: '--help', after: head }],
  ]) {
    assert.equal(selectChecks(f.root, eventName, event).runTests, true, JSON.stringify([eventName, event]));
  }
});

test('CLI writes GitHub output and summary for docs-only and unreadable events', t => {
  const f = fixture(t);
  f.write('docs/usage.md', '# Usage\n');
  const head = f.commit();
  // Store workflow metadata outside the committed file range.
  const metadata = path.join(f.root, '.git', 'ci-test');
  fs.mkdirSync(metadata);
  const eventPath = path.join(metadata, 'event.json');
  const outputPath = path.join(metadata, 'output');
  const summaryPath = path.join(metadata, 'summary');
  fs.writeFileSync(eventPath, JSON.stringify({ before: f.base, after: head }));
  const run = () => spawnSync(process.execPath, [script], {
    cwd: f.root, encoding: 'utf8',
    env: { ...process.env, GITHUB_EVENT_NAME: 'push', GITHUB_EVENT_PATH: eventPath, GITHUB_OUTPUT: outputPath, GITHUB_STEP_SUMMARY: summaryPath },
  });
  assert.equal(run().status, 0);
  assert.equal(fs.readFileSync(outputPath, 'utf8'), 'run-tests=false\n');
  assert.match(fs.readFileSync(summaryPath, 'utf8'), /Documentation only/);
  fs.writeFileSync(eventPath, 'invalid JSON');
  assert.equal(run().status, 0);
  assert.equal(fs.readFileSync(outputPath, 'utf8'), 'run-tests=false\nrun-tests=true\n');
});
