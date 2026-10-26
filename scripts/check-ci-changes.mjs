import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Prompts are runtime inputs, even though they are Markdown. Unknown paths run CI.
export function isDocumentation(file) {
  if (file.startsWith('prompts/')) return false;
  if (file.endsWith('.md')) return true;
  return file.startsWith('docs/') && /\.(?:txt|rst|png|jpe?g|gif|webp|svg|pdf|docx)$/.test(file);
}

const full = reason => ({ runTests: true, reason, changedFiles: [] });
const commitSha = value => typeof value === 'string' && /^[a-f0-9]{40}$/.test(value) && !/^0+$/.test(value);

export function selectChecks(root, eventName, event) {
  let base;
  let head;
  let separator;
  if (eventName === 'pull_request') {
    base = event.pull_request?.base?.sha;
    head = event.pull_request?.head?.sha;
    separator = '...'; // Include the whole PR, excluding unrelated base-branch changes.
  } else if (eventName === 'push') {
    base = event.before;
    head = event.after;
    separator = '..'; // Include every commit in the push, not just the last one.
  } else {
    return full('Manual or unsupported event: run all application checks.');
  }
  if (!commitSha(base) || !commitSha(head)) {
    return full('Missing comparison commits or new branch: run all application checks.');
  }
  let changedFiles;
  try {
    // Disabling rename detection retains BOTH paths when code is moved into docs.
    // NUL delimiters preserve filenames containing whitespace or newlines.
    changedFiles = execFileSync('git', ['diff', '--name-only', '--no-renames', '-z', `${base}${separator}${head}`, '--'], {
      cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
    }).split('\0').filter(Boolean);
  } catch {
    return full('Comparison unavailable: run all application checks.');
  }
  if (changedFiles.length === 0) return full('Empty comparison: run all application checks.');
  const runTests = !changedFiles.every(isDocumentation);
  return {
    runTests,
    reason: runTests ? 'Application or configuration changes: run all application checks.' : 'Documentation only: skip Python, frontend and browser checks.',
    changedFiles,
  };
}

function main() {
  let report;
  try {
    const event = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'));
    report = selectChecks(process.cwd(), process.env.GITHUB_EVENT_NAME, event);
  } catch {
    report = full('Event unavailable: run all application checks.');
  }
  console.log(JSON.stringify(report, null, 2));
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT, `run-tests=${report.runTests}\n`);
  }
  if (process.env.GITHUB_STEP_SUMMARY) {
    fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `\n## Application CI selection\n${report.reason}\nChanged files: ${report.changedFiles.length}\n`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
