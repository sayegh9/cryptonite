import re

with open("apps/web/src/components/AgentChatPanel.tsx", "r") as f:
    code = f.read()

# Replace mode type
code = code.replace(
    'const [mode, setMode] = useState<"research" | "explain">("research");',
    'const [mode, setMode] = useState<"research" | "explain" | "strategist">("research");'
)

# Replace tabs
tabs_old = """      <div className="flex bg-zinc-900 border-b border-zinc-800 p-1">
        <button
          onClick={() => setMode("research")}
          className={`flex-1 py-2 text-xs font-bold uppercase rounded ${
            mode === "research" ? "bg-zinc-800 text-cyan-400" : "text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Research Agent
        </button>
        <button
          onClick={() => setMode("explain")}
          className={`flex-1 py-2 text-xs font-bold uppercase rounded ${
            mode === "explain" ? "bg-zinc-800 text-cyan-400" : "text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Explainer Agent
        </button>
      </div>"""

tabs_new = """      <div className="flex bg-zinc-900 border-b border-zinc-800 p-1">
        <button
          onClick={() => setMode("research")}
          className={`flex-1 py-2 text-xs font-bold uppercase rounded ${
            mode === "research" ? "bg-zinc-800 text-cyan-400" : "text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Research Agent
        </button>
        <button
          onClick={() => setMode("strategist")}
          className={`flex-1 py-2 text-xs font-bold uppercase rounded ${
            mode === "strategist" ? "bg-zinc-800 text-cyan-400" : "text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Strategist Agent
        </button>
        <button
          onClick={() => setMode("explain")}
          className={`flex-1 py-2 text-xs font-bold uppercase rounded ${
            mode === "explain" ? "bg-zinc-800 text-cyan-400" : "text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Explainer Agent
        </button>
      </div>"""

code = code.replace(tabs_old, tabs_new)

# Update handleSubmit conditions
code = code.replace(
    'if (mode === "explain" && !backtestIdInput.trim()) return;',
    'if (mode === "explain" && !backtestIdInput.trim()) return;\n    if (mode === "strategist" && !inputValue.trim()) return;'
)

# Update handleSubmit fetch
fetch_old = """      const endpoint = mode === "research" ? "/agents/chat" : "/agents/explain";
      const payload = mode === "research" 
        ? { message: userMsg } 
        : { backtest_id: backtestIdInput, message: "Please explain this backtest." };

      // Make raw fetch call since fetchApi uses /api/v1 prefix but agents uses the proxy to 8085
      const res = await fetch(`/api/v1${endpoint}`, {"""

fetch_new = """      const endpoint = mode === "research" ? "/agents/chat" : mode === "explain" ? "/agents/explain" : "/agents/strategist";
      const payload = mode === "research" 
        ? { message: userMsg } 
        : mode === "explain"
        ? { backtest_id: backtestIdInput, message: "Please explain this backtest." }
        : { message: userMsg, instructions: "" };

      // Make raw fetch call since fetchApi uses /api/v1 prefix but agents uses the proxy to 8085
      const res = await fetch(`/api/v1${endpoint}`, {"""

code = code.replace(fetch_old, fetch_new)

# Update input disabled conditions
input_old = """            {mode === "research" && (
              <input
                type="text"
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                placeholder="Ask about markets or strategy code..."
                className="flex-1 bg-zinc-950 border border-zinc-800 rounded-full px-4 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
              />
            )}"""

input_new = """            {mode !== "explain" && (
              <input
                type="text"
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                placeholder={
                  mode === "strategist"
                    ? "Ask Execution Strategist or enter trade query..."
                    : "Ask about markets or strategy code..."
                }
                className="flex-1 bg-zinc-950 border border-zinc-800 rounded-full px-4 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
              />
            )}"""

code = code.replace(input_old, input_new)

# Update submit disabled conditions
btn_old = 'disabled={loading || (mode === "research" && !inputValue.trim()) || (mode === "explain" && !backtestIdInput.trim())}'
btn_new = 'disabled={loading || (mode !== "explain" && !inputValue.trim()) || (mode === "explain" && !backtestIdInput.trim())}'

code = code.replace(btn_old, btn_new)

with open("apps/web/src/components/AgentChatPanel.tsx", "w") as f:
    f.write(code)
