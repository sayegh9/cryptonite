import asyncio
import json
import logging
import os
import uuid
import time
import asyncpg
import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("debate_worker")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cryptonite_api:api_dev_pass@127.0.0.1:5432/cryptonite")
DEBATE_QUEUE = "stream:debate_queue"
DEBATE_WORKER_GROUP = "debate_workers"
DEBATE_WORKER_NAME = f"worker-{uuid.uuid4().hex[:8]}"

def resolve_execution_mode(configured: str | None) -> str:
    """Autonomous debate execution is frozen until AI mandates exist (AI-first blueprint D11, A0.6).

    A debate may only ever stage a decision for operator approval; the DB setting cannot
    re-enable autonomous orders. Mandated autonomy (Phase 5) replaces this switch.
    """
    if configured == "autonomous":
        logger.warning("DEBATE_EXECUTION_MODE=autonomous ignored: autonomous execution is frozen until mandates exist")
    return "interactive"


async def init_redis_group(redis_client: redis.Redis):
    try:
        await redis_client.xgroup_create(DEBATE_QUEUE, DEBATE_WORKER_GROUP, id="0", mkstream=True)
        logger.info(f"Created consumer group {DEBATE_WORKER_GROUP} for {DEBATE_QUEUE}")
    except Exception as e:
        if "BUSYGROUP Consumer Group name already exists" not in str(e):
            logger.error(f"Error creating consumer group: {e}")

async def emit_heartbeat(redis_client, role_name: str, status: str, msg: str = ""):
    role_lower = role_name.lower()
    if "analyst" in role_lower: agent_id = "scout"
    elif "bull" in role_lower or "bear" in role_lower: agent_id = "debater"
    elif "risk" in role_lower or "cro" in role_lower: agent_id = "cro"
    elif "manager" in role_lower or "orchestrator" in role_lower: agent_id = "orchestrator"
    else: agent_id = "scout"
    
    await redis_client.publish("channel:agent_heartbeat", json.dumps({
        "type": "HEARTBEAT",
        "agentId": agent_id,
        "status": status,
        "msg": msg,
        "timestamp": time.time()
    }))

class AntiCorruptionLayer:
    """Provides sanitized read-only data facades over DuckDB and Postgres."""
    def __init__(self, pg_pool):
        self.pg_pool = pg_pool
        
    async def get_instrument_context(self, instrument_id: str) -> dict:
        """Fetch sanitized instrument and market state context."""
        async with self.pg_pool.acquire() as conn:
            position = await conn.fetchrow(
                "SELECT * FROM agent_safe_positions WHERE instrument_id = $1", 
                instrument_id
            )
            return {
                "instrument_id": instrument_id,
                "current_position": dict(position) if position else None,
                "timestamp": time.time()
            }

    async def get_portfolio_state(self) -> dict:
        """Fetch sanitized total portfolio exposure across all tickers."""
        async with self.pg_pool.acquire() as conn:
            positions = await conn.fetch("SELECT * FROM agent_safe_positions WHERE size != 0")
            return {
                "open_positions": [dict(p) for p in positions],
                "total_exposure_count": len(positions),
                "timestamp": time.time()
            }

async def process_debate_trigger(pg_pool, redis_client, payload: dict):
    instrument_id = payload.get("instrument_id")
    trigger_source = payload.get("trigger_source", "scanner")
    
    logger.info(f"Starting debate run for {instrument_id} triggered by {trigger_source}")
    acl = AntiCorruptionLayer(pg_pool)
    context = await acl.get_instrument_context(instrument_id)
    
    sym = (instrument_id or "BTCUSDT").split(".")[0].upper()
    decision_price = None
    try:
        cached_market = await redis_client.get(f"market:24h:{sym}")
        if cached_market:
            decision_price = float(json.loads(cached_market).get("last_price", 0))
    except Exception:
        pass

    if not decision_price:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=4.0) as http_client:
                bn_r = await http_client.get("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": sym})
                if bn_r.status_code == 200:
                    decision_price = float(bn_r.json().get("lastPrice", 0))
        except Exception as e:
            logger.warning(f"Could not fetch live price for {sym}: {e}")

    context["current_price"] = decision_price
    
    run_id = uuid.uuid4()
    
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at)
               VALUES ($1, $2, 'RUNNING', $3, now())""",
            run_id, instrument_id, trigger_source
        )
    
    try:
        from cryptonite_agents.debate_fleet import (
            get_market_analyst, get_sentiment_analyst, get_onchain_analyst,
            get_bull_debater, get_bear_debater, get_research_manager
        )
        
        logger.info(f"Phase 1: Gathering Intelligence for {instrument_id}...")
        
        market_agent = get_market_analyst()
        sentiment_agent = get_sentiment_analyst()
        onchain_agent = get_onchain_analyst()
        
        prompt = f"Analyze {instrument_id} based on your specialization. Current context: {json.dumps(context)}"
        
        async def run_agent(agent, role_name, prompt_text):
            await emit_heartbeat(redis_client, role_name, "PROCESSING", f"Analyzing {instrument_id}...")
            try:
                async with agent:
                    resp = await agent.chat(prompt_text)
                    txt = await resp.text()
                    async with pg_pool.acquire() as c:
                        await c.execute("INSERT INTO debate_turns (run_id, agent_role, content) VALUES ($1, $2, $3)", run_id, role_name, txt)
                    # Broadcast over websocket
                    await redis_client.publish(f"channel:debates:{instrument_id}", json.dumps({
                        "type": "DEBATE_TURN", "run_id": str(run_id), "role": role_name, "content": txt
                    }))
                    await emit_heartbeat(redis_client, role_name, "ONLINE", "Standby")
                    return f"--- {role_name.upper()} ---\n{txt}"
            except Exception as e:
                await emit_heartbeat(redis_client, role_name, "ERROR", str(e))
                raise
                
        results = await asyncio.gather(
            run_agent(market_agent, "Market Analyst", prompt),
            run_agent(sentiment_agent, "Sentiment Analyst", prompt),
            run_agent(onchain_agent, "On-Chain Analyst", prompt)
        )
        
        
        intel_dossier = "\n\n".join(results)

        
        logger.info(f"Phase 2: Bull vs Bear Debate for {instrument_id}...")
        bull = get_bull_debater()
        bear = get_bear_debater()
        
        results_debate = []
        
        custom_thesis = payload.get("custom_thesis")
        custom_thesis_inject = f"\n\n--- SPECIFIC STRATEGY EVALUATION REQUIRED ---\n{custom_thesis}" if custom_thesis else ""

        bull_prompt = f"Here is the intel dossier for {instrument_id}:\n{intel_dossier}{custom_thesis_inject}\nMake your opening Bull case. If a specific strategy evaluation is required above, ground your case in its backtest metrics."
        bull_thesis = await run_agent(bull, "Bull Researcher", bull_prompt)
        results_debate.append(bull_thesis)

        bear_prompt = f"The Bull Researcher has presented this opening thesis:\n{bull_thesis}\n\nRebut their points and present the Bear case."
        bear_rebuttal = await run_agent(bear, "Bear Researcher", bear_prompt)
        results_debate.append(bear_rebuttal)

        bull_def_prompt = f"The Bear Researcher has rebutted with:\n{bear_rebuttal}\n\nDefend your thesis and provide the final word."
        bull_defense = await run_agent(bull, "Bull Researcher", bull_def_prompt)
        results_debate.append(bull_defense)
        
        full_transcript = intel_dossier + "\n\n" + "\n\n".join(results_debate)
        
        logger.info(f"Phase 3: Portfolio Manager Synthesis for {instrument_id}...")
        
        # Inject Reflection Lessons
        try:
            from google import genai
            import os
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            query_emb_result = await client.aio.models.embed_content(
                model="text-embedding-004",
                contents=f"Market context for {instrument_id}:\n{intel_dossier}"
            )
            q_emb_str = f"[{','.join(map(str, query_emb_result.embeddings[0].values))}]"
            
            async with pg_pool.acquire() as conn:
                lessons = await conn.fetch(
                    "SELECT lessons_learned FROM reflections WHERE embedding IS NOT NULL ORDER BY embedding <=> $1 LIMIT 3", 
                    q_emb_str
                )
                past_lessons = "\n- ".join([r['lessons_learned'] for r in lessons]) if lessons else "No previous lessons yet."
        except Exception as e:
            logger.error(f"Semantic retrieval failed: {e}")
            async with pg_pool.acquire() as conn:
                lessons = await conn.fetch("SELECT lessons_learned FROM reflections ORDER BY created_at DESC LIMIT 3")
                past_lessons = "\n- ".join([r['lessons_learned'] for r in lessons]) if lessons else "No previous lessons yet."
            
        pm = get_research_manager(dynamic_context=past_lessons)
        pm_prompt = f"Based on the following debate transcript for {instrument_id}, make the final portfolio decision in JSON format.\n\n{full_transcript}"
        
        async with pm:
            resp = await pm.chat(pm_prompt)
            txt = await resp.text()
            
            # Save PM turn
            async with pg_pool.acquire() as c:
                await c.execute("INSERT INTO debate_turns (run_id, agent_role, content) VALUES ($1, $2, $3)", run_id, "Portfolio Manager", txt)
            await redis_client.publish(f"channel:debates:{instrument_id}", json.dumps({
                "type": "DEBATE_TURN", "run_id": str(run_id), "role": "Portfolio Manager", "content": txt
            }))
            
            # Parse JSON decision
            try:
                # Use simple regex/parsing to extract JSON if wrapped in markdown block
                import re
                match = re.search(r'\{.*\}', txt, re.DOTALL)
                if match:
                    decision_json = json.loads(match.group(0))
                else:
                    decision_json = json.loads(txt)
                    
                decision = decision_json.get("recommendation", "HOLD")
                conviction = float(decision_json.get("conviction_score", 0.0))
                allocation = float(decision_json.get("allocation_pct", 0.0))
                
                # Output Validation Layer (Reject suspicious decisions)
                if allocation > 25.0:
                    logger.warning(f"Rejecting suspicious allocation: {allocation}%. Capping at 25.0%.")
                    allocation = 25.0
                if decision not in ["BUY", "HOLD", "SELL"]:
                    decision = "HOLD"
                    
                reasoning = decision_json.get("reasoning", "Parsed from PM")
            except Exception as e:
                logger.error(f"Failed to parse PM JSON: {e}. Defaulting to HOLD.")
                decision = "HOLD"
                conviction = 0.0
                allocation = 0.0
                reasoning = "JSON Parsing failed."

        # Phase 4: Risk Debate & Sizing
        from cryptonite_agents.debate_fleet import (
            get_aggressive_risk_agent, get_conservative_risk_agent, get_neutral_risk_agent, get_chief_risk_officer
        )
        logger.info(f"Phase 4: Risk Debate & Sizing for {instrument_id}...")
        portfolio_state = await acl.get_portfolio_state()
        
        risk_prompt = (
            f"Portfolio Manager proposed {decision} for {instrument_id} with {allocation}% allocation.\n"
            f"Current Portfolio State: {json.dumps(portfolio_state)}\n"
            f"Make your risk argument."
        )
        
        agg = get_aggressive_risk_agent()
        con = get_conservative_risk_agent()
        neu = get_neutral_risk_agent()
        
        results_risk = await asyncio.gather(
            run_agent(agg, "Aggressive Risk", risk_prompt),
            run_agent(con, "Conservative Risk", risk_prompt),
            run_agent(neu, "Neutral Risk", risk_prompt)
        )
        
        risk_transcript = f"PM Proposal: {decision} ({allocation}%)\n\n" + "\n\n".join(results_risk)
        
        cro = get_chief_risk_officer()
        cro_prompt = f"Based on the risk debate, make the final sizing decision in JSON:\n\n{risk_transcript}"
        
        async with cro:
            resp = await cro.chat(cro_prompt)
            txt = await resp.text()
            
            # Save CRO turn
            async with pg_pool.acquire() as c:
                await c.execute("INSERT INTO debate_turns (run_id, agent_role, content) VALUES ($1, $2, $3)", run_id, "Chief Risk Officer", txt)
            await redis_client.publish(f"channel:debates:{instrument_id}", json.dumps({
                "type": "DEBATE_TURN", "run_id": str(run_id), "role": "Chief Risk Officer", "content": txt
            }))
            
            # Parse CRO JSON
            try:
                import re
                match = re.search(r'\{.*\}', txt, re.DOTALL)
                if match:
                    cro_json = json.loads(match.group(0))
                else:
                    cro_json = json.loads(txt)
                    
                allocation = float(cro_json.get("final_allocation_pct", allocation))
                conviction_level = cro_json.get("conviction_level", decision)
                if allocation > 25.0:
                    allocation = 25.0
                
                # Update reasoning with risk context
                reasoning += "\n\nRisk Adjustment: " + cro_json.get("risk_reasoning", "")
            except Exception as e:
                logger.error(f"Failed to parse CRO JSON: {e}. Falling back to PM allocation.")
                conviction_level = decision

        async with pg_pool.acquire() as conn:
            # Check system setting for autonomous vs interactive
            mode_row = await conn.fetchrow("SELECT value FROM system_settings WHERE key = 'DEBATE_EXECUTION_MODE'")
            execution_mode = resolve_execution_mode(mode_row["value"] if mode_row else None)
            
            final_status = 'COMPLETED' if execution_mode == 'autonomous' else 'PENDING_APPROVAL'
            
            # Save final decision
            await conn.execute(
                """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, reasoning, decision_price)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                run_id, instrument_id, conviction_level, conviction, allocation, reasoning, decision_price
            )
            
            if execution_mode == 'autonomous':
                # Route to paper execution only if autonomous (or if user approves later)
                await conn.execute(
                    """INSERT INTO paper_portfolio_decisions (run_id, instrument_id, decision_payload)
                       VALUES ($1, $2, $3)""",
                    run_id, instrument_id, json.dumps({
                        "recommendation": conviction_level,
                        "conviction": conviction,
                        "allocation": allocation
                    })
                )
                
                await conn.execute(
                    """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload, status, environment, account_id)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                    uuid.uuid4(), f'debate_autonomous:{run_id}', 'SUBMIT_ORDER', 'AI_DEBATE_WORKER', 'ai:debate_worker',
                    json.dumps({
                        "account_id": "PAPER-ACCOUNT-001",
                        "instrument_id": instrument_id,
                        "recommendation": conviction_level,
                        "conviction_score": conviction,
                        "allocation_pct": allocation
                    }),
                    'PENDING', 'PAPER', 'PAPER-ACCOUNT-001'
                )
                
            await conn.execute(
                "UPDATE debate_runs SET status = $1, completed_at = now() WHERE id = $2",
                final_status, run_id
            )
            
        logger.info(f"Debate {final_status} for {instrument_id}. Decision: {conviction_level}")
        
        # Publish result to websocket channel
        await redis_client.publish(f"channel:debates:{instrument_id}", json.dumps({
            "type": "DEBATE_COMPLETED",
            "run_id": str(run_id),
            "instrument_id": instrument_id,
            "decision": decision
        }))

    except Exception as e:
        logger.error(f"Debate failed for {instrument_id}: {e}")
        async with pg_pool.acquire() as conn:
            await conn.execute("UPDATE debate_runs SET status = 'FAILED', completed_at = now() WHERE id = $1", run_id)

async def process_exit_debate_trigger(pg_pool, redis_client, payload: dict):
    instrument_id = payload.get("instrument_id")
    run_id_str = payload.get("run_id")
    run_id = uuid.UUID(run_id_str) if run_id_str else uuid.uuid4()
    
    logger.info(f"Starting exit debate run for {instrument_id}")
    acl = AntiCorruptionLayer(pg_pool)
    context = await acl.get_instrument_context(instrument_id)
    
    if not context.get("current_position"):
        logger.info(f"No open position found for {instrument_id}. Skipping exit debate.")
        return
        
    try:
        from cryptonite_agents.debate_fleet import (
            get_exit_bull_debater, get_exit_bear_debater, get_exit_manager
        )
        
        logger.info(f"Phase 1: Exit Debate for {instrument_id}...")
        bull = get_exit_bull_debater()
        bear = get_exit_bear_debater()
        
        position = context["current_position"]
        intel = f"Position Context: {json.dumps(position)}"
        
        async def run_agent(agent, role_name, prompt_text):
            await emit_heartbeat(redis_client, role_name, "PROCESSING", f"Exit debate analysis for {instrument_id}")
            try:
                async with agent:
                    resp = await agent.chat(prompt_text)
                    txt = await resp.text()
                    async with pg_pool.acquire() as c:
                        await c.execute("INSERT INTO debate_turns (run_id, agent_role, content) VALUES ($1, $2, $3)", run_id, role_name, txt)
                    await emit_heartbeat(redis_client, role_name, "ONLINE", "Standby")
                    return f"--- {role_name.upper()} ---\n{txt}"
            except Exception as e:
                await emit_heartbeat(redis_client, role_name, "ERROR", str(e))
                raise
                
        results_debate = []
        bull_prompt = f"Context: {intel}\nMake your case to CUT/REDUCE the position."
        bull_thesis = await run_agent(bull, "Exit Bull Researcher", bull_prompt)
        results_debate.append(bull_thesis)
        
        bear_prompt = f"The Exit Bull says: {bull_thesis}\nMake your case to HOLD/ADD to the position."
        bear_rebuttal = await run_agent(bear, "Exit Bear Researcher", bear_prompt)
        results_debate.append(bear_rebuttal)
        
        transcript = "\n\n".join(results_debate)
        
        manager = get_exit_manager()
        manager_prompt = f"Based on the following debate, output your exit decision in JSON format.\n\n{transcript}"
        
        async with manager:
            resp = await manager.chat(manager_prompt)
            txt = await resp.text()
            async with pg_pool.acquire() as c:
                await c.execute("INSERT INTO debate_turns (run_id, agent_role, content) VALUES ($1, $2, $3)", run_id, "Exit Manager", txt)
            
            try:
                import re
                match = re.search(r'\{.*\}', txt, re.DOTALL)
                decision_json = json.loads(match.group(0)) if match else json.loads(txt)
                
                recommendation = decision_json.get("recommendation", "HOLD")
                conviction = float(decision_json.get("conviction_score", 0.0))
                stop_price = decision_json.get("target_stop_price")
                reasoning = decision_json.get("reasoning", "Parsed from PM")
            except Exception as e:
                logger.error(f"Failed to parse exit manager JSON: {e}")
                recommendation, conviction, stop_price, reasoning = "HOLD", 0.0, None, "JSON Parsing failed"
                
        async with pg_pool.acquire() as conn:
            mode_row = await conn.fetchrow("SELECT value FROM system_settings WHERE key = 'DEBATE_EXECUTION_MODE'")
            execution_mode = resolve_execution_mode(mode_row["value"] if mode_row else None)
            final_status = 'COMPLETED' if execution_mode == 'autonomous' else 'PENDING_APPROVAL'
            
            await conn.execute(
                """INSERT INTO exit_decisions (id, run_id, instrument_id, recommendation, conviction_score, target_stop_price, reasoning, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, now())""",
                uuid.uuid4(), run_id, instrument_id, recommendation, conviction, stop_price, reasoning
            )
            
            if execution_mode == 'autonomous' and recommendation in ['REDUCE', 'CLOSE']:
                await conn.execute(
                    """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload, status, environment, account_id)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                    uuid.uuid4(), f'exit_debate_autonomous:{run_id}', 'MODIFY_ORDER', 'AI_DEBATE_WORKER', 'ai:debate_worker',
                    json.dumps({
                        "account_id": "PAPER-ACCOUNT-001",
                        "instrument_id": instrument_id,
                        "action": recommendation,
                        "reduce_only": True
                    }),
                    'PENDING', 'PAPER', 'PAPER-ACCOUNT-001'
                )
            
            await conn.execute("UPDATE debate_runs SET status = $1, completed_at = now() WHERE id = $2", final_status, run_id)
            
    except Exception as e:
        logger.error(f"Exit debate failed for {instrument_id}: {e}")
        async with pg_pool.acquire() as conn:
            await conn.execute("UPDATE debate_runs SET status = 'FAILED', completed_at = now() WHERE id = $1", run_id)

async def main():
    logger.info(f"Starting Cryptonite Debate Worker: {DEBATE_WORKER_NAME}")
    
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    
    await init_redis_group(redis_client)
    try:
        await redis_client.xgroup_create("stream:exit_debate_queue", DEBATE_WORKER_GROUP, id="0", mkstream=True)
    except Exception as e:
        pass
    
    while True:
        try:
            messages = await redis_client.xreadgroup(
                DEBATE_WORKER_GROUP, DEBATE_WORKER_NAME, {DEBATE_QUEUE: ">", "stream:exit_debate_queue": ">"}, count=1, block=5000
            )
            
            if not messages:
                continue
                
            for stream, msgs in messages:
                for msg_id, payload in msgs:
                    logger.info(f"Received message {msg_id} from {stream}: {payload}")
                    try:
                        if stream == DEBATE_QUEUE:
                            await process_debate_trigger(pg_pool, redis_client, payload)
                        elif stream == "stream:exit_debate_queue":
                            await process_exit_debate_trigger(pg_pool, redis_client, payload)
                        await redis_client.xack(stream, DEBATE_WORKER_GROUP, msg_id)
                    except Exception as e:
                        logger.error(f"Error processing message {msg_id}: {e}")
        
        except Exception as e:
            logger.error(f"Redis consumer error: {e}")
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
