"""WebSocket stream endpoint with explicit Origin validation and subscription handling."""

import json
import logging

from cryptonite_api.stream_gateway import ALLOWED_ORIGINS, StreamHub
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("cryptonite.api.ws")
router = APIRouter(tags=["stream"])


@router.websocket("/ws/stream")
async def websocket_stream_endpoint(websocket: WebSocket) -> None:
    # 1. Explicit Origin Header Validation
    origin = websocket.headers.get("origin")
    # Allow empty origin only for local non-browser test clients / curl
    if origin and origin not in ALLOWED_ORIGINS:
        logger.warning("Rejected WebSocket connection from unauthorized origin: %s", origin)
        await websocket.close(code=1008, reason="Unauthorized origin")
        return

    await websocket.accept()
    hub: StreamHub = websocket.app.state.stream_hub
    client = hub.register_client(websocket)
    logger.info("Client connected: %s (origin=%s)", client.client_id, origin)

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                data = json.loads(raw_text)
                if not isinstance(data, dict):
                    await websocket.send_text(json.dumps({"error": "Payload must be a JSON object"}))
                    continue

                resp = await hub.handle_subscription_message(client, data)
                await websocket.send_text(json.dumps(resp))
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"error": "Malformed JSON message"}))

    except WebSocketDisconnect:
        logger.info("Client disconnected: %s", client.client_id)
    except Exception as e:
        logger.error("Error in websocket connection %s: %s", client.client_id, e)
    finally:
        await hub.unregister_client(client)
