with open("src/components/TradingViewChart.tsx", "r") as f:
    code = f.read()

code = code.replace('{/* Timeframe selector tabs */}', '</div>\n          {/* Timeframe selector tabs */}', 1)

with open("src/components/TradingViewChart.tsx", "w") as f:
    f.write(code)
