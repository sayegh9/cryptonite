with open("src/components/TradingViewChart.tsx", "r") as f:
    code = f.read()

code = code.replace('import { createChart, ColorType, IChartApi, ISeriesApi, Time, HistogramSeries } from "lightweight-charts";', 'import { createChart, ColorType, IChartApi, ISeriesApi, Time, HistogramSeries, LineSeries } from "lightweight-charts";')
code = code.replace('<DialogTrigger asChild>', '<DialogTrigger>')

with open("src/components/TradingViewChart.tsx", "w") as f:
    f.write(code)
