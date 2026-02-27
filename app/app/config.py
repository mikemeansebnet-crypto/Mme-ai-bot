import os
import redis

REDIS_URL = os.getenv("REDIS_URL")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "mmeai:call:")
REDIS_TTL_SECONDS = int(os.getenv("REDIS_TTL_SECONDS", "7200"))

redis_client = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
