import { render, screen } from '@testing-library/react';
import { createElement } from 'react';
import type { Schemas } from '@/api/client';
import { pieLegend, specOption } from './spec';
import { SpecChart } from './SpecChart';
import type { ChartTheme } from './theme';

const theme: ChartTheme = {
  ink: '#111', muted: '#777', line: '#ddd', surface: '#fff', font: 'sans-serif', fontSize: 12, radius: '8px',
  primary: '#06c', palette: ['#06c'],
};

const columnCases: { name: string; columns: NonNullable<Schemas['ResultData']['columns']> }[] = [
  { name: 'string', columns: ['category', 'amount'] },
  { name: 'descriptor', columns: [{ name: 'category', type: 'category' }, { name: 'amount', type: 'number', label: 'Сумма' }] },
  { name: 'mixed', columns: ['category', { name: 'amount' }] },
  { name: 'nullable descriptor metadata', columns: [{ name: 'category', type: null, label: null }, { name: 'amount', type: null, label: null }] },
];

interface AxisOption {
  xAxis: { data: string[] };
  series: { data: number[] }[];
  tooltip: { formatter: (points: { dataIndex: number; value: number; seriesIndex: number }[]) => string };
}

interface PieOption {
  series: { data: { name: string; value: number }[] }[];
  tooltip: { formatter: (point: { name: string; value: number }) => string };
}

describe.each(columnCases)('$name result columns', ({ columns }) => {
  const payload = '<img src=x onerror="globalThis.injected=true"> & <svg onload=alert(1)>\'quoted\'</svg>';
  const data: Schemas['ResultData'] = { columns, rows: [[payload, 42], ['B', 8]], row_count: 2, truncated: false };
  const spec: Schemas['ChartSpec'] = { type: 'bar', title: 'Сумма', x: { field: 'category' }, y: [{ field: 'amount' }] };

  function expectSafeTooltip(html: string) {
    // Match the HTML sink used by ECharts, including entity decoding by the browser.
    const tooltip = document.createElement('div');
    tooltip.innerHTML = html;
    expect(tooltip.querySelector('img, svg, script, [onerror], [onload]')).toBeNull();
    expect(tooltip.textContent).toContain(payload);
    expect(tooltip.querySelector('b')?.textContent).toBe('42');
  }

  it('preserves axis values and renders an untrusted category as tooltip text', () => {
    const option = specOption(spec, data, theme) as unknown as AxisOption;
    expect(option.xAxis.data).toEqual([payload, 'B']);
    expect(option.series[0]?.data).toEqual([42, 8]);
    expectSafeTooltip(option.tooltip.formatter([{ dataIndex: 0, value: 42, seriesIndex: 0 }]));
  });

  it('preserves pie values and renders an untrusted item as tooltip text', () => {
    const option = specOption({ ...spec, type: 'pie' }, data, theme) as unknown as PieOption;
    expect(option.series[0]?.data).toEqual([{ name: payload, value: 42 }, { name: 'B', value: 8 }]);
    expect(pieLegend({ ...spec, type: 'pie' }, data).map((item) => item.label)).toEqual([payload, 'B']);
    expectSafeTooltip(option.tooltip.formatter({ name: payload, value: 42 }));
  });

  it('escapes an invalid time label that falls back to the original data', () => {
    const option = specOption({ ...spec, type: 'line', x: { field: 'category', kind: 'time' } }, data, theme) as unknown as AxisOption;
    expectSafeTooltip(option.tooltip.formatter([{ dataIndex: 0, value: 42, seriesIndex: 0 }]));
  });
});

describe('defaulted backend result fields', () => {
  const spec: Schemas['ChartSpec'] = { type: 'bar', title: 'Сумма', x: { field: 'category' }, y: [{ field: 'amount' }] };

  it('treats omitted rows and columns as an empty result', () => {
    const data: Schemas['ResultData'] = {};
    const option = specOption(spec, data, theme) as unknown as AxisOption;
    expect(option.xAxis.data).toEqual([]);
    expect(option.series[0]?.data).toEqual([]);
    expect(pieLegend({ ...spec, type: 'pie' }, data)).toEqual([]);
    render(createElement(SpecChart, { spec, data }));
    expect(screen.getByText('Нет данных для графика')).toBeInTheDocument();
  });

  it('preserves row positions when column definitions are omitted', () => {
    const option = specOption(spec, { rows: [['A', 42]] }, theme) as unknown as AxisOption;
    expect(option.xAxis.data).toEqual(['null']);
    expect(option.series[0]?.data).toEqual([null]);
  });
});
