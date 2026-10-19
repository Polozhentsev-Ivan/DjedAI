import { act, render, screen } from '@testing-library/react';
import { setTheme } from '@/lib/theme';
import { Chart } from './Chart';
import { categoriesOption, REGIONS, regionsOption, revenueOption } from './demo';
import { DemoChart } from './DemoChart';
import { readChartTheme, type ChartTheme } from './theme';

const instance = { setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() };
vi.mock('./echarts', () => ({
  echarts: {
    init: vi.fn(() => instance),
    getInstanceByDom: vi.fn(() => instance),
  },
}));

const theme: ChartTheme = {
  ink: '#1d1d1f', muted: '#6e6e73', line: '#e0e0e0', surface: '#fff', font: 'Inter', fontSize: 12, radius: '8px',
  primary: '#0066cc', palette: ['#0066cc', '#34a37a', '#e3a008', '#8e5cd9', '#d9577b', '#7a7a7a'],
};

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

  it('renders the regions legend as HTML', () => {
    render(<DemoChart name="regions" height="md" />);
    expect(screen.getAllByRole('listitem')).toHaveLength(REGIONS.length);
    expect(screen.getByText('Санкт-Петербург')).toBeInTheDocument();
  });

  it('renders other demo charts without a legend', () => {
    render(<DemoChart name="revenue" />);
    expect(screen.getByRole('img', { name: /выручки по месяцам/ })).toBeInTheDocument();
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });
});

describe('demo options', () => {
  it('formats revenue labels in millions', () => {
    const option = revenueOption(theme) as { yAxis: { axisLabel: { formatter: (v: number) => string } }; tooltip: { formatter: (p: { name: string; value: number }[]) => string } };
    expect(option.yAxis.axisLabel.formatter(2500000).replace(/\s/g, ' ')).toBe('2,5 млн');
    expect(option.yAxis.axisLabel.formatter(0).replace(/\s/g, ' ')).toBe('0,0 млн');
    expect(option.tooltip.formatter([{ name: 'Июл', value: 3400000 }])).toContain('Июл 2026');
  });

  it('keeps the pie without an ECharts legend', () => {
    const option = regionsOption(theme) as Record<string, unknown>;
    expect(option).not.toHaveProperty('legend');
  });

  it('highlights the leading category', () => {
    const option = categoriesOption(theme) as { series: { data: { itemStyle: { opacity: number } }[] }[] };
    expect(option.series[0]?.data.map((d) => d.itemStyle.opacity)).toEqual([1, 0.45, 0.45, 0.45, 0.45]);
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
