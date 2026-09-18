const WebSocket = require('ws');
const ws = new WebSocket('ws://127.0.0.1:8000/ws/stream');

ws.on('open', () => {
    ws.send(JSON.stringify({
        action: "subscribe",
        channels: ["open_kline"],
        instruments: ["BTCUSDT.BINANCE"]
    }));
});

ws.on('message', (data) => {
    console.log("MSG:", data.toString());
    process.exit(0);
});

setTimeout(() => {
    console.log("Timeout");
    process.exit(0);
}, 5000);
