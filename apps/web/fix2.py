with open("src/components/TradingViewChart.tsx", "r") as f:
    code = f.read()

bad = '''    const volumeSeries = chart.addSeries(HistogramSeries,
  LineSeries, {'''
good = '''    const volumeSeries = chart.addSeries(HistogramSeries, {'''

code = code.replace(bad, good)

with open("src/components/TradingViewChart.tsx", "w") as f:
    f.write(code)
