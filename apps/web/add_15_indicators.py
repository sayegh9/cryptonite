with open("src/components/TradingViewChart.tsx", "r") as f:
    code = f.read()

# Replace the imports
old_imp = 'import { SMA, EMA, RSI } from "technicalindicators";'
new_imp = 'import { SMA, EMA, WMA, WEMA, RSI, ATR, ROC, WilliamsR, TRIX, CCI, ForceIndex, MFI, SD, AverageGain, AverageLoss } from "technicalindicators";'
code = code.replace(old_imp, new_imp)

# Replace the type
old_type = '  type: "SMA" | "EMA" | "RSI";'
new_type = '  type: "SMA" | "EMA" | "WMA" | "WEMA" | "RSI" | "ATR" | "ROC" | "WilliamsR" | "TRIX" | "CCI" | "ForceIndex" | "MFI" | "SD" | "AverageGain" | "AverageLoss";'
code = code.replace(old_type, new_type)

# Replace the calculation logic
old_calc = '''      if (ind.type === "SMA") values = SMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "EMA") values = EMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "RSI") values = RSI.calculate({ period: ind.period, values: closes });'''

new_calc = '''      const opens = bars.map(b => Number(b.open));
      const highs = bars.map(b => Number(b.high));
      const lows = bars.map(b => Number(b.low));
      const vols = bars.map(b => Number(b.volume));

      if (ind.type === "SMA") values = SMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "EMA") values = EMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "WMA") values = WMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "WEMA") values = WEMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "RSI") values = RSI.calculate({ period: ind.period, values: closes });
      else if (ind.type === "ATR") values = ATR.calculate({ period: ind.period, high: highs, low: lows, close: closes });
      else if (ind.type === "ROC") values = ROC.calculate({ period: ind.period, values: closes });
      else if (ind.type === "WilliamsR") values = WilliamsR.calculate({ period: ind.period, high: highs, low: lows, close: closes });
      else if (ind.type === "TRIX") values = TRIX.calculate({ period: ind.period, values: closes });
      else if (ind.type === "CCI") values = CCI.calculate({ period: ind.period, open: opens, high: highs, low: lows, close: closes });
      else if (ind.type === "ForceIndex") values = ForceIndex.calculate({ period: ind.period, close: closes, volume: vols });
      else if (ind.type === "MFI") values = MFI.calculate({ period: ind.period, high: highs, low: lows, close: closes, volume: vols });
      else if (ind.type === "SD") values = SD.calculate({ period: ind.period, values: closes });
      else if (ind.type === "AverageGain") values = AverageGain.calculate({ period: ind.period, values: closes });
      else if (ind.type === "AverageLoss") values = AverageLoss.calculate({ period: ind.period, values: closes });'''

code = code.replace(old_calc, new_calc)

# Replace the HTML select dropdown
old_options = '''                    <option value="SMA">SMA</option>
                    <option value="EMA">EMA</option>
                    <option value="RSI">RSI</option>'''

new_options = '''                    <option value="SMA">SMA</option>
                    <option value="EMA">EMA</option>
                    <option value="WMA">WMA</option>
                    <option value="WEMA">WEMA</option>
                    <option value="RSI">RSI</option>
                    <option value="ATR">ATR</option>
                    <option value="ROC">ROC</option>
                    <option value="WilliamsR">Williams %R</option>
                    <option value="TRIX">TRIX</option>
                    <option value="CCI">CCI</option>
                    <option value="ForceIndex">Force Index</option>
                    <option value="MFI">MFI</option>
                    <option value="SD">Standard Deviation</option>
                    <option value="AverageGain">Avg Gain</option>
                    <option value="AverageLoss">Avg Loss</option>'''

code = code.replace(old_options, new_options)

# Update the pane selection logic when adding
old_pane = 'pane: newIndType === "RSI" ? "bottom" : "main",'
new_pane = 'pane: ["SMA", "EMA", "WMA", "WEMA", "SD"].includes(newIndType) ? "main" : "bottom",'
code = code.replace(old_pane, new_pane)

with open("src/components/TradingViewChart.tsx", "w") as f:
    f.write(code)

