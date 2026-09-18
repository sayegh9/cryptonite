const fs = require('fs');

const path = "apps/web/src/components/WatchlistPanel.tsx";
let code = fs.readFileSync(path, 'utf8');

const oldSearchAdd = `onClick={() => {
                  const newTicker: TickerItem = {
                    symbol: inst.symbol,
                    name: \`\${inst.base_currency} / \${inst.quote_currency}\`,
                    price: 0,
                    change24h: 0,
                    high24h: 0,
                    low24h: 0,
                    volume: 0,
                  };
                  setTickers((prev) => [...prev, newTicker]);
                  setSearch("");
                  onSelectSymbol(inst.symbol);
                }}`;

const newSearchAdd = `onClick={async () => {
                  let initialPrice = 0;
                  try {
                    const barsRes = await fetch(\`/api/v1/bars?symbol=\${inst.symbol}&timeframe=1m&limit=1\`);
                    const barsData = await barsRes.json();
                    if (barsData && barsData.length > 0) {
                      initialPrice = Number(barsData[0].close) || 0;
                    }
                  } catch(e) {}
                  
                  const newTicker: TickerItem = {
                    symbol: inst.symbol,
                    name: \`\${inst.base_currency} / \${inst.quote_currency}\`,
                    price: initialPrice,
                    change24h: 0,
                    high24h: 0,
                    low24h: 0,
                    volume: 0,
                  };
                  setTickers((prev) => {
                    if (prev.some(t => t.symbol === inst.symbol)) return prev;
                    return [...prev, newTicker];
                  });
                  setSearch("");
                  onSelectSymbol(inst.symbol);
                }}`;

code = code.replace(oldSearchAdd, newSearchAdd);
fs.writeFileSync(path, code);
