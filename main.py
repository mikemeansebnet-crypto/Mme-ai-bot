from flask import Flask, request, jsonify, Response 
import os
import requests

from twilio.twiml.voice_response import VoiceResponse, Gather

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

app = Flask(__name__)

import json
import time
import redis

REDIS_URL = os.getenv("REDIS_URL")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "mmeai:call:")
REDIS_TTL_SECONDS = int(os.getenv("REDIS_TTL_SECONDS", "7200"))

redis_client = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None

def _redis_key(call_sid: str) -> str:
    return f"{REDIS_PREFIX}{call_sid}"

def get_state(call_sid: str) -> dict:
    if not redis_client or not call_sid:
        return {}
    raw = redis_client.get(_redis_key(call_sid))
    return json.loads(raw) if raw else {}

def set_state(call_sid: str, state: dict) -> None:
    if not redis_client or not call_sid:
        return
    redis_client.setex(_redis_key(call_sid), REDIS_TTL_SECONDS, json.dumps(state))

def clear_state(call_sid: str) -> None:
    if redis_client and call_sid:
        redis_client.delete(_redis_key(call_sid))

# ================= Alias Helpers =================

def alias_key(new_call_sid: str) -> str:
    return f"mmea:alias:{new_call_sid}"

def set_call_alias(new_call_sid: str, old_call_sid: str, ttl_seconds: int = 900):
    if not redis_client or not new_call_sid or not old_call_sid:
        return
    redis_client.setex(alias_key(new_call_sid), ttl_seconds, old_call_sid)

def get_call_alias(new_call_sid: str):
    if not redis_client or not new_call_sid:
        return None
    v = redis_client.get(alias_key(new_call_sid))
    return v if v else None

def clear_call_alias(new_call_sid: str):
    if redis_client and new_call_sid:
        redis_client.delete(alias_key(new_call_sid))

def contractor_calls_key(contractor_key: str) -> str:
    return f"mmeai:contractor:{contractor_key}:calls"

def register_live_call(contractor_key: str, call_sid: str) -> None:
    """
    Track that this call is active for this contractor.
    Uses a Redis SET to avoid duplicates.
    """
    if not redis_client or not contractor_key or not call_sid:
        return
    k = contractor_calls_key(contractor_key)
    redis_client.sadd(k, call_sid)
    # Keep the contractor live-call set from lasting forever
    redis_client.expire(k, REDIS_TTL_SECONDS)

def unregister_live_call(contractor_key: str, call_sid: str) -> None:
    if not redis_client or not contractor_key or not call_sid:
        return
    k = contractor_calls_key(contractor_key)
    redis_client.srem(k, call_sid)

# ---------------- Resume Helpers ----------------

def resume_key(to_number: str, from_number: str) -> str:
    return f"mmeai:resume:{to_number}:{from_number}"

def save_resume_pointer(to_number: str, from_number: str, call_sid: str, ttl_seconds: int = 600):
    if not redis_client or not to_number or not from_number or not call_sid:
        return
    redis_client.setex(resume_key(to_number, from_number), ttl_seconds, call_sid)

def get_resume_pointer(to_number: str, from_number: str):
    if not redis_client:
        return None
    value = redis_client.get(resume_key(to_number, from_number))
    return value if value else None

def clear_resume_pointer(to_number: str, from_number: str):
    if not redis_client:
        return
    redis_client.delete(resume_key(to_number, from_number))

def list_live_calls(contractor_key: str) -> list[str]:
    """
    Returns list of CallSids currently registered as live for this contractor.
    """
    if not redis_client or not contractor_key:
        return []
    k = contractor_calls_key(contractor_key)
    return list(redis_client.smembers(k))

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
    
def get_contractor_by_twilio_number(to_number: str) -> dict:
    token = os.getenv("AIRTABLE_TOKEN")
    base_id = os.getenv("AIRTABLE_BASE_ID")
    contractors_table = os.getenv("AIRTABLE_CONTRACTORS_TABLE", "Contractors")

    if not token or not base_id:
        return {}

    if not to_number:
        return {}

    url = f"https://api.airtable.com/v0/{base_id}/{contractors_table}"
    headers = {"Authorization": f"Bearer {token}"}

    # Airtable formula: match Twilio Number AND Active is true
    formula = f"AND({{Twilio Number}}='{to_number}', {{Active}}=TRUE())"
    params = {"filterByFormula": formula, "maxRecords": 1}

    r = requests.get(url, headers=headers, params=params, timeout=20)
    if r.status_code >= 400:
        print("Contractor lookup error:", r.status_code, r.text)
        return {}

    data = r.json()
    records = data.get("records", [])
    if not records:
        return {}

    return records[0].get("fields", {})

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
        f"Client Name: {state.get('name', '')}\n"
        f"Service Address: {state.get('service_address', '')}\n"
        f"Job Requested: {state.get('job_description', '')}\n"
        f"Timing Needed: {state.get('timing', '')}\n"
        f"Callback Number: {state.get('callback', '')}\n"
        f"Call SID: {state.get('call_sid', '')}\n"
    )

    
    # Build Airtable payload (SAFE – no forced datetime)
    airtable_fields = {
        "Client Name": state.get("name", ""),
        "Call Back Number": state.get("callback", ""),
        "Service Address": state.get("service_address", ""),
        "Job Description": state.get("job_description", ""),
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

    to_number = request.values.get("To", "")
    contractor = get_contractor_by_twilio_number(to_number)
    business_name = contractor.get("Business Name", "our office")

    gather = Gather(
        num_digits=1,
        action="/voice-menu",
        method="POST",
        timeout=6
    )
    gather.say(
        f"Thanks for calling {business_name}. "
        "If this is an emergency, press 1. "
        "To leave details for an estimate, press 2.",
        voice="Polly.Joanna",
        language="en-US"
    )

    vr.append(gather)

    vr.say(
        "No problem. We’ll take your details now.",
        voice="Polly.Joanna",
        language="en-US"
    )
    vr.redirect("/voice-intake")
    return Response(str(vr), mimetype="text/xml")


@app.route("/voice-menu", methods=["POST", "GET"])
def voice_menu():
    digit = (request.values.get("Digits") or "").strip()
    vr = VoiceResponse()

    if digit == "1":
        vr.redirect("/voice-emergency", method="POST")
    else:
        vr.redirect("/voice-intake", method="POST")

    return Response(str(vr), mimetype="text/xml")


@app.route("/twilio/voicemail", methods=["POST"])
def twilio_voicemail():
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    recording_url = request.values.get("RecordingUrl", "")
    recording_duration = request.values.get("RecordingDuration", "")

    print("📞 Voicemail received:", call_sid, from_number, recording_url, recording_duration)

    # OPTIONAL: save to Airtable (add fields that exist in your Airtable table)
    try:
        airtable_create_record({
            "Source": "Voicemail",
            "Call SID": call_sid,
            "Call Back Number": from_number,
            "Job Description": f"VOICEMAIL: {recording_url} ({recording_duration}s)",
            "Lead Status": "New Lead",
        })
    except Exception as e:
        print("Airtable voicemail save failed:", e)

    vr = VoiceResponse()
    vr.say("Thank you. Your message has been recorded. Goodbye.", voice="Polly.Joanna", language="en-US")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")

@app.route("/voice-intake", methods=["POST", "GET"])
def voice_intake():
    # Start your existing 4-question flow
    
    call_sid = request.values.get("CallSid", "unknown")
    caller = request.values.get("From", "")

    to_number = request.values.get("To", "")  # Twilio number called
    contractor_key = to_number or "unknown"

    state = {
        "step": 0,
        "callback": caller,
        "retries": 0,
        "name": "",
        "service_address": "",
        "job_description": "",
        "timing": "",
        "call_sid": call_sid,
        "to_number": to_number,
        "contractor_key": contractor_key,
        "started_at": int(time.time()),
    }

    set_state(call_sid, state)
    register_live_call(contractor_key, call_sid)

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

    to_number = request.values.get("To", "")
    contractor = get_contractor_by_twilio_number(to_number)
    emergency_phone = contractor.get("Emergency Phone")
    
    print("DEBUG To number:", to_number)
    print("DEBUG contractor:", contractor)
    print("DEBUG emergency_phone:", emergency_phone)
    
    if emergency_phone:
        vr.say(
            "Okay. Connecting you now.",
            voice="Polly.Joanna",
            language="en-US"
        )

        dial = vr.dial(
            timeout=20,
            callerId=to_number
        )
        dial.number(emergency_phone)

        # IMPORTANT: return immediately after dial
        return Response(str(vr), mimetype="text/xml")

    # ---- FALLBACK ONLY IF NO EMERGENCY PHONE ----
    vr.say(
        "We're unable to connect you right now. "
        "Please leave your name, address, and details after the beep.",
        voice="Polly.Joanna",
        language="en-US"
    )

    vr.record(
        maxLength=120,
        playBeep=True,
        action="/twilio/voicemail",
        method="POST"
    )

    vr.hangup()
    return Response(str(vr), mimetype="text/xml")



@app.route("/voice-process", methods=["POST"])
def voice_process():
    call_sid = request.values.get("CallSid", "unknown")
    step = int(request.args.get("step", "0"))
    digits = (request.values.get("Digits") or "").strip()
   
    speech = (
    
        request.values.get("SpeechResult") 
        or request.values.get("UnstableSpeechResult")
        or ""
    ).strip()

    to_number = (request.values.get("To") or "").strip()
    from_number = (request.values.get("From") or "").strip()
    

    # -------- Resume / Alias logic (caller hung up and called back) --------

    # Keep the NEW CallSid so we can map it to the OLD one
    new_call_sid = call_sid

    # If this new CallSid was already aliased earlier, follow it
    aliased = get_call_alias(new_call_sid)
    if aliased:
        call_sid = aliased

    # If we are at step 0, try to resume by mapping this new CallSid -> old CallSid
    if step == 0 and redis_client and to_number and from_number:
        old_call_sid = get_resume_pointer(to_number, from_number)
        if old_call_sid and old_call_sid != call_sid:
            set_call_alias(new_call_sid, old_call_sid)   # NEW -> OLD mapping
            call_sid = old_call_sid

    # Always refresh the resume pointer so it stays alive while caller is interacting
    if redis_client and to_number and from_number and call_sid:
        save_resume_pointer(to_number, from_number, call_sid)
        print("RESUME PTR SAVED:", to_number, from_number, call_sid)

    
    print("DEBUG SpeechResult:", request.values.get("SpeechResult"))
    print("DEBUG UnstableSpeechResult:", request.values.get("UnstableSpeechResult"))
    
    state = get_state(call_sid)

    # -------- Restore step on callback by checking which fields are already filled --------
    def _resume_step_from_fields(s: dict) -> int:
        name_ok = bool((s.get("name") or "").strip())
        addr_ok = bool((s.get("service_address") or "").strip())
        job_ok  = bool((s.get("job_description") or "").strip())
        time_ok = bool((s.get("timing") or "").strip())

        if not name_ok:
            return 0
        if not addr_ok:
            return 1
        if not job_ok:
            return 2
        if not time_ok:
            return 3
        return 4

    # If Twilio hits us with step=0 again on a callback, jump to the correct step
    if step == 0:
        inferred_step = _resume_step_from_fields(state)
        if inferred_step > 0:
            print("RESUME STEP INFERRED:", inferred_step, "| from keys:", list(state.keys()))
            step = inferred_step
            state["step"] = inferred_step
            set_state(call_sid, state)
    # -------------------------------------------------------------------------------

    print("DEBUG resume check | request step:", step, "| call_sid:", call_sid, "| state.step:", state.get("step"))
    print("DEBUG state keys:", list(state.keys()))

    

    if step == 0 and saved_step > 0:
        print("RESUME STEP OVERRIDE:", saved_step)
        step = saved_step
# ------------------------------------------------------------------

   

    # Always store CallSid
    state["call_sid"] = call_sid

    

    # Safe defaults (define keys first)
    state.setdefault("retries", 0)
    state.setdefault("name", "")
    state.setdefault("service_address", "")
    state.setdefault("job_description", "")
    state.setdefault("timing", "")
    state.setdefault("callback", "")

    # Always capture caller phone number (do not overwrite if already set)
    state["callback"] = state["callback"] or request.values.get("From", "")

    # Save back immediately
    set_state(call_sid, state)

    vr = VoiceResponse()

# Make sure these exist earlier in your handler:
# speech = (request.values.get("SpeechResult") or "").strip()
# digits = (request.values.get("Digits") or "").strip()
# state = CALLS.get(call_sid, {"retries": 0})
# vr = VoiceResponse()

    # STEP 0: Client name
    if step == 0:
        if not speech:
            gather = Gather(
                input="speech",
                action="/voice-process?step=0",
                method="POST",
                timeout=8,
                speech_timeout="auto",
                hints="name full-name first-name last-name",
            )
            gather.say(
                "Please say your full name now.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # Speech EXISTS → save name and move to step 1
        state["name"] = speech
        state["step"] = 1
        state["retries"] = 0
        set_state(call_sid, state)

        gather = Gather(
            input="speech",
            action="/voice-process?step=1",
            method="POST",
            timeout=8,
            speech_timeout="auto",
        )
        gather.say(
            "Thanks. Please say the service address now.",
            voice="Polly.Joanna",
            language="en-US",
        )
        vr.append(gather)
        return Response(str(vr), mimetype="text/xml")


    # STEP 1: Service address
    if step == 1:
        if not speech:
            state["retries"] = state.get("retries", 0) + 1
            set_state(call_sid, state)

            if state["retries"] >= 2:
                vr.say(
                    "Sorry, I'm having trouble hearing you. We'll follow up shortly.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.hangup()
                return Response(str(vr), mimetype="text/xml")

            gather = Gather(
                input="speech",
                action="/voice-process?step=1",
                method="POST",
                timeout=8,
                speech_timeout="auto",
            )
            gather.say(
                "Please say the service address now.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # Speech EXISTS → save and move to step 2
        state["service_address"] = speech
        state["step"] = 2
        state["retries"] = 0
        set_state(call_sid, state)

        gather = Gather(
            input="speech",
            action="/voice-process?step=2",
            method="POST",
            timeout=8,
            speech_timeout="auto",
        )
        gather.say(
            "What service do you need today?",
            voice="Polly.Joanna",
            language="en-US",
        )
        vr.append(gather)
        return Response(str(vr), mimetype="text/xml")


    # STEP 2: Job description + confirm/repeat
    if step == 2:
        # If we don't have a job description yet, ask for it
        if not state.get("job_description"):
            if not speech:
                gather = Gather(
                    input="speech",
                    action="/voice-process?step=2",
                    method="POST",
                    timeout=8,
                    speech_timeout="auto",
                )
                gather.say(
                    "Please briefly describe the service you need.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            # Speech exists → save it
            state["job_description"] = speech.strip()
            state["step"] = 3
            set_state(call_sid, state)

            # Now ask for confirm via DTMF
            gather = Gather(
                input="dtmf",
                num_digits=1,
                action="/voice-process?step=2",
                method="POST",
                timeout=6,
            )
            gather.say(
                f"I heard: {state['job_description']}. Press 1 to confirm, or press 2 to repeat.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # We already have a job description → we are waiting on digits
        if not digits:
            gather = Gather(
                input="dtmf",
                num_digits=1,
                action="/voice-process?step=2",
                method="POST",
                timeout=6,
            )
            gather.say(
                "Press 1 to confirm, or press 2 to repeat.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        if digits == "2":
            state.pop("job_description", None)
            set_state(call_sid, state)

            vr.redirect("/voice-process?step=2", method="POST")
            return Response(str(vr), mimetype="text/xml")

        if digits == "1":
            state["step"] = 3
            state["retries"] = 0
            set_state(call_sid, state)

            vr.redirect("/voice-process?step=3", method="POST")
            return Response(str(vr), mimetype="text/xml")

        # Any other key → reprompt
        gather = Gather(
            input="dtmf",
            num_digits=1,
            action="/voice-process?step=2",
            method="POST",
            timeout=6,
        )
        gather.say(
            "Please press 1 to confirm, or press 2 to repeat.",
            voice="Polly.Joanna",
            language="en-US",
        )
        vr.append(gather)
        return Response(str(vr), mimetype="text/xml")


    # STEP 3: Timing
    if step == 3:
        if not speech:
            state["retries"] = state.get("retries", 0) + 1
            set_state(call_sid, state)

            if state["retries"] >= 2:
                vr.say(
                    "Sorry, I'm having trouble hearing you. We'll follow up shortly.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.hangup()
                return Response(str(vr), mimetype="text/xml")

            gather = Gather(
                input="speech",
                action="/voice-process?step=4",
                method="POST",
                timeout=8,
                speech_timeout="auto",
            )
            gather.say(
                "Please tell me when you need the service.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        state["timing"] = speech
        state["retries"] = 0
        state["step"] = 4
        set_state(call_sid, state)

        gather = Gather(
            input="speech",
            action="/voice-process?step=4",
            method="POST",
            timeout=8,
            speech_timeout="auto",
        )
        gather.say(
            "What is the best callback phone number?",
            voice="Polly.Joanna",
            language="en-US",
        )
        vr.append(gather)
        return Response(str(vr), mimetype="text/xml")


    # STEP 4: Callback number
    if step == 4:
        # If digits were entered, use them
        if digits and len(digits) >= 7:
            state["callback"] = digits
        else:
            # Fallback to caller ID
            state["callback"] = state.get("callback") or request.values.get("From", "")

        set_state(call_sid, state)

        try:
            send_intake_summary(state)
        except Exception as e:
            print("send_intake_summary failed:", e)

        unregister_live_call(state.get("contractor_key", "unknown"), call_sid)
        clear_state(call_sid)


        

        vr.say(
            "Thank you. We received your request and will follow up shortly.",
            voice="Polly.Joanna",
            language="en-US"
        )
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
