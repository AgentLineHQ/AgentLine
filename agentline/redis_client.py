"""
AgentLine — Redis Client
Optional async Redis connection for caching, rate limiting, and pub/sub.
The app runs fine without Redis — it's only needed for future features.
"""

import logging

logger = logging.getLogger(__name__)

_redis = None


async def init_redis():
    """Initialize Redis connection if REDIS_URL is configured."""
    global _redis
    from agentline.config import settings
    if not settings.REDIS_URL:
        logger.info("Redis not configured — skipping (not required for MVP)")
        return
    try:
        import redis.asyncio as redis_lib
        _redis = redis_lib.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        # Test connection
        await _redis.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.warning("Redis unavailable (%s) — running without it", e)
        _redis = None


async def close_redis():
    """Close Redis connection if active."""
    global _redis
    if _redis:
        await _redis.close()
        _redis = None


def get_redis():
    """Get the Redis client instance. Returns None if Redis is not available."""
    return _redis
