# app/state.py
import json
from typing import Optional

from .config import redis_client, REDIS_PREFIX, REDIS_TTL_SECONDS


# ================= State (per CallSid) =================

def _redis_key(call_sid: str) -> str:
    return f"{REDIS_PREFIX}{call_sid}"


def get_state(call_sid: str) -> dict:
    if not redis_client or not call_sid:
        return {}

    raw = redis_client.get(_redis_key(call_sid))
    if not raw:
        return {}

    try:
        return json.loads(raw)
    except Exception as e:
        print("Bad state JSON in Redis for", call_sid, "err:", e)
        return {}


def set_state(call_sid: str, state: dict) -> None:
    if not redis_client or not call_sid:
        return
    redis_client.setex(_redis_key(call_sid), REDIS_TTL_SECONDS, json.dumps(state))


def clear_state(call_sid: str) -> None:
    if redis_client and call_sid:
        redis_client.delete(_redis_key(call_sid))


# ================= Alias Helpers =================
# Maps NEW CallSid -> OLD CallSid for resume/reconnect flow

def alias_key(new_call_sid: str) -> str:
    return f"mmeai:alias:{new_call_sid}"


def set_call_alias(new_call_sid: str, old_call_sid: str, ttl_seconds: int = 900) -> None:
    if not redis_client or not new_call_sid or not old_call_sid:
        return
    redis_client.setex(alias_key(new_call_sid), ttl_seconds, old_call_sid)


def get_call_alias(new_call_sid: str) -> Optional[str]:
    if not redis_client or not new_call_sid:
        return None
    v = redis_client.get(alias_key(new_call_sid))
    return v if v else None


def clear_call_alias(new_call_sid: str) -> None:
    if redis_client and new_call_sid:
        redis_client.delete(alias_key(new_call_sid))


# ================= Live Call Tracking (per contractor) =================

def contractor_calls_key(contractor_key: str) -> str:
    return f"mmeai:contractor:{contractor_key}:calls"


def register_live_call(contractor_key: str, call_sid: str) -> None:
    """
    Track that this call is active for this contractor.
    Uses a Redis SET to avoid duplicates.
    """
    if not redis_client or not contractor_key or not call_sid:
        return

    k = contractor_calls_key(contractor_key)
    redis_client.sadd(k, call_sid)
    # Keep the contractor live-call set from lasting forever
    redis_client.expire(k, REDIS_TTL_SECONDS)


def unregister_live_call(contractor_key: str, call_sid: str) -> None:
    if not redis_client or not contractor_key or not call_sid:
        return
    k = contractor_calls_key(contractor_key)
    redis_client.srem(k, call_sid)


def list_live_calls(contractor_key: str) -> list[str]:
    """
    Returns list of CallSids currently registered as live for this contractor.
    """
    if not redis_client or not contractor_key:
        return []
    k = contractor_calls_key(contractor_key)
    return list(redis_client.smembers(k))


# ================= Resume Pointer Helpers =================
# Maps (ToNumber + FromNumber) -> CallSid being resumed

def resume_key(to_number: str, from_number: str) -> str:
    return f"mmeai:resume:{to_number}:{from_number}"


def save_resume_pointer(to_number: str, from_number: str, call_sid: str, ttl_seconds: int = 600) -> None:
    if not redis_client or not to_number or not from_number or not call_sid:
        return
    redis_client.setex(resume_key(to_number, from_number), ttl_seconds, call_sid)


def get_resume_pointer(to_number: str, from_number: str) -> Optional[str]:
    if not redis_client:
        return None
    value = redis_client.get(resume_key(to_number, from_number))
    if not value:
        return None
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else value


def clear_resume_pointer(to_number: str, from_number: str) -> None:
    if not redis_client:
        return
    redis_client.delete(resume_key(to_number, from_number))
