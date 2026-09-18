const { AverageGain, AverageLoss } = require("technicalindicators");
const closes = [9, 11, 10, 12, 13, 14, 10, 9, 8, 11, 12, 13, 14, 15, 16, 17, 18, 19];
console.log("AverageGain", AverageGain.calculate({ period: 5, values: closes }).slice(-1));
console.log("AverageLoss", AverageLoss.calculate({ period: 5, values: closes }).slice(-1));
