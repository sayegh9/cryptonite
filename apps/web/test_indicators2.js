const { SMA, ADX, ForceIndex, MFI, ATR, CCI, WilliamsR } = require("technicalindicators");

const highs = [10, 12, 11, 13, 14, 15, 12, 11, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20];
const lows = [8, 9, 8, 10, 11, 12, 9, 8, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17];
const closes = [9, 11, 10, 12, 13, 14, 10, 9, 8, 11, 12, 13, 14, 15, 16, 17, 18, 19];
const volumes = [100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250, 260, 270];

console.log("ATR:", ATR.calculate({ period: 5, high: highs, low: lows, close: closes }).slice(-1));
console.log("CCI:", CCI.calculate({ period: 5, open: closes, high: highs, low: lows, close: closes }).slice(-1));
console.log("WilliamsR:", WilliamsR.calculate({ period: 5, high: highs, low: lows, close: closes }).slice(-1));
console.log("ForceIndex:", ForceIndex.calculate({ period: 5, close: closes, volume: volumes }).slice(-1));
console.log("MFI:", MFI.calculate({ period: 5, high: highs, low: lows, close: closes, volume: volumes }).slice(-1));
console.log("ADX:", ADX.calculate({ period: 5, high: highs, low: lows, close: closes }).slice(-1));
