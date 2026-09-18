const fs = require('fs');

const path = "apps/web/src/components/WatchlistPanel.tsx";
let code = fs.readFileSync(path, 'utf8');

const oldState = `const [tickers, setTickers] = useState<TickerItem[]>(() => {
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

const newState = `const [tickers, setTickers] = useState<TickerItem[]>(DEFAULT_TICKERS);
  const [isLoaded, setIsLoaded] = useState(false);

  useEffect(() => {
    const saved = localStorage.getItem("watchlist_tickers");
    if (saved) {
      try {
        setTickers(JSON.parse(saved));
      } catch (e) {}
    }
    setIsLoaded(true);
  }, []);

  useEffect(() => {
    if (!isLoaded) return;
    localStorage.setItem("watchlist_tickers", JSON.stringify(tickers));
    const client = getStreamClient();
    client.updateSubscriptions(
      ["open_kline", "closed_bar_committed", "signal_candidate"],
      tickers.map(t => \`\${t.symbol}.BINANCE\`)
    );
  }, [tickers, isLoaded]);`;

code = code.replace(oldState, newState);
fs.writeFileSync(path, code);
