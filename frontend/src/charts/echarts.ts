// Tree-shaken ECharts build: only the series and components the screens use.
import { BarChart, BoxplotChart, HeatmapChart, LineChart, PieChart, ScatterChart } from 'echarts/charts';
import { GridComponent, LegendScrollComponent, MarkPointComponent, TooltipComponent, VisualMapContinuousComponent } from 'echarts/components';
import * as echarts from 'echarts/core';
import { SVGRenderer } from 'echarts/renderers';

echarts.use([LineChart, BarChart, PieChart, ScatterChart, HeatmapChart, BoxplotChart, GridComponent, TooltipComponent, MarkPointComponent, LegendScrollComponent, VisualMapContinuousComponent, SVGRenderer]);

export { echarts };
export type { EChartsCoreOption } from 'echarts/core';
