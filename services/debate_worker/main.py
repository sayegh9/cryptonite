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

async def init_redis_group(redis_client: redis.Redis):
    try:
        await redis_client.xgroup_create(DEBATE_QUEUE, DEBATE_WORKER_GROUP, id="0", mkstream=True)
        logger.info(f"Created consumer group {DEBATE_WORKER_GROUP} for {DEBATE_QUEUE}")
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP Consumer Group name already exists" not in str(e):
            logger.error(f"Error creating consumer group: {e}")

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
        
        async def run_agent(agent, role_name):
            async with agent:
                resp = await agent.chat(prompt)
                txt = await resp.text()
                async with pg_pool.acquire() as c:
                    await c.execute("INSERT INTO debate_turns (run_id, agent_role, content) VALUES ($1, $2, $3)", run_id, role_name, txt)
                # Broadcast over websocket
                await redis_client.publish(f"channel:debates:{instrument_id}", json.dumps({
                    "type": "DEBATE_TURN", "run_id": str(run_id), "role": role_name, "content": txt
                }))
                return f"--- {role_name.upper()} ---\n{txt}"
                
        results = await asyncio.gather(
            run_agent(market_agent, "Market Analyst"),
            run_agent(sentiment_agent, "Sentiment Analyst"),
            run_agent(onchain_agent, "On-Chain Analyst")
        )
        
        intel_dossier = "\n\n".join(results)
        
        logger.info(f"Phase 2: Bull vs Bear Debate for {instrument_id}...")
        bull = get_bull_debater()
        bear = get_bear_debater()
        
        debate_prompt = f"Here is the intel dossier for {instrument_id}:\n{intel_dossier}\nMake your case."
        
        results_debate = await asyncio.gather(
            run_agent(bull, "Bull Researcher"),
            run_agent(bear, "Bear Researcher")
        )
        
        full_transcript = intel_dossier + "\n\n" + "\n\n".join(results_debate)
        
        logger.info(f"Phase 3: Portfolio Manager Synthesis for {instrument_id}...")
        
        # Inject Reflection Lessons
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
            run_agent(agg, "Aggressive Risk"),
            run_agent(con, "Conservative Risk"),
            run_agent(neu, "Neutral Risk")
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
            execution_mode = mode_row["value"] if mode_row else "interactive"
            
            final_status = 'COMPLETED' if execution_mode == 'autonomous' else 'PENDING_APPROVAL'
            
            # Save final decision
            await conn.execute(
                """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, reasoning)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                run_id, instrument_id, conviction_level, conviction, allocation, reasoning
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

async def main():
    logger.info(f"Starting Cryptonite Debate Worker: {DEBATE_WORKER_NAME}")
    
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    
    await init_redis_group(redis_client)
    
    while True:
        try:
            # XREADGROUP block for 5 seconds
            messages = await redis_client.xreadgroup(
                DEBATE_WORKER_GROUP, DEBATE_WORKER_NAME, {DEBATE_QUEUE: ">"}, count=1, block=5000
            )
            
            if not messages:
                continue
                
            for stream, msgs in messages:
                for msg_id, payload in msgs:
                    logger.info(f"Received message {msg_id}: {payload}")
                    try:
                        # process
                        await process_debate_trigger(pg_pool, redis_client, payload)
                        # ack
                        await redis_client.xack(stream, DEBATE_WORKER_GROUP, msg_id)
                    except Exception as e:
                        logger.error(f"Error processing message {msg_id}: {e}")
        
        except Exception as e:
            logger.error(f"Redis consumer error: {e}")
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
