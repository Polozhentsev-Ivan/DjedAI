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

// Remove block examples before collecting either links or reference definitions.
function markdownProse(text) {
  const visible = [];
  let fence = null;
  let paragraph = false;
  let quoteDepth = 0;
  let listIndents = [];
  for (const rawLine of text.split(/\r?\n/)) {
    // Tabs use four-column stops, including after quote and list markers.
    let line = rawLine.split('\t').reduce((expanded, part) => expanded + ' '.repeat(4 - expanded.length % 4) + part);
    let depth = 0;
    while ((!fence || depth < quoteDepth) && /^ {0,3}> ?/.test(line)) {
      line = line.replace(/^ {0,3}> ?/, '');
      depth++;
    }
    if (depth !== quoteDepth) {
      fence = null;
      paragraph = false;
      listIndents = [];
      quoteDepth = depth;
    }
    if (line.trim()) {
      const indent = line.match(/^ */)[0].length;
      while (listIndents.length && indent < listIndents.at(-1)) {
        listIndents.pop();
        fence = null;
        paragraph = false;
      }
      line = line.slice(listIndents.at(-1) ?? 0);
    }
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = null;
      visible.push('');
      continue;
    }
    if (!line.trim()) {
      paragraph = false;
      visible.push('');
      continue;
    }
    // Measure indentation relative to a list item's content, not the document.
    const list = line.match(/^( {0,3}(?:[-+*]|\d{1,9}[.)]))( +)(.*)$/);
    const thematic = /^ {0,3}(?:(?:\* *){3,}|(?:- *){3,}|(?:_ *){3,})$/.test(line);
    if (list && !thematic) {
      const padding = list[2].length > 4 ? 1 : list[2].length;
      const width = list[1].length + padding;
      listIndents.push((listIndents.at(-1) ?? 0) + width);
      line = line.slice(width);
      paragraph = false;
    }
    // An indented block cannot interrupt a paragraph (CommonMark section 4.4).
    if (line.startsWith('    ') && !paragraph) {
      visible.push('');
      continue;
    }
    const opening = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (opening && (opening[1][0] === '~' || !opening[2].includes('`'))) {
      fence = opening[1];
      paragraph = false;
      visible.push('');
      continue;
    }
    visible.push(line);
    paragraph = !thematic && !/^ {0,3}(?:#{1,6}(?: |$)|[=-]+ *$|\[[^\]]+\]:)/.test(line);
  }
  return visible.join('\n');
}

// Ignore code examples and comments: links written as examples are not navigation.
export function markdownLinks(text) {
  let content = markdownProse(text).replace(/<!--[\s\S]*?-->/g, '').replace(/(`+)[^\n]*?\1/g, '');
  const normalizeLabel = label => label.trim().replace(/\s+/g, ' ').toLowerCase();
  const definitions = new Map();
  // Definitions are metadata, so their labels must not become shortcut links.
  content = content.replace(/^ {0,3}\[([^\]\n]+)\]:\s*(<[^>\n]+>|\S+)[^\n]*$/gm, (_, label, target) => {
    const key = normalizeLabel(label);
    if (!definitions.has(key)) definitions.set(key, target.replace(/^<|>$/g, ''));
    return '';
  });
  const destinations = [];
  // Consume inline/full/collapsed links before considering a shortcut label.
  const pattern = /(?<!\\)!?\[([^\[\]\n]*)\](?:\(\s*(<[^>\n]+>|(?:\\.|[^\s()]|\([^()]*\))+)(?:\s+["'][^\n]*?["'])?\s*\)|\[([^\]\n]*)\])?/g;
  for (const match of content.matchAll(pattern)) {
    const destination = match[2]
      ? match[2].replace(/^<|>$/g, '').replace(/\\([() ])/g, '$1')
      : definitions.get(normalizeLabel(match[3] || match[1]));
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
