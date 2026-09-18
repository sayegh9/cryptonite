const fs = require('fs');

const path = "apps/web/src/components/WatchlistPanel.tsx";
let code = fs.readFileSync(path, 'utf8');

const oldState = `const [tickers, setTickers] = useState<TickerItem[]>(DEFAULT_TICKERS);`;
const newState = `const [tickers, setTickers] = useState<TickerItem[]>(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("watchlist_tickers");
      if (saved) {
        try {
          return JSON.parse(saved);
        } catch (e) {}
      }
    }
    return DEFAULT_TICKERS;
  });

  useEffect(() => {
    localStorage.setItem("watchlist_tickers", JSON.stringify(tickers));
  }, [tickers]);`;

code = code.replace(oldState, newState);

// Let's also fix the onboarding block
const oldOnboard = `const newTicker: TickerItem = {
                    symbol: newSym,
                    name: \`\${newSym} Market\`,
                    price: 0,
                    change24h: 0,
                    high24h: 0,
                    low24h: 0,
                    volume: 0,
                  };
                  setTickers((prev) => [...prev, newTicker]);`;

const newOnboard = `let initialPrice = 0;
                  try {
                    const barsRes = await fetch(\`/api/v1/bars?symbol=\${newSym}&timeframe=1m&limit=1\`);
                    const barsData = await barsRes.json();
                    if (barsData && barsData.length > 0) {
                      initialPrice = Number(barsData[0].close) || 0;
                    }
                  } catch(e) {}
                  
                  const newTicker: TickerItem = {
                    symbol: newSym,
                    name: \`\${newSym} Market\`,
                    price: initialPrice,
                    change24h: 0,
                    high24h: 0,
                    low24h: 0,
                    volume: 0,
                  };
                  setTickers((prev) => {
                    if (prev.some(t => t.symbol === newSym)) return prev;
                    return [...prev, newTicker];
                  });`;

code = code.replace(oldOnboard, newOnboard);

fs.writeFileSync(path, code);
