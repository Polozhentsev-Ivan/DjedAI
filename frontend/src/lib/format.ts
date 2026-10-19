// Business formatting from docs/ui-design/README.md: ru-RU digits, comma decimals, U+2212 minus.
const MINUS = '−';
const NBSP = String.fromCharCode(0xa0);

const integer = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const oneDecimal = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
const date = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
const time = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', hour12: false });

function withMinus(text: string): string {
  return text.replace('-', MINUS);
}

/** 120450 → «120 450». */
export function formatCount(value: number): string {
  return withMinus(integer.format(value));
}

/** 1250000 → «1 250 000 ₽». */
export function formatMoney(value: number): string {
  return `${formatCount(value)}${NBSP}₽`;
}

/** 3400000 → «3,4 млн ₽»; below a million the full amount is shown. */
export function formatMoneyShort(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${withMinus(oneDecimal.format(value / 1e9))}${NBSP}млрд${NBSP}₽`;
  if (abs >= 1e6) return `${withMinus(oneDecimal.format(value / 1e6))}${NBSP}млн${NBSP}₽`;
  return formatMoney(value);
}

const fixedDecimal = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 });

/** Chart scale: 2500000 → «2,5 млн», 0 → «0,0 млн», with an optional unit («0,9 млн ₽»). */
export function formatMillions(value: number, unit?: string): string {
  const text = `${withMinus(fixedDecimal.format(value / 1e6))}${NBSP}млн`;
  return unit ? `${text}${NBSP}${unit}` : text;
}

/** 19293798 → «18,4 МБ». */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}${NBSP}Б`;
  if (bytes < 1024 * 1024) return `${oneDecimal.format(bytes / 1024)}${NBSP}КБ`;
  return `${oneDecimal.format(bytes / 1024 / 1024)}${NBSP}МБ`;
}

/** 12.5 → «12,5%». The value is already in percent. */
export function formatPercent(value: number): string {
  return `${withMinus(oneDecimal.format(value))}%`;
}

/** 34 → «+34%», −5 → «−5%». */
export function formatDelta(value: number): string {
  const sign = value > 0 ? '+' : '';
  return `${sign}${formatPercent(value)}`;
}

export type DeltaTone = 'success' | 'danger' | 'neutral';

export function deltaTone(value: number): DeltaTone {
  if (value > 0) return 'success';
  if (value < 0) return 'danger';
  return 'neutral';
}

function toDate(value: Date | string): Date {
  return typeof value === 'string' ? new Date(value) : value;
}

/** ISO string or Date → «18.10.2026». */
export function formatDate(value: Date | string): string {
  return date.format(toDate(value));
}

/** ISO string or Date → «18.10.2026 14:00». */
export function formatDateTime(value: Date | string): string {
  const d = toDate(value);
  return `${date.format(d)} ${time.format(d)}`;
}

/** Russian noun form for a count: plural(5, ['строка', 'строки', 'строк']) → «строк». */
export function plural(count: number, [one, few, many]: readonly [string, string, string]): string {
  const n = Math.abs(count) % 100;
  const last = n % 10;
  if (n > 10 && n < 20) return many;
  if (last === 1) return one;
  if (last >= 2 && last <= 4) return few;
  return many;
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** «Сегодня, 14:05», «Вчера, 17:40» or «12.10.2026» for older dates (chat history). */
export function formatRelativeDay(value: Date | string, now: Date = new Date()): string {
  const d = toDate(value);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameDay(d, now)) return `Сегодня, ${time.format(d)}`;
  if (sameDay(d, yesterday)) return `Вчера, ${time.format(d)}`;
  return date.format(d);
}
