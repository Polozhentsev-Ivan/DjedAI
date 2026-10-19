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
