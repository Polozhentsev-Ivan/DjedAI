// Tree-shaken ECharts build: only the series and components the screens use.
import { BarChart, LineChart, PieChart } from 'echarts/charts';
import { GridComponent, MarkPointComponent, TooltipComponent } from 'echarts/components';
import * as echarts from 'echarts/core';
import { SVGRenderer } from 'echarts/renderers';

echarts.use([LineChart, BarChart, PieChart, GridComponent, TooltipComponent, MarkPointComponent, SVGRenderer]);

export { echarts };
export type { EChartsCoreOption } from 'echarts/core';
