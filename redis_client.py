import os
import redis
import logging

logger = logging.getLogger(__name__)

def redis_client():
    """Create Redis client from REDIS_URL environment variable"""
    url = os.getenv("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL missing")
    
    use_tls = url.startswith("rediss://")
    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            ssl=use_tls,
            ssl_cert_reqs=None,  # Render Redis uses TLS; this avoids cert issues
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
        raise RuntimeError(f"Redis connection failed: {e}")

def create_redis_client():
    """Legacy function for backward compatibility"""
    try:
        return redis_client()
    except Exception:
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