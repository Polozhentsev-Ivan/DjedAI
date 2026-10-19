import { deltaTone, formatCount, formatMillions, formatDate, formatDateTime, formatDelta, formatMoney, formatMoneyShort, formatPercent } from './format';

// Intl separates digit groups with no-break spaces; \s matches them.
const plain = (text: string) => text.replace(/\s/g, ' ');

describe('format', () => {
  it('groups digits with spaces', () => {
    expect(plain(formatCount(120450))).toBe('120 450');
    expect(plain(formatCount(1420000))).toBe('1 420 000');
  });

  it('formats money with the ruble sign', () => {
    expect(plain(formatMoney(1250000))).toBe('1 250 000 ₽');
    expect(plain(formatMoney(4820))).toBe('4 820 ₽');
  });

  it('shortens millions and billions', () => {
    expect(plain(formatMoneyShort(3400000))).toBe('3,4 млн ₽');
    expect(plain(formatMoneyShort(12400000))).toBe('12,4 млн ₽');
    expect(plain(formatMoneyShort(2_500_000_000))).toBe('2,5 млрд ₽');
    expect(plain(formatMoneyShort(-1800000))).toBe('−1,8 млн ₽');
    expect(plain(formatMoneyShort(950000))).toBe('950 000 ₽');
  });

  it('keeps one decimal on chart scales', () => {
    expect(plain(formatMillions(0))).toBe('0,0 млн');
    expect(plain(formatMillions(3000000))).toBe('3,0 млн');
    expect(plain(formatMillions(900000, '₽'))).toBe('0,9 млн ₽');
  });

  it('formats percents and deltas with a comma and true minus', () => {
    expect(formatPercent(12.5)).toBe('12,5%');
    expect(formatPercent(0)).toBe('0%');
    expect(formatDelta(34)).toBe('+34%');
    expect(formatDelta(-5)).toBe('−5%');
    expect(formatDelta(0)).toBe('0%');
  });

  it('maps deltas to tones', () => {
    expect(deltaTone(1)).toBe('success');
    expect(deltaTone(-1)).toBe('danger');
    expect(deltaTone(0)).toBe('neutral');
  });

  it('formats dates as DD.MM.YYYY', () => {
    expect(formatDate(new Date(2026, 9, 18))).toBe('18.10.2026');
    expect(formatDate('2026-01-05T00:00:00')).toBe('05.01.2026');
    expect(formatDateTime(new Date(2026, 9, 18, 14, 0))).toBe('18.10.2026 14:00');
  });
});
