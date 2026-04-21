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

# -----------------------------------------------


from math import radians, sin, cos, sqrt, atan2


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def is_address_in_service_area(
    address: str,
    home_base_lat: float,
    home_base_lon: float,
    max_radius_miles: float,
    hard_max_miles: float = None
) -> dict:
    """
    Geocodes an address and checks if it falls within the service radius.

    Returns:
    {
        "ok": True/False,
        "in_range": True/False,
        "distance_miles": float,
        "place_name": str,
        "lat": float,
        "lon": float,
        "error": str  # only if ok=False
    }
    """
    result = mapbox_geocode_one(address)

    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "Geocoding failed")}

    feature = result.get("feature")
    if not feature:
        return {"ok": False, "error": "Address not found"}

    addr_lat = feature.get("lat")
    addr_lon = feature.get("lon")

    if addr_lat is None or addr_lon is None:
        return {"ok": False, "error": "Could not get coordinates"}

    distance = haversine_miles(home_base_lat, home_base_lon, addr_lat, addr_lon)

    # Use hard_max_miles if provided, otherwise use max_radius_miles
    limit = hard_max_miles if hard_max_miles else max_radius_miles
    in_range = distance <= limit

    return {
        "ok": True,
        "in_range": in_range,
        "distance_miles": round(distance, 1),
        "place_name": feature.get("place_name"),
        "lat": addr_lat,
        "lon": addr_lon,
    }
