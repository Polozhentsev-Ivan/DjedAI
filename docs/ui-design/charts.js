// DjedAI chart options (ECharts). Colors/fonts read from CSS tokens on the target element.
(function () {
  const MONTHS = ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт'];
  const REV = [1800000, 2000000, 2200000, 2300000, 2500000, 2800000, 3400000, 2900000, 2700000, 2800000];
  const REGIONS = [['Москва', 45], ['Санкт-Петербург', 25], ['Казань', 12], ['Екатеринбург', 10], ['Другие', 8]];
  const CATS = [['Электроника', 4100000], ['Бытовая техника', 2600000], ['Одежда', 1900000], ['Дом и сад', 1400000], ['Спорт', 900000]];
  const fmtM = n => (n / 1e6).toFixed(1).replace('.', ',');

  function options(name, T) {
    const font = { fontFamily: T.font, color: T.muted, fontSize: T.fs };
    const tip = { backgroundColor: T.surface, borderColor: T.line, textStyle: { color: T.ink, fontSize: T.fs }, extraCssText: 'box-shadow:none;border-radius:' + T.radius };
    if (name === 'revenue') return {
      textStyle: font, color: [T.pal[0]], grid: { left: 8, right: 20, top: 34, bottom: 4, containLabel: true },
      tooltip: { ...tip, trigger: 'axis', formatter: p => `${p[0].name} 2026<br/><b>${fmtM(p[0].value)} млн ₽</b>` },
      xAxis: { type: 'category', data: MONTHS, boundaryGap: false, axisLine: { lineStyle: { color: T.line } }, axisTick: { show: false }, axisLabel: { color: T.muted } },
      yAxis: { type: 'value', splitLine: { lineStyle: { color: T.line, type: 'dashed' } }, axisLabel: { color: T.muted, formatter: x => fmtM(x) + ' млн' } },
      series: [{ type: 'line', data: REV, symbol: 'circle', symbolSize: 6, lineStyle: { width: 2 }, areaStyle: { opacity: 0.08 },
        markPoint: { symbol: 'circle', symbolSize: 10, data: [{ type: 'max' }], label: { show: true, position: 'top', distance: 8, formatter: p => fmtM(p.value) + ' млн ₽', color: T.ink, fontWeight: 600, fontSize: T.fs } } }]
    };
    if (name === 'regions') return {
      textStyle: font, color: T.pal,
      tooltip: { ...tip, trigger: 'item', formatter: p => `${p.name}: <b>${p.value}%</b>` },
      legend: { orient: 'vertical', left: '56%', top: 'middle', itemGap: 12, icon: 'circle', itemWidth: 8, itemHeight: 8, textStyle: { color: T.ink, fontSize: T.fs },
        formatter: n => n + '  ' + Object.fromEntries(REGIONS)[n] + '%' },
      series: [{ type: 'pie', radius: ['44%', '68%'], center: ['24%', '50%'], label: { show: false }, itemStyle: { borderColor: T.surface, borderWidth: 2 },
        data: REGIONS.map(([name, value]) => ({ name, value })) }]
    };
    return {
      textStyle: font, grid: { left: 8, right: 80, top: 8, bottom: 4, containLabel: true },
      tooltip: { ...tip, trigger: 'axis', axisPointer: { type: 'none' }, formatter: p => `${p[0].name}<br/><b>${fmtM(p[0].value)} млн ₽</b>` },
      xAxis: { type: 'value', show: false },
      yAxis: { type: 'category', inverse: true, data: CATS.map(c => c[0]), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: T.ink } },
      series: [{ type: 'bar', barWidth: 14, data: CATS.map((c, i) => ({ value: c[1], itemStyle: { color: T.pal[0], opacity: i === 0 ? 1 : 0.45, borderRadius: [0, 4, 4, 0] } })),
        label: { show: true, position: 'right', color: T.ink, fontSize: T.fs, formatter: p => fmtM(p.value) + ' млн ₽' } }]
    };
  }

  function render(el, name, theme) {
    if (!el || !name) return;
    if (!window.echarts || !el.isConnected || !el.clientWidth) {
      clearTimeout(el.__retry); el.__retry = setTimeout(() => render(el, name, theme), 200); return;
    }
    const cs = getComputedStyle(el), v = n => cs.getPropertyValue(n).trim();
    const T = { ink: v('--foreground'), muted: v('--muted-foreground'), line: v('--border'), surface: v('--card'), font: v('--font-sans'),
      fs: parseInt(v('--text-xs'), 10) || 12, radius: v('--radius-sm'), pal: [1, 2, 3, 4, 5, 6].map(i => v('--chart-' + i)) };
    const key = name + '|' + theme + '|' + T.pal[0];
    const inst = window.echarts.getInstanceByDom(el) || window.echarts.init(el, null, { renderer: 'svg' });
    if (el.__key !== key) { inst.setOption(options(name, T), true); el.__key = key; }
    if (!el.__ro) { el.__ro = new ResizeObserver(() => inst.resize()); el.__ro.observe(el); }
  }

  window.DjedCharts = { render, options, fmtM };
})();
