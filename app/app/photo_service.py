# app/app/photo_service.py
# Handles photo uploads to Cloudinary and Claude Vision analysis

import os
import base64
import cloudinary
import cloudinary.uploader
import requests
import anthropic

# ─────────────────────────────────────────────
# Cloudinary config
# ─────────────────────────────────────────────

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)


# ─────────────────────────────────────────────
# Upload photo to Cloudinary
# ─────────────────────────────────────────────

def upload_photo(file_data: bytes, lead_id: str, photo_index: int) -> dict:
    """
    Upload a single photo to Cloudinary.
    Organizes photos in folders by lead ID.
    Returns {"ok": True, "url": "...", "public_id": "..."} or {"ok": False, "error": "..."}
    """
    try:
        folder = f"contractoros/leads/{lead_id}"
        public_id = f"{folder}/photo_{photo_index}"

        result = cloudinary.uploader.upload(
            file_data,
            public_id=public_id,
            overwrite=True,
            resource_type="image",
            transformation=[
                {"quality": "auto", "fetch_format": "auto"},
                {"width": 1200, "crop": "limit"},  # max 1200px wide
            ],
        )

        print("CLOUDINARY UPLOAD | lead:", lead_id, "| url:", result.get("secure_url"))

        return {
            "ok": True,
            "url": result.get("secure_url"),
            "public_id": result.get("public_id"),
        }

    except Exception as e:
        print("CLOUDINARY UPLOAD ERROR |", str(e))
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────
# Download image and convert to base64
# ─────────────────────────────────────────────

def image_url_to_base64(url: str) -> str | None:
    """
    Download an image from a URL and return base64 encoded string.
    Needed for Claude Vision API.
    """
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return base64.standard_b64encode(response.content).decode("utf-8")
    except Exception as e:
        print("IMAGE DOWNLOAD ERROR |", url, "|", str(e))
        return None


# ─────────────────────────────────────────────
# Claude Vision analysis
# ─────────────────────────────────────────────

def analyze_photos_with_claude(
    photo_urls: list,
    job_description: str,
    contractor_trade: str = "general contractor",
) -> dict:
    """
    Send photos to Claude Vision for job scope analysis.
    Returns {"ok": True, "summary": "...", "estimate_range": "...", "full_analysis": "..."}
    """
    if not photo_urls:
        return {"ok": False, "error": "no_photos"}

    try:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        client = anthropic.Anthropic(api_key=api_key)

        # Build image content blocks for Claude
        content = []

        for i, url in enumerate(photo_urls[:5]):  # max 5 photos
            b64 = image_url_to_base64(url)
            if b64:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    },
                })
                print(f"CLAUDE VISION | Added photo {i+1} of {len(photo_urls)}")

        if not content:
            return {"ok": False, "error": "could_not_load_photos"}

        # Add the analysis prompt
        content.append({
            "type": "text",
            "text": f"""You are an expert {contractor_trade} estimator reviewing job photos.

The customer described the job as: "{job_description}"

Please analyze these photos and provide:

1. SCOPE SUMMARY (2-3 sentences): What do you see in the photos? Describe the condition, size, and scope of the work needed.

2. ESTIMATE RANGE: Based on what you can see, provide a rough dollar range for this type of work. Be conservative and note that this is a visual estimate only.

3. KEY OBSERVATIONS: List 3-5 specific things the contractor should know before the estimate visit (access issues, materials needed, potential complications, etc.)

4. PRIORITY LEVEL: Rate as STANDARD, HIGH_PRIORITY, or URGENT based on what you see.

Format your response exactly like this:
SCOPE SUMMARY:
[your summary]

ESTIMATE RANGE:
[dollar range]

KEY OBSERVATIONS:
- [observation 1]
- [observation 2]
- [observation 3]

PRIORITY LEVEL:
[level]"""
        })

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": content}],
        )

        full_analysis = response.content[0].text.strip()
        print("CLAUDE VISION RESPONSE |", full_analysis[:100], "...")

        # Parse out the sections
        scope_summary = ""
        estimate_range = ""
        priority = "STANDARD"

        lines = full_analysis.split("\n")
        current_section = None

        for line in lines:
            line = line.strip()
            if line.startswith("SCOPE SUMMARY:"):
                current_section = "scope"
            elif line.startswith("ESTIMATE RANGE:"):
                current_section = "estimate"
            elif line.startswith("KEY OBSERVATIONS:"):
                current_section = "observations"
            elif line.startswith("PRIORITY LEVEL:"):
                current_section = "priority"
            elif current_section == "scope" and line:
                scope_summary += line + " "
            elif current_section == "estimate" and line:
                estimate_range = line
            elif current_section == "priority" and line:
                if "URGENT" in line.upper():
                    priority = "URGENT"
                elif "HIGH" in line.upper():
                    priority = "HIGH_PRIORITY"

        return {
            "ok": True,
            "summary": scope_summary.strip(),
            "estimate_range": estimate_range.strip(),
            "priority": priority,
            "full_analysis": full_analysis,
        }

    except Exception as e:
        print("CLAUDE VISION ERROR |", str(e))
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────
# Build photo upload link for SMS
# ─────────────────────────────────────────────

def build_photo_upload_link(lead_id: str, base_url: str) -> str:
    """
    Build the photo upload URL to send to the customer via SMS.
    """
    return f"{base_url.rstrip('/')}/upload-photos/{lead_id}"
