from flask import Flask, request, jsonify, Response
import os
import re

from twilio.twiml.voice_response import VoiceResponse, Gather
import smtplib

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def send_email(subject: str, body: str):
    host = os.environ.get("EMAIL_HOST")
    port = int(os.environ.get("EMAIL_PORT", 587))
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = user
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain"))

    server = smtplib.SMTP_SSL(host, 465, timeout=10)
    server.login(user, password)
    server.send_message(msg)
    server.quit()
app = Flask(__name__)

# ------------------------------
# Simple in-memory call storage
# (Phase 2: move to DB/Redis)
# ------------------------------
CALLS = {}

@app.get("/")
def home():
    return jsonify({"status": "running", "message": "MME AI bot is live"})

# ------------------------------
# SMS (keep this even if A2P pending)
# ------------------------------
@app.route("/sms", methods=["POST"])
def sms():
    incoming_msg = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "")

    print(f"📩 SMS from {from_number}: {incoming_msg}")

    reply = "✅ MME AI Bot is live! We received your message."
    return Response(f"<Response><Message>{reply}</Message></Response>", mimetype="text/xml")

# ------------------------------
# VOICE: 4-question intake
# ------------------------------
@app.route("/voice", methods=["POST", "GET"])
def voice():
    call_sid = request.values.get("CallSid", "unknown")
    CALLS[call_sid] = {"step": 1}  # reset for new call

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/voice-process?step=1",
        method="POST",
        timeout=6,
        speech_timeout="auto",
    )
    gather.say("Thanks for calling M M E Lawn Care and More.")
    gather.say("First, please say the service address after the beep.")
    vr.append(gather)

    vr.say("Sorry, I didn't catch that. Please call back and try again. Goodbye.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


@app.route("/voice-process", methods=["POST"])
def voice_process():
    call_sid = request.values.get("CallSid", "unknown")
    step = int(request.args.get("step", "1"))
    speech = request.values.get("SpeechResult", "").strip()

    state = CALLS.get(call_sid, {})
    state["step"] = step

    # Save answer by step
    if step == 1:
        state["address"] = speech
        next_step = 2
        prompt = "Thanks. Now briefly tell me what you need help with after the beep."
    elif step == 2:
        state["job"] = speech
        next_step = 3
        prompt = "Got it. When do you need this done? You can say today, tomorrow, or a date."
    elif step == 3:
        state["timing"] = speech
        next_step = 4
        prompt = "Last question. What is the best callback phone number?"
    else:
        state["callback"] = speech
        next_step = 5

    CALLS[call_sid] = state

    vr = VoiceResponse()

    # If we still have questions to ask, gather again
    if next_step <= 4:
        gather = Gather(
            input="speech",
            action=f"/voice-process?step={next_step}",
            method="POST",
            timeout=6,
            speech_timeout="auto",
        )
        gather.say(prompt)
        vr.append(gather)

        vr.say("Sorry, I didn't catch that. Please call back and try again. Goodbye.")
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    # Done: confirm + log
    address = state.get("address", "")
    job = state.get("job", "")
    timing = state.get("timing", "")
    callback = state.get("callback", "")

    print("📞 NEW VOICE INTAKE:")
    print(f"  CallSid: {call_sid}")
    print(f"  Address: {address}")
    print(f"  Job: {job}")
    print(f"  Timing: {timing}")
    print(f"  Callback: {callback}")
    email_body = f"""
    New phone intake received:

    Address:
    {address}

    Job:
    {job}

    Timing:
    {timing}

    Callback:
    {callback}

    CallSid:
    {call_sid}
    """

    send_email(
    subject="📞 New Call Intake – MME AI Bot",
    body=email_body
)
    vr.say("Thanks. I recorded your request.")
    vr.say("We will follow up shortly. Goodbye.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


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
