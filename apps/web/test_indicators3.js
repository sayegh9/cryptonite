const { VWAP, KST, Stochastic } = require("technicalindicators");
const highs = [10, 12, 11, 13, 14, 15, 12, 11, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20];
const lows = [8, 9, 8, 10, 11, 12, 9, 8, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17];
const closes = [9, 11, 10, 12, 13, 14, 10, 9, 8, 11, 12, 13, 14, 15, 16, 17, 18, 19];
const volumes = [100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250, 260, 270];

console.log("VWAP", VWAP.calculate({ high: highs, low: lows, close: closes, volume: volumes }).slice(-1));
