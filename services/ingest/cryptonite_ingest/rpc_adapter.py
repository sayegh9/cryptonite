import asyncio
import json
import logging
import os
import httpx
from typing import Optional, Dict, Any
import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Fallback to public RPC if no private one is configured
ETH_RPC_URL = os.getenv("ETH_RPC_URL", "https://eth.llamarpc.com")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/1")

class OnChainRPCAdapter:
    """
    Custom RPC adapter to fetch on-chain data directly via JSON-RPC.
    Uses Redis to cache results by block-height to prevent rate limits
    and avoid unnecessary node hammering.
    """
    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self.redis = redis_client or redis.from_url(REDIS_URL, decode_responses=True)
        self.client = httpx.AsyncClient(timeout=10.0)

    async def get_latest_block_number(self) -> int:
        """Fetch the latest Ethereum block number."""
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1
        }
        resp = await self.client.post(ETH_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return int(data["result"], 16)

    async def get_block_by_number(self, block_number: int) -> Dict[str, Any]:
        """Fetch block data, utilizing Redis cache."""
        cache_key = f"rpc:eth:block:{block_number}"
        cached = await self.redis.get(cache_key)
        if cached:
            return json.loads(cached)

        hex_block = hex(block_number)
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": [hex_block, False],
            "id": 1
        }
        resp = await self.client.post(ETH_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()["result"]

        # Cache permanently since blocks are immutable
        await self.redis.set(cache_key, json.dumps(data))
        return data

    async def get_erc20_balance(self, contract_address: str, wallet_address: str, block: str = "latest") -> str:
        """Fetch ERC20 balance using eth_call."""
        # Simple ERC20 balanceOf(address) signature
        # 0x70a08231 + padded address
        addr_padded = wallet_address.lower().replace("0x", "").rjust(64, "0")
        data_param = f"0x70a08231{addr_padded}"
        
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": contract_address, "data": data_param}, block],
            "id": 1
        }
        resp = await self.client.post(ETH_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", "0x0")

    async def get_gas_price(self) -> int:
        """Fetch current gas price (cached for 12 seconds)."""
        cache_key = "rpc:eth:gas_price"
        cached = await self.redis.get(cache_key)
        if cached:
            return int(cached)
            
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_gasPrice",
            "params": [],
            "id": 1
        }
        resp = await self.client.post(ETH_RPC_URL, json=payload)
        resp.raise_for_status()
        price = int(resp.json()["result"], 16)
        
        await self.redis.set(cache_key, str(price), ex=12) # Cache for ~1 block
        return price

    async def close(self):
        await self.client.aclose()
        await self.redis.aclose()
