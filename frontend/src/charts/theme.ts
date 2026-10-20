/** Chart colors and type read from CSS tokens, so charts follow the active theme. */
export interface ChartTheme {
  ink: string;
  muted: string;
  line: string;
  surface: string;
  font: string;
  fontSize: number;
  radius: string;
  /** --chart-1, the main series color. */
  primary: string;
  palette: string[];
}

export function readChartTheme(element: Element): ChartTheme {
  const style = getComputedStyle(element);
  const value = (name: string) => style.getPropertyValue(name).trim();
  return {
    ink: value('--foreground'),
    muted: value('--muted-foreground'),
    line: value('--border'),
    surface: value('--card'),
    font: value('--font-sans'),
    fontSize: Number.parseInt(value('--text-xs'), 10) || 12,
    radius: value('--radius-sm'),
    primary: value('--chart-1'),
    palette: [1, 2, 3, 4, 5, 6].map((index) => value(`--chart-${index}`)),
  };
}
