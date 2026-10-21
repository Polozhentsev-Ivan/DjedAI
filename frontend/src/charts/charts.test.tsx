import { act, render, screen } from '@testing-library/react';
import type { Schemas } from '@/api/client';
import { setTheme } from '@/lib/theme';
import { Chart } from './Chart';
import { isSupported, measureLabel, pieLegend, specOption } from './spec';
import { SpecChart } from './SpecChart';
import { readChartTheme, type ChartTheme } from './theme';

const instance = { setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() };
vi.mock('./echarts', () => ({
  echarts: { init: vi.fn(() => instance), getInstanceByDom: vi.fn(() => instance) },
}));

const theme: ChartTheme = {
  ink: '#1d1d1f', muted: '#6e6e73', line: '#e0e0e0', surface: '#fff', font: 'Inter', fontSize: 12, radius: '8px',
  primary: '#0066cc', palette: ['#0066cc', '#34a37a', '#e3a008', '#8e5cd9', '#d9577b', '#7a7a7a'],
};
// Intl separates digit groups with no-break spaces; \s matches them.
const plain = (text: string) => text.replace(/\s/g, ' ');

const money = { field: 'revenue', label: 'Выручка', format: 'money' } as const;
const revenue: Schemas['ResultData'] = {
  columns: [{ name: 'month', type: 'date' }, { name: 'revenue', type: 'money' }],
  rows: [['2026-01-01', 1800000], ['2026-07-01', 3400000]], row_count: 2, truncated: false,
};
const line: Schemas['ChartSpec'] = { chart_id: 'chart_line', result_id: 'r_line', type: 'line', title: 'Выручка', x: { field: 'month', kind: 'time' }, y: [money] };
const shares: Schemas['ResultData'] = {
  columns: [{ name: 'region', type: 'category' }, { name: 'share', type: 'percent' }],
  rows: [['Москва', 45], ['Казань', 12]], row_count: 2, truncated: false,
};
const pie: Schemas['ChartSpec'] = { chart_id: 'chart_pie', result_id: 'r_pie', type: 'pie', title: 'Регионы', x: { field: 'region' }, y: [{ field: 'share', format: 'percent' }] };

interface LineOption {
  xAxis: { data: string[] };
  yAxis: { axisLabel: { formatter: (v: number) => string } };
  tooltip: { formatter: (p: { dataIndex: number; value: number; seriesIndex: number }[]) => string };
  series: { type: string; markPoint?: unknown; label?: { formatter: (p: { value: number }) => string }; data: { itemStyle: { opacity: number } }[] }[];
}

describe('Chart', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('draws, redraws on theme change and disposes', () => {
    const option = vi.fn(() => ({ series: [] }));
    const { unmount } = render(<Chart option={option} label="График" height="sm" />);
    expect(screen.getByRole('img', { name: 'График' })).toHaveClass('h-(--chart-height-sm)');
    expect(instance.setOption).toHaveBeenCalledTimes(1);
    act(() => { setTheme('dark'); });
    expect(instance.setOption).toHaveBeenCalledTimes(2);
    act(() => { setTheme('light'); });
    unmount();
    expect(instance.dispose).toHaveBeenCalled();
  });

  it('reads tokens from CSS', () => {
    document.documentElement.style.setProperty('--chart-1', '#0066cc');
    document.documentElement.style.setProperty('--text-xs', '12px');
    const read = readChartTheme(document.documentElement);
    expect(read.primary).toBe('#0066cc');
    expect(read.fontSize).toBe(12);
    expect(read.palette).toHaveLength(6);
  });
});

describe('specOption', () => {
  it('turns monthly dates into month names and money into millions', () => {
    const option = specOption(line, revenue, theme) as unknown as LineOption;
    expect(option.xAxis.data).toEqual(['Янв', 'Июл']);
    expect(plain(option.yAxis.axisLabel.formatter(0))).toBe('0,0 млн');
    expect(option.tooltip.formatter([{ dataIndex: 1, value: 3400000, seriesIndex: 0 }])).toContain('Июл 2026');
    expect(option.series[0]?.markPoint).toBeDefined();
  });

  it('keeps plain dates and categories', () => {
    const daily = { ...revenue, rows: [['2026-03-15', 10], ['2026-03-16', 20]] };
    const option = specOption({ ...line, y: [{ field: 'revenue' }] }, daily, theme) as unknown as LineOption;
    expect(option.xAxis.data).toEqual(['15.03.2026', '16.03.2026']);
    expect(plain(option.yAxis.axisLabel.formatter(1500))).toBe('1 500');
  });

  it('builds a horizontal top-N with a solid leader', () => {
    const option = specOption({ ...line, type: 'bar', orientation: 'horizontal', x: { field: 'month' } }, revenue, theme) as unknown as LineOption;
    expect(option.series[0]?.data.map((d) => d.itemStyle.opacity)).toEqual([1, 0.45]);
    expect(plain(option.series[0]?.label?.formatter({ value: 900000 }) ?? '')).toBe('0,9 млн ₽');
  });

  it('builds vertical bars and stacked areas', () => {
    const bars = specOption({ ...line, type: 'bar' }, revenue, theme) as unknown as LineOption;
    expect(bars.series[0]?.type).toBe('bar');
    const area = specOption({ ...line, type: 'area', stacked: true, y: [money, { field: 'revenue', label: 'Ещё' }] }, revenue, theme) as unknown as LineOption;
    expect(area.series).toHaveLength(2);
    expect(area.series[0]?.markPoint).toBeUndefined();
  });

  it('builds a pie without an ECharts legend and lists shares', () => {
    expect(specOption(pie, shares, theme)).not.toHaveProperty('legend');
    expect(pieLegend(pie, shares).map((i) => i.value)).toEqual(['45%', '12%']);
    const absolute = { ...pie, y: [{ field: 'share' }] };
    expect(pieLegend(absolute, shares).map((i) => i.value)).toEqual(['78,9%', '21,1%']);
  });

  it('formats measures by kind', () => {
    expect(measureLabel(12.5, { field: 'p', format: 'percent' }, 0)).toBe('12,5%');
    expect(plain(measureLabel(4820, money, 4820))).toBe('4 820 ₽');
    expect(plain(measureLabel(2573, { field: 'n', format: 'int' }, 0))).toBe('2 573');
    expect(isSupported('heatmap')).toBe(false);
  });
});

describe('SpecChart', () => {
  it('renders the pie legend as HTML', () => {
    render(<SpecChart spec={pie} data={shares} height="md" />);
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByText('Москва')).toBeInTheDocument();
  });

  it('labels the chart for screen readers', () => {
    render(<SpecChart spec={{ ...line, subtitle: 'по месяцам' }} data={revenue} />);
    expect(screen.getByRole('img', { name: 'Выручка. по месяцам' })).toBeInTheDocument();
  });

  it('explains unsupported types and empty data', () => {
    const { rerender } = render(<SpecChart spec={{ ...line, type: 'heatmap' }} data={revenue} />);
    expect(screen.getByText('Этот тип графика пока не отображается')).toBeInTheDocument();
    rerender(<SpecChart spec={line} data={{ ...revenue, rows: [] }} />);
    expect(screen.getByText('Нет данных для графика')).toBeInTheDocument();
  });
});
