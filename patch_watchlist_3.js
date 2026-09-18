const fs = require('fs');
const path = "apps/web/src/components/WatchlistPanel.tsx";
let code = fs.readFileSync(path, 'utf8');

const oldRender = `<span className="text-[10px] text-zinc-500 truncate max-w-[110px]">
                  {item.name}
                </span>
              </div>

              <div className="flex flex-col items-end">`;

const newRender = `<span className="text-[10px] text-zinc-500 truncate max-w-[110px]">
                  {item.name}
                </span>
              </div>

              <div className="flex items-center gap-3">
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setTickers(prev => prev.filter(t => t.symbol !== item.symbol));
                  }}
                  className="opacity-0 group-hover:opacity-100 text-zinc-500 hover:text-red-400 text-xs px-1"
                >
                  ×
                </button>
                <div className="flex flex-col items-end">`;

// I need to add 'group' class to the parent div
const oldWrapper = `className={\`flex items-center justify-between px-4 py-3 cursor-pointer transition-colors border-b border-white/5 \${`;
const newWrapper = `className={\`group flex items-center justify-between px-4 py-3 cursor-pointer transition-colors border-b border-white/5 \${`;

code = code.replace(oldRender, newRender);
code = code.replace(oldWrapper, newWrapper);
fs.writeFileSync(path, code);
