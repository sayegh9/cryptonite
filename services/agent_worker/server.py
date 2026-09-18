import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from cryptonite_agents.fleet import get_research_agent, get_explainer_agent, get_strategist_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Cryptonite Agent Worker")


class ChatRequest(BaseModel):
    message: str
    instructions: Optional[str] = None
    
class ExplainRequest(BaseModel):
    backtest_id: str

@app.post("/api/v1/agents/chat")
async def chat_with_researcher(req: ChatRequest):
    try:
        async with get_research_agent() as agent:
            response = await agent.chat(req.message)
            text = await response.text()
            return {"response": text}
    except Exception as e:
        logger.error(f"Research agent error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/agents/strategist")
async def chat_with_strategist(req: ChatRequest):
    try:
        agent = get_strategist_agent(custom_instructions=req.instructions)
        async with agent:
            response = await agent.chat(req.message)
            text = await response.text()
            return {"response": text}
    except Exception as e:
        logger.error(f"Strategist agent error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/agents/explain")
async def explain_backtest(req: ExplainRequest):
    try:
        async with get_explainer_agent() as agent:
            prompt = f"Please explain the performance of backtest {req.backtest_id}."
            response = await agent.chat(prompt)
            text = await response.text()
            return {"response": text}
    except Exception as e:
        logger.error(f"Explainer agent error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}
