import httpx
import asyncio
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Global HTTP client with connection pooling
_http_client: Optional[httpx.AsyncClient] = None

async def get_http_client() -> httpx.AsyncClient:
    """Get or create a shared HTTP client with connection pooling"""
    global _http_client
    
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=30.0
            ),
            timeout=httpx.Timeout(
                connect=10.0,
                read=30.0,
                write=10.0,
                pool=30.0
            ),
            headers={
                "User-Agent": "MoraBets/1.0 (Worker)"
            }
        )
        logger.info("✅ HTTP client created with connection pooling")
    
    return _http_client

async def close_http_client():
    """Close the shared HTTP client"""
    global _http_client
    
    if _http_client:
        await _http_client.aclose()
        _http_client = None
        logger.info("✅ HTTP client closed")

async def make_request(url: str, method: str = "GET", **kwargs) -> httpx.Response:
    """Make an HTTP request using the shared client"""
    client = await get_http_client()
    
    try:
        response = await client.request(method, url, **kwargs)
        response.raise_for_status()
        return response
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP {e.response.status_code} for {url}: {e}")
        raise
    except httpx.RequestError as e:
        logger.error(f"Request failed for {url}: {e}")
        raise 