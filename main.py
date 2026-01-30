from flask import Flask, request, jsonify, Response
import os
import requests

from twilio.twiml.voice_response import VoiceResponse, Gather

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

app = Flask(__name__)

def airtable_create_record(fields: dict):
    token = os.getenv("AIRTABLE_TOKEN")
    base_id = os.getenv("AIRTABLE_BASE_ID")
    table_name = os.getenv("AIRTABLE_TABLE_NAME")

    if not token or not base_id or not table_name:
        return {"ok": False, "error": "Missing AIRTABLE_TOKEN / AIRTABLE_BASE_ID / AIRTABLE_TABLE_NAME env vars"}

    url = f"https://api.airtable.com/v0/{base_id}/{table_name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"fields": fields}

    r = requests.post(url, headers=headers, json=payload, timeout=20)

    # Airtable returns 200 for success, 4xx for errors
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "airtable_error": r.text}

    return {"ok": True, "status": r.status_code, "data": r.json()}


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
    
def send_intake_summary(state: dict):
    subject = "New MME AI Bot Intake"

    body = (
        "New lead captured by MME AI Bot:\n\n"
        f"Service Address: {state.get('address', '')}\n"
        f"Job Requested: {state.get('job', '')}\n"
        f"Timing Needed: {state.get('timing', '')}\n"
        f"Callback Number: {state.get('callback', '')}\n"
    )
    # Build Airtable payload (SAFE – no forced datetime)
    airtable_fields = {
        "Client Name": state.get("name", ""),
        "Call Back Number": state.get("callback", ""),
        "Service Address": state.get("address", ""),
        "Job Description": state.get("job", ""),
        "Source": "AI Phone Call",
        "Call SID": state.get("call_sid", ""),
        "Appointment Requested": state.get("timing", ""),
        "Lead Status": "New Lead",
}

    # Only include real datetime if it exists and is valid
    appt_datetime = state.get("appointment")
    if appt_datetime and "T" in appt_datetime:
        airtable_fields["Appointment Date and Time"] = appt_datetime

    airtable_result = airtable_create_record(airtable_fields)
    print("Airtable result:", airtable_result)

    send_email(subject, body)
    # Optional: helpful in Render logs
    


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

@app.route("/voice", methods=["POST", "GET"])
def voice():
    vr = VoiceResponse()

    gather = Gather(
        num_digits=1,
        action="/voice-menu",
        method="POST",
        timeout=6
    )
    gather.say(
        "Thanks for calling M M E Lawn Care and More. "
        "If this is an emergency, press 1 to reach Mike now. "
        "To leave details for an estimate, press 2."
    )
    vr.append(gather)

    # If they don’t press anything
    vr.say("No problem. We’ll take your details now.")
    vr.redirect("/voice-intake")
    return Response(str(vr), mimetype="text/xml")

@app.route("/voice-menu", methods=["POST"])
def voice_menu():
    digit = request.form.get("Digits", "")
    vr = VoiceResponse()

    if digit == "1":
        vr.redirect("/voice-emergency")
    else:
        vr.redirect("/voice-intake")

    return Response(str(vr), mimetype="text/xml")


@app.route("/voice-intake", methods=["POST", "GET"])
def voice_intake():
    # Start your existing 4-question flow
    
    call_sid = request.values.get("CallSid", "unknown")
    caller = request.values.get("From", "")

    CALLS[call_sid] = {
        "step": 0,
        "callback": caller
}

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/voice-process?step=0",
        method="POST",
        timeout=6,
        speech_timeout="auto",
    )
    gather.say("First, please say your full name.")
    vr.append(gather)

    vr.say("Sorry, I didn’t catch that. Please call back and try again. Goodbye.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")

@app.route("/voice-emergency", methods=["POST", "GET"])
def voice_emergency():
    vr = VoiceResponse()

    vr.say("Okay. Connecting you now.")
    dial = vr.dial(timeout=20, callerId=request.form.get("To", None))
    dial.number("+17632132731")  # Mike’s business phone

    # If no answer/busy, go to voicemail recording
    vr.say("Sorry we missed you. Please leave your name, service address, and what you need help with after the beep.")
    vr.record(
        maxLength=120,
        playBeep=True,
        action="/twilio/voicemail",
        method="POST"
    )
    vr.say("Thank you. Goodbye.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")



@app.route("/voice-process", methods=["POST"])
def voice_process():
    call_sid = request.values.get("CallSid", "unknown")
    step = int(request.args.get("step", "0"))
    digits = (request.values.get("Digits") or "").strip()
    speech = (request.values.get("SpeechResult") or "").strip()

    state = CALLS.get(call_sid, {})
    vr = VoiceResponse()

    # STEP 0: Client name
    if step == 0:
        if not speech:
            gather = Gather(
                input="speech",
                action="/voice-process?step=0",
                method="POST",
                timeout=6,
                speech_timeout="auto",
            )
            gather.say(
                "Please say your full name now.",
                voice="Polly.Joanna",
                language="en-US"
)
            return Response(str(vr), mimetype="text/xml")

        
            state["name"] = speech
            CALLS[call_sid] = state

            gather = Gather(
                input="speech",
                action="/voice-process?step=1",
                method="POST",
                timeout=6,
                speech_timeout="auto",
            )
            gather.say(
                "Thanks. Please say the service address now.",
                voice="Polly.Joanna",
                language="en-US"
            )

            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")
    
    # STEP 1: Service address
    if step == 1:
        # If speech is blank -> reprompt and stay on step 1
        if not speech:
            gather = Gather(
                input="speech",
                action="/voice-process?step=1",
                method="POST",
                timeout=6,
                speech_timeout="auto",
            )
            gather.say("Sorry, I didn’t catch the service address. Please say the service address now.")
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # Speech exists -> save it and move to step 2
        state["address"] = speech
        CALLS[call_sid] = state

        gather = Gather(
            input="speech",
            action="/voice-process?step=2",
            method="POST",
            timeout=6,
            speech_timeout="auto",
        )
        gather.say("Thanks. Now briefly tell me what you need help with.")
        vr.append(gather)
        return Response(str(vr), mimetype="text/xml")

    # STEP 2A: Capture job description, then confirm
    if step == 2 and not digits:
        # Save spoken job description
        state["job_temp"] = speech
        CALLS[call_sid] = state

        gather = Gather(
            input="dtmf",
            num_digits=1,
            action="/voice-process?step=2",
            method="POST",
            timeout=6,
        )
        gather.say(
            f"I heard {speech}. "
            "Press 1 to confirm. "
            "Press 2 to say it again."
        )
        vr.append(gather)
        return Response(str(vr), mimetype="text/xml")


    # STEP 2B: Handle confirmation
    if step == 2 and digits == "1":
        # Confirm job description
        state["job"] = state.get("job_temp", "")
        CALLS[call_sid] = state

        gather = Gather(
            input="speech",
            action="/voice-process?step=3",
            method="POST",
            timeout=6,
            speech_timeout="auto",
        )
        gather.say(
            "Got it. When do you need this done? "
            "You can say today, tomorrow, or a specific date."
        )
        vr.append(gather)
        return Response(str(vr), mimetype="text/xml")


    if step == 2 and digits == "2":
        # Re-ask job description
        gather = Gather(
            input="speech",
            action="/voice-process?step=2",
            method="POST",
            timeout=6,
            speech_timeout="auto",
        )
        gather.say("No problem. Please say the job description again.")
        vr.append(gather)
        return Response(str(vr), mimetype="text/xml")

    # STEP 3: Timing
    if step == 3:
        state["timing"] = speech
        CALLS[call_sid] = state

        gather = Gather(
            input="speech",
            action="/voice-process?step=4",
            method="POST",
            timeout=6,
            speech_timeout="auto",
        )
        gather.say("Last question. What is the best callback phone number?")
        vr.append(gather)
        return Response(str(vr), mimetype="text/xml")

    # STEP 4: Callback + finish
    if step == 4:
        # If Twilio didn’t capture speech, re-ask Step 4
        if not speech:
            gather = Gather(
                input="speech",
                action="/voice-process?step=4",
                method="POST",
                timeout=6,
                speech_timeout="auto",
            )
            gather.say("Sorry, I didn’t catch that. Please say your callback number now.")
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # Save callback and finish
        state["callback"] = speech
        CALLS[call_sid] = state

        try:
            send_intake_summary(state)
        except Exception as e:
            print("send_intake_summary failed:", e)

        vr.say("Thank you. We received your request and will follow up shortly.")
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
