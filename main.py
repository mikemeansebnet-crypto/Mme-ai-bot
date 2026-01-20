from flask import Flask, request, jsonify, Response
import os
import re

from twilio.twiml.voice_response import VoiceResponse, Gather


from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

def send_email(subject: str, body: str):
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("FROM_EMAIL")
    to_email = os.environ.get("TO_EMAIL")

    if not api_key:
        raise Exception("Missing SENDGRID_API_KEY env var")
    if not from_email:
        raise Exception("Missing FROM_EMAIL env var")
    if not to_email:
        raise Exception("Missing TO_EMAIL env var")

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        plain_text_content=body
    )

    sg = SendGridAPIClient(api_key)
    response = sg.send(message)

    # Optional: helpful in Render logs
    print("SendGrid status:", response.status_code)

app = Flask(__name__)

@app.get("/test-email")
def test_email():
    try:
        send_email(
            "MME AI Bot Test",
            "If you got this, SendGrid is working ✅"
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500
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

@app.route("/voice", methods=["POST"])
def voice():
    call_sid = request.values.get("CallSid", "unknown")
    from_number = request.values.get("From", "unknown")
    to_number = request.values.get("To", "unknown")

    CALLS[call_sid] = {"step": 1}  # reset for new call

    # ✅ NEW: Email you immediately that a call started
    try:
        send_email(
            "New call started (MME AI Bot)",
            f"Incoming call\nFrom: {from_number}\nTo: {to_number}\nCallSid: {call_sid}\n\nThe bot is starting intake now."
        )
    except Exception as e:
        print("Email notify failed:", e)

    vr = VoiceResponse()
    
    gather = Gather(
    input="speech",
    action="/voice-process?step=1",
    method="POST",
    timeout=6,
    speech_timeout="auto",
    play_beep=True
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
    else:
        # FINAL STEP: we have all answers now
        email_body = f"""
    New phone intake received:

    Address: {state.get('address','')}
    Job: {state.get('job','')}
    Timing: {state.get('timing','')}
    Callback: {state.get('callback','')}
    CallSid: {call_sid}
    """

    try:
        send_email(
            subject="📞 New Call Intake — MME AI Bot",
            body=email_body
        )
    except Exception as e:
        print("EMAIL FAILED:", str(e))

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
