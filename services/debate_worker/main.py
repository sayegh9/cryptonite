import asyncio
import json
import logging
import os
import time
import uuid

import asyncpg
import redis.asyncio as redis
from cryptonite_agents import news
from cryptonite_agents.debate_fleet import (
    AGGRESSIVE_RISK,
    BEAR,
    BULL,
    CHIEF_RISK_OFFICER,
    CONSERVATIVE_RISK,
    DEBATE_LITE,
    EXIT_BEAR,
    EXIT_BULL,
    EXIT_MANAGER,
    MARKET_ANALYST,
    NEUTRAL_RISK,
    RISK_PANEL,
    SENTIMENT_ANALYST,
    research_manager,
)
from cryptonite_agents.gateway import BudgetGuard, Gateway
from cryptonite_schemas.debate import normalize_verdict
from cryptonite_schemas.marks import read_live_quote, read_marks, resolve_quote_price
from debate_queue import (
    DEBATE_QUEUE,
    EXIT_DEBATE_QUEUE,
    HOUSEKEEPING_S,
    DebateQueue,
    PoisonMessage,
    consumer_name,
    ensure_groups,
    housekeeping,
    is_pel_owner,
)
from gate import (
    AUTOMATIC_SOURCES,
    BRAIN_SOURCES,
    decision_trace_id,
    pre_debate_gate,
    release_cooldown,
)
from mandate_routing import route_entry, route_exit

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("debate_worker")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cryptonite_api:api_dev_pass@127.0.0.1:5432/cryptonite")
DEBATE_WORKER_NAME = consumer_name()  # stable per replica container (B4, F-H6); see debate_queue.py

LITE_MAX_ALLOCATION_PCT = 5.0  # below this a lite verdict stands on its own
LITE_MIN_CONVICTION = 0.35  # a small lite verdict weaker than this is recorded as NO_TRADE
LITE_FIRST_SOURCES = AUTOMATIC_SOURCES | BRAIN_SOURCES  # debates that get one lite call before the full fleet
# "merged": one Pro risk-panel call; "full": 3 risk managers + CRO (4 calls). See docs/adr/0002.
RISK_PANEL_MODE = os.getenv("DEBATE_RISK_PANEL_MODE", "merged")
# R3.2: no on-chain feed exists, so the full debate has no On-Chain Analyst (it argued from invented data)
FULL_DEBATE_ANALYSTS = (MARKET_ANALYST, SENTIMENT_ANALYST)
SENTIMENT_LAST_OK_KEY = "sentiment:last_ok"  # R7.5: the sentiment service has no Redis access by design


async def load_brief(redis_client, payload: dict) -> str | None:
    """The scanner's cached market brief for this candidate, if any."""
    key = payload.get("brief_key")
    if not key or not str(key).startswith("brief:"):
        return None
    try:
        brief = await redis_client.get(key)
        return brief.decode() if isinstance(brief, bytes) else brief
    except Exception as e:
        logger.warning(f"Could not load market brief {key}: {e}")
        return None


async def load_brief_data(redis_client, payload: dict) -> dict | None:
    """The structured twin of the brief (``<brief_key>:data``), if the scanner stored one."""
    key = payload.get("brief_key")
    if not key or not str(key).startswith("brief:"):
        return None
    try:
        raw = await redis_client.get(f"{key}:data")
        data = json.loads(raw) if raw else None
        return data if isinstance(data, dict) else None
    except Exception as e:  # missing or bad data only means the gate asks the model, as before
        logger.warning(f"Could not load market brief data {key}: {e}")
        return None


async def publish_sentiment_ok(redis_client) -> None:
    """Record the last time the local sentiment service tagged headlines for this process."""
    if (ts := news.last_tag_ok()) is None:
        return
    try:
        await redis_client.set(SENTIMENT_LAST_OK_KEY, int(ts))
    except Exception as e:
        logger.debug(f"Could not publish {SENTIMENT_LAST_OK_KEY}: {e}")


def turn_text(output) -> str:
    """Debate turn content: free text as-is, structured verdicts as JSON."""
    return output.model_dump_json() if hasattr(output, "model_dump_json") else str(output)


async def recall_lessons(pg_pool, gateway: Gateway, context_text: str) -> str:
    """Of the 10 reflections closest to this context (pgvector), the 3 with the largest outcome
    (absolute alpha) come first (A7.1: lessons ranked by outcome); else the 3 most recent. Only vectors
    from the model that embedded the query are compared: different embedding models are different spaces."""
    try:
        (vector,), model = await gateway.embed_tagged([context_text], kind="query")
        q_emb_str = f"[{','.join(map(str, vector))}]"
        async with pg_pool.acquire() as conn:
            lessons = await conn.fetch(
                """WITH near AS (SELECT lessons_learned, alpha_vs_benchmark, embedding <=> $1 AS distance
                                 FROM reflections WHERE embedding_model = $2 ORDER BY distance LIMIT 10)
                   SELECT lessons_learned FROM near ORDER BY abs(alpha_vs_benchmark) DESC, distance LIMIT 3""",
                q_emb_str, model,
            )
        if not lessons:  # nothing embedded with this model yet (e.g. right after a model switch)
            async with pg_pool.acquire() as conn:
                lessons = await conn.fetch("SELECT lessons_learned FROM reflections ORDER BY created_at DESC LIMIT 3")
    except Exception as e:
        logger.error(f"Semantic retrieval failed: {e}")
        async with pg_pool.acquire() as conn:
            lessons = await conn.fetch("SELECT lessons_learned FROM reflections ORDER BY created_at DESC LIMIT 3")
    return "\n- ".join(r["lessons_learned"] for r in lessons) if lessons else "No previous lessons yet."


async def emit_heartbeat(redis_client, role_name: str, status: str, msg: str = ""):
    role_lower = role_name.lower()
    if "analyst" in role_lower:
        agent_id = "scout"
    elif "bull" in role_lower or "bear" in role_lower:
        agent_id = "debater"
    elif "risk" in role_lower or "cro" in role_lower:
        agent_id = "cro"
    elif "manager" in role_lower or "orchestrator" in role_lower:
        agent_id = "orchestrator"
    else:
        agent_id = "scout"
    
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

async def already_handled(pg_pool, message_ref: str | None, redis_client=None, consumer: str | None = None) -> bool:
    """Redelivery guard (B4): this stream message already has a run. A finished run means the worker died
    between commit and ack; a RUNNING one means it died mid-debate, so the run is closed as WORKER_LOST
    (not re-debated: the setup has aged by the reclaim window, and a rerun would pay for the models twice).
    The close happens under the row lock only if we still own the message in the PEL: a stale attempt must
    never close the healthy run of the consumer that took the entry over."""
    if not message_ref:
        return False
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, status, claim_gen FROM debate_runs WHERE stream_message_id = $1 FOR UPDATE", message_ref
            )
            if row is None:
                return False
            if row["status"] == "RUNNING":
                if consumer and redis_client:
                    stream, _, msg_id = message_ref.partition("/")
                    if not await is_pel_owner(redis_client, stream, msg_id, consumer):
                        logger.warning(f"Redelivered {message_ref} is no longer owned by {consumer}; "
                                       f"leaving run {row['id']} (RUNNING) to its current owner")
                        return True
                await conn.execute(
                    """UPDATE debate_runs SET status = 'FAILED', completed_at = now(), failure_reason = 'WORKER_LOST'
                       WHERE id = $1 AND status = 'RUNNING' AND claim_gen = $2""",
                    row["id"], row["claim_gen"],
                )
            logger.warning(f"Redelivered {message_ref} already has run {row['id']} ({row['status']}): not debated again")
            return True


async def process_debate_trigger(pg_pool, redis_client, payload: dict, gateway: Gateway | None = None,
                                 message_ref: str | None = None, delivered: int | None = None,
                                 consumer: str | None = None):
    gateway = gateway or Gateway(redis=redis_client)
    instrument_id = payload.get("instrument_id")
    trigger_source = payload.get("trigger_source", "scanner")
    if not instrument_id:
        logger.warning("Debate trigger without instrument_id: refusing (no fallback symbol)")
        return
    if await already_handled(pg_pool, message_ref, redis_client, consumer):
        return

    trace_id = decision_trace_id(payload)
    payload = {**payload, "decision_trace_id": trace_id}
    # D1: the candidate id makes the run row durable dedupe (partial unique index, migration 039)
    candidate_id = None
    try:
        candidate = json.loads(payload.get("candidate_payload") or "{}")
        if isinstance(candidate, dict) and candidate.get("candidate_id"):
            candidate_id = uuid.UUID(str(candidate["candidate_id"]))
    except (TypeError, ValueError):
        candidate_id = None
    logger.info(f"Starting debate run for {instrument_id} triggered by {trigger_source} (decision_trace_id={trace_id})")
    # B5: the decision price is a bounded quote from the orchestrator's all-market quote source —
    # the per-instrument latest-quote snapshot the ingest writes in its quote pipeline (same Binance
    # bookTicker feed as the orchestrator's quote cache): a finite positive bid/ask no older than
    # DECISION_PRICE_MAX_AGE_S. No 24h-ticker cache, no HTTP fallback, no stream scan (a shared
    # stream is retention-bounded but not age-bounded: another symbol's volume could evict a fresh
    # tick). Resolved before the gate, so an unusable quote ends the attempt as NO_PRICE: a terminal debate_runs row
    # (candidate/message/trace, completed_at, reason NO_PRICE) is persisted under the same
    # ownership/unique guards as a run, with no portfolio decision, model call or order. A
    # redelivered message then hits that row in already_handled (idempotent skip, no re-debate).
    # (The mandate-room read below still uses paper:marks at decision time.)
    quoted = resolve_quote_price(instrument_id, await read_live_quote(redis_client, instrument_id))
    if quoted.price is None:
        logger.warning(f"No usable decision price for {instrument_id} (source={quoted.source}, "
                       f"quote_age_s={quoted.age_s}): NO_PRICE, recording the terminal outcome")
        try:
            async with pg_pool.acquire() as conn:
                async with conn.transaction():
                    if consumer and message_ref:
                        sr, _, mr = message_ref.partition("/")
                        if not await is_pel_owner(redis_client, sr, mr, consumer):
                            logger.warning(f"NO_PRICE outcome for {message_ref} refused: {consumer} "
                                           f"no longer owns it in PEL")
                            return
                    await conn.execute(
                        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at,
                                                    completed_at, decision_trace_id, candidate_id,
                                                    stream_message_id, claim_gen, failure_reason)
                           VALUES ($1, $2, 'NO_PRICE', $3, now(), now(), $4, $5, $6, 0, 'NO_PRICE')""",
                        uuid.uuid4(), instrument_id, trigger_source, uuid.UUID(trace_id),
                        candidate_id, message_ref)
        except asyncpg.UniqueViolationError:
            logger.info(f"NO_PRICE outcome for {message_ref or candidate_id} already recorded: "
                        f"redelivery is idempotent")
        return
    logger.info(f"Decision price for {instrument_id}: {quoted.price} (source={quoted.source}, "
                f"quote_age_s={round(quoted.age_s, 1)}, quote_ts_ns={quoted.quote_ts_ns})")
    decision_price = float(quoted.price)

    brief = await load_brief(redis_client, payload)
    gate = await pre_debate_gate(pg_pool, redis_client, payload, gateway, brief,
                                 brief_data=await load_brief_data(redis_client, payload))
    if not gate.allowed:
        return

    # D1: the run row is the first durable write of a run. Until it commits, any failure releases the
    # cooldown and re-raises, so the PEL retry of the same message passes the gate again instead of
    # hitting a cooldown (or dedupe) left behind by an attempt that never became durable.
    my_generation = 0  # the INSERT itself is this attempt's claim (the unique message ref orders entries)
    run_id = uuid.uuid4()
    try:
        acl = AntiCorruptionLayer(pg_pool)
        context = await acl.get_instrument_context(instrument_id)

        context["current_price"] = decision_price

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                if consumer and message_ref:
                    sr, _, mr = message_ref.partition("/")
                    if not await is_pel_owner(redis_client, sr, mr, consumer):
                        logger.warning(
                            "Debate run %s insert refused: %s no longer owns %s in PEL",
                            run_id, consumer, message_ref,
                        )
                        await release_cooldown(redis_client, gate.cooldown_key, gate.cooldown_token)
                        return
                await conn.execute(
                    """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at,
                                                decision_trace_id, candidate_id, stream_message_id, claim_gen)
                       VALUES ($1, $2, 'RUNNING', $3, now(), $4, $5, $6, $7)""",
                    run_id, instrument_id, trigger_source, uuid.UUID(trace_id), candidate_id, message_ref, my_generation,
                )
    except asyncpg.UniqueViolationError:
        # D1 / B4: two deliveries raced past the gate; the winner owns the candidate/message. This one ends here —
        # no model call and never a second durable decision.
        logger.info(f"Duplicate debate run for candidate {candidate_id} / {message_ref}; dropping the redelivery")
        return
    except BaseException:
        # Covers CancelledError too (except Exception misses it). A hard crash still leaves the cooldown
        # key behind, but its value is the candidate's own token, so the redelivery passes it anyway.
        await release_cooldown(redis_client, gate.cooldown_key, gate.cooldown_token)
        raise
    try:
        logger.info(f"Phase 1: Gathering Intelligence for {instrument_id}...")
        context_text = f"{brief}\n\n" if brief else ""
        context_text += f"Position and price context: {json.dumps(context, default=str)}"
        prompt = f"Analyze {instrument_id} based on your specialization.\n\n{context_text}"

        async def run_agent(persona, prompt_text):
            """Run one debate persona; record and broadcast its turn. Returns the gateway result."""
            role_name = persona.name
            await emit_heartbeat(redis_client, role_name, "PROCESSING", f"Analyzing {instrument_id}...")
            try:
                # parent = the setup's decision_trace_id: telemetry only (links traces and F2 shadows to outcomes)
                result = await persona.run(gateway, prompt_text, parent_trace_id=uuid.UUID(trace_id))
                txt = turn_text(result.output)
                async with pg_pool.acquire() as c:
                    await c.execute("INSERT INTO debate_turns (run_id, agent_role, content) VALUES ($1, $2, $3)", run_id, role_name, txt)
                # Broadcast over websocket
                await redis_client.publish(f"channel:debates:{instrument_id}", json.dumps({
                    "type": "DEBATE_TURN", "run_id": str(run_id), "role": role_name, "content": txt
                }))
                await emit_heartbeat(redis_client, role_name, "ONLINE", "Standby")
                return result
            except Exception as e:
                await emit_heartbeat(redis_client, role_name, "ERROR", str(e))
                raise

        def section(persona, result) -> str:
            return f"--- {persona.name.upper()} ---\n{turn_text(result.output)}"

        # Stakes-sized debate (A2.4): scanner candidates get one lite call first; any verdict
        # sized at >= 5% escalates to the full fleet, whatever its conviction.
        # Operator-triggered debates always get the full fleet.
        lite = None
        if trigger_source in LITE_FIRST_SOURCES:
            lite_prompt = f"{context_text}\n\nDebate and decide for {instrument_id}."
            lite = (await run_agent(DEBATE_LITE, lite_prompt)).output
            if lite.recommendation != "HOLD" and lite.allocation_pct >= LITE_MAX_ALLOCATION_PCT:
                # Any sizeable position gets the full review, whatever the lite conviction
                logger.info(f"Lite verdict for {instrument_id} is high-stakes ({lite.allocation_pct}%): escalating to the full debate")
                lite = None
            else:
                decision = lite.recommendation
                if decision != "HOLD" and lite.conviction_score < LITE_MIN_CONVICTION:
                    decision = "HOLD"  # a weak, small verdict is not worth staging
                conviction = lite.conviction_score
                allocation = lite.allocation_pct if decision != "HOLD" else 0.0
                conviction_level = decision
                reasoning = f"{lite.reasoning}\n\nBull: {lite.bull_case}\n\nBear: {lite.bear_case}"

        if lite is None:
            analysts = FULL_DEBATE_ANALYSTS
            results = await asyncio.gather(*(run_agent(a, prompt) for a in analysts))
            await publish_sentiment_ok(redis_client)
            intel_dossier = "\n\n".join(section(a, r) for a, r in zip(analysts, results))

            logger.info(f"Phase 2: Bull vs Bear Debate for {instrument_id}...")
            custom_thesis = payload.get("custom_thesis")
            custom_thesis_inject = f"\n\n--- SPECIFIC STRATEGY EVALUATION REQUIRED ---\n{custom_thesis}" if custom_thesis else ""

            bull_prompt = f"Here is the intel dossier for {instrument_id}:\n{intel_dossier}{custom_thesis_inject}\nMake your opening Bull case. If a specific strategy evaluation is required above, ground your case in its backtest metrics."
            bull_thesis = section(BULL, await run_agent(BULL, bull_prompt))

            bear_prompt = f"The Bull Researcher has presented this opening thesis:\n{bull_thesis}\n\nRebut their points and present the Bear case."
            bear_rebuttal = section(BEAR, await run_agent(BEAR, bear_prompt))

            bull_def_prompt = f"The Bear Researcher has rebutted with:\n{bear_rebuttal}\n\nDefend your thesis and provide the final word."
            bull_defense = section(BULL, await run_agent(BULL, bull_def_prompt))

            full_transcript = intel_dossier + "\n\n" + "\n\n".join([bull_thesis, bear_rebuttal, bull_defense])

            logger.info(f"Phase 3: Portfolio Manager Synthesis for {instrument_id}...")
            past_lessons = await recall_lessons(pg_pool, gateway, f"Market context for {instrument_id}:\n{intel_dossier}")

            pm = research_manager(lessons=past_lessons)
            pm_prompt = f"Based on the following debate transcript for {instrument_id}, make the final portfolio decision.\n\n{full_transcript}"
            # Validated PortfolioDecision; an invalid or missing verdict fails the run (no HOLD default)
            pm_decision = (await run_agent(pm, pm_prompt)).output
            decision = pm_decision.recommendation
            conviction = pm_decision.conviction_score
            allocation = pm_decision.allocation_pct
            reasoning = pm_decision.reasoning

            # Phase 4: Risk Debate & Sizing
            logger.info(f"Phase 4: Risk Debate & Sizing for {instrument_id}...")
            portfolio_state = await acl.get_portfolio_state()

            risk_prompt = (
                f"Portfolio Manager proposed {decision} for {instrument_id} with {allocation}% allocation.\n"
                f"Current Portfolio State: {json.dumps(portfolio_state, default=str)}\n"
                f"Make your risk argument."
            )
            if RISK_PANEL_MODE == "merged":
                # One Pro call weighs the aggressive/conservative/neutral views and decides (A2.4)
                cro_decision = (await run_agent(RISK_PANEL, risk_prompt + "\nThen make the final sizing decision.")).output
            else:
                risk_panel = (AGGRESSIVE_RISK, CONSERVATIVE_RISK, NEUTRAL_RISK)
                results_risk = await asyncio.gather(*(run_agent(r, risk_prompt) for r in risk_panel))
                risk_transcript = f"PM Proposal: {decision} ({allocation}%)\n\n" + "\n\n".join(
                    section(p, r) for p, r in zip(risk_panel, results_risk)
                )
                cro_prompt = f"Based on the risk debate, make the final sizing decision:\n\n{risk_transcript}"
                cro_decision = (await run_agent(CHIEF_RISK_OFFICER, cro_prompt)).output
            allocation = cro_decision.final_allocation_pct
            conviction_level = cro_decision.conviction_level
            reasoning += "\n\nRisk Adjustment: " + cro_decision.risk_reasoning

        # One verdict vocabulary (B2): ``recommendation`` stores the direction (BUY / SELL / HOLD) that
        # scoring, routing and the approve endpoint read; ``conviction_level`` keeps the CRO's own
        # wording (Overweight, ...; the lite path's is its direction). Unknown wording fails the run.
        recommendation = normalize_verdict(conviction_level)
        marks = await read_marks(redis_client)  # B3: the mandate's room at live marks, not entry prices
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                # Durable finalization fence: lock the row FOR UPDATE; only the holder of the current
                # claim_gen (bumped by every claim under this lock) may write its decision and route.
                run_row = await conn.fetchrow(
                    "SELECT status, stream_message_id, claim_gen FROM debate_runs WHERE id = $1 FOR UPDATE", run_id
                )
                if (
                    run_row is None
                    or run_row["status"] != "RUNNING"
                    or run_row["claim_gen"] != my_generation
                    or (message_ref and run_row["stream_message_id"] != message_ref)
                ):
                    logger.warning(
                        "Debate run %s is no longer RUNNING or claimed by another owner (status=%s, gen=%s): finalization fenced, aborting decision and route",
                        run_id, run_row["status"] if run_row else None, run_row["claim_gen"] if run_row else None,
                    )
                    return
                if consumer and message_ref:
                    sr, _, mr = message_ref.partition("/")
                    if not await is_pel_owner(redis_client, sr, mr, consumer):
                        logger.warning("Debate run %s finalization: %s no longer owns %s in PEL; aborting decision and route", run_id, consumer, message_ref)
                        return

                # B5: revalidate the quote at decision time, under the same row lock and PEL proof: a
                # quote fresh when the debate started may be minutes stale now. The final snapshot mid
                # is the persisted decision_price and the mandate-room marks still come from
                # paper:marks; a stale one ends the run as terminal NO_PRICE with no decision and
                # no route.
                final_quoted = resolve_quote_price(instrument_id,
                                                   await read_live_quote(redis_client, instrument_id))
                if final_quoted.price is None:
                    logger.warning(f"Decision price for {instrument_id} went stale during the debate "
                                   f"(source={final_quoted.source}, quote_age_s={final_quoted.age_s}): "
                                   f"run {run_id} ends NO_PRICE, aborting decision and route")
                    await conn.execute(
                        """UPDATE debate_runs SET status = 'NO_PRICE', completed_at = now(),
                                                    failure_reason = 'NO_PRICE'
                           WHERE id = $1 AND status = 'RUNNING' AND claim_gen = $2""",
                        run_id, my_generation,
                    )
                    return
                decision_price = float(final_quoted.price)
                logger.info(f"Final decision price for {instrument_id}: {final_quoted.price} "
                            f"(quote_age_s={round(final_quoted.age_s, 1)})")

                # Save final decision
                await conn.execute(
                    """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_level, conviction_score,
                                                        allocation_pct, reasoning, decision_price)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                    run_id, instrument_id, recommendation, conviction_level, conviction, allocation, reasoning, decision_price
                )
                # Autonomous execution exists only inside a mandate, at its earned level (A5.7; there
                # is no global execution mode any more): L1 approval, L2 veto window, L3 queued.
                if recommendation == "HOLD":
                    final_status, mandate = 'NO_TRADE', None  # nothing to approve or execute
                else:
                    final_status, mandate = await route_entry(
                        conn, run_id, instrument_id, str(conviction_level), float(conviction or 0), float(allocation or 0),
                        mandate_id=payload.get("mandate_id"), decision_trace_id=trace_id, marks=marks,
                    )
                await conn.execute(
                    """UPDATE debate_runs SET status = $1, completed_at = now(), mandate_id = $3
                        WHERE id = $2 AND status = 'RUNNING' AND claim_gen = $4""",
                    final_status, run_id, mandate.id if mandate else None, my_generation,
                )

        logger.info(f"Debate {final_status} for {instrument_id}. Decision: {conviction_level} (decision_trace_id={trace_id})")
        
        # Publish result to websocket channel
        await redis_client.publish(f"channel:debates:{instrument_id}", json.dumps({
            "type": "DEBATE_COMPLETED",
            "run_id": str(run_id),
            "instrument_id": instrument_id,
            "decision": decision
        }))

    except Exception as e:
        logger.error(f"Debate failed for {instrument_id}: {e} (decision_trace_id={trace_id})")
        await release_cooldown(redis_client, gate.cooldown_key, gate.cooldown_token)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                rr = await conn.fetchrow("SELECT status,stream_message_id,claim_gen FROM debate_runs WHERE id=$1 FOR UPDATE", run_id)
                if rr and rr["status"]=="RUNNING" and rr["claim_gen"]==my_generation:
                    owned = True
                    if consumer and message_ref:
                        sr,_,mr=message_ref.partition("/")
                        try:
                            owned = await is_pel_owner(redis_client,sr,mr,consumer)
                        except Exception:
                            owned = False
                    if owned:
                        await conn.execute("UPDATE debate_runs SET status='FAILED',completed_at=now() WHERE id=$1 AND status='RUNNING' AND claim_gen=$2", run_id,my_generation)

async def process_exit_debate_trigger(pg_pool, redis_client, payload: dict, gateway: Gateway | None = None,
                                      message_ref: str | None = None, delivered: int | None = None,
                                      consumer: str | None = None):
    gateway = gateway or Gateway(redis=redis_client)
    instrument_id = payload.get("instrument_id")
    run_id_str = payload.get("run_id")
    run_id = uuid.UUID(run_id_str) if run_id_str else uuid.uuid4()
    my_generation = 0

    logger.info(f"Starting exit debate run for {instrument_id}")
    if run_id_str:
        # Claim under the row lock: FOR UPDATE serializes concurrent attempts of this same run, the
        # current-PEL-owner proof inside the transaction refuses a stale attempt, and the generation
        # bump fences every earlier holder (their later writes match the old generation only).
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status, stream_message_id, claim_gen FROM debate_runs WHERE id = $1 FOR UPDATE", run_id)
                if row is None or row["status"] != "RUNNING":
                    logger.warning(f"Exit debate run {run_id} is not RUNNING (status={row['status'] if row else None}): skipping")
                    return
                if row["stream_message_id"] and message_ref and row["stream_message_id"] != message_ref:
                    logger.warning(f"Exit debate run {run_id} belongs to {row['stream_message_id']}, not {message_ref}: skipping")
                    return
                if consumer and message_ref:
                    stream, _, msg_id = message_ref.partition("/")
                    if not await is_pel_owner(redis_client, stream, msg_id, consumer):
                        logger.warning(f"Exit claim for {run_id} refused: {consumer} no longer owns {message_ref} in PEL")
                        return
                my_generation = row["claim_gen"] + 1
                await conn.execute(
                    """UPDATE debate_runs SET claim_gen = $2, stream_message_id = COALESCE($3, stream_message_id)
                       WHERE id = $1""",
                    run_id, my_generation, message_ref,
                )
    else:
        try:
            async with pg_pool.acquire() as conn:
                async with conn.transaction():
                    if consumer and message_ref:
                        sr, _, mr = message_ref.partition("/")
                        if not await is_pel_owner(redis_client, sr, mr, consumer):
                            logger.warning(
                                "Exit debate run %s insert refused: %s no longer owns %s in PEL",
                                run_id, consumer, message_ref,
                            )
                            return
                    await conn.execute(
                        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at, stream_message_id, claim_gen)
                           VALUES ($1, $2, 'RUNNING', 'exit_monitor', now(), $3, $4)""",
                        run_id, instrument_id, message_ref, my_generation,
                    )
        except asyncpg.UniqueViolationError:
            logger.info(f"Duplicate exit run for {message_ref}: the existing run wins")
            await already_handled(pg_pool, message_ref, redis_client, consumer)
            return
    acl = AntiCorruptionLayer(pg_pool)
    context = await acl.get_instrument_context(instrument_id)

    if not context.get("current_position"):
        logger.info(f"No open position found for {instrument_id}. Skipping exit debate.")
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                rr = await conn.fetchrow("SELECT status,claim_gen,stream_message_id FROM debate_runs WHERE id=$1 FOR UPDATE", run_id)
                if rr and rr["status"] == "RUNNING" and rr["claim_gen"] == my_generation:
                    owned = True
                    if consumer and message_ref:
                        sr, _, mr = message_ref.partition("/")
                        try:
                            owned = await is_pel_owner(redis_client, sr, mr, consumer)
                        except Exception:
                            owned = False
                    if owned:
                        await conn.execute(
                            "UPDATE debate_runs SET status='NO_TRADE',completed_at=now() WHERE id=$1 AND status='RUNNING' AND claim_gen=$2",
                            run_id, my_generation,
                        )
        return

    try:
        logger.info(f"Phase 1: Exit Debate for {instrument_id}...")
        position = context["current_position"]
        intel = f"Position Context: {json.dumps(position, default=str)}"

        async def run_agent(persona, prompt_text):
            role_name = persona.name
            await emit_heartbeat(redis_client, role_name, "PROCESSING", f"Exit debate analysis for {instrument_id}")
            try:
                result = await persona.run(gateway, prompt_text)
                async with pg_pool.acquire() as c:
                    await c.execute("INSERT INTO debate_turns (run_id, agent_role, content) VALUES ($1, $2, $3)", run_id, role_name, turn_text(result.output))
                await emit_heartbeat(redis_client, role_name, "ONLINE", "Standby")
                return result
            except Exception as e:
                await emit_heartbeat(redis_client, role_name, "ERROR", str(e))
                raise

        bull_thesis = turn_text((await run_agent(EXIT_BULL, f"Context: {intel}\nMake your case to CUT/REDUCE the position.")).output)
        bear_rebuttal = turn_text((await run_agent(EXIT_BEAR, f"The Exit Bull says: --- EXIT BULL RESEARCHER ---\n{bull_thesis}\nMake your case to HOLD/ADD to the position.")).output)
        transcript = f"--- EXIT BULL RESEARCHER ---\n{bull_thesis}\n\n--- EXIT BEAR RESEARCHER ---\n{bear_rebuttal}"

        manager_prompt = f"Based on the following debate, make your exit decision.\n\n{transcript}"
        exit_decision = (await run_agent(EXIT_MANAGER, manager_prompt)).output
        recommendation = exit_decision.recommendation
        conviction = exit_decision.conviction_score
        stop_price = exit_decision.target_stop_price
        reasoning = exit_decision.reasoning

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                # Durable finalization fence: lock the row FOR UPDATE; only the holder of the current
                # claim_gen may insert its decision and route. A newer claim (takeover) has bumped past us.
                run_row = await conn.fetchrow(
                    "SELECT status, stream_message_id, claim_gen FROM debate_runs WHERE id = $1 FOR UPDATE", run_id
                )
                if (
                    run_row is None
                    or run_row["status"] != "RUNNING"
                    or run_row["claim_gen"] != my_generation
                    or (message_ref and run_row["stream_message_id"] != message_ref)
                ):
                    logger.warning(
                        "Exit debate run %s is no longer RUNNING or claimed by a newer generation (status=%s, gen=%s, expected=%s): finalization fenced, aborting route",
                        run_id, run_row["status"] if run_row else None, run_row["claim_gen"] if run_row else None, my_generation,
                    )
                    return
                if consumer and message_ref:
                    sr, _, mr = message_ref.partition("/")
                    try:
                        if not await is_pel_owner(redis_client, sr, mr, consumer):
                            logger.warning("Exit debate run %s finalization: %s no longer owns %s in PEL; aborting route", run_id, consumer, message_ref)
                            return
                    except Exception:
                        return

                await conn.execute(
                    """INSERT INTO exit_decisions (id, run_id, instrument_id, recommendation, conviction_score, target_stop_price, reasoning, created_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, now())""",
                    uuid.uuid4(), run_id, instrument_id, recommendation, conviction, stop_price, reasoning
                )
                # Risk reducing on a mandate's own position: immediate at L2/L3, a veto window that
                # proceeds at L1 (D8). Anything else waits for the operator.
                final_status, mandate = await route_exit(conn, run_id, instrument_id, recommendation)
                await conn.execute(
                    """UPDATE debate_runs SET status = $1, completed_at = now(), mandate_id = $3
                        WHERE id = $2 AND status = 'RUNNING' AND claim_gen = $4""",
                    final_status, run_id, mandate.id if mandate else None, my_generation,
                )

    except Exception as e:
        logger.error(f"Exit debate failed for {instrument_id}: {e}")
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                rr = await conn.fetchrow("SELECT status,claim_gen,stream_message_id FROM debate_runs WHERE id=$1 FOR UPDATE", run_id)
                if rr and rr["status"] == "RUNNING" and rr["claim_gen"] == my_generation:
                    owned = True
                    if consumer and message_ref:
                        sr, _, mr = message_ref.partition("/")
                        try:
                            owned = await is_pel_owner(redis_client, sr, mr, consumer)
                        except Exception:
                            owned = False
                    if owned:
                        await conn.execute(
                            "UPDATE debate_runs SET status='FAILED',completed_at=now() WHERE id=$1 AND status='RUNNING' AND claim_gen=$2",
                            run_id, my_generation,
                        )

async def main():
    logger.info(f"Starting Cryptonite Debate Worker: {DEBATE_WORKER_NAME}")
    
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=30)  # redis-py 8 times a 5 s XREADGROUP out at exactly its block time
    pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    
    # The gateway writes telemetry as its own role; the model's SQL tools use the read-only agent role
    telemetry_url = os.getenv("AI_TELEMETRY_DATABASE_URL")
    telemetry_pool = await asyncpg.create_pool(telemetry_url, min_size=1, max_size=3) if telemetry_url else None
    if telemetry_pool is None:
        logger.error("AI_TELEMETRY_DATABASE_URL not set: LLM spend is counted in this process only")
    gateway = Gateway(pool=telemetry_pool, redis=redis_client,
                      budget=BudgetGuard(pool=telemetry_pool, redis=redis_client, spend_unknown=telemetry_pool is None))
    async def handle(stream: str, msg_id: str, payload: dict, delivered: int) -> None:
        if not payload.get("instrument_id"):
            raise PoisonMessage("no instrument_id")
        # Queue ownership context (B4): the consumer name + the live PEL proof the DB claims re-check
        # under their row lock. The queue has already proven ownership once before invoking this handler;
        # each claim proves it again at the moment it takes the run.
        if stream == DEBATE_QUEUE:
            await process_debate_trigger(pg_pool, redis_client, payload, gateway, message_ref=f"{stream}/{msg_id}",
                                         delivered=delivered, consumer=DEBATE_WORKER_NAME)
        elif stream == EXIT_DEBATE_QUEUE:
            await process_exit_debate_trigger(pg_pool, redis_client, payload, gateway, message_ref=f"{stream}/{msg_id}",
                                              delivered=delivered, consumer=DEBATE_WORKER_NAME)

    # At-least-once with reclaim, bounded retries and a DLQ (B4, F-H6): see debate_queue.py
    queue = DebateQueue(redis_client, DEBATE_WORKER_NAME, handle)
    await ensure_groups(redis_client)
    try:
        await queue.recover_own()
    except Exception as e:
        logger.error(f"Recovering own pending entries failed: {e}")

    next_housekeeping = 0.0
    while True:
        try:
            if time.monotonic() >= next_housekeeping:
                next_housekeeping = time.monotonic() + HOUSEKEEPING_S
                await housekeeping(redis_client, pg_pool, queue)
            await queue.read(block_ms=5000)
        except Exception as e:
            logger.error(f"Redis consumer error: {e}")
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
