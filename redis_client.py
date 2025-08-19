import os
import redis
from typing import Optional
import logging

logger = logging.getLogger(__name__)

def create_redis_client() -> Optional[redis.Redis]:
    """Create Redis client from REDIS_URL environment variable"""
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        logger.warning("REDIS_URL not set, Redis functionality disabled")
        return None
    
    try:
        # Handle both redis:// and rediss:// (SSL) URLs
        if redis_url.startswith("rediss://"):
            # SSL connection
            client = redis.from_url(
                redis_url,
                decode_responses=True,
                ssl=True,
                ssl_cert_reqs=None,  # Don't verify SSL cert
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True
            )
        else:
            # Standard connection
            client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True
            )
        
        # Test connection
        client.ping()
        logger.info("✅ Redis connection established")
        return client
        
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        return None

def get_redis_lock(redis_client: redis.Redis, lock_name: str, ttl: int = 900) -> bool:
    """Acquire a distributed lock with TTL"""
    if not redis_client:
        return False
    
    lock_key = f"lock:mjob:{lock_name}"
    try:
        # Try to set the lock (NX = only if not exists, EX = expire in seconds)
        return redis_client.set(lock_key, "1", nx=True, ex=ttl)
    except Exception as e:
        logger.error(f"Failed to acquire lock {lock_name}: {e}")
        return False

def release_redis_lock(redis_client: redis.Redis, lock_name: str) -> bool:
    """Release a distributed lock"""
    if not redis_client:
        return False
    
    lock_key = f"lock:mjob:{lock_name}"
    try:
        redis_client.delete(lock_key)
        return True
    except Exception as e:
        logger.error(f"Failed to release lock {lock_name}: {e}")
        return False 