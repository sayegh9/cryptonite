# ARCHITECTURE.md - System Interfaces & Contract Map

This document maps all high-level contracts, database schemas, Redis communication channels, and core TypeScript/Python types across the Cryptonite platform.

---

## 1. System Communication Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Next.js Web Frontend                     │
│  (AI Brain Workspace | Research Lab | Terminal | Operator)  │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP / JSON & WebSocket
┌──────────────────────────────▼──────────────────────────────┐
│                  FastAPI Gateway (:8000)                    │
│      (REST Endpoints, /agent-api Proxy, WS Stream Fanout)   │
└──────────────┬───────────────────────────────┬──────────────┘
               │ Redis Streams / PubSub        │ Asyncpg / SQLAlchemy
┌──────────────▼─────────────┐   ┌─────────────▼──────────────┐
│       Redis 7 (:6379)      │   │   PostgreSQL 16 (:5432)    │
│  - stream:debate_queue     │   │   - strategies             │
│  - stream:order_signals    │   │   - strategy_versions      │
│  - channel:agent_heartbeat │   │   - backtests / tearsheets │
│  - stream:market_bars      │   │   - agent_reflections (pgv)│
└──────────────┬─────────────┘   └────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────────┐
│                    Backend Worker Services                  │
│  - debate_worker: Multi-agent debate & consensus engine     │
│  - ingest: Binance Kline WebSocket ingester                 │
│  - backtester: NautilusTrader simulation node               │
│  - executor: Order execution & risk dispatcher              │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Shared Frontend State Contracts (Zustand)

### A. `useChatStore` (`apps/web/src/lib/stores/useChatStore.ts`)
```typescript
interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp?: number;
  dispatches?: any[];
}

interface ChatState {
  threads: Record<string, ChatMessage[]>; // Keyed by agentMode (e.g., "research", "strategist", "explain")
  addMessage: (threadKey: string, message: ChatMessage) => void;
  setMessages: (threadKey: string, messages: ChatMessage[]) => void;
  clearThread: (threadKey: string) => void;
}
```

### B. `useBacktestStore` (`apps/web/src/lib/stores/useBacktestStore.ts`)
```typescript
interface BacktestState {
  activeRun: BacktestRun | null;
  strategies: Strategy[];
  selectedSlug: string;
  setActiveRun: (run: BacktestRun | null) => void;
  setStrategies: (strategies: Strategy[]) => void;
  setSelectedSlug: (slug: string) => void;
}
```

### C. `useDebateStore` (`apps/web/src/lib/stores/useDebateStore.ts`)
```typescript
interface DebateTurn {
  runId: string;
  role: string;
  content: string;
  timestamp: number;
}

interface DebateRun {
  runId: string;
  instrumentId: string;
  status: "RUNNING" | "COMPLETED" | "REJECTED" | "FAILED";
  decision?: string;
  timestamp?: number;
  turns: DebateTurn[];
}
```

---

## 3. Cross-Feature Event Bus Contracts (`window.dispatchEvent`)

| Event Name | Payload Schema | Source Component | Target Listener |
| :--- | :--- | :--- | :--- |
| `cryptonite:load-strategy` | `{ manifest: { slug, name, default_parameters, timeframe, instrument_id }, codeText: string }` | `StrategyProposalCard` (Copilot) | `ResearchLab.tsx` |
| `cryptonite:open-copilot` | `{ mode: "explain" \| "strategist", prompt?: string, autoSend?: boolean }` | `TearsheetCards.tsx`, `ActionInbox.tsx`, `StrategyInstancesPanel.tsx` | `AgentChatPanel.tsx` |
| `cryptonite:switch-workspace`| `{ mode: "terminal" \| "research" \| "debates" \| "portfolio" \| "operator" }` | Anywhere | `page.tsx` |
| `cryptonite:open-chart` | `{ symbol: string, timeframe?: string, candleTime?: number }` | Terminal / Scanner | `page.tsx` |

---

## 4. Database Schema (PostgreSQL 16 + pgvector)

### A. Strategies & Versions
```sql
CREATE TABLE strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(255) UNIQUE NOT NULL,
    description TEXT,
    author VARCHAR(100) DEFAULT 'AI Agent',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE strategy_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID REFERENCES strategies(id) ON DELETE CASCADE,
    version_num INT NOT NULL,
    code_hash VARCHAR(64) NOT NULL,
    git_commit VARCHAR(40),
    default_parameters JSONB DEFAULT '{}'::jsonb,
    parameter_schema JSONB DEFAULT '{}'::jsonb,
    python_code TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### B. Backtests & Tearsheets
```sql
CREATE TABLE backtest_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_version_id UUID REFERENCES strategy_versions(id),
    instrument_id VARCHAR(50) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    status VARCHAR(20) NOT NULL, -- PENDING, RUNNING, COMPLETED, FAILED
    metrics JSONB,               -- Sharpe, Sortino, MaxDD, WinRate, PnL
    equity_curve JSONB,
    trades JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### C. Agent Vector Reflections
```sql
CREATE TABLE agent_reflections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id VARCHAR(100) NOT NULL,
    agent_name VARCHAR(50) NOT NULL,
    instrument_id VARCHAR(50) NOT NULL,
    reflection_text TEXT NOT NULL,
    outcome VARCHAR(20) NOT NULL, -- WIN, LOSS, NEUTRAL
    embedding vector(1536),       -- pgvector for semantic retrieval
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 5. Redis Streaming & Pub/Sub Channels

| Channel / Stream | Type | Producers | Consumers | Payload Schema |
| :--- | :--- | :--- | :--- | :--- |
| `channel:agent_heartbeat` | Pub/Sub | `debate_worker` | `stream_gateway.py` → Web UI | `{"type": "HEARTBEAT", "agent_id": str, "status": "ONLINE"\|"PROCESSING"\|"ERROR", "message": str, "timestamp": int}` |
| `stream:debate_queue` | Stream | API Gateway (`/agent-api/debates/trigger`) | `debate_worker` | `{"symbol": str, "timeframe": str, "custom_thesis": str}` |
| `stream:order_signals` | Stream | `debate_worker`, Scanner | `cryptonite_executor` | `{"client_order_id": str, "symbol": str, "side": "BUY"\|"SELL", "quantity": float, "price": float}` |
| `stream:market_bars` | Stream | `cryptonite_ingest` | Scanner, Backtester, WebSocket | `EnrichedBar` JSON payload |

---

## 6. Execution & Risk Guardrails (`GuardedOrderDispatcher`)

Located at: `packages/execution/cryptonite_execution/risk.py`

**Core Invariants Enforced (Synchronous & Non-Bypassable):**
1. **Max Position Sizing Clamp:** Single trade allocation capped at 25% of total portfolio equity.
2. **Global Drawdown Circuit Breaker:** Daily realized loss latching immediately trips the emergency kill-switch.
3. **Transactional Outbox Fencing:** Every staged trade is dual-written to PostgreSQL transactional outbox before dispatching to Redis execution stream.
