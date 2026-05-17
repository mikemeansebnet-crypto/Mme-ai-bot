# -----------------------------------------------
# FILE: app/app/aerial_service.py
# What it does: Pulls satellite image of a property
# using Mapbox, analyzes with Claude Vision,
# generates square footage estimate and quote range
# -----------------------------------------------

import os
import requests
import base64
import anthropic
from app.app.mapbox_service import mapbox_geocode_one

MAPBOX_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN", "").strip()
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "").strip()

# Quote ranges per job type per 1000 sq ft
QUOTE_RANGES = {
    "lawn": {"min": 45, "max": 85, "unit": "per 1000 sq ft"},
    "mowing": {"min": 45, "max": 85, "unit": "per 1000 sq ft"},
    "mulch": {"min": 75, "max": 150, "unit": "per 1000 sq ft"},
    "aeration": {"min": 80, "max": 140, "unit": "per 1000 sq ft"},
    "overseeding": {"min": 60, "max": 120, "unit": "per 1000 sq ft"},
    "trimming": {"min": 50, "max": 100, "unit": "per 1000 sq ft"},
    "pressure washing": {"min": 0.08, "max": 0.20, "unit": "per sq ft"},
    "snow removal": {"min": 60, "max": 150, "unit": "per visit per 1000 sq ft"},
    "fence": {"min": 15, "max": 40, "unit": "per linear ft"},
    "roofing": {"min": 3.50, "max": 8.00, "unit": "per sq ft"},
    "painting": {"min": 1.50, "max": 4.00, "unit": "per sq ft"},
    "drywall": {"min": 1.50, "max": 3.50, "unit": "per sq ft"},
    "default": {"min": 50, "max": 150, "unit": "per 1000 sq ft"},
}


def get_satellite_image_url(lat: float, lon: float, zoom: int = 19) -> str:
    """
    Returns Mapbox satellite image URL for a given lat/lon.
    Zoom 19 = building/property level detail.
    """
    return (
        f"https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static/"
        f"{lon},{lat},{zoom},0/1000x1000@2x"
        f"?access_token={MAPBOX_TOKEN}"
    )


def download_satellite_image(lat: float, lon: float, zoom: int = 19) -> bytes | None:
    """Downloads satellite image as bytes."""
    try:
        url = get_satellite_image_url(lat, lon, zoom)
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            print(f"AERIAL | Satellite image downloaded | {lat},{lon} | zoom:{zoom}")
            return response.content
        else:
            print(f"AERIAL | Satellite image failed | {response.status_code}")
            return None
    except Exception as e:
        print(f"AERIAL | Download error | {e}")
        return None


def upload_to_cloudinary(image_bytes: bytes, lead_id: str) -> str | None:
    """Uploads satellite image to Cloudinary and returns URL."""
    try:
        import cloudinary
        import cloudinary.uploader

        result = cloudinary.uploader.upload(
            image_bytes,
            folder="aerial_views",
            public_id=f"aerial_{lead_id}",
            overwrite=True,
            resource_type="image"
        )
        url = result.get("secure_url")
        print(f"AERIAL | Uploaded to Cloudinary | {url}")
        return url
    except Exception as e:
        print(f"AERIAL | Cloudinary upload error | {e}")
        return None


def analyze_aerial_with_claude(
    image_bytes: bytes,
    address: str,
    job_description: str,
    customer_name: str = ""
) -> dict:
    """
    Sends satellite image to Claude Vision for analysis.
    Returns estimated square footage, scope, and quote range.
    """
    try:
        # Encode image as base64
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

        # Determine job type for pricing
        job_lower = job_description.lower()
        pricing = QUOTE_RANGES["default"]
        for key in QUOTE_RANGES:
            if key in job_lower:
                pricing = QUOTE_RANGES[key]
                break

        prompt = f"""You are an expert contractor estimator reviewing a satellite aerial image of a property.

Property Address: {address}
Customer: {customer_name}
Job Requested: {job_description}

Analyze the image carefully and provide a practical contractor-facing estimate.

Important accuracy rules:
- Only describe features clearly visible in the image
- Do NOT guess hidden areas, backyard sections, or unclear boundaries
- If unsure, say “verify on-site”
- Square footage must be a realistic working estimate, not exact measurement

Square footage guidelines:
- Small residential front yard: 1,000–3,000 sq ft
- Medium property: 3,000–8,000 sq ft
- Large property: 8,000–20,000+ sq ft
- Only include the WORK AREA related to the job (not entire property unless applicable)
- Exclude roads, neighbors, and unclear areas

Format your response EXACTLY like this:

PROPERTY SIZE: [rough lot size or “not clearly visible”]
WORK AREA: [estimated work area in sq ft]
DESCRIPTION: [2-3 sentences about visible layout]
SCOPE: [clear scope based on job requested]
COMPLEXITY: [Simple/Moderate/Complex] - [reason]
SQUARE_FOOTAGE: [number only]

Important notes:
- Use rounded numbers (e.g., 2500, 4800, 10000 — no decimals)
- Be conservative to avoid overestimating
- If multiple areas exist, estimate the primary work zone only
- This estimate will be verified on-site — accuracy over confidence
"""


        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }]
        )

        analysis_text = message.content[0].text.strip()
        print(f"AERIAL | Claude analysis complete | {address}")

        # Extract square footage
        sq_ft = 0
        for line in analysis_text.split("\n"):
            if "SQUARE_FOOTAGE:" in line:
                try:
                    sq_ft = int(line.split(":")[1].strip().replace(",", ""))
                except Exception:
                    sq_ft = 0

        # Calculate quote range
        sq_ft_units = sq_ft / 1000 if "per 1000 sq ft" in pricing["unit"] else sq_ft
        quote_min = round(sq_ft_units * pricing["min"])
        quote_max = round(sq_ft_units * pricing["max"])

        # Ensure reasonable minimums
        quote_min = max(quote_min, 50)
        quote_max = max(quote_max, 100)

        return {
            "ok": True,
            "analysis": analysis_text,
            "square_footage": sq_ft,
            "quote_min": quote_min,
            "quote_max": quote_max,
            "quote_range": f"${quote_min} - ${quote_max}",
            "pricing_unit": pricing["unit"],
        }

    except Exception as e:
        print(f"AERIAL | Claude analysis error | {e}")
        return {"ok": False, "error": str(e)}


def run_aerial_quote(
    address: str,
    job_description: str,
    lead_id: str,
    customer_name: str = "",
    zoom: int = 18
) -> dict:
    """
    Full aerial quote pipeline:
    1. Geocode address → lat/lon
    2. Download satellite image
    3. Upload to Cloudinary
    4. Analyze with Claude Vision
    5. Return full result
    """
    print(f"AERIAL QUOTE | Starting | {address} | {job_description}")

    # Step 1 — Geocode
    geo = mapbox_geocode_one(address)
    if not geo.get("ok") or not geo.get("feature"):
        return {"ok": False, "error": "Could not geocode address"}

    lat = geo["feature"]["lat"]
    lon = geo["feature"]["lon"]
    place_name = geo["feature"]["place_name"] or address
    print(f"AERIAL QUOTE | Geocoded | {lat},{lon}")

    # Step 2 — Download satellite image
    image_bytes = download_satellite_image(lat, lon, zoom)
    if not image_bytes:
        return {"ok": False, "error": "Could not download satellite image"}

    # Step 3 — Upload to Cloudinary
    satellite_url = upload_to_cloudinary(image_bytes, lead_id)

    # Step 4 — Analyze with Claude
    analysis = analyze_aerial_with_claude(
        image_bytes, place_name, job_description, customer_name
    )

    if not analysis.get("ok"):
        return {
            "ok": False,
            "error": analysis.get("error"),
            "satellite_url": satellite_url
        }

    return {
        "ok": True,
        "address": place_name,
        "lat": lat,
        "lon": lon,
        "satellite_url": satellite_url,
        "analysis": analysis.get("analysis"),
        "square_footage": analysis.get("square_footage"),
        "quote_range": analysis.get("quote_range"),
        "quote_min": analysis.get("quote_min"),
        "quote_max": analysis.get("quote_max"),
        "pricing_unit": analysis.get("pricing_unit"),
        
    }
