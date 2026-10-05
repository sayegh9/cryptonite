import {
  AreaSeries,
  BarSeries,
  BaselineSeries,
  CandlestickSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
} from "lightweight-charts";
import type { ChartStyle } from "./bars";

/** The chart's main price series for a style (the only lightweight-charts runtime use outside the component). */
export function createMainSeries(chart: IChartApi, style: ChartStyle): ISeriesApi<any> {
  switch (style) {
    case "bar":
      return chart.addSeries(BarSeries, { upColor: "#10b981", downColor: "#ef4444" });
    case "line":
      return chart.addSeries(LineSeries, { color: "#38bdf8", lineWidth: 2 });
    case "area":
      return chart.addSeries(AreaSeries, {
        topColor: "rgba(56, 189, 248, 0.4)",
        bottomColor: "rgba(56, 189, 248, 0.0)",
        lineColor: "#38bdf8",
        lineWidth: 2,
      });
    case "baseline":
      return chart.addSeries(BaselineSeries, {
        topLineColor: "#10b981",
        topFillColor1: "rgba(16, 185, 129, 0.28)",
        topFillColor2: "rgba(16, 185, 129, 0.05)",
        bottomLineColor: "#ef4444",
        bottomFillColor1: "rgba(239, 68, 68, 0.05)",
        bottomFillColor2: "rgba(239, 68, 68, 0.28)",
        lineWidth: 2,
      });
    case "candlestick":
    default:
      return chart.addSeries(CandlestickSeries, {
        upColor: "#10b981",
        downColor: "#ef4444",
        borderVisible: false,
        wickUpColor: "#10b981",
        wickDownColor: "#ef4444",
      });
  }
}
