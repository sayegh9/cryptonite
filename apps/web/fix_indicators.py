with open("src/components/TradingViewChart.tsx", "r") as f:
    code = f.read()

# 1. Update rawBarsRef.current during fetchHistorical
code = code.replace(
    'const candles = rawBars.map((b: any) => ({',
    'rawBarsRef.current = rawBars;\n        const candles = rawBars.map((b: any) => ({'
)

# 2. Make updateIndicators robust to time property vs bar_open_ns
bad_time_logic = '''      const indData = values.map((val, i) => ({
        time: (Math.floor(bars[i + offset].bar_open_ns / 1_000_000_000)) as unknown as Time,
        value: val,
      }));'''

good_time_logic = '''      const indData = values.map((val, i) => {
        const b = bars[i + offset];
        const t = b.time !== undefined ? b.time : Math.floor((b.bar_open_ns || 0) / 1_000_000_000);
        return {
          time: t as unknown as Time,
          value: val,
        };
      });'''
code = code.replace(bad_time_logic, good_time_logic)

# 3. Handle live update on closed_bar_committed
bad_closed = '''      // Handle durable closed bar committed events
      if (msg.type === "closed_bar_committed") {
        if (msg.instrument_id !== instId || msg.timeframe !== timeframe) return;

        const openTimeSec = Math.floor(msg.bar_open_ns / 1_000_000_000);
        candleSeriesRef.current.update({
          time: openTimeSec as unknown as Time,
          open: Number(msg.open),
          high: Number(msg.high),
          low: Number(msg.low),
          close: Number(msg.close),
        });
        volumeSeriesRef.current.update({
          time: openTimeSec as unknown as Time,
          value: Number(msg.volume),
          color: Number(msg.close) >= Number(msg.open) ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)",
        });
      }'''

good_closed = '''      // Handle durable closed bar committed events
      if (msg.type === "closed_bar_committed") {
        if (msg.instrument_id !== instId || msg.timeframe !== timeframe) return;

        const openTimeSec = Math.floor(msg.bar_open_ns / 1_000_000_000);
        candleSeriesRef.current.update({
          time: openTimeSec as unknown as Time,
          open: Number(msg.open),
          high: Number(msg.high),
          low: Number(msg.low),
          close: Number(msg.close),
        });
        volumeSeriesRef.current.update({
          time: openTimeSec as unknown as Time,
          value: Number(msg.volume),
          color: Number(msg.close) >= Number(msg.open) ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)",
        });
        
        // Push to raw bars and recalculate indicators if any exist
        rawBarsRef.current.push({
            time: openTimeSec,
            open: Number(msg.open),
            high: Number(msg.high),
            low: Number(msg.low),
            close: Number(msg.close),
            volume: Number(msg.volume),
        });
        if (rawBarsRef.current.length > 2000) rawBarsRef.current.shift(); // prevent unbounded memory
        updateIndicators(rawBarsRef.current);
      }'''
code = code.replace(bad_closed, good_closed)

# 4. Trigger initial render
bad_init = '''        const volumes = rawBars.map((b: any) => ({
          time: b.time as Time,
          value: Number(b.volume),
          color: Number(b.close) >= Number(b.open) ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)",
        }));

        rawBars.forEach((b: any) => {'''

good_init = '''        const volumes = rawBars.map((b: any) => ({
          time: b.time as Time,
          value: Number(b.volume),
          color: Number(b.close) >= Number(b.open) ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)",
        }));
        
        updateIndicators(rawBars);

        rawBars.forEach((b: any) => {'''
code = code.replace(bad_init, good_init)

with open("src/components/TradingViewChart.tsx", "w") as f:
    f.write(code)

