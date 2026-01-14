from flask import Flask, request, jsonify
import os
import re

app = Flask(__name__)

@app.get("/")
def home():
    return jsonify({"status": "running", "message": "MME AI bot is live"})

# ---------- Helpers ----------
def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())

def normalize_service(service_input: str) -> str:
    s = norm(service_input)
    for canonical, aliases in SERVICE_ALIASES.items():
        if s == canonical or s in aliases:
            return canonical
    return "other"

def normalize_size(size_input: str) -> str:
    s = norm(size_input)
    if s in ("small", "minor", "s"):
        return "small"
    if s in ("medium", "m"):
        return "medium"
    if s in ("large", "big", "l"):
        return "large"
    return ""

def pick_range(service_key: str, size_key: str) -> str:
    tiers = PRICING.get(service_key)
    if isinstance(tiers, dict):
        return tiers.get(size_key, tiers.get("default"))
    return tiers or "$100–$300"

# ---------- Phase 1 Services ----------
SERVICE_ALIASES = {
    "lawn": [
        "lawn", "lawn mowing", "mowing", "grass cut", "yard cut"
    ],
    "mulch": [
        "mulch", "mulching", "mulch install"
    ],
    "drywall repair": [
        "drywall patch", "hole in wall", "wall hole", "sheetrock repair"
    ],
    "door lock replace": [
        "replace lock", "change lock", "deadbolt replace", "install lock"
    ],
    "faucet replace": [
        "replace faucet", "install faucet", "kitchen faucet", "bath faucet"
    ],
    "toilet unclog": [
        "unclog toilet", "clogged toilet", "toilet clog"
    ],
    "toilet repair": [
        "running toilet", "toilet leaking", "fix toilet"
    ],
    "light fixture replace": [
        "replace light fixture", "install light fixture", "ceiling light"
    ],
    "outlet switch replace": [
        "replace outlet", "replace switch", "outlet not working"
    ],
    "tv mount": [
        "mount tv", "tv mounting", "hang tv"
    ],
}

# ---------- Pricing (Ranges Only) ----------
PRICING = {
    "lawn": "$60–$150",
    "mulch": "$300–$900",

    "drywall repair": {
        "small": "$150–$300",
        "medium": "$300–$650",
        "large": "$650–$1,400",
        "default": "$150–$1,400",
    },

    "door lock replace": "$175–$450",
    "faucet replace": "$200–$650",
    "toilet unclog": "$125–$225",
    "toilet repair": "$150–$350",
    "light fixture replace": "$175–$550",
    "outlet switch replace": "$125–$450",
    "tv mount": "$150–$450",
}

@app.post("/estimate")
def estimate():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json() or {}
    service_input = data.get("service", "")
    size_input = data.get("size", "")
    details = data.get("details", "")

    service_key = normalize_service(service_input)
    size_key = normalize_size(size_input)

    price_range = pick_range(service_key, size_key)

    return jsonify({
        "service_requested": service_input,
        "service_matched": service_key,
        "size": size_key or "unspecified",
        "estimated_range": price_range,
        "notes_received": details,
        "disclaimer": "Rough estimate only. Final pricing depends on site conditions, scope, and materials."
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)