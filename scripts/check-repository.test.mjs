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

test('the separate frontend is required and the former Python UI is not required', t => {
  const f = fixture(t);
  assert.equal(f.report().ok, true);
  f.tracked.delete('frontend/README.md');
  f.add('src/djedai/ui/README.md');
  assert.ok(f.report().errors.some(e => e.includes('frontend/')));
});

test('frontend build and coverage outputs cannot be tracked', async t => {
  for (const file of ['frontend/dist/assets/app.js', 'frontend/build/index.html', 'frontend/coverage/lcov.info', 'frontend/tsconfig.app.tsbuildinfo', 'frontend/node_modules/react/index.js']) {
    await t.test(file, child => {
      const f = fixture(child);
      f.add(file, 'generated output\n');
      assert.ok(f.report().errors.some(e => e.includes(file)));
    });
  }
});

test('frontend TSX is checked as UTF-8 source without claiming Python tests', t => {
  const f = fixture(t);
  f.add('frontend/src/app/App.tsx', 'export const title = "Данные";\n');
  assert.equal(f.report().ok, true);
  assert.equal(f.report().pythonEnabled, false);
  f.add('frontend/src/app/App.tsx', Buffer.from([0xff, 0xfe, 0x80]));
  assert.ok(f.report().errors.some(e => e.includes('UTF-8 файл frontend/src/app/App.tsx')));
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

test('shortcut references and images use normalized labels and the first definition', () => {
  const text = [
    '[Guide]', '![Diagram]', '[guide][]', '[Read more][  GUIDE ]', '[Build   guide]',
    '', '[guide]: README.md', '[GUIDE]: ignored.md', '[diagram]: diagram.svg',
    '[build guide]: CONTRIBUTING.md',
  ].join('\n');
  assert.deepEqual(markdownLinks(text), [
    'README.md', 'diagram.svg', 'README.md', 'README.md', 'CONTRIBUTING.md',
  ]);
});

test('a broken shortcut reference fails repository validation', t => {
  const f = fixture(t);
  f.add('README.md', '[Guide]\n\n[Guide]: missing.md\n');
  const report = f.report();
  assert.equal(report.ok, false);
  assert.equal(report.markdownLinks, 1);
  assert.ok(report.errors.some(error => error.includes('README.md -> missing.md')));
});

test('unused definitions and labels belonging to other link forms are not shortcuts', () => {
  const text = [
    '[Guide](README.md)', '[Guide][Other]', '\\[Guide]', '[Unknown]',
    '', '[Guide]: missing.md', '[Other]: CONTRIBUTING.md', '[Unused]: missing.md',
  ].join('\n');
  assert.deepEqual(markdownLinks(text), ['README.md', 'CONTRIBUTING.md']);
});

test('images without alternative text still have their targets checked', t => {
  const f = fixture(t);
  f.add('README.md', '![](missing.png)\n');
  assert.ok(f.report().errors.some(error => error.includes('README.md -> missing.png')));
});

test('code blocks do not create links or reference definitions', async t => {
  const examples = {
    indented: 'Example:\n\n    [Example](missing.md)\n    [Guide]: missing.md',
    tabbed: 'Example:\n\n\t[Example](missing.md)\n\t[Guide]: missing.md',
    quoted: '> ```md\n> [Example](missing.md)\n> [Guide]: missing.md\n> ```',
    'nested quote': '> > ~~~md\n> > [Example](missing.md)\n> > [Guide]: missing.md\n> > ~~~',
    'quoted indentation': '> Example:\n>\n>     [Example](missing.md)\n>     [Guide]: missing.md',
    'list fence': '- ```md\n  [Example](missing.md)\n  [Guide]: missing.md\n  ```',
    'list indentation': '- Example:\n\n      [Example](missing.md)\n      [Guide]: missing.md',
    'quote syntax inside a fence': '```md\n> [Example](missing.md)\n> [Guide]: missing.md\n```',
    'nested quote syntax inside a quoted fence': '> ```md\n> > [Example](missing.md)\n> > [Guide]: missing.md\n> ```',
  };
  for (const [name, example] of Object.entries(examples)) {
    await t.test(name, child => {
      const f = fixture(child);
      f.add('README.md', `${example}\n\n[Guide]\n\n[real](CONTRIBUTING.md)\n`);
      assert.deepEqual(markdownLinks(fs.readFileSync(path.join(f.root, 'README.md'), 'utf8')), ['CONTRIBUTING.md']);
      const report = f.report();
      assert.deepEqual(report.errors, []);
      assert.equal(report.markdownLinks, 1);
    });
  }
});

test('indentation in paragraphs and list continuation still contains navigation', () => {
  for (const text of [
    'Paragraph\n    [real](README.md)',
    '> Paragraph\n>     [real](README.md)',
    '- Paragraph\n\n    [real](README.md)',
    '- Outer\n  - Inner\n\n      [real](README.md)',
  ]) assert.deepEqual(markdownLinks(text), ['README.md'], text);
});

test('an unclosed fence stops at the end of its quote or list container', () => {
  for (const text of [
    '> ```md\n> [Example](missing.md)\n\n[real](README.md)',
    '- ```md\n  [Example](missing.md)\n\n[real](README.md)',
  ]) assert.deepEqual(markdownLinks(text), ['README.md'], text);
});

test('a closing fence must match the opening marker and contain no info string', () => {
  const text = [
    '````md', '```', '[Example](missing.md)', '````md', '[Example](missing.md)',
    '~~~~', '[Example](missing.md)', '````', '[real](README.md)',
  ].join('\n');
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
  assert.equal(requiredResult('success', 'false', 'skipped', 'false', 'skipped'), true);
  assert.equal(requiredResult('success', 'true', 'success', 'false', 'skipped'), true);
  assert.equal(requiredResult('success', 'true', 'success', 'true', 'success'), true);
  for (const [repository, expected, python] of [
    ['failure', 'false', 'skipped'], ['cancelled', 'false', 'skipped'],
    ['success', 'true', 'failure'], ['success', 'true', 'skipped'],
    ['success', 'true', 'cancelled'], ['success', 'true', ''],
    ['success', '', 'skipped'], ['success', 'false', 'success'],
  ]) assert.equal(requiredResult(repository, expected, python, 'false', 'skipped'), false);
});

test('a failed, skipped or missing frontend job blocks the final gate', () => {
  for (const [expected, frontend] of [
    ['true', 'failure'], ['true', 'skipped'], ['true', 'cancelled'], ['true', ''],
    ['false', 'success'], ['', 'skipped'], [undefined, undefined],
  ]) assert.equal(requiredResult('success', 'true', 'success', expected, frontend), false, `${expected}/${frontend}`);
});

function enableFrontend(f, overrides = {}) {
  const manifest = {
    name: 'frontend', private: true, packageManager: 'npm@11.19.1',
    engines: { node: '24.22.0', npm: '11.19.1' },
    dependencies: { react: '19.3.0' }, devDependencies: { vitest: '5.0.3' },
    ...overrides,
  };
  f.add('.nvmrc', '24.22.0\n');
  f.add('frontend/.npmrc', 'save-exact=true\nengine-strict=true\n');
  f.add('frontend/package.json', JSON.stringify(manifest));
  f.add('frontend/package-lock.json', JSON.stringify({ lockfileVersion: 3, packages: { '': { dependencies: manifest.dependencies, devDependencies: manifest.devDependencies } } }));
  f.add('frontend/src/app.test.ts', 'test("x", () => {});\n');
}

test('a complete frontend requests the separate frontend job', t => {
  const f = fixture(t);
  enableFrontend(f);
  const report = f.report();
  assert.deepEqual(report.errors, []);
  assert.equal(report.frontendEnabled, true);
  assert.equal(report.frontendTestFiles, 1);
});

test('frontend dependencies must use exact versions', t => {
  const f = fixture(t);
  enableFrontend(f, { dependencies: { react: '^19.3.0' }, devDependencies: { vitest: 'latest' } });
  const report = f.report();
  assert.equal(report.frontendEnabled, false);
  assert.ok(report.errors.some(e => e.includes('react (dependencies): ^19.3.0')));
  assert.ok(report.errors.some(e => e.includes('vitest (devDependencies): latest')));
});

test('Node and npm are pinned like CI', t => {
  const f = fixture(t);
  enableFrontend(f, { engines: { node: '24', npm: '11' }, packageManager: 'npm@11' });
  f.add('.nvmrc', '24\n');
  const errors = f.report().errors;
  assert.ok(errors.some(e => e.includes('точная версия Node')));
  assert.ok(errors.some(e => e.includes('engines.npm')));
});

test('engines.node must match .nvmrc and packageManager must match engines.npm', t => {
  const f = fixture(t);
  enableFrontend(f, { engines: { node: '22.0.0', npm: '11.19.1' }, packageManager: 'npm@10.0.0' });
  const errors = f.report().errors;
  assert.ok(errors.some(e => e.includes('должен совпадать с .nvmrc')));
  assert.ok(errors.some(e => e.includes('npm@11.19.1')));
});

test('frontend needs its lock, save-exact, tests and a lock in sync', t => {
  const f = fixture(t);
  enableFrontend(f);
  f.add('frontend/.npmrc', 'engine-strict=true\n');
  f.add('frontend/package-lock.json', JSON.stringify({ packages: { '': { dependencies: { react: '19.2.0' } } } }));
  f.tracked.delete('frontend/src/app.test.ts');
  const errors = f.report().errors;
  assert.ok(errors.some(e => e.includes('save-exact=true')));
  assert.ok(errors.some(e => e.includes('не согласован с package.json (dependencies)')));
  assert.ok(errors.some(e => e.includes('настоящие тесты в frontend/src')));
  f.tracked.delete('frontend/package-lock.json');
  f.tracked.delete('.nvmrc');
  const missing = f.report().errors;
  assert.ok(missing.some(e => e.includes('frontend/package-lock.json')));
  assert.ok(missing.some(e => e.includes('.nvmrc')));
});

test('an unreadable package.json is reported', t => {
  const f = fixture(t);
  enableFrontend(f);
  f.add('frontend/package.json', '{ broken');
  assert.ok(f.report().errors.some(e => e.includes('Не удалось прочитать frontend/package.json')));
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
  assert.match(fs.readFileSync(output, 'utf8'), /frontend-enabled=false/);
  assert.match(fs.readFileSync(summary, 'utf8'), /tests\/coverage not claimed/);
  assert.match(fs.readFileSync(summary, 'utf8'), /Frontend: not present/);
});

test('CLI final gate fails on a required Python job being skipped', t => {
  const f = fixture(t);
  const run = spawnSync(process.execPath, [script, '--required'], {
    cwd: f.root, encoding: 'utf8',
    env: { ...process.env, REPOSITORY_RESULT: 'success', PYTHON_EXPECTED: 'true', PYTHON_RESULT: 'skipped', FRONTEND_EXPECTED: 'false', FRONTEND_RESULT: 'skipped' },
  });
  assert.equal(run.status, 1);
  assert.match(run.stdout, /Required CI: failed/);
});

test('CLI final gate fails when the expected frontend job fails', t => {
  const f = fixture(t);
  const run = spawnSync(process.execPath, [script, '--required'], {
    cwd: f.root, encoding: 'utf8',
    env: { ...process.env, REPOSITORY_RESULT: 'success', PYTHON_EXPECTED: 'true', PYTHON_RESULT: 'success', FRONTEND_EXPECTED: 'true', FRONTEND_RESULT: 'failure' },
  });
  assert.equal(run.status, 1);
  assert.match(run.stdout, /Required CI: failed/);
});

test('CLI final gate passes when every expected job passed', t => {
  const f = fixture(t);
  const run = spawnSync(process.execPath, [script, '--required'], {
    cwd: f.root, encoding: 'utf8',
    env: { ...process.env, REPOSITORY_RESULT: 'success', PYTHON_EXPECTED: 'true', PYTHON_RESULT: 'success', FRONTEND_EXPECTED: 'true', FRONTEND_RESULT: 'success' },
  });
  assert.equal(run.status, 0, run.stdout);
});

test('workflow final gate preserves required results without downloading actions', { skip: process.platform !== 'linux' }, () => {
  const workflow = fs.readFileSync(new URL('../.github/workflows/ci.yml', import.meta.url), 'utf8');
  const required = workflow.split('\n  required:\n')[1];
  assert.ok(required);
  assert.doesNotMatch(required, /uses:/);
  const body = required.split('        run: |\n')[1].replace(/^          /gm, '');
  for (const repository of ['success', 'failure', 'cancelled']) {
    for (const pythonExpected of ['true', 'false']) {
      for (const frontendExpected of ['true', 'false']) {
        for (const python of ['success', 'failure', 'skipped', 'cancelled']) {
          for (const frontend of ['success', 'failure', 'skipped', 'cancelled']) {
            for (const browser of ['success', 'failure', 'skipped', 'cancelled']) {
              const expected = requiredResult(repository, pythonExpected, python, frontendExpected, frontend)
                && browser === (pythonExpected === 'true' && frontendExpected === 'true' ? 'success' : 'skipped');
              const run = spawnSync('bash', ['-e', '-c', body], {
                env: { ...process.env, REPOSITORY_RESULT: repository,
                  PYTHON_EXPECTED: pythonExpected, PYTHON_RESULT: python,
                  FRONTEND_EXPECTED: frontendExpected, FRONTEND_RESULT: frontend, BROWSER_RESULT: browser },
                encoding: 'utf8', timeout: 5000,
              });
              assert.equal(run.status === 0, expected, JSON.stringify({ repository, pythonExpected, frontendExpected, python, frontend, browser }));
            }
          }
        }
      }
    }
  }
});
