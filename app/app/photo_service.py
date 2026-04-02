# app/app/photo_service.py
# Handles photo uploads to Cloudinary and Claude Vision analysis
# Generates two outputs:
#   1. Internal contractor analysis (detailed notes)
#   2. Customer-ready estimate (forward directly to customer)

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

    # Check file size — compress if too large for Cloudinary free tier
    MAX_SIZE = 9 * 1024 * 1024  # 9MB

    if len(file_data) > MAX_SIZE:
        print(f"FILE TOO LARGE | {len(file_data)} bytes - compressing")
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(file_data))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.thumbnail((1800, 1800), Image.LANCZOS)
            output = io.BytesIO()
            img.save(output, format="JPEG", quality=75, optimize=True)
            file_data = output.getvalue()
            print(f"COMPRESSED | new size: {len(file_data)} bytes")
        except Exception as e:
            print("COMPRESSION ERROR |", e)
            return {"ok": False, "error": "File too large and compression failed"}

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
                {"width": 1200, "crop": "limit"},
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
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return base64.standard_b64encode(response.content).decode("utf-8")
    except Exception as e:
        print("IMAGE DOWNLOAD ERROR |", url, "|", str(e))
        return None


# ─────────────────────────────────────────────
# Claude Vision analysis — dual output
# ─────────────────────────────────────────────

def analyze_photos_with_claude(
    photo_urls: list,
    job_description: str,
    contractor_name: str = "our team",
    client_name: str = "Customer",
    service_address: str = "",
) -> dict:
    """
    Send photos to Claude Vision for job scope analysis.
    Returns two outputs:
      - internal_analysis: detailed notes for contractor
      - customer_estimate: clean forward-ready estimate for customer
    """
    if not photo_urls:
        return {"ok": False, "error": "no_photos"}

    try:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        client = anthropic.Anthropic(api_key=api_key)

        # Build image content blocks
        content = []
        for i, url in enumerate(photo_urls[:5]):
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

        content.append({
            "type": "text",
            "text": f"""You are an expert contractor estimator reviewing job photos.

Customer name: {client_name}
Service address: {service_address}
Job described as: "{job_description}"
Contractor business: {contractor_name}

Analyze these photos and provide TWO separate outputs:

=== CONTRACTOR INTERNAL NOTES ===
(Detailed technical notes for the contractor only)

SCOPE SUMMARY:
[2-3 sentences describing exactly what you see — materials, size, condition, scope]

ESTIMATE RANGE:
[Dollar range based on visible scope]

KEY OBSERVATIONS:
- [Technical observation 1]
- [Technical observation 2]
- [Technical observation 3]
- [Technical observation 4 if needed]
- [Technical observation 5 if needed]

POTENTIAL UPSELLS:
- [Any additional services the contractor could offer based on what you see]

PRIORITY LEVEL:
[STANDARD / HIGH_PRIORITY / URGENT]

=== CUSTOMER ESTIMATE EMAIL ===
(Clean, professional email the contractor can forward directly to the customer)

Subject: Your Estimate — {job_description} at {service_address}

[Write a warm, professional 3-4 paragraph email that includes:
- Thank them for sending photos
- Brief friendly description of what was observed (no technical jargon)
- The estimate range with a note that final price confirmed at visit
- 2-3 bullet points of what to expect during the service
- A closing sentence about confirming the appointment
- Sign off as {contractor_name}

Keep it conversational and reassuring. Do not mention AI or photo analysis.]"""
        })

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": content}],
        )

        full_response = response.content[0].text.strip()
        print("CLAUDE VISION RESPONSE | length:", len(full_response))

        # ── Parse the two sections ──────────────────────────────────────────
        internal_analysis = ""
        customer_estimate = ""
        customer_subject = ""

        if "=== CONTRACTOR INTERNAL NOTES ===" in full_response:
            parts = full_response.split("=== CUSTOMER ESTIMATE EMAIL ===")
            internal_part = parts[0].replace("=== CONTRACTOR INTERNAL NOTES ===", "").strip()
            internal_analysis = internal_part

            if len(parts) > 1:
                customer_part = parts[1].strip()
                # Extract subject line
                lines = customer_part.split("\n")
                for i, line in enumerate(lines):
                    if line.startswith("Subject:"):
                        customer_subject = line.replace("Subject:", "").strip()
                        customer_estimate = "\n".join(lines[i+1:]).strip()
                        break
                if not customer_subject:
                    customer_estimate = customer_part
                    customer_subject = f"Your Estimate — {job_description}"
        else:
            internal_analysis = full_response

        # ── Parse estimate range and priority from internal notes ───────────
        estimate_range = ""
        priority = "STANDARD"

        for line in internal_analysis.split("\n"):
            line = line.strip()
            if line.startswith("$") or ("$" in line and "-" in line and len(line) < 60):
                estimate_range = line
            if "URGENT" in line.upper() and "PRIORITY" in line.upper():
                priority = "URGENT"
            elif "HIGH_PRIORITY" in line.upper() or "HIGH PRIORITY" in line.upper():
                priority = "HIGH_PRIORITY"

        return {
            "ok": True,
            "internal_analysis": internal_analysis,
            "customer_estimate": customer_estimate,
            "customer_subject": customer_subject,
            "estimate_range": estimate_range,
            "priority": priority,
            "full_response": full_response,
        }

    except Exception as e:
        print("CLAUDE VISION ERROR |", str(e))
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────
# Build photo upload link for SMS
# ─────────────────────────────────────────────

def build_photo_upload_link(lead_id: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/upload-photos/{lead_id}"
