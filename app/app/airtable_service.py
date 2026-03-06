# app/airtable_service.py
import os
import json
import requests

from .config import redis_client


def airtable_create_record(fields: dict) -> dict:
    airtable_token = os.getenv("AIRTABLE_TOKEN")
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID")
    air_table_name = os.getenv("AIRTABLE_TABLE_NAME")

    if not airtable_token or not airtable_base_id or not air_table_name:
        return {"ok": False, "error": "Missing AIRTABLE_TOKEN / AIRTABLE_BASE_ID / AIRTABLE_TABLE_NAME env vars"}

    url = f"https://api.airtable.com/v0/{airtable_base_id}/{air_table_name}"
    headers = {
        "Authorization": f"Bearer {airtable_token}",
        "Content-Type": "application/json",
    }
    payload = {"fields": fields}

    r = requests.post(url, headers=headers, json=payload, timeout=20)

    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "airtable_error": r.text}

    return {"ok": True, "status": r.status_code, "data": r.json()}


def airtable_get_city_corrections() -> dict:
    """
    Returns dictionary like:
    {"bully": "Bowie", "laham": "Lanham"}
    """

    airtable_token = os.getenv("AIRTABLE_TOKEN")
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID")

    if not airtable_token or not airtable_base_id:
        return {}

    url = f"https://api.airtable.com/v0/{airtable_base_id}/City%20Corrections"

    headers = {
        "Authorization": f"Bearer {airtable_token}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.get(url, headers=headers, timeout=20)

        if r.status_code >= 400:
            print("CITY CORRECTIONS ERROR:", r.text)
            return {}

        data = r.json()
        corrections = {}

        for rec in data.get("records", []):
            f = rec.get("fields", {})
            misheard = (f.get("Misheard") or "").strip().lower()
            correct = (f.get("Correct") or "").strip()

            if misheard and correct:
                corrections[misheard] = correct

        return corrections

    except Exception as e:
        print("CITY CORRECTIONS EXCEPTION:", e)
        return {}


def normalize_city(city: str, corrections: dict | None = None) -> str:
    """
    Normalize city input and apply Airtable City Corrections mapping.
    Example: bully -> Bowie
    """
    if not city:
        return ""

    raw = city.strip().lower()
    raw = raw.replace(".", " ").replace(",", " ")
    raw = " ".join(raw.split())

    if corrections and raw in corrections:
        return corrections[raw]

    return " ".join(word.capitalize() for word in raw.split())


def get_contractor_by_twilio_number(to_number: str) -> dict:
    """
    Lookup contractor config by the Twilio number (To).
    Uses Redis cache to reduce Airtable calls and speed up call flow.
    """
    if not to_number:
        return {}

    cache_key = f"mmeai:contractor_cache:{to_number}"

    # 1) Try Redis first
    if redis_client:
        cached_raw = redis_client.get(cache_key)
        if cached_raw:
            try:
                return json.loads(cached_raw)
            except Exception as e:
                print("Bad contractor cache JSON; ignoring cache:", e)

    # 2) Fall back to Airtable
    airtable_token = os.getenv("AIRTABLE_TOKEN")
    airtable_base_id = os.getenv("AIRTABLE_BASE_ID")
    contractors_table = os.getenv("AIRTABLE_CONTRACTORS_TABLE", "Contractors")

    if not airtable_token or not airtable_base_id:
        return {}

    url = f"https://api.airtable.com/v0/{airtable_base_id}/{contractors_table}"
    headers = {"Authorization": f"Bearer {airtable_token}"}

    formula = f"AND({{Twilio Number}}='{to_number}', {{Active}}=TRUE())"
    params = {"filterByFormula": formula, "maxRecords": 1}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code >= 400:
            print("Contractor lookup error:", r.status_code, r.text)
            return {}

        records = r.json().get("records", [])
        if not records:
            return {}

        contractor_fields = records[0].get("fields", {}) or {}

        # Cache for 1 hour
        if redis_client and contractor_fields:
            redis_client.setex(cache_key, 3600, json.dumps(contractor_fields))

        return contractor_fields

    except Exception as e:
        print("Contractor lookup exception:", e)
        return {}
