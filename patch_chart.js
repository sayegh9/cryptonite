const fs = require('fs');
const path = "apps/web/src/components/TradingViewChart.tsx";
let code = fs.readFileSync(path, 'utf8');

const oldUpdate1 = `candleSeriesRef.current.update({
            time: openTimeSec as unknown as Time,
            open: msg.open,
            high: msg.high,
            low: msg.low,
            close: msg.close,
          });`;

const newUpdate1 = `candleSeriesRef.current.update({
            time: openTimeSec as unknown as Time,
            open: Number(msg.open),
            high: Number(msg.high),
            low: Number(msg.low),
            close: Number(msg.close),
          });`;

code = code.replace(oldUpdate1, newUpdate1);

const oldUpdate2 = `volumeSeriesRef.current.update({
            time: openTimeSec as unknown as Time,
            value: msg.volume,
            color: msg.close >= msg.open ? "rgba(16, 185, 129, 0.4)" : "rgba(239, 68, 68, 0.4)",
          });`;

const newUpdate2 = `volumeSeriesRef.current.update({
            time: openTimeSec as unknown as Time,
            value: Number(msg.volume),
            color: Number(msg.close) >= Number(msg.open) ? "rgba(16, 185, 129, 0.4)" : "rgba(239, 68, 68, 0.4)",
          });`;

code = code.replace(oldUpdate2, newUpdate2);

fs.writeFileSync(path, code);
