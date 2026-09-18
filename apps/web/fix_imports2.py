with open("src/components/TradingViewChart.tsx", "r") as f:
    code = f.read()

code = code.replace('  HistogramSeries,', '  HistogramSeries,\n  LineSeries,')

with open("src/components/TradingViewChart.tsx", "w") as f:
    f.write(code)
