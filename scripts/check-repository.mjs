import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const requiredFiles = [
  'README.md', 'CONTRIBUTING.md', '.editorconfig', '.gitattributes',
  '.gitignore', '.env.example', '.github/pull_request_template.md',
];

export const requiredDirectories = [
  'src/djedai/contracts', 'src/djedai/data', 'src/djedai/analytics',
  'src/djedai/agent', 'src/djedai/backend', 'src/djedai/ui',
  'prompts', 'tests/unit', 'tests/integration', 'tests/e2e',
  'tests/fixtures', 'eval', 'infra', 'scripts',
];

const textExtensions = new Set([
  '.md', '.mjs', '.js', '.cjs', '.py', '.toml', '.json',
  '.yml', '.yaml', '.html', '.css', '.sh', '.ps1', '.sql',
]);
const textNames = new Set(['.editorconfig', '.gitattributes', '.gitignore', '.env.example', '.python-version']);

export function forbiddenTrackedFile(file) {
  const name = path.posix.basename(file);
  if (/^\.env(?:[._].*)?$/i.test(name) && name !== '.env.example') return true;
  if (/(^|\/)(?:\.venv|venv|__pycache__|node_modules|\.pytest_cache|\.ruff_cache|\.mypy_cache)(\/|$)/i.test(file)) return true;
  if (/^(?:var|models|artifacts|htmlcov|build|dist)\//i.test(file)) return true;
  if (/(^|\/)(?:\.coverage(?:\..*)?|coverage\.xml)$/i.test(file)) return true;
  return /\.(?:gguf|safetensors|pt|pth|pyc|pyo)$/i.test(file);
}

// Ignore code examples and comments: links written as examples are not navigation.
export function markdownLinks(text) {
  const visible = [];
  let fence = null;
  for (const line of text.split(/\r?\n/)) {
    const marker = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = null;
      continue;
    }
    if (!fence) visible.push(line);
  }
  const content = visible.join('\n').replace(/<!--[\s\S]*?-->/g, '').replace(/(`+)[^\n]*?\1/g, '');
  const destinations = [];
  const pattern = /!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|(?:\\.|[^\s()]|\([^()]*\))+)(?:\s+["'][^\n]*?["'])?\s*\)/g;
  for (const match of content.matchAll(pattern)) {
    destinations.push(match[1].replace(/^<|>$/g, '').replace(/\\([() ])/g, '$1'));
  }
  const definitions = new Map();
  for (const match of content.matchAll(/^\s{0,3}\[([^\]]+)\]:\s*(<[^>\n]+>|\S+)/gm)) {
    definitions.set(match[1].trim().toLowerCase(), match[2].replace(/^<|>$/g, ''));
  }
  for (const match of content.matchAll(/!?\[([^\]\n]+)\]\[([^\]\n]*)\]/g)) {
    const destination = definitions.get((match[2] || match[1]).trim().toLowerCase());
    if (destination) destinations.push(destination);
  }
  return destinations;
}

function pathInRepository(from, target) {
  const decoded = decodeURIComponent(target.split(/[?#]/)[0]);
  if (!decoded) return null;
  if (decoded.includes('\\')) throw new Error('используйте / в локальных Markdown-ссылках');
  const resolved = decoded.startsWith('/')
    ? path.posix.normalize(decoded.slice(1))
    : path.posix.normalize(path.posix.join(path.posix.dirname(from), decoded));
  if (resolved === '..' || resolved.startsWith('../')) throw new Error('ссылка выходит за пределы репозитория');
  return resolved.replace(/\/$/, '');
}

export function checkRepository(root, trackedFiles) {
  const files = [...new Set(trackedFiles.map(file => file.replaceAll('\\', '/')))];
  const tracked = new Set(files);
  const errors = [];
  let markdownLinkCount = 0;
  let textFileCount = 0;
  for (const file of requiredFiles) {
    if (!tracked.has(file)) errors.push(`Отсутствует обязательный файл: ${file}`);
  }
  for (const directory of requiredDirectories) {
    if (!files.some(file => file.startsWith(`${directory}/`))) errors.push(`Каталог не содержит отслеживаемых файлов: ${directory}/`);
  }
  for (const file of files) {
    if (forbiddenTrackedFile(file)) errors.push(`Локальный или сгенерированный файл попал в Git: ${file}`);
    // Dirty CSV, invalid encodings and malformed files are legitimate fixtures.
    if (file.startsWith('tests/fixtures/')) continue;
    if (!textExtensions.has(path.posix.extname(file)) && !textNames.has(path.posix.basename(file))) continue;
    let text;
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(fs.readFileSync(path.join(root, file)));
      textFileCount++;
    } catch (error) {
      errors.push(`Невозможно прочитать UTF-8 файл ${file}: ${error.code ?? error.name}`);
      continue;
    }
    if (path.posix.basename(file) === '.env.example') {
      for (const line of text.split(/\r?\n/)) {
        const entry = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
        if (!entry) continue;
        const [, key, rawValue] = entry;
        const value = rawValue.replace(/^(["'])(.*)\1$/, '$2');
        if (/(?:SECRET|PASSWORD|TOKEN|API_KEY|PRIVATE_KEY|DATABASE_URL|DSN)(?:_|$)/i.test(key) && value) {
          errors.push(`Чувствительное поле в ${file} должно быть пустым: ${key}`);
        }
      }
    }
    if (!file.endsWith('.md')) continue;
    for (const destination of markdownLinks(text)) {
      if (/^(?:[a-z][a-z\d+.-]*:|#|\/\/)/i.test(destination)) continue;
      markdownLinkCount++;
      try {
        const resolved = pathInRepository(file, destination);
        if (!resolved) continue;
        const existsInCommit = resolved === '.' || tracked.has(resolved) || files.some(candidate => candidate.startsWith(`${resolved}/`));
        if (!existsInCommit) errors.push(`Нет цели локальной ссылки: ${file} -> ${destination}`);
      } catch (error) {
        errors.push(`Некорректная локальная ссылка в ${file}: ${error.message}`);
      }
    }
  }

  const applicationFiles = files.filter(file => file.startsWith('src/') && file.endsWith('.py'));
  const metadataFiles = ['pyproject.toml', 'uv.lock', '.python-version'];
  const configuringPython = applicationFiles.length > 0 || metadataFiles.some(file => tracked.has(file));
  const testFiles = files.filter(file => file.startsWith('tests/') && /(?:^|\/)(?:test_[^/]+|[^/]+_test)\.py$/.test(file));
  if (configuringPython) {
    for (const file of metadataFiles) {
      if (!tracked.has(file)) errors.push(`Для Python CI нужен файл: ${file}`);
    }
    if (!applicationFiles.length) errors.push('Python-окружение добавлено без кода приложения; завершите настройку Python CI в том же изменении.');
    if (!testFiles.length) errors.push('Для Python-кода нужны настоящие тесты в tests/.');
    if (tracked.has('.python-version')) {
      const version = fs.readFileSync(path.join(root, '.python-version'), 'utf8').trim();
      if (!/^3\.\d+\.\d+$/.test(version)) errors.push('В .python-version нужна точная версия Python: 3.X.Y.');
    }
  }
  const pythonEnabled = configuringPython && errors.length === 0;
  return { ok: errors.length === 0, errors, trackedFiles: files.length, textFiles: textFileCount, markdownLinks: markdownLinkCount, applicationFiles: applicationFiles.length, testFiles: testFiles.length, pythonEnabled };
}

export function requiredResult(repository, pythonExpected, pythonResult) {
  return repository === 'success' && (pythonExpected === 'true' ? pythonResult === 'success' : pythonExpected === 'false' && pythonResult === 'skipped');
}

function main() {
  if (process.argv[2] === '--required') {
    const ok = requiredResult(process.env.REPOSITORY_RESULT, process.env.PYTHON_EXPECTED, process.env.PYTHON_RESULT);
    console.log(`Required CI: ${ok ? 'passed' : 'failed'}`);
    process.exitCode = ok ? 0 : 1;
    return;
  }
  const root = process.cwd();
  const tracked = execFileSync('git', ['ls-files', '-z'], { cwd: root, encoding: 'utf8' }).split('\0').filter(Boolean);
  const report = checkRepository(root, tracked);
  console.log(JSON.stringify(report, null, 2));
  if (process.env.GITHUB_OUTPUT) fs.appendFileSync(process.env.GITHUB_OUTPUT, `python-enabled=${report.pythonEnabled}\n`);
  if (process.env.GITHUB_STEP_SUMMARY) {
    const summary = [
      '## Repository CI',
      `- Result: ${report.ok ? 'passed' : 'failed'}`,
      `- Tracked files: ${report.trackedFiles}`,
      `- Local Markdown links: ${report.markdownLinks}`,
      `- Python: ${report.pythonEnabled ? 'configured; separate job required' : report.applicationFiles ? 'configuration incomplete' : 'application not implemented; tests/coverage not claimed'}`,
      ...report.errors.map(error => `- Error: ${error.replaceAll('\n', ' ')}`), '',
    ].join('\n');
    fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary);
  }
  process.exitCode = report.ok ? 0 : 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
