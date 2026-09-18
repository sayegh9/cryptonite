with open("src/components/TradingViewChart.tsx", "r") as f:
    code = f.read()

bad = """            <DialogTrigger>
              <button className="flex items-center space-x-1 px-2 py-0.5 text-[11px] font-medium rounded text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60 transition-colors border border-transparent hover:border-zinc-700">
                <Settings2 className="w-3 h-3" />
                <span>Indicators</span>
              </button>
            </DialogTrigger>"""

good = """            <DialogTrigger className="flex items-center space-x-1 px-2 py-0.5 text-[11px] font-medium rounded text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60 transition-colors border border-transparent hover:border-zinc-700">
              <Settings2 className="w-3 h-3" />
              <span>Indicators</span>
            </DialogTrigger>"""

code = code.replace(bad, good)

with open("src/components/TradingViewChart.tsx", "w") as f:
    f.write(code)
