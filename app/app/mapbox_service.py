# app/app/mapbox_service.py
import os
import requests
from urllib.parse import quote_plus
from typing import Optional 

MAPBOX_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN", "").strip()

def mapbox_address_candidates(query: str, limit: int = 3, country: str = "US", proximity: Optional[str] = None):
    """
    Uses Mapbox Geocoding v6 forward endpoint to return top address candidates.
    Returns: list of dicts: {"full_address": str, "confidence": float|None}
    """
    if not MAPBOX_TOKEN:
        return []

    q = quote_plus(query)
    url = (
        "https://api.mapbox.com/search/geocode/v6/forward"
        f"?q={q}"
        f"&types=address"
        f"&country={country}"
        f"&limit={limit}"
        f"&access_token={MAPBOX_TOKEN}"
    )

    # Add proximity bias if provided 
    if proximity:
        url += f"&proximity={proximity}"

    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    out = []
    for f in (data.get("features") or []):
        props = f.get("properties") or {}
        full_address = props.get("full_address")  # documented field
        # match_code.confidence exists for address features (helps decide if you should trust it)
        match_code = props.get("match_code") or {}
        confidence = match_code.get("confidence")  # usually 0..1 (not guaranteed)
        if full_address:
            out.append({"full_address": full_address, "confidence": confidence})
    return out

def mapbox_geocode_one(query: str, country: str = "US", proximity: str | None = None) -> dict:
    """
    Returns one best geocoded result with place_name + lat/lon.
    Example return:
    {
        "ok": True,
        "feature": {
            "place_name": "4515 Primrose Folly Court, Bowie, Maryland 20720, United States",
            "lat": 38.94,
            "lon": -76.73
        }
    }
    """
    if not MAPBOX_TOKEN:
        return {"ok": False, "error": "Missing MAPBOX_ACCESS_TOKEN"}

    q = quote_plus(query)
    url = (
        "https://api.mapbox.com/geocoding/v5/mapbox.places/"
        f"{q}.json"
        f"?types=address"
        f"&country={country}"
        f"&limit=1"
        f"&access_token={MAPBOX_TOKEN}"
    )

    if proximity:
        url += f"&proximity={proximity}"

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()

        features = data.get("features", []) or []
        if not features:
            return {"ok": True, "feature": None}

        f = features[0]
        center = f.get("center") or [None, None]

        return {
            "ok": True,
            "feature": {
                "place_name": f.get("place_name"),
                "lon": center[0],
                "lat": center[1],
            },
        }

    except Exception as e:
        print("MAPBOX GEOCODE ONE ERROR |", e)
        return {"ok": False, "error": str(e)}
