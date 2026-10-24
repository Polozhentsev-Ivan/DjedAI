import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw/http';
import { API_BASE, type Schemas } from '@/api/client';
import { setAccessToken } from '@/api/session';
import { chartExamples } from '@/fixtures/charts';
import { testQueryClient } from '@/test/renderRoute';
import { server } from '@/test/server';
import { dataError, displayData } from './data';
import { measureLabel } from './format';
import { ResultChart } from './ResultChart';
import { isSupported, pieLegend, specOption } from './spec';
import { SpecChart } from './SpecChart';
import type { ChartTheme } from './theme';

const instance = { setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() };
vi.mock('./echarts', () => ({ echarts: { init: vi.fn(() => instance), getInstanceByDom: vi.fn(() => instance) } }));
const theme: ChartTheme = { ink: '#111', muted: '#777', line: '#ddd', surface: '#fff', font: 'Inter', fontSize: 12, radius: '8px', primary: '#06c', palette: ['#06c', '#3a7'] };
const example = (type: Schemas['ChartSpec']['type']) => chartExamples.find((item) => item.spec.type === type)!;
const plain = (text: string) => text.replace(/\s/g, ' ');
interface Option {
  xAxis: { type: string; data: string[] };
  yAxis: { data: string[] };
  series: { type: string; name: string; data: unknown[]; stack?: string; markPoint?: { data: unknown[] } }[];
  legend: { formatter: (name: string) => string };
  tooltip: { formatter: (point: never) => string };
  visualMap: { min: number; max: number };
}
const optionFor = (type: Schemas['ChartSpec']['type']) => {
  const { spec, data } = example(type);
  return specOption(spec, data, theme) as unknown as Option;
};

describe.each(chartExamples)('hand-authored $spec.type specification', ({ spec, data }) => {
  it('renders its saved rows using the existing design components', () => {
    expect(isSupported(spec.type)).toBe(true);
    expect(dataError(spec, data)).toBeUndefined();
    render(<SpecChart spec={spec} data={data} />);
    if (spec.type === 'table') {
      const table = screen.getByRole('table', { name: spec.title });
      expect(within(table).getByRole('columnheader', { name: 'Доля' })).toBeInTheDocument();
      expect(plain(within(table).getByText(/2\s400\s000/).textContent)).toBe('2 400 000 ₽');
      expect(within(table).getByText('60%')).toBeInTheDocument();
    } else if (spec.type === 'kpi') {
      expect(plain(screen.getByRole('group', { name: `${spec.title}. ${spec.subtitle}` }).textContent)).toContain('4,0 млн ₽');
    } else {
      expect(screen.getByRole('img', { name: `${spec.title}. ${spec.subtitle}` })).toBeInTheDocument();
      const expected = spec.type === 'histogram' ? 'bar' : spec.type === 'box' ? 'boxplot' : spec.type === 'area' ? 'line' : spec.type;
      expect((specOption(spec, data, theme) as unknown as Option).series[0]?.type).toBe(expected);
    }
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows an empty state for an empty saved result', () => {
    render(<SpecChart spec={spec} data={{ ...data, rows: [] }} />);
    expect(screen.getByText('Нет данных для графика')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
});

describe('data mapping without client-side analytical calculations', () => {
  it('preserves numeric scatter coordinates and color groups, dropping only incomplete pairs', () => {
    const { spec, data } = example('scatter');
    const option = specOption(spec, { ...data, rows: [...(data.rows ?? []), [null, 5, 'Москва'], [1, null, 'Казань'], [0, 0, 'Казань']] }, theme) as unknown as Option;
    expect(option.xAxis.type).toBe('value');
    expect(option.series.map((s) => s.data)).toEqual([[[12, 820], [25, 950], [40, 1250]], [[18, 760], [32, 1100], [0, 0]]]);
    expect(option.series.map((s) => option.legend.formatter(s.name))).toEqual(['Москва · Средний чек, ₽', 'Казань · Средний чек, ₽']);
  });

  it.each([
    [null, 'Нет значения', 'Нет значения (пропуск)', '"Нет значения" (строка)'],
    [1, '1', '1 (число)', '"1" (строка)'],
    [true, 'true', 'true (логическое)', '"true" (строка)'],
  ] as const)('keeps scatter categories %s and %s separate despite matching display labels', (first, second, firstLabel, secondLabel) => {
    const { spec } = example('scatter');
    const data = { columns: ['orders', 'average', 'region'], rows: [[1, 10, first], [2, 20, second], [3, 30, first]] };
    expect(dataError(spec, data)).toBeUndefined();
    const option = specOption(spec, data, theme) as unknown as Option;
    expect(option.series.map((s) => s.data)).toEqual([[[1, 10], [3, 30]], [[2, 20]]]);
    expect(new Set(option.series.map((s) => s.name)).size).toBe(2);
    expect(option.series.map((s) => option.legend.formatter(s.name))).toEqual([
      `${firstLabel} · Средний чек, ₽`, `${secondLabel} · Средний чек, ₽`,
    ]);
    option.series.forEach((series, seriesIndex) => {
      const tooltip = document.createElement('div');
      tooltip.innerHTML = option.tooltip.formatter({ value: series.data[0], seriesName: series.name, seriesIndex } as never);
      expect(tooltip.textContent).toContain(option.legend.formatter(series.name));
    });
  });

  it('renders ready histogram intervals and frequencies without binning', () => {
    const option = optionFor('histogram');
    expect(option.xAxis.data).toEqual(['0–500', '500–1000', '1000–1500', '1500–2000', '2000–2500']);
    expect(option.series[0]?.data).toEqual([3, 12, 8, 4, 1]);
  });

  it('places a heatmap cell on the two correct categories without summing rows', () => {
    const option = optionFor('heatmap');
    expect(option.xAxis.data).toEqual(['I', 'II', 'III']);
    expect(option.yAxis.data).toEqual(['Москва', 'Казань']);
    expect(option.series[0]?.data).toEqual([[0, 0, 120000], [1, 0, 180000], [2, 0, 240000], [0, 1, 80000], [1, 1, 150000], [2, 1, 210000]]);
    expect(option.visualMap).toMatchObject({ min: 80000, max: 240000 });
  });

  it('keeps supplied box statistics unchanged and supports a horizontal box', () => {
    const { spec, data } = example('box');
    const option = specOption({ ...spec, orientation: 'horizontal' }, data, theme) as unknown as Option;
    expect(option.series[0]?.data).toEqual([[100, 400, 800, 1200, 1800], [80, 350, 600, 950, 1500], [50, 250, 450, 700, 1200]]);
    expect(option.yAxis.data).toEqual(['Москва', 'Казань', 'Томск']);
    expect(plain(option.tooltip.formatter({ dataIndex: 1 } as never))).toContain('Медиана: <b>600 ₽</b>');
  });

  it('sorts numerically, keeps nulls last and limits a copy of the available rows', () => {
    const { spec } = example('bar');
    const data: Schemas['ResultData'] = { columns: ['region', 'amount'], rows: [['A', 2], ['B', null], ['C', 10], ['D', 10]], row_count: 4 };
    const original = JSON.stringify(data);
    const projection = { ...spec, sort: { field: 'amount', order: 'desc' as const }, limit: 2 };
    expect(displayData(projection, data).rows).toEqual([['C', 10], ['D', 10]]);
    expect(JSON.stringify(data)).toBe(original);
    render(<SpecChart spec={projection} data={data} />);
    expect(screen.getByText(/Показана часть результата: 2 строк/)).toBeInTheDocument();
    expect(pieLegend({ ...projection, type: 'pie' }, data).map((v) => plain(v.value))).toEqual(['10 ₽', '10 ₽']);
  });

  it('pivots pre-aggregated grouped series and keeps gaps distinct from zero', () => {
    const { spec } = example('area');
    const data = { columns: ['month', 'region', 'amount'], rows: [['2026-01-01', 'Москва', 10], ['2026-01-01', 'Казань', 0], ['2026-02-01', 'Москва', 20]] };
    const grouped = { ...spec, color: { field: 'region' }, stacked: true };
    const option = specOption(grouped, data, theme) as unknown as Option;
    expect(option.series.map((s) => s.data)).toEqual([[10, 20], [0, null]]);
    expect(option.series.map((s) => s.stack)).toEqual(['total', 'total']);
    expect(measureLabel(null, undefined, 0)).toBe('—');
    expect(plain(measureLabel(-1.25, undefined, 0))).toBe('−1,25');
  });

  it('maps explicit annotations to retained source points', () => {
    const { spec, data } = example('line');
    const option = specOption({ ...spec, annotations: [{ x: '2026-02-01', text: 'Запуск' }, { x: 'missing', text: 'Нет точки' }] }, data, theme) as unknown as Option;
    expect(option.series[0]?.markPoint?.data).toEqual([{ name: 'Запуск', coord: [1, 2400000], value: 'Запуск' }]);
  });

  it('keeps colliding display labels on separate heatmap coordinates', () => {
    const { spec } = example('heatmap');
    const option = specOption(spec, { columns: ['quarter', 'region', 'amount'], rows: [[1, 'A', 10], ['1', 'A', 20]] }, theme) as unknown as Option;
    expect(option.series[0]?.data).toEqual([[0, 0, 10], [1, 0, 20]]);
  });

  it('uses the measure format belonging to each scatter series', () => {
    const { spec, data } = example('scatter');
    const option = specOption({ ...spec, color: null, y: [{ field: 'average', format: 'money' }, { field: 'orders', format: 'percent' }] }, data, theme) as unknown as Option;
    expect(option.tooltip.formatter({ value: [12, 12.5], seriesName: 'Доля', seriesIndex: 1 } as never)).toContain('12,5%');
  });

  it('keeps measure formats across typed scatter groups', () => {
    const { spec } = example('scatter');
    const data = { columns: ['orders', 'average', 'region'], rows: [[1, 10, null], [2, 20, 'Нет значения']] };
    const option = specOption({ ...spec, y: [...(spec.y ?? []), { field: 'orders', label: 'Доля', format: 'percent' }] }, data, theme) as unknown as Option;
    expect(option.series.map((s) => s.data)).toEqual([[[1, 10]], [[2, 20]], [[1, 1]], [[2, 2]]]);
    expect(new Set(option.series.map((s) => s.name)).size).toBe(4);
    expect(plain(option.tooltip.formatter({ value: [2, 20], seriesName: option.series[1]!.name, seriesIndex: 1 } as never))).toContain('20 ₽');
    expect(option.tooltip.formatter({ value: [2, 2], seriesName: option.series[3]!.name, seriesIndex: 3 } as never)).toContain('2%');
  });

  it('preserves gaps in a heatmap and handles a constant zero scale', () => {
    const { spec } = example('heatmap');
    const option = specOption(spec, { columns: ['quarter', 'region', 'amount'], rows: [['I', 'A', 0], ['II', 'A', null]] }, theme) as unknown as Option;
    expect(option.series[0]?.data).toEqual([[0, 0, 0]]);
    expect(option.visualMap).toMatchObject({ min: 0, max: 0 });
  });

  it('keeps horizontal bar annotations on the matching row', () => {
    const { spec, data } = example('bar');
    const option = specOption({ ...spec, orientation: 'horizontal', annotations: [{ x: 'Казань', text: 'Запуск' }] }, data, theme) as unknown as Option;
    expect(option.series[0]?.markPoint?.data).toEqual([{ name: 'Запуск', coord: [1800000, 1], value: 'Запуск' }]);
  });

  it('uses numerical X as horizontal bar categories without swapping the measure', () => {
    const { spec } = example('bar');
    const option = specOption({ ...spec, x: { field: 'year', kind: 'number' }, orientation: 'horizontal' }, { columns: ['year', 'amount'], rows: [[2024, 10], [2025, 25]] }, theme) as unknown as Option;
    expect(option.series[0]?.data).toEqual([
      { value: 10, itemStyle: { color: '#06c', opacity: 1, borderRadius: [0, 4, 4, 0] } },
      { value: 25, itemStyle: { color: '#06c', opacity: 0.45, borderRadius: [0, 4, 4, 0] } },
    ]);
  });

  it('reads descriptor and string result columns identically for every new plot', () => {
    for (const type of ['scatter', 'histogram', 'heatmap', 'box'] as const) {
      const { spec, data } = example(type);
      const mapped = { ...data, columns: (data.columns ?? []).map((c) => typeof c === 'string' ? { name: c, label: null, type: null } : c) };
      expect(dataError(spec, mapped)).toBeUndefined();
      expect((specOption(spec, mapped, theme) as unknown as Option).series.map((s) => s.data)).toEqual((specOption(spec, data, theme) as unknown as Option).series.map((s) => s.data));
    }
  });

  it('renders table labels, dates, missing cells and literal untrusted text', () => {
    const { spec } = example('table');
    const data = { columns: ['date', 'note', 'amount'], rows: [['2026-01-15', '<img onerror=alert(1)>', null]] };
    const { container } = render(<SpecChart spec={{ ...spec, x: { field: 'date', kind: 'time', label: 'Дата' }, color: { field: 'note', label: 'Заметка' }, y: [{ field: 'amount', label: 'Сумма', format: 'money' }] }} data={data} />);
    expect(screen.getByText('15.01.2026')).toBeInTheDocument();
    expect(screen.getByText('<img onerror=alert(1)>')).toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });

  it('shows every result column for a table without a projection', () => {
    const { spec, data } = example('table');
    render(<SpecChart spec={{ ...spec, x: null, y: [] }} data={data} />);
    expect(screen.getAllByRole('columnheader').map((v) => v.textContent)).toEqual(['region', 'amount', 'share']);
  });
});

describe('incompatible and missing data', () => {
  it.each([
    ['scatter', { columns: ['orders', 'average', 'region'], rows: [['text', 5, 'A']] }, 'числа'],
    ['histogram', { columns: ['interval', 'count'], rows: [['A', -1]] }, 'отрицательными'],
    ['box', { columns: ['region', 'low', 'q1', 'median', 'q3', 'high'], rows: [['A', 1, 3, 2, 4, 5]] }, 'возрастанию'],
    ['box', { columns: ['region', 'low', 'q1', 'median', 'q3', 'high'], rows: [['A', 1, null, 2, 4, 5]] }, 'полными'],
    ['heatmap', { columns: ['quarter', 'region', 'amount'], rows: [['I', 'A', 5], ['I', 'A', 7]] }, 'несколько строк'],
    ['kpi', { columns: ['amount'], rows: [[1], [2]] }, 'одной строкой'],
    ['kpi', { columns: ['amount'], rows: [[1]], truncated: true }, 'полный результат'],
    ['bar', { columns: ['wrong'], rows: [[1]] }, 'нет одного из полей'],
  ] as const)('rejects %s with incompatible rows', (type, data, text) => {
    const { spec } = example(type);
    expect(dataError(spec, data as unknown as Schemas['ResultData'])).toContain(text);
    render(<SpecChart spec={spec} data={data as unknown as Schemas['ResultData']} />);
    expect(screen.getByRole('alert')).toHaveTextContent(text);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('does not turn a missing KPI into a zero', () => {
    const { spec } = example('kpi');
    render(<SpecChart spec={spec} data={{ columns: ['amount'], rows: [[null]] }} />);
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(screen.queryByText('0 ₽')).not.toBeInTheDocument();
  });

  it.each(['scatter', 'heatmap', 'histogram', 'line', 'bar', 'pie'] as const)('explains %s without plottable values', (type) => {
    const { spec, data } = example(type);
    const empty = { ...data, rows: (data.rows ?? []).map((row) => row.map((v) => typeof v === 'number' ? null : v)) };
    render(<SpecChart spec={spec} data={empty} />);
    expect(screen.getByText('Нет значений для графика')).toBeInTheDocument();
  });

  it.each(['scatter', 'heatmap', 'box', 'histogram'] as const)('escapes %s tooltip HTML', (type) => {
    const { spec, data } = example(type);
    const payload = '<img src=x onerror=alert(1)>';
    const marked = { ...data, rows: (data.rows ?? []).map((row) => row.map((v) => typeof v === 'string' ? payload : v)) };
    const option = specOption({ ...spec, x: spec.x ? { ...spec.x, label: payload } : null }, marked, theme) as unknown as Option;
    const tooltip = document.createElement('div');
    tooltip.innerHTML = option.tooltip.formatter({ dataIndex: 0, value: type === 'heatmap' ? [0, 0, 120000] : type === 'scatter' ? [12, 820] : 3, seriesName: payload, seriesIndex: 0, name: payload } as never);
    expect(tooltip.querySelector('img, script, [onerror]')).toBeNull();
    expect(tooltip.textContent).toContain(payload);
  });
});

describe('result data API', () => {
  it('loads the spec result_id, shows the API error, and retries only on request', async () => {
    const { spec, data } = example('heatmap');
    let calls = 0;
    setAccessToken('test-session');
    server.use(http.get(`${API_BASE}/results/${spec.result_id}/data`, ({ request }) => {
      expect(request.headers.get('Authorization')).toBe('Bearer test-session');
      calls += 1;
      return calls === 1 ? HttpResponse.json({ error: { code: 'not_found', message: 'Результат не найден' } }, { status: 404 }) : HttpResponse.json(data);
    }));
    render(<QueryClientProvider client={testQueryClient()}><ResultChart spec={spec} /></QueryClientProvider>);
    expect(screen.getByText('Загружаем данные графика…')).toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('Результат не найден');
    expect(calls).toBe(1);
    await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByRole('img')).toHaveAccessibleName(`${spec.title}. ${spec.subtitle}`);
    expect(calls).toBe(2);
  });
});
