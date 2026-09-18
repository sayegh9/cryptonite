with open("src/components/TradingViewChart.tsx", "r") as f:
    code = f.read()

code = code.replace(
    'else if (ind.type === "CCI") values = CCI.calculate({ period: ind.period, open: opens, high: highs, low: lows, close: closes });',
    'else if (ind.type === "CCI") values = CCI.calculate({ period: ind.period, high: highs, low: lows, close: closes });'
)

with open("src/components/TradingViewChart.tsx", "w") as f:
    f.write(code)
