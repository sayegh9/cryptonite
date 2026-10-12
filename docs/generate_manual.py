import sys
from pathlib import Path

docs_dir = Path(__file__).parent.resolve()
manual_images_dir = (docs_dir / 'manual_images').resolve()
screenshots_dir = (docs_dir / 'screenshots').resolve()

html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Cryptonite Platform - Comprehensive Enterprise User Manual &amp; Operations Reference</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap');

  @page {{
    size: A4;
    margin: 16mm 14mm 18mm 14mm;
    @bottom-right {{
      content: "Page " counter(page);
      font-family: 'Inter', sans-serif;
      font-size: 8pt;
      color: #71717a;
    }}
    @bottom-left {{
      content: "Cryptonite Platform | Comprehensive Operator & Quantitative Operations Manual";
      font-family: 'Inter', sans-serif;
      font-size: 8pt;
      color: #71717a;
    }}
  }}

  * {{
    box-sizing: border-box;
  }}

  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    color: #18181b;
    background: #ffffff;
    line-height: 1.55;
    font-size: 9pt;
    margin: 0;
    padding: 0;
  }}

  /* Page Break Utilities */
  .page-break {{
    page-break-after: always;
    break-after: page;
  }}
  .avoid-break {{
    page-break-inside: avoid;
    break-inside: avoid;
  }}

  /* Cover Page */
  .cover-page {{
    height: 100vh;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 30px 10px;
    page-break-after: always;
  }}

  .cover-badge {{
    display: inline-block;
    background: #0f172a;
    color: #38bdf8;
    padding: 6px 14px;
    border-radius: 9999px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 8.5pt;
    font-weight: 600;
    letter-spacing: 0.05em;
    width: fit-content;
    margin-bottom: 20px;
    border: 1px solid #1e293b;
  }}

  .cover-title {{
    font-size: 32pt;
    font-weight: 800;
    line-height: 1.15;
    color: #09090b;
    margin: 0 0 14px 0;
    letter-spacing: -0.03em;
  }}

  .cover-subtitle {{
    font-size: 13pt;
    font-weight: 400;
    color: #52525b;
    margin: 0 0 24px 0;
    line-height: 1.5;
  }}

  .cover-meta {{
    border-top: 2px solid #e4e4e7;
    padding-top: 20px;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    font-size: 8.5pt;
  }}

  .cover-meta-item strong {{
    display: block;
    color: #71717a;
    text-transform: uppercase;
    font-size: 7pt;
    letter-spacing: 0.08em;
    margin-bottom: 3px;
  }}

  .cover-meta-item span {{
    color: #09090b;
    font-weight: 600;
  }}

  /* Typography */
  h1 {{
    font-size: 17.5pt;
    font-weight: 700;
    color: #09090b;
    margin: 24px 0 10px 0;
    border-bottom: 1.5px solid #e4e4e7;
    padding-bottom: 6px;
    letter-spacing: -0.02em;
  }}

  h2 {{
    font-size: 12.5pt;
    font-weight: 600;
    color: #0f172a;
    margin: 18px 0 8px 0;
    letter-spacing: -0.01em;
  }}

  h3 {{
    font-size: 10pt;
    font-weight: 600;
    color: #27272a;
    margin: 12px 0 6px 0;
  }}

  p {{
    margin: 0 0 8px 0;
    color: #3f3f46;
  }}

  ul, ol {{
    margin: 0 0 10px 0;
    padding-left: 18px;
    color: #3f3f46;
  }}

  li {{
    margin-bottom: 4px;
  }}

  /* Code & Pre */
  code {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 8pt;
    background: #f4f4f5;
    padding: 2px 4px;
    border-radius: 4px;
    color: #0284c7;
    border: 1px solid #e4e4e7;
  }}

  pre {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 7.5pt;
    background: #09090b;
    color: #f4f4f5;
    padding: 10px 12px;
    border-radius: 6px;
    overflow-x: auto;
    margin: 8px 0 12px 0;
    line-height: 1.45;
  }}

  pre code {{
    background: transparent;
    border: none;
    padding: 0;
    color: inherit;
  }}

  /* Tables */
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0 14px 0;
    font-size: 8pt;
  }}

  th, td {{
    padding: 6px 8px;
    text-align: left;
    border-bottom: 1px solid #e4e4e7;
  }}

  th {{
    background: #f8fafc;
    color: #0f172a;
    font-weight: 600;
    border-top: 1px solid #cbd5e1;
    border-bottom: 2px solid #cbd5e1;
  }}

  tr:nth-child(even) td {{
    background: #fafafa;
  }}

  /* Callout Boxes */
  .callout {{
    border-radius: 6px;
    padding: 8px 12px;
    margin: 10px 0;
    font-size: 8.5pt;
  }}

  .callout-info {{
    background: #f0f9ff;
    border-left: 4px solid #0284c7;
    color: #0369a1;
  }}

  .callout-warning {{
    background: #fffbeb;
    border-left: 4px solid #f59e0b;
    color: #b45309;
  }}

  .callout-danger {{
    background: #fef2f2;
    border-left: 4px solid #ef4444;
    color: #991b1b;
  }}

  .callout-success {{
    background: #f0fdf4;
    border-left: 4px solid #16a34a;
    color: #15803d;
  }}

  .callout strong {{
    display: block;
    margin-bottom: 2px;
  }}

  /* Figures & Image Containers */
  .figure {{
    margin: 10px 0 14px 0;
    text-align: center;
    page-break-inside: avoid;
  }}

  .figure img {{
    width: 100%;
    max-height: 400px;
    object-fit: contain;
    border: 1px solid #d4d4d8;
    border-radius: 6px;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
    background: #18181b;
  }}

  .figure-caption {{
    font-size: 7.5pt;
    color: #71717a;
    margin-top: 5px;
    font-weight: 500;
  }}

  /* Diagram Canvas Styling */
  .diagram-box {{
    background: #090d16;
    border: 1px solid #1e293b;
    border-radius: 8px;
    padding: 14px;
    margin: 12px 0;
    page-break-inside: avoid;
    box-shadow: inset 0 2px 4px rgba(0,0,0,0.5);
  }}

  .diagram-title {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 8pt;
    font-weight: 700;
    color: #38bdf8;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}

  .diagram-title::before {{
    content: "";
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #38bdf8;
  }}

  /* Table of Contents */
  .toc {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 18px;
    margin-bottom: 20px;
  }}

  .toc-title {{
    font-size: 12.5pt;
    font-weight: 700;
    margin-top: 0;
    margin-bottom: 10px;
    color: #0f172a;
    border-bottom: 1px solid #cbd5e1;
    padding-bottom: 5px;
  }}

  .toc-list {{
    list-style: none;
    padding: 0;
    margin: 0;
  }}

  .toc-item {{
    display: flex;
    justify-content: space-between;
    padding: 4px 0;
    border-bottom: 1px dotted #cbd5e1;
    font-size: 8.5pt;
  }}

  .toc-item a {{
    color: #0284c7;
    text-decoration: none;
    font-weight: 500;
  }}

  .step-box {{
    background: #fafafa;
    border: 1px solid #e4e4e7;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 8px 0 12px 0;
  }}

  .step-title {{
    font-weight: 700;
    color: #09090b;
    font-size: 9pt;
    margin-bottom: 4px;
  }}

  .badge {{
    display: inline-block;
    padding: 2px 6px;
    font-size: 7pt;
    font-weight: 600;
    border-radius: 4px;
    text-transform: uppercase;
  }}
  .badge-blue {{ background: #e0f2fe; color: #0369a1; }}
  .badge-green {{ background: #dcfce7; color: #15803d; }}
  .badge-amber {{ background: #fef3c7; color: #b45309; }}
  .badge-red {{ background: #fee2e2; color: #b91c1c; }}
  .badge-purple {{ background: #f3e8ff; color: #7e22ce; }}
</style>
</head>
<body>

<!-- COVER PAGE -->
<div class="cover-page">
  <div>
    <div class="cover-badge">CRYPTONITE ARCHITECTURE &amp; OPERATOR REFERENCE</div>
    <div class="cover-title">Cryptonite Platform<br>User Manual &amp; Multi-Agent AI Operations</div>
    <div class="cover-subtitle">
      The Complete Institutional Operations Guide: Low-Latency Telemetry, Polars Vectorized Screening, Discrete-Event Backtesting, Deterministic Risk Guardrails, and Antigravity Gemini AI Fleet.
    </div>
  </div>

  <div class="figure" style="margin: 10px 0;">
    <img src="file://{manual_images_dir}/01_terminal_overview.png" style="max-height: 380px;" alt="Cryptonite Platform Dashboard">
    <div class="figure-caption">Figure 1.0: Cryptonite High-Frequency Multi-Timeframe Trading Terminal &amp; Intelligence Workstation.</div>
  </div>

  <div class="cover-meta">
    <div class="cover-meta-item">
      <strong>Platform Core Principles</strong>
      <span>AI Proposes &bull; Rules Decide &bull; NautilusTrader Executes</span>
    </div>
    <div class="cover-meta-item">
      <strong>Execution Engine</strong>
      <span>Nautilus Trader 2.0.0rc4 LiveNode / Backtester</span>
    </div>
    <div class="cover-meta-item">
      <strong>AI Agent Fleet</strong>
      <span>Google Antigravity SDK &bull; Gemini 2.5 Pro &bull; Gemini 2.5 Flash</span>
    </div>
    <div class="cover-meta-item">
      <strong>Security &amp; Isolation</strong>
      <span>Argon2id &bull; TOTP 2FA &bull; cryptonite_agent_ro &bull; Signed HMAC</span>
    </div>
  </div>
</div>

<!-- TABLE OF CONTENTS -->
<div class="page-break"></div>

<div class="toc">
  <div class="toc-title">Comprehensive Table of Contents</div>
  <ul class="toc-list">
    <li class="toc-item"><span>1. Platform Architecture &amp; Core Operating Principles</span> <span>Section 1</span></li>
    <li class="toc-item"><span>2. Operator Authentication, Identity &amp; RBAC Security</span> <span>Section 2</span></li>
    <li class="toc-item"><span>3. Multi-Chart Trading Terminal &amp; Real-Time Market Streaming</span> <span>Section 3</span></li>
    <li class="toc-item"><span>4. Market Overview &amp; Cross-Asset Telemetry Matrix</span> <span>Section 4</span></li>
    <li class="toc-item"><span>5. Vectorized Polars Scanner Studio &amp; Signal Detection</span> <span>Section 5</span></li>
    <li class="toc-item"><span>6. Quantitative Research Lab &amp; NautilusTrader Backtesting</span> <span>Section 6</span></li>
    <li class="toc-item"><span>7. Portfolio Management &amp; Paper/Live Execution Engine</span> <span>Section 7</span></li>
    <li class="toc-item"><span>8. Operator Governance, Emergency Kill-Switch &amp; Risk Guardrails</span> <span>Section 8</span></li>
    <li class="toc-item"><span>9. Cryptonite AI Operations Fleet &amp; Antigravity Architecture</span> <span>Section 9</span></li>
    <li class="toc-item"><span>&nbsp;&nbsp;&nbsp;&nbsp;9.1 Multi-Agent System Topology &amp; Security Boundary</span> <span>Section 9.1</span></li>
    <li class="toc-item"><span>&nbsp;&nbsp;&nbsp;&nbsp;9.2 Research Agent (Senior Quantitative Analyst) &amp; DuckDB Queries</span> <span>Section 9.2</span></li>
    <li class="toc-item"><span>&nbsp;&nbsp;&nbsp;&nbsp;9.3 Explainer Agent (Risk &amp; Performance Auditor) &amp; Metric Synthesis</span> <span>Section 9.3</span></li>
    <li class="toc-item"><span>&nbsp;&nbsp;&nbsp;&nbsp;9.4 Strategist &amp; Generator Agents (Nautilus Strategy Synthesis)</span> <span>Section 9.4</span></li>
    <li class="toc-item"><span>&nbsp;&nbsp;&nbsp;&nbsp;9.5 The 5-Rule Hierarchical Execution Protocol (StrategistEngine)</span> <span>Section 9.5</span></li>
    <li class="toc-item"><span>&nbsp;&nbsp;&nbsp;&nbsp;9.6 Opportunity Hunter Agent (Event-Driven Market Scanning)</span> <span>Section 9.6</span></li>
    <li class="toc-item"><span>10. Dynamic System Settings &amp; AI Fleet Configuration</span> <span>Section 10</span></li>
    <li class="toc-item"><span>11. Local Network (LAN) &amp; Multi-Device Access Architecture</span> <span>Section 11</span></li>
    <li class="toc-item"><span>12. Operational Troubleshooting &amp; Diagnostic Runbooks</span> <span>Section 12</span></li>
  </ul>
</div>

<!-- SECTION 1 -->
<h1>1. Platform Architecture &amp; Core Operating Principles</h1>
<p>
  <strong>Cryptonite</strong> is a self-hosted, institutional-grade cryptocurrency intelligence and algorithmic execution platform engineered for auditable research, deterministic risk controls, and automated high-frequency execution.
</p>
<p>
  The architecture is strictly governed by three uncompromisable axioms:
</p>
<ol>
  <li><strong>AI and UI can propose:</strong> Predictive models, automated screeners, and interactive human operators identify opportunities and formulate trading hypotheses.</li>
  <li><strong>Deterministic rules and risk controls decide:</strong> In-memory risk engines, price collars, daily drawdown circuit breakers, and cash reservation models validate every parameter before transmission. No probabilistic model can override deterministic safeguards.</li>
  <li><strong>NautilusTrader executes:</strong> An event-driven simulation and execution kernel processes orders, coordinates order state machines, and reconciles fills with exchange venues.</li>
</ol>

<!-- SYSTEM TOPOLOGY SVG DIAGRAM -->
<div class="diagram-box">
  <div class="diagram-title">Figure 1.1: Cryptonite High-Level Microservice Topology &amp; Inter-Process Communication</div>
  <svg viewBox="0 0 880 320" width="100%" height="240" xmlns="http://www.w3.org/2000/svg">
    <!-- Market Data Ingestion -->
    <rect x="20" y="30" width="220" height="90" rx="8" fill="#111827" stroke="#38bdf8" stroke-width="2"/>
    <text x="130" y="55" fill="#38bdf8" font-family="Inter" font-weight="700" font-size="12" text-anchor="middle">Market Ingestion (8080)</text>
    <text x="130" y="75" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Binance WebSocket / REST</text>
    <text x="130" y="95" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">Advisory Locked &bull; Nanosecond Cadence</text>

    <!-- Redis & Parquet Data Stores -->
    <rect x="330" y="30" width="220" height="90" rx="8" fill="#111827" stroke="#10b981" stroke-width="2"/>
    <text x="440" y="55" fill="#10b981" font-family="Inter" font-weight="700" font-size="12" text-anchor="middle">Data Stores &amp; Bus</text>
    <text x="440" y="75" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Redis 7 Streams &bull; Parquet Catalog</text>
    <text x="440" y="95" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">nautilus:stream:bus &bull; Analytics Lake</text>

    <!-- Analytical Cores -->
    <rect x="640" y="30" width="220" height="90" rx="8" fill="#111827" stroke="#a855f7" stroke-width="2"/>
    <text x="750" y="55" fill="#a855f7" font-family="Inter" font-weight="700" font-size="12" text-anchor="middle">Vectorized Analytics</text>
    <text x="750" y="75" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Polars Scanner &bull; DuckDB Engine</text>
    <text x="750" y="95" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">&lt;10ms LazyFrame Evaluation</text>

    <!-- Execution Plane -->
    <rect x="20" y="190" width="220" height="100" rx="8" fill="#111827" stroke="#f43f5e" stroke-width="2"/>
    <text x="130" y="215" fill="#f43f5e" font-family="Inter" font-weight="700" font-size="12" text-anchor="middle">Execution Plane (8084)</text>
    <text x="130" y="235" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Nautilus LiveNode Paper Executor</text>
    <text x="130" y="255" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Transactional Outbox Poller (8081)</text>
    <text x="130" y="275" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">Zero-DB Hot Loop &bull; Collars &amp; Cash Res</text>

    <!-- AI Operations Fleet -->
    <rect x="330" y="190" width="220" height="100" rx="8" fill="#111827" stroke="#f59e0b" stroke-width="2"/>
    <text x="440" y="215" fill="#f59e0b" font-family="Inter" font-weight="700" font-size="12" text-anchor="middle">AI Fleet (8085)</text>
    <text x="440" y="235" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Google Antigravity + Gemini API</text>
    <text x="440" y="255" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Research &bull; Explainer &bull; Strategist</text>
    <text x="440" y="275" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">cryptonite_agent_ro &bull; Signed HMAC</text>

    <!-- Web Terminal & Gateway -->
    <rect x="640" y="190" width="220" height="100" rx="8" fill="#111827" stroke="#06b6d4" stroke-width="2"/>
    <text x="750" y="215" fill="#06b6d4" font-family="Inter" font-weight="700" font-size="12" text-anchor="middle">Interface &amp; Gateway</text>
    <text x="750" y="235" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Next.js 16 Web Terminal (3000)</text>
    <text x="750" y="255" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">FastAPI Gateway (8000, 0.0.0.0)</text>
    <text x="750" y="275" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">TradingView &bull; WS /ws/stream &bull; LAN Ready</text>

    <!-- Connecting Arrows -->
    <line x1="240" y1="75" x2="330" y2="75" stroke="#475569" stroke-width="2"/>
    <line x1="550" y1="75" x2="640" y2="75" stroke="#475569" stroke-width="2"/>
    <line x1="440" y1="120" x2="440" y2="190" stroke="#475569" stroke-width="2"/>
    <line x1="240" y1="240" x2="330" y2="240" stroke="#475569" stroke-width="2"/>
    <line x1="550" y1="240" x2="640" y2="240" stroke="#475569" stroke-width="2"/>
  </svg>
</div>

<h3>Subsystem Topology &amp; Ports</h3>
<table>
  <thead>
    <tr>
      <th>Service Name</th>
      <th>Container Name</th>
      <th>Port Binding</th>
      <th>Technology Core</th>
      <th>Primary Mandate</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><strong>API Gateway</strong></td>
      <td><code>cryptonite_api</code></td>
      <td><code>8000:8000 (0.0.0.0)</code></td>
      <td>FastAPI &bull; AsyncPG</td>
      <td>REST routing, operator authentication, WebSocket multiplexing (<code>/ws/stream</code>), and alerts.</td>
    </tr>
    <tr>
      <td><strong>Web Terminal</strong></td>
      <td>Host Process</td>
      <td><code>3000:3000 (0.0.0.0)</code></td>
      <td>Next.js 16 &bull; React &bull; Tailwind</td>
      <td>TradingView charts, scanner studio, portfolio console, AI copilot, and operator panel.</td>
    </tr>
    <tr>
      <td><strong>Ingestion Engine</strong></td>
      <td><code>cryptonite_ingest</code></td>
      <td><code>8080</code> (Internal)</td>
      <td>AsyncIO &bull; WebSockets</td>
      <td>Binance Kline streaming, advisory-locked single writer, partition-aligned Parquet writes.</td>
    </tr>
    <tr>
      <td><strong>Outbox Relay</strong></td>
      <td><code>cryptonite_outbox</code></td>
      <td><code>8081</code> (Internal)</td>
      <td>Python &bull; AsyncPG</td>
      <td>Guaranteed at-least-once order transmission using transactional outbox tables.</td>
    </tr>
    <tr>
      <td><strong>Scanner Engine</strong></td>
      <td><code>cryptonite_scanner</code></td>
      <td><code>8082</code> (Internal)</td>
      <td>Polars &bull; Python</td>
      <td>Vectorized technical condition evaluations across rolling candle windows in single-digit ms.</td>
    </tr>
    <tr>
      <td><strong>Backtester Worker</strong></td>
      <td><code>cryptonite_backtester</code></td>
      <td><code>8083</code> (Internal)</td>
      <td>Nautilus Trader C++</td>
      <td>Deterministic discrete-event simulations with realistic slippage, fees, and latency profiles.</td>
    </tr>
    <tr>
      <td><strong>Execution Engine</strong></td>
      <td><code>cryptonite_executor</code></td>
      <td><code>8084</code> (Internal)</td>
      <td>Nautilus Trader &bull; Python</td>
      <td>Paper/live order execution, cash reservation, dynamic price collars, and kill-switch listener.</td>
    </tr>
    <tr>
      <td><strong>AI Agent Worker</strong></td>
      <td><code>cryptonite_agent_worker</code></td>
      <td><code>8085</code> (Internal)</td>
      <td>Google Antigravity &bull; Gemini</td>
      <td>Multi-agent fleet: quantitative research, tearsheet audits, strategy generation, and execution signals.</td>
    </tr>
  </tbody>
</table>

<!-- SECTION 2 -->
<div class="page-break"></div>
<h1>2. Operator Authentication, Identity &amp; RBAC Security</h1>
<p>
  To safeguard algorithmic capital, Cryptonite enforces multi-layered operator governance, role-based access control (RBAC), and cryptographic step-up verification.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/00_operator_login.png" alt="Operator Authentication Screen">
  <div class="figure-caption">Figure 2.1: Operator Authentication Portal with Password Hashing &amp; Two-Factor Authentication (TOTP).</div>
</div>

<h3>Operator Authentication Protocol</h3>
<ol>
  <li><strong>Argon2id Password Hashing:</strong> Operator credentials stored in PostgreSQL utilize state-of-the-art memory-hard Argon2id hashing algorithms with random salts.</li>
  <li><strong>Time-Based One-Time Passwords (TOTP):</strong> Mandatory 6-digit TOTP validation. Secrets are encrypted at rest using AES-256 (<code>CRYPTONITE_TOTP_AES_KEY</code>). For local development, bypass code <code>000000</code> is supported.</li>
  <li><strong>Encrypted HTTP-Only Cookies:</strong> Session tokens are serialized into encrypted, HTTP-only cookies with <code>SameSite=Lax</code> to eliminate Cross-Site Scripting (XSS) credential theft.</li>
  <li><strong>Double-Submit CSRF Protection:</strong> All state-mutating requests (e.g., submitting orders, deploying strategies, tripping the kill-switch) require a valid <code>X-CSRF-Token</code> header matching the active session.</li>
  <li><strong>Least-Privilege Database Roles:</strong> The API runs under <code>cryptonite_api</code>, while AI agents connect strictly through the unprivileged <code>cryptonite_agent_ro</code> role (<code>default_transaction_read_only = 'on'</code>).</li>
</ol>

<div class="callout callout-info avoid-break">
  <strong>Standard Development Credentials</strong>
  Operator ID: <code>admin</code> &bull; Password: <code>admin123</code> &bull; TOTP Verification Code: <code>000000</code><br>
  Login URL: <code>http://localhost:3000/operator/login</code> (or via header <strong>Operator</strong> tab).
</div>

<!-- SECTION 3 -->
<div class="page-break"></div>
<h1>3. Multi-Chart Trading Terminal &amp; Real-Time Market Streaming</h1>
<p>
  The <strong>Trading Terminal</strong> is the primary visual operations desk. It renders high-density financial time-series directly from Binance WebSocket streams and the internal Redis pub/sub bus.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/01_terminal_overview.png" alt="Trading Terminal Overview">
  <div class="figure-caption">Figure 3.1: Dual-Pane Trading Terminal with TradingView Candlestick Charts, Multi-Timeframe Toggles, and Live Telemetry.</div>
</div>

<h3>Step-by-Step Operator Interaction</h3>
<div class="step-box">
  <div class="step-title">Step 1: Selecting Assets from the Watchlist</div>
  <p>Use the left sidebar to monitor 24h ticker updates. Click any asset (e.g. <code>BTCUSDT</code>, <code>ETHUSDT</code>, <code>SOLUSDT</code>, <code>BNBUSDT</code>) to instantly retarget both chart panes and update the order book feed.</p>
</div>

<div class="step-box">
  <div class="step-title">Step 2: Configuring Multi-Timeframe Perspectives</div>
  <p>The terminal features dual independent chart panes (<code>1x1</code>, <code>1x2</code>, or <code>2x2</code> grid layouts). Set Pane 1 to a macro timeframe (<code>1h</code> or <code>4h</code>) to identify institutional support/resistance levels, while keeping Pane 2 on micro execution intervals (<code>1m</code> or <code>5m</code>) for entry confirmation.</p>
</div>

<div class="step-box">
  <div class="step-title">Step 3: Crosshair Inspection &amp; Indicator Overlays</div>
  <p>Hover across any candle to trigger synchronized crosshairs. The top telemetry bar displays precise Open, High, Low, Close, and Volume values alongside active technical indicators.</p>
</div>

<div class="callout callout-success avoid-break">
  <strong>Performance Telemetry Gauge</strong>
  The bottom status bar monitors query health in real time: <strong>DuckDB p95 latency: 12ms</strong> &bull; <strong>Polars vectorized scan speed: 8ms</strong> &bull; <strong>Stream Status: ONLINE</strong>.
</div>

<!-- SECTION 4 -->
<div class="page-break"></div>
<h1>4. Market Overview &amp; Cross-Asset Telemetry Matrix</h1>
<p>
  The <strong>Market Overview</strong> workspace synthesizes cross-market telemetry across all tracked instruments, identifying momentum leaders, volume anomalies, and broad regime shifts.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/02_market_overview.png" alt="Market Overview Matrix">
  <div class="figure-caption">Figure 4.1: Cross-Market Heatmap Matrix displaying Top Gainers, Losers, Volume Leaders, and Sector Heatmaps.</div>
</div>

<h3>Market Intelligence Features</h3>
<ul>
  <li><strong>Top Movers Matrix:</strong> Real-time ranking of the day's top performing and underperforming crypto assets, calculated from nanosecond timestamp klines.</li>
  <li><strong>Volume Concentration Tracker:</strong> Ranks assets by quote asset volume (e.g. USDC $2.0B, BTC $958.9M, ETH $691.8M), highlighting where institutional liquidity is clustering.</li>
  <li><strong>Sector Heatmap:</strong> Visual grid showing percentage price action color-coded by magnitude (deep green for &gt;+15%, vibrant red for &lt;-5%), enabling instantaneous market sentiment diagnosis.</li>
  <li><strong>1-Click Deep Navigation:</strong> Clicking any heatmap tile immediately navigates back to the Terminal with that instrument pre-loaded for execution.</li>
</ul>

<!-- SECTION 5 -->
<div class="page-break"></div>
<h1>5. Vectorized Polars Scanner Studio &amp; Signal Detection</h1>
<p>
  The <strong>Scanner Studio</strong> delivers high-throughput technical screening. Utilizing Polars LazyFrames and Apache Arrow memory buffers, it evaluates technical rules across thousands of bars in under 10 milliseconds.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/01b_scanner_studio_detail.png" alt="Scanner Studio Detail">
  <div class="figure-caption">Figure 5.1: Polars Vectorized Scanner Studio with Active Specification, Rules Inspector, and Detected Signal Candidates.</div>
</div>

<div class="figure avoid-break">
  <img src="file://{screenshots_dir}/02_scanner_builder.png" alt="Scanner Builder Interface">
  <div class="figure-caption">Figure 5.2: Custom Scanner Builder Modal for Configuring Multi-Condition Vectorized Filters.</div>
</div>

<h3>Step-by-Step: Creating &amp; Running a Custom Scanner</h3>
<div class="step-box">
  <div class="step-title">Step 1: Open the Scanner Builder</div>
  <p>In the Scanner Studio banner beneath the trading chart, click the cyan <strong>Create Scanner</strong> button.</p>
</div>

<div class="step-box">
  <div class="step-title">Step 2: Define Universe &amp; Rule Expressions</div>
  <p>Enter a unique scanner name (e.g. <code>Oversold RSI &amp; Volume Expansion</code>) and target universe (e.g. <code>BTCUSDT, ETHUSDT, SOLUSDT</code>). Add rule criteria:</p>
  <ul>
    <li><strong>Trigger Rule:</strong> <code>RSI(14) &lt; 30</code> (Oversold condition).</li>
    <li><strong>Confirmation Rule:</strong> <code>Close &gt; Lower_Bollinger_Band(20, 2.0)</code> (Bounce off lower band).</li>
    <li><strong>Volume Filter:</strong> <code>Volume &gt;= SMA(Volume, 20) * 1.5</code> (Expansion confirmation).</li>
  </ul>
</div>

<div class="step-box">
  <div class="step-title">Step 3: Save &amp; Execute Vectorized Scan</div>
  <p>Click <strong>Save Scanner Spec</strong>, then click <strong>Run Scanner</strong>. The Polars engine evaluates the Parquet historical partition and streams detected signal candidates (with Entry, Stop, and Target prices) directly to the right panel.</p>
</div>

<!-- SECTION 6 -->
<div class="page-break"></div>
<h1>6. Quantitative Research Lab &amp; NautilusTrader Backtesting</h1>
<p>
  The <strong>Research Lab</strong> provides institutional backtesting powered by the discrete-event simulation kernel of <strong>Nautilus Trader</strong>. It enforces temporal monotonicity and simulates realistic execution frictions.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/03_research_lab.png" alt="Research Lab Console">
  <div class="figure-caption">Figure 6.1: Research Lab Backtesting Workbench with Execution Profile Selection and Parameter Tuners.</div>
</div>

<h3>Execution Profile Configurations</h3>
<table>
  <thead>
    <tr>
      <th>Execution Profile</th>
      <th>Simulated Latency</th>
      <th>Slippage Model</th>
      <th>Taker Commission</th>
      <th>Intended Use Case</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><span class="badge badge-blue">Baseline (Optimistic)</span></td>
      <td>0 ms</td>
      <td>Zero Slippage</td>
      <td>0.00% (Zero Fees)</td>
      <td>Theoretical benchmark to assess mathematical strategy alpha without frictions.</td>
    </tr>
    <tr>
      <td><span class="badge badge-amber">Adverse Slippage (Stress)</span></td>
      <td>50 ms</td>
      <td>Forces 1 adverse tick per fill</td>
      <td>0.05% (5 bps)</td>
      <td>Simulates competitive market orders in trending markets.</td>
    </tr>
    <tr>
      <td><span class="badge badge-purple">Conservative Institutional</span></td>
      <td>150 ms</td>
      <td>50% limit fill rate, dynamic slippage</td>
      <td>0.10% (10 bps)</td>
      <td>Realistic live production proxy under volatile liquidity conditions.</td>
    </tr>
  </tbody>
</table>

<h3>Step-by-Step: Launching a Backtest Simulation</h3>
<ol>
  <li><strong>Target Strategy:</strong> Select from registered strategies (e.g. <code>Supertrend Breakout</code>, <code>EMA Cross</code>, or user-synthesized AI strategies).</li>
  <li><strong>Instrument &amp; Resolution:</strong> Choose pair (<code>BTCUSDT.BINANCE</code>) and timeframe (<code>1m</code>, <code>5m</code>, <code>15m</code>, <code>1h</code>).</li>
  <li><strong>Tunable Parameters:</strong> Adjust strategy hyperparameters (e.g. <code>ATR Period: 10</code>, <code>ATR Multiplier: 3</code>, <code>Trade Size: 0.01 BTC</code>).</li>
  <li><strong>Click Run Quantitative Backtest:</strong> The engine executes the run, generates <code>tearsheet.json</code>, <code>equity_curve.parquet</code>, and <code>trades.parquet</code>, rendering Sharpe Ratio, Sortino Ratio, Calmar Ratio, Max Drawdown %, and monthly return heatmaps.</li>
</ol>

<!-- SECTION 7 -->
<div class="page-break"></div>
<h1>7. Portfolio Management &amp; Paper/Live Execution Engine</h1>
<p>
  The <strong>Portfolio Console</strong> centralizes position monitoring, order execution status, and realized/unrealized profit and loss tracking across both paper trading accounts and exchange integrations.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/04_portfolio_console.png" alt="Portfolio Console">
  <div class="figure-caption">Figure 7.1: Real-Time Portfolio Management Console showing Net Equity, Available Cash, and Active Positions.</div>
</div>

<h3>Key Invariants &amp; Execution Telemetry</h3>
<ul>
  <li><strong>Zero-DB Hot Callback Loop:</strong> Strategy event handlers never perform blocking disk or network I/O. Fills and order updates are placed onto a bounded thread-safe memory queue (<code>maxsize=10,000</code>).</li>
  <li><strong>Conservative Cash Reservation:</strong> Market buy orders atomically reserve <code>notional * (1 + 0.002)</code> in cash to guarantee margin availability against adverse slippage before order transmission.</li>
  <li><strong>Spot Cash Invariant:</strong> Base asset holdings cannot drop below zero (<code>HOLDINGS &gt;= 0</code>). Naked short selling on spot instruments is rejected at the engine admission gate.</li>
  <li><strong>Dynamic Price Collars:</strong> Buy limit orders exceeding <code>quote.ask * (1 + collar_pct)</code> or sell orders below <code>quote.bid * (1 - collar_pct)</code> are rejected immediately.</li>
</ul>

<!-- SECTION 8 -->
<div class="page-break"></div>
<h1>8. Operator Governance, Emergency Kill-Switch &amp; Risk Guardrails</h1>
<p>
  The <strong>Operator Control Plane</strong> is the administrative cockpit for live capital governance, emergency risk management, manual execution, and multi-asset audit ledger reconciliation.
</p>

<div class="figure avoid-break">
  <img src="file://{screenshots_dir}/07_operator_dashboard.png" alt="Operator Dashboard">
  <div class="figure-caption">Figure 8.1: Mission-Critical Operator Safeguards, Armed Kill-Switch, and Multi-Asset Audit Ledger.</div>
</div>

<div class="figure avoid-break">
  <img src="file://{screenshots_dir}/08_deploy_strategy_modal.png" alt="Deploy Strategy Modal">
  <div class="figure-caption">Figure 8.2: Deploy Strategy Dialog with Capital Allocation, Timeframe, and Parameter Assignment.</div>
</div>

<div class="figure avoid-break">
  <img src="file://{screenshots_dir}/09_manual_trade_modal.png" alt="Manual Trade Modal">
  <div class="figure-caption">Figure 8.3: Direct Manual Trade Entry Dialog with Limit/Market Options and Post-Only/IOC Styles.</div>
</div>

<h3>Step-by-Step Operator Procedures</h3>
<div class="step-box">
  <div class="step-title">Procedure 1: Engaging the Emergency Kill-Switch</div>
  <p>In case of flash crash, venue API corruption, or algorithmic anomaly, click the prominent red <strong>TRIP KILL-SWITCH</strong> button:</p>
  <ol>
    <li>Instantly closes the order admission gate.</li>
    <li>Atomically updates database status: all running paper strategy instances are set to <code>STOPPED</code>.</li>
    <li>Queries the exchange and cancels all resting limit and stop orders across connected venues.</li>
    <li>Latching generation counter is incremented in Redis and PostgreSQL; new inbound orders are rejected with <code>HTTP 423 Locked</code>.</li>
  </ol>
</div>

<div class="step-box">
  <div class="step-title">Procedure 2: Deploying a Strategy Instance</div>
  <p>Click <strong>Deploy New</strong> on the Active Strategy Instances panel. Select the strategy protocol (e.g. <code>EMA Cross</code>), instrument (<code>BTCUSDT.BINANCE</code>), timeframe (<code>1m</code>), and USD allocation. The executor creates an instance record in <code>RUNNING</code> status and begins routing bars.</p>
</div>

<div class="step-box">
  <div class="step-title">Procedure 3: Executing a Manual OTC Order</div>
  <p>Click <strong>Manual Trade</strong>. Specify Instrument, Side (<code>BUY</code> or <code>SELL</code>), Order Type (<code>MARKET</code> or <code>LIMIT</code>), and Quantity. The order is committed to PostgreSQL and dispatched through the Transactional Outbox relay.</p>
</div>

<!-- SECTION 9 -->
<div class="page-break"></div>
<h1>9. Cryptonite AI Operations Fleet &amp; Antigravity Architecture</h1>
<p>
  <strong>Cryptonite AI</strong> is an autonomous quantitative intelligence layer integrated natively into the platform. Powered by the <strong>Google Antigravity SDK</strong> and <strong>Google Gemini</strong>, it operates as an institutional multi-agent desk.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/06b_ai_chat_detail.png" alt="AI Agent Chat Detail">
  <div class="figure-caption">Figure 9.1: Live Cryptonite AI Assistant Interface rendering Risk Officer Evaluation with Natural Language Breakdown.</div>
</div>

<h2>9.1 Multi-Agent System Topology &amp; Security Boundary</h2>
<p>
  The AI fleet operates under strict architectural isolation, least-privilege database roles, and cryptographic message signing:
</p>

<!-- AI AGENT ARCHITECTURE SVG DIAGRAM -->
<div class="diagram-box">
  <div class="diagram-title">Figure 9.2: Multi-Agent Fleet Security Boundary &amp; Tool Access Architecture</div>
  <svg viewBox="0 0 880 260" width="100%" height="210" xmlns="http://www.w3.org/2000/svg">
    <!-- API Gateway Box -->
    <rect x="20" y="25" width="220" height="210" rx="8" fill="#111827" stroke="#0284c7" stroke-width="2"/>
    <text x="130" y="55" fill="#38bdf8" font-family="Inter" font-weight="700" font-size="12" text-anchor="middle">cryptonite_api (Port 8000)</text>
    <text x="130" y="80" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">FastAPI Gateway</text>
    <text x="130" y="105" fill="#e2e8f0" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">&bull; Operator Auth &amp; CSRF</text>
    <text x="130" y="125" fill="#e2e8f0" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">&bull; Next.js Proxy (300s Timeout)</text>
    <text x="130" y="150" fill="#f59e0b" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">&bull; HMAC Request Signer</text>
    <text x="130" y="170" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">X-Internal-Timestamp</text>
    <text x="130" y="185" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">X-Internal-Nonce</text>
    <text x="130" y="200" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">X-Internal-Signature</text>

    <!-- Agent Worker Box -->
    <rect x="330" y="25" width="230" height="210" rx="8" fill="#111827" stroke="#f59e0b" stroke-width="2"/>
    <text x="445" y="55" fill="#f59e0b" font-family="Inter" font-weight="700" font-size="12" text-anchor="middle">cryptonite_agent_worker (8085)</text>
    <text x="445" y="80" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Google Antigravity SDK Fleet</text>
    <text x="445" y="105" fill="#38bdf8" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">&bull; Research Agent</text>
    <text x="445" y="125" fill="#10b981" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">&bull; Explainer Agent</text>
    <text x="445" y="145" fill="#a855f7" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">&bull; Strategist Agent</text>
    <text x="445" y="165" fill="#ec4899" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">&bull; Strategy Generator</text>
    <text x="445" y="185" fill="#6366f1" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">&bull; Opportunity Hunter</text>
    <text x="445" y="210" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">Gemini 2.5 Pro / Flash Backend</text>

    <!-- Data & Storage Layer Box -->
    <rect x="640" y="25" width="220" height="210" rx="8" fill="#111827" stroke="#10b981" stroke-width="2"/>
    <text x="750" y="55" fill="#10b981" font-family="Inter" font-weight="700" font-size="12" text-anchor="middle">Storage &amp; Data Isolation</text>
    <text x="750" y="80" fill="#94a3b8" font-family="Inter" font-size="9" text-anchor="middle">Least-Privilege Security Seams</text>
    <text x="750" y="110" fill="#f43f5e" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">PostgreSQL RO Replica</text>
    <text x="750" y="128" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">cryptonite_agent_ro</text>
    <text x="750" y="142" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">read_only = 'on' (NO WRITES)</text>
    <text x="750" y="170" fill="#38bdf8" font-family="JetBrains Mono" font-size="8.5" text-anchor="middle">DuckDB Parquet Sandbox</text>
    <text x="750" y="188" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">CATALOG_PATH Containment</text>
    <text x="750" y="202" fill="#64748b" font-family="JetBrains Mono" font-size="8" text-anchor="middle">Blocks: DROP, DELETE, COPY</text>

    <!-- Arrows -->
    <line x1="240" y1="130" x2="330" y2="130" stroke="#f59e0b" stroke-width="2"/>
    <line x1="560" y1="130" x2="640" y2="130" stroke="#10b981" stroke-width="2"/>
  </svg>
</div>

<!-- SECTION 9.2 -->
<div class="page-break"></div>
<h2>9.2 Research Agent (Senior Quantitative Analyst) &amp; DuckDB Queries</h2>
<p>
  The <strong>Research Agent</strong> acts as a senior quantitative researcher. Equipped with direct SQL inspection tools, it mines historical Parquet time series and audits operational database records.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/06_ai_copilot_research.png" alt="Research Agent Interface">
  <div class="figure-caption">Figure 9.3: Research Agent Persona in the Cryptonite AI Workspace.</div>
</div>

<h3>Agent Tooling &amp; Data Access</h3>
<table>
  <thead>
    <tr>
      <th>Tool Signature</th>
      <th>Target Database</th>
      <th>Security Safeguards</th>
      <th>Intended Usage</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><code>query_postgres_readonly(sql: str)</code></td>
      <td>PostgreSQL 16 Engine</td>
      <td>Role <code>cryptonite_agent_ro</code> enforces <code>default_transaction_read_only = 'on'</code>.</td>
      <td>Inspecting active instruments, open orders, paper trade ledgers, and scanner specs.</td>
    </tr>
    <tr>
      <td><code>query_parquet_duckdb(query: str)</code></td>
      <td>Nautilus Parquet Catalog &amp; Analytics Lake</td>
      <td>Regex blacklist blocks <code>DROP</code>, <code>DELETE</code>, <code>COPY</code>, <code>INSERT</code>, <code>httpfs</code>. Strict path containment relative to <code>CATALOG_PATH</code>.</td>
      <td>Running sub-second SQL aggregations across multi-gigabyte tick and bar catalogs.</td>
    </tr>
  </tbody>
</table>

<h3>Example Operator Interaction</h3>
<div class="step-box">
  <div class="step-title">Prompt:</div>
  <p><code>"Calculate the 30-day realized volatility and average daily volume for BTCUSDT from the parquet catalog."</code></p>
  <div class="step-title" style="margin-top: 6px;">Agent Execution &amp; Tool Call:</div>
  <pre><code>SELECT
  STDDEV_SAMP(log_ret) * SQRT(365 * 1440) as annualized_vol,
  AVG(daily_vol) as avg_volume
FROM (
  SELECT
    LN(close / LAG(close) OVER (ORDER BY ts_event_ns)) as log_ret,
    SUM(volume) OVER (PARTITION BY date_trunc('day', to_timestamp(ts_event_ns / 1e9))) as daily_vol
  FROM 'data/bars/**/*.parquet'
  WHERE instrument_id = 'BTCUSDT.BINANCE'
  LIMIT 50000
)</code></pre>
</div>

<!-- SECTION 9.3 -->
<div class="page-break"></div>
<h2>9.3 Explainer Agent (Risk &amp; Performance Auditor) &amp; Metric Synthesis</h2>
<p>
  The <strong>Explainer Agent</strong> acts as an independent Chief Risk Officer. It audits backtesting simulation artifacts and translates quantitative statistical ratios into executive summaries.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/07_ai_copilot_explainer.png" alt="Explainer Agent Interface">
  <div class="figure-caption">Figure 9.4: Explainer Agent Mode with Target Backtest Run ID Input and Performance Audit Prompts.</div>
</div>

<h3>Dynamic Metric Synthesis Fallback</h3>
<p>
  When inspecting incomplete, in-progress, or legacy simulations where <code>tearsheet.json</code> is missing, the tool <code>read_nautilus_tearsheet</code> automatically falls back to an embedded <strong>DuckDB Synthesis Engine</strong>:
</p>
<ul>
  <li>Reads <code>equity_curve.parquet</code> to calculate <strong>Total Return %</strong>, <strong>Maximum Drawdown %</strong>, and peak-to-trough duration.</li>
  <li>Reads <code>trades.parquet</code> to aggregate <strong>Total Executed Trades</strong>, <strong>Win Rate %</strong>, <strong>Average Risk/Reward</strong>, and <strong>Realized PnL</strong>.</li>
  <li>Produces complete statistical summaries without requiring simulation re-runs.</li>
</ul>

<h3>Example Operator Interaction</h3>
<div class="step-box">
  <div class="step-title">Prompt:</div>
  <p><code>"Audit backtest run '7abf5fae-fe31-4348-b290-a2be86687275' and explain its performance weaknesses."</code></p>
  <div class="step-title" style="margin-top: 6px;">Explainer Agent Response:</div>
  <p><em>"The strategy achieved a Total Realized PnL of +$48.20 across 1 execution, but has critical statistical deficiencies: (1) Sample size of N=1 provides zero statistical validity; (2) The Sharpe ratio is uncomputable; (3) Market regime dependency is untested across bear markets. Recommendation: Extend simulation window across minimum 180 days."</em></p>
</div>

<!-- SECTION 9.4 -->
<div class="page-break"></div>
<h2>9.4 Strategist &amp; Generator Agents (Nautilus Strategy Synthesis)</h2>
<p>
  The <strong>Strategist</strong> and <strong>Generator Agents</strong> automate quantitative strategy engineering. They transform plain-English trading concepts into fully typed, production-ready Nautilus Trader Python code.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/08_ai_copilot_strategist.png" alt="Strategist Agent Interface">
  <div class="figure-caption">Figure 9.5: Strategist Agent with Automated Strategy Code Generation and One-Click Save Feature.</div>
</div>

<h3>Strict AST &amp; Safety Compliance Rules</h3>
<p>
  All generated algorithms must conform to Cryptonite's abstract syntax tree (AST) safety enforcer:
</p>
<ol>
  <li>Must import and inherit from <code>CryptoniteStrategyConfig</code> and <code>CryptoniteStrategy</code>.</li>
  <li><strong>STRICT AST COMPLIANCE:</strong> Direct calls to <code>self.submit_order()</code>, <code>self.cancel_order()</code>, or direct access to <code>self.portfolio</code>, <code>self.msgbus</code>, or <code>self.exec_engine</code> are strictly prohibited.</li>
  <li>All order submissions must use safe, risk-managed helpers: <code>self.buy_market()</code>, <code>self.sell_market()</code>, <code>self.close_all()</code>, <code>self.is_flat()</code>, <code>self.is_long()</code>, <code>self.is_short()</code>.</li>
</ol>

<div class="step-box">
  <div class="step-title">1-Click Registration Workflow</div>
  <p>When the agent outputs executable strategy code, the UI chat panel renders an integrated green <strong>Save Strategy</strong> button. Clicking this instantly registers the strategy in PostgreSQL, making it immediately selectable in the <strong>Research Lab</strong> for backtesting!</p>
</div>

<!-- SECTION 9.5 -->
<div class="page-break"></div>
<h2>9.5 The 5-Rule Hierarchical Execution Protocol (StrategistEngine)</h2>
<p>
  To eliminate emotional bias and enforce institutional capital preservation, the Strategist Agent's deterministic engine (<code>StrategistEngine</code>) analyzes multi-timeframe snapshots using a strict 5-layer hierarchy:
</p>

<!-- 5-RULE HIERARCHY SVG DIAGRAM -->
<div class="diagram-box">
  <div class="diagram-title">Figure 9.6: The 5-Rule Hierarchical Decision Protocol Pipeline</div>
  <svg viewBox="0 0 880 200" width="100%" height="160" xmlns="http://www.w3.org/2000/svg">
    <!-- Step 1 -->
    <rect x="10" y="20" width="155" height="150" rx="6" fill="#111827" stroke="#38bdf8" stroke-width="1.5"/>
    <text x="87" y="45" fill="#38bdf8" font-family="Inter" font-weight="700" font-size="11" text-anchor="middle">1. Macro Context</text>
    <text x="87" y="65" fill="#f8fafc" font-family="JetBrains Mono" font-size="9" text-anchor="middle">1D &amp; 4H Frames</text>
    <text x="87" y="90" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Derives Structural</text>
    <text x="87" y="105" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Floor (Support) &amp;</text>
    <text x="87" y="120" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Ceiling (Resistance)</text>
    <rect x="25" y="135" width="125" height="22" rx="4" fill="#0369a1"/>
    <text x="87" y="150" fill="#ffffff" font-family="Inter" font-weight="600" font-size="8" text-anchor="middle">Defines Bounds</text>

    <!-- Step 2 -->
    <rect x="185" y="20" width="155" height="150" rx="6" fill="#111827" stroke="#f43f5e" stroke-width="1.5"/>
    <text x="262" y="45" fill="#f43f5e" font-family="Inter" font-weight="700" font-size="11" text-anchor="middle">2. Volume Filter</text>
    <text x="262" y="65" fill="#f8fafc" font-family="JetBrains Mono" font-size="9" text-anchor="middle">15m &amp; 5m Orderflow</text>
    <text x="262" y="90" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Detects Bull Traps,</text>
    <text x="262" y="105" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Bear Traps, and</text>
    <text x="262" y="120" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Stop Hunts</text>
    <rect x="200" y="135" width="125" height="22" rx="4" fill="#be123c"/>
    <text x="262" y="150" fill="#ffffff" font-family="Inter" font-weight="600" font-size="8" text-anchor="middle">Fades False Moves</text>

    <!-- Step 3 -->
    <rect x="360" y="20" width="155" height="150" rx="6" fill="#111827" stroke="#a855f7" stroke-width="1.5"/>
    <text x="437" y="45" fill="#a855f7" font-family="Inter" font-weight="700" font-size="11" text-anchor="middle">3. Momentum</text>
    <text x="437" y="65" fill="#f8fafc" font-family="JetBrains Mono" font-size="9" text-anchor="middle">1H &amp; 15m Squeeze</text>
    <text x="437" y="90" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Validates Trend</text>
    <text x="437" y="105" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Alignment Across</text>
    <text x="437" y="120" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Moving Averages</text>
    <rect x="375" y="135" width="125" height="22" rx="4" fill="#7e22ce"/>
    <text x="437" y="150" fill="#ffffff" font-family="Inter" font-weight="600" font-size="8" text-anchor="middle">Directional Bias</text>

    <!-- Step 4 -->
    <rect x="535" y="20" width="155" height="150" rx="6" fill="#111827" stroke="#10b981" stroke-width="1.5"/>
    <text x="612" y="45" fill="#10b981" font-family="Inter" font-weight="700" font-size="11" text-anchor="middle">4. Micro Trigger</text>
    <text x="612" y="65" fill="#f8fafc" font-family="JetBrains Mono" font-size="9" text-anchor="middle">5m Candle Signal</text>
    <text x="612" y="90" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Requires Precision</text>
    <text x="612" y="105" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Candle Closure in</text>
    <text x="612" y="120" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Consensus Direction</text>
    <rect x="550" y="135" width="125" height="22" rx="4" fill="#047857"/>
    <text x="612" y="150" fill="#ffffff" font-family="Inter" font-weight="600" font-size="8" text-anchor="middle">Entry Execution</text>

    <!-- Step 5 -->
    <rect x="710" y="20" width="155" height="150" rx="6" fill="#111827" stroke="#64748b" stroke-width="1.5"/>
    <text x="787" y="45" fill="#cbd5e1" font-family="Inter" font-weight="700" font-size="11" text-anchor="middle">5. Chop Filter</text>
    <text x="787" y="65" fill="#f8fafc" font-family="JetBrains Mono" font-size="9" text-anchor="middle">Conflict Resolution</text>
    <text x="787" y="90" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">If Indicators</text>
    <text x="787" y="105" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">Conflict or Trend</text>
    <text x="787" y="120" fill="#94a3b8" font-family="Inter" font-size="8" text-anchor="middle">is Flat &rarr; Stand Down</text>
    <rect x="725" y="135" width="125" height="22" rx="4" fill="#334155"/>
    <text x="787" y="150" fill="#ffffff" font-family="Inter" font-weight="600" font-size="8" text-anchor="middle">Capital Protected</text>
  </svg>
</div>

<h3>Rule Protocol Specifications</h3>
<table>
  <thead>
    <tr>
      <th>Layer</th>
      <th>Rule / Protocol</th>
      <th>Timeframe</th>
      <th>Condition &amp; Mathematical Logic</th>
      <th>Execution Outcome</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><strong>1. Macro Context</strong></td>
      <td>Floor &amp; Ceiling</td>
      <td><code>1D</code> &amp; <code>4H</code></td>
      <td>Derives structural floor (min(Low_1D, Low_4H)) and ceiling (max(High_1D, High_4H)) relative to mark price.</td>
      <td>Rejects long entries into overhead ceilings or shorts into support floors.</td>
    </tr>
    <tr>
      <td><strong>2. Volume Trap</strong></td>
      <td>Trap / Hunt Filter</td>
      <td><code>15m</code> &amp; <code>5m</code></td>
      <td>Detects bull traps (price &gt; upper BB on declining volume) and stop hunts (massive volume spike with long wicks).</td>
      <td><strong>Fades the breakout:</strong> generates counter-trend bias or aborts trade.</td>
    </tr>
    <tr>
      <td><strong>3. Momentum Layer</strong></td>
      <td>Trend Alignment</td>
      <td><code>1H</code> &amp; <code>15m</code></td>
      <td>Requires EMA alignment (EMA_20 &gt; EMA_50) on both 1H and 15m intervals.</td>
      <td>Sets directional bias: <code>LONG</code> or <code>SHORT</code>.</td>
    </tr>
    <tr>
      <td><strong>4. Micro Trigger</strong></td>
      <td>Entry Confirmation</td>
      <td><code>5m</code></td>
      <td>Validates precision 5m candle close conforming to momentum bias.</td>
      <td>Calculates exact entry price, stop-loss, and take-profit target.</td>
    </tr>
    <tr>
      <td><strong>5. Chop Avoidance</strong></td>
      <td>Capital Protection</td>
      <td>All Frames</td>
      <td>Triggered whenever indicators conflict or volatility drops below threshold.</td>
      <td>Outputs <strong>CHOP</strong> (flat/no trade), preserving capital.</td>
    </tr>
  </tbody>
</table>

<!-- SECTION 10 -->
<div class="page-break"></div>
<h1>10. Dynamic System Settings &amp; AI Fleet Configuration</h1>
<p>
  The <strong>Settings Dashboard</strong> enables runtime customization of AI agent prompts, model parameters, and environment health auditing without restarting microservice containers.
</p>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/settings_verified.png" alt="AI Agent Settings Console">
  <div class="figure-caption">Figure 10.1: AI Agent Settings Dashboard with Model Selection (Gemini 2.5 Pro vs Flash) and System Instructions.</div>
</div>

<div class="figure avoid-break">
  <img src="file://{manual_images_dir}/settings_saved_verified.png" alt="Environment Health and Saved Verification">
  <div class="figure-caption">Figure 10.2: Environment Health Audit and Toast Notification Confirming Saved Agent Configuration.</div>
</div>

<h3>Configuration Capabilities</h3>
<ul>
  <li><strong>Per-Agent Customization:</strong> Select between <code>Research</code>, <code>Explainer</code>, <code>Strategist</code>, <code>Generator</code>, and <code>Hunter</code> tabs to tune individual personas.</li>
  <li><strong>Model Choice Dropdown:</strong> Toggle between <code>Gemini 2.5 Flash</code> (optimized for sub-second, low-latency queries) and <code>Gemini 2.5 Pro</code> (optimized for complex, multi-step quantitative reasoning and AST validation).</li>
  <li><strong>Temperature Slider:</strong> Adjust model randomness between <code>0.0</code> (strictly deterministic and analytical) and <code>1.0</code> (creative and exploratory).</li>
  <li><strong>System Instructions Editor:</strong> Directly edit agent prompts in the text area. Clicking <strong>Save Configuration</strong> persists changes to the PostgreSQL <code>agent_configs</code> table, which refreshes within 60 seconds across all worker pods.</li>
  <li><strong>Security &amp; Env Health Audit:</strong> Inspects active environment variables (<code>GEMINI_API_KEY</code>, <code>BINANCE_DEMO_KEY</code>, <code>Execution Mode</code>, <code>Kill Switch Status</code>) with visual badges (<span class="badge badge-green">CONFIGURED</span> / <span class="badge badge-amber">STANDBY</span>).</li>
</ul>

<!-- SECTION 11 -->
<div class="page-break"></div>
<h1>11. Local Network (LAN) &amp; Multi-Device Access Architecture</h1>
<p>
  Cryptonite is engineered for seamless operation from secondary devices (laptops, iPads, smartphones) on your local network (LAN):
</p>

<!-- LAN TOPOLOGY SVG DIAGRAM -->
<div class="diagram-box">
  <div class="diagram-title">Figure 11.1: Local Area Network (LAN) Multi-Device Connection Architecture</div>
  <svg viewBox="0 0 880 180" width="100%" height="150" xmlns="http://www.w3.org/2000/svg">
    <!-- Host Machine -->
    <rect x="30" y="20" width="280" height="140" rx="8" fill="#111827" stroke="#0284c7" stroke-width="2"/>
    <text x="170" y="45" fill="#38bdf8" font-family="Inter" font-weight="700" font-size="11" text-anchor="middle">Primary Host Machine (192.168.1.203)</text>
    <text x="170" y="70" fill="#94a3b8" font-family="Inter" font-size="8.5" text-anchor="middle">API Gateway &bull; Bound to 0.0.0.0:8000</text>
    <text x="170" y="90" fill="#94a3b8" font-family="Inter" font-size="8.5" text-anchor="middle">Web Terminal &bull; Bound to 0.0.0.0:3000</text>
    <text x="170" y="110" fill="#94a3b8" font-family="Inter" font-size="8.5" text-anchor="middle">Redis Streams &bull; PostgreSQL 16</text>
    <rect x="50" y="125" width="240" height="22" rx="4" fill="#0369a1"/>
    <text x="170" y="140" fill="#ffffff" font-family="Inter" font-weight="600" font-size="8" text-anchor="middle">Turbopack allowedDevOrigins Enabled</text>

    <!-- Wi-Fi Router -->
    <circle cx="440" cy="90" r="45" fill="#111827" stroke="#10b981" stroke-width="2"/>
    <text x="440" y="85" fill="#10b981" font-family="Inter" font-weight="700" font-size="10" text-anchor="middle">Local Wi-Fi</text>
    <text x="440" y="102" fill="#94a3b8" font-family="JetBrains Mono" font-size="7.5" text-anchor="middle">192.168.1.0/24</text>

    <!-- Secondary Devices -->
    <rect x="570" y="20" width="280" height="140" rx="8" fill="#111827" stroke="#a855f7" stroke-width="2"/>
    <text x="710" y="45" fill="#c084fc" font-family="Inter" font-weight="700" font-size="11" text-anchor="middle">Remote Client Devices</text>
    <text x="710" y="70" fill="#94a3b8" font-family="Inter" font-size="8.5" text-anchor="middle">iPad Pro / Tablet &bull; Trader Laptop &bull; Mobile</text>
    <text x="710" y="90" fill="#94a3b8" font-family="Inter" font-size="8.5" text-anchor="middle">Connect to: http://192.168.1.203:3000</text>
    <text x="710" y="110" fill="#94a3b8" font-family="Inter" font-size="8.5" text-anchor="middle">WS Stream: ws://192.168.1.203:8000/ws/stream</text>
    <rect x="590" y="125" width="240" height="22" rx="4" fill="#7e22ce"/>
    <text x="710" y="140" fill="#ffffff" font-family="Inter" font-weight="600" font-size="8" text-anchor="middle">Dynamic Host Header Resolution</text>

    <!-- Lines -->
    <line x1="310" y1="90" x2="395" y2="90" stroke="#475569" stroke-width="2"/>
    <line x1="485" y1="90" x2="570" y2="90" stroke="#475569" stroke-width="2"/>
  </svg>
</div>

<h3>Configuration Summary</h3>
<table>
  <thead>
    <tr>
      <th>Interface</th>
      <th>Host Binding Address</th>
      <th>Port</th>
      <th>Remote Device Access URL</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><strong>Web User Interface</strong></td>
      <td><code>0.0.0.0</code> (All interfaces)</td>
      <td><code>3000</code></td>
      <td><code>http://192.168.1.203:3000</code></td>
    </tr>
    <tr>
      <td><strong>API &amp; WebSocket Gateway</strong></td>
      <td><code>0.0.0.0</code> (All interfaces)</td>
      <td><code>8000</code></td>
      <td><code>ws://192.168.1.203:8000/ws/stream</code></td>
    </tr>
  </tbody>
</table>

<h3>Steps for Remote LAN Access</h3>
<ol>
  <li>Ensure your secondary device is connected to the same local Wi-Fi subnet (<code>192.168.1.0/24</code>).</li>
  <li>Open any modern browser (Chrome, Safari, Firefox) on your device.</li>
  <li>Navigate to: <strong><code>http://192.168.1.203:3000</code></strong>.</li>
  <li>Log in with operator credentials: <code>admin</code> / <code>admin123</code> / TOTP: <code>000000</code>.</li>
  <li>Live WebSocket price streams and AI multi-agent chats route automatically over the LAN IP.</li>
</ol>

<!-- SECTION 12 -->
<div class="page-break"></div>
<h1>12. Operational Troubleshooting &amp; Diagnostic Runbooks</h1>
<p>
  When diagnosing system issues, adhere to the following verified diagnostic procedures:
</p>

<h3>Diagnostic Runbooks</h3>
<table>
  <thead>
    <tr>
      <th>Symptom</th>
      <th>Probable Root Cause</th>
      <th>Diagnostic Command</th>
      <th>Corrective Action</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><strong>AI Copilot shows "API Key Required"</strong></td>
      <td><code>GEMINI_API_KEY</code> missing or empty in root <code>.env</code> file.</td>
      <td><code>docker compose exec agent_worker env | grep GEMINI</code></td>
      <td>Add valid key to <code>.env</code> and run <code>docker compose restart agent_worker</code>.</td>
    </tr>
    <tr>
      <td><strong>Backtester reports "Out of order bars"</strong></td>
      <td>Historical parquet files contain non-monotonic timestamps.</td>
      <td><code>uv run pytest tests/test_full_stack_backtest_e2e.py</code></td>
      <td>The updated <code>cadence.py</code> engine automatically deduplicates and sorts timestamps.</td>
    </tr>
    <tr>
      <td><strong>WebSocket disconnects on LAN device</strong></td>
      <td>Firewall blocking port 8000 or client resolving localhost.</td>
      <td><code>curl http://192.168.1.203:8000/health</code></td>
      <td>Allow incoming connections on port 8000; verify <code>ws.ts</code> hostname resolution.</td>
    </tr>
    <tr>
      <td><strong>Kill-Switch tripped (HTTP 423 Locked)</strong></td>
      <td>Circuit breaker tripped due to drawdown or operator manual trip.</td>
      <td><code>curl -X GET 'http://localhost:8000/api/v1/control/kill-switch/status?environment=PAPER&amp;account_id=PAPER-ACCOUNT-001'</code></td>
      <td>Authenticate as operator and submit step-up password to reset latch generation.</td>
    </tr>
  </tbody>
</table>

<div class="callout callout-success avoid-break">
  <strong>Complete System Health Check Command</strong>
  To verify all containers are operational, run:<br>
  <code>cd infra &amp;&amp; docker compose ps</code><br>
  All 8 microservices (<code>postgres</code>, <code>redis</code>, <code>api</code>, <code>ingest</code>, <code>scanner</code>, <code>backtester</code>, <code>executor</code>, <code>agent_worker</code>) should show status <code>Up</code> or <code>Healthy</code>.
</div>

</body>
</html>
"""

output_path = docs_dir / 'Cryptonite_User_Manual.html'
output_path.write_text(html_content, encoding='utf-8')
print(f"HTML manual generated successfully: {output_path}")
