# CHECKPOINT: state.py integrated and production verified before removing duplicates

import os
import requests
import json
import time
import redis
import re
from flask import Flask, request, jsonify, Response 
from twilio.twiml.voice_response import VoiceResponse, Gather
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from app.app.state import (
    get_state, set_state, clear_state,
    set_call_alias, get_call_alias, clear_call_alias,
    save_resume_pointer, get_resume_pointer, clear_resume_pointer,
    register_live_call, unregister_live_call, list_live_calls
)




app = Flask(__name__)


# Gather all environment variables 
REDIS_URL = os.getenv("REDIS_URL")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "mmeai:call:")
REDIS_TTL_SECONDS = int(os.getenv("REDIS_TTL_SECONDS", "7200"))
airtable_token = os.getenv("AIRTABLE_TOKEN")
airtable_base_id = os.getenv("AIRTABLE_BASE_ID")
air_table_name = os.getenv("AIRTABLE_TABLE_NAME")
email_api_key = os.environ.get("SENDGRID_API_KEY")
from_email = os.environ.get("FROM_EMAIL")
to_email = os.environ.get("TO_EMAIL")



redis_client = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None

def _redis_key(call_sid: str) -> str:
    return f"{REDIS_PREFIX}{call_sid}"

def get_state(call_sid: str) -> dict:
    if not redis_client or not call_sid:
        return {}
    raw = redis_client.get(_redis_key(call_sid))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as e:
        print("Bad state JSON in Redis for", call_sid, "err:", e)
        return {}

def set_state(call_sid: str, state: dict) -> None:
    if not redis_client or not call_sid:
        return
    redis_client.setex(_redis_key(call_sid), REDIS_TTL_SECONDS, json.dumps(state))

def clear_state(call_sid: str) -> None:
    if redis_client and call_sid:
        redis_client.delete(_redis_key(call_sid))

# ================= Alias Helpers =================

def alias_key(new_call_sid: str) -> str:
    return f"mmeai:alias:{new_call_sid}"

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
    if not value:
        return None
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else value

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
    

    if not airtable_token or not airtable_base_id or not air_table_name:
        return {"ok": False, "error": "Missing AIRTABLE_TOKEN / AIRTABLE_BASE_ID / AIRTABLE_TABLE_NAME env vars"}

    url = f"https://api.airtable.com/v0/{airtable_base_id}/{air_table_name}"
    headers = {
        "Authorization": f"Bearer {airtable_token}",
        "Content-Type": "application/json",
    }
    payload = {"fields": fields}

    r = requests.post(url, headers=headers, json=payload, timeout=20)

    # Airtable returns 200 for success, 4xx for errors
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "airtable_error": r.text}

    return {"ok": True, "status": r.status_code, "data": r.json()}
    
def get_contractor_by_twilio_number(to_number: str) -> dict:
    if not to_number:
        return {}

    # 1. Define a unique Cache Key
    # Using a specific prefix like 'mmeai:contractor_cache:' is a Redis best practice
    cache_key = f"mmeai:contractor_cache:{to_number}"
    """
    Lookup contractor config by the Twilio number (To).
    Uses Redis cache to reduce Airtable calls and speed up call flow.
    """
    if not to_number:
        return {}

    # Redis cache key for this Twilio number
    cache_key = f"mmeai:contractor_cache:{to_number}"

    # 1) Try Redis first
    if redis_client:
        cached_raw = redis_client.get(cache_key)
        if cached_raw:
            try:
                return json.loads(cached_raw)
            except Exception as e:
                print("Bad contractor cache JSON; ignoring cache:", e)

    # 2) Fall back to Airtable
    token = os.getenv("AIRTABLE_TOKEN")
    base_id = os.getenv("AIRTABLE_BASE_ID")
    contractors_table = os.getenv("AIRTABLE_CONTRACTORS_TABLE", "Contractors")

    if not token or not base_id:
        return {}

    url = f"https://api.airtable.com/v0/{base_id}/{contractors_table}"
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Attempt to fetch from Redis first
    if redis_client:
        cached_raw = redis_client.get(cache_key)
        if cached_raw:
            print(f"Redis Cache Hit for {to_number}")
            return json.loads(cached_raw)

    # 3. If not found in Redis, proceed to Airtable
    print(f"Redis Cache Miss. Fetching {to_number} from Airtable...")
    
    contractors_table = os.getenv("AIRTABLE_CONTRACTORS_TABLE", "Contractors")

    if not airtable_token or not airtable_base_id:
        return {}

    url = f"https://api.airtable.com/v0/{airtable_base_id}/{contractors_table}"
    headers = {"Authorization": f"Bearer {airtable_token}"}
    formula = f"AND({{Twilio Number}}='{to_number}', {{Active}}=TRUE())"
    params = {"filterByFormula": formula, "maxRecords": 1}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        records = data.get("records", [])
        
        if not records:
            return {}

        contractor_fields = records[0].get("fields", {})

        # 4. Store the result in Redis for future calls
        # We set an expiration (TTL) so if business info changes in Airtable, 
        # Redis will eventually refresh. 3600 seconds = 1 hour.
        if redis_client and contractor_fields:
            redis_client.setex(cache_key, 3600, json.dumps(contractor_fields))
            print(f"Cached contractor data for {to_number}")

        return contractor_fields

    except Exception as e:
        print(f"Contractor lookup error: {e}")
        if r.status_code >= 400:
            print("Contractor lookup error:", r.status_code, r.text)
            return {}

        records = r.json().get("records", [])
        if not records:
            return {}

        contractor_fields = records[0].get("fields", {}) or {}

        # 3) Cache for 1 hour
        if redis_client and contractor_fields:
            redis_client.setex(cache_key, 3600, json.dumps(contractor_fields))

        return contractor_fields

    except Exception as e:
        print("Contractor lookup exception:", e)
        return {}

def send_email(subject: str, body: str):
    # Pull fresh every time (prevents refactor breakage)
    api_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("FROM_EMAIL")
    to_email = os.getenv("TO_EMAIL")

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

    print("EMAIL SENT:", response.status_code)
    
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

@app.get("/health")
def health():
    redis_ok = False

    if redis_client:
        try:
            redis_client.ping()
            redis_ok = True
        except Exception as e:
            print("Redis health check failed:", e)
            redis_ok = False

    status_code = 200 if redis_ok else 500

    return jsonify({
        "status": "ok" if redis_ok else "degraded",
        "redis_connected": redis_ok
    }), status_code


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

    print("DEBUG INCOMING WEBHOOK:", dict(request.values))

    vr = VoiceResponse()

    # Prevent first-word clipping on some carriers
    vr.pause(length=2)

    to_number = request.values.get("To", "")
    contractor = get_contractor_by_twilio_number(to_number)
    business_name = contractor.get("Business Name", "our office")

    # Say business name first (sounds more premium)
    vr.say(
        f"Thank you for calling {business_name}.",
        voice="Polly.Joanna",
        language="en-US",
    )

    vr.pause(length=2)

    gather = Gather(
        num_digits=1,
        action="/voice-menu",
        method="POST",
        timeout=6
    )

    gather.say(
        "If this is an emergency, press 1. "
        "To leave details for an estimate, press 2.",
        voice="Polly.Joanna",
        language="en-US",
    )

    vr.append(gather)

    return Response(str(vr), mimetype="text/xml")

    # If they press nothing, treat it like estimate flow (go to voice_menu so resume logic can run)
    vr.redirect("/voice-menu", method="POST")
    return Response(str(vr), mimetype="text/xml")


@app.route("/voice-menu", methods=["POST", "GET"])
def voice_menu():
    digit = (request.values.get("Digits") or "").strip()
    vr = VoiceResponse()

    to_number = (request.values.get("To") or "").strip()
    from_number = (request.values.get("From") or "").strip()

    # Emergency stays immediate
    if digit == "1":
        vr.redirect("/voice-emergency", method="POST")
        return Response(str(vr), mimetype="text/xml")

    # Anything else (including blank/timeout) = estimate flow with AUTO-RESUME check

    old_call_sid = None
    inferred_step = 0

    def _resume_step_from_fields(s: dict) -> int:
        if not (s.get("name") or "").strip():
            return 0
        if not (s.get("service_address") or "").strip():
            return 1
        if not (s.get("job_description") or "").strip():
            return 2
        if not (s.get("timing") or "").strip():
            return 3
        return 4

    if redis_client and to_number and from_number:
        old_call_sid = get_resume_pointer(to_number, from_number)
        if old_call_sid:
            old_state = get_state(old_call_sid) or {}
            inferred_step = _resume_step_from_fields(old_state)

    # If we have progress, auto-resume unless they press 2
    if old_call_sid and inferred_step > 0:
        g = Gather(
            input="dtmf",
            num_digits=1,
            timeout=3,
            action=f"/resume-choice?old={old_call_sid}&step={inferred_step}",
            method="POST",
            actionOnEmptyResult=True,
        )
        g.say(
            "Looks like we were in the middle of a request. "
            "I will resume where we left off. "
            "Press 2 to start over.",
            voice="Polly.Joanna",
            language="en-US",
        )
        vr.append(g)

        # No input -> Twilio will still hit /resume-choice after timeout
        return Response(str(vr), mimetype="text/xml")

    # No resume progress -> start fresh
    vr.redirect("/voice-intake", method="POST")
    return Response(str(vr), mimetype="text/xml")

@app.route("/resume-prompt", methods=["POST"])
def resume_prompt():
    vr = VoiceResponse()

    # carry forward what /resume-choice needs
    old_call_sid = request.values.get("old", "") or request.args.get("old", "")
    step = request.values.get("step", "0") or request.args.get("step", "0")

    gather = Gather(
        num_digits=1,
        action=f"/resume-choice?old={old_call_sid}&step={step}",
        method="POST",
        timeout=6,
        actionOnEmptyResult=True,   # ✅ KEY FIX (so it won’t hang up on silence)
    )
    gather.say(
        "I see we got disconnected. "
        "Press 1 to resume where you left off, "
        "or press 2 to start over.",
        voice="Polly.Joanna",
        language="en-US",
    )
    vr.append(gather)

    # Fallback: if Twilio still doesn't send digits, force it to hit resume-choice
    vr.redirect(f"/resume-choice?old={old_call_sid}&step={step}", method="POST")
    return Response(str(vr), mimetype="text/xml")

@app.route("/resume-choice", methods=["POST"])
def resume_choice():
    new_call_sid = request.values.get("CallSid", "unknown")
    digits = (request.values.get("Digits") or "").strip()
    # Treat "no input" as resume
    if digits == "":
        digits = "1"   # treat silence as resume

    to_number = (request.values.get("To") or "").strip()
    from_number = (request.values.get("From") or "").strip()

    old_call_sid = request.args.get("old", "") or ""
    inferred_step = int(request.args.get("step", "0") or "0")

    vr = VoiceResponse()

    # Press 2 => start over fresh
    if digits == "2":
        if redis_client and to_number and from_number:
            clear_resume_pointer(to_number, from_number)
            print("RESUME PTR CLEARED (restart):", to_number, from_number)

        vr.say("No problem. We'll start over.", voice="Polly.Joanna", language="en-US")
        vr.redirect("/voice-intake", method="POST")
        return Response(str(vr), mimetype="text/xml")

    # Default => resume
    if old_call_sid and inferred_step > 0:
        # Map NEW CallSid -> OLD CallSid so /voice-process uses old state
        set_call_alias(new_call_sid, old_call_sid)

        # Keep pointer alive pointing at OLD call sid
        if redis_client and to_number and from_number:
            save_resume_pointer(to_number, from_number, old_call_sid)

        vr.say("Resuming your request now.", voice="Polly.Joanna", language="en-US")
        vr.redirect(f"/voice-process?step={inferred_step}", method="POST")
        return Response(str(vr), mimetype="text/xml")

    vr.redirect("/voice-intake", method="POST")
    return Response(str(vr), mimetype="text/xml")


@app.route("/twilio/voicemail", methods=["POST"])
def twilio_voicemail():
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    recording_url = request.values.get("RecordingUrl", "")
    recording_duration = request.values.get("RecordingDuration", "")

    print("Voicemail received:", call_sid, from_number, recording_url, recording_duration)

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

@app.route("/voice-intake", methods=["POST"])
def voice_intake():
    """
    Start a fresh intake.
    Resume/restart decision happens only in /voice-menu -> /resume-choice.
    """

    call_sid = request.values.get("CallSid", "unknown")

    # Normalize To/From (Twilio sometimes uses Called/Caller depending on webhook)
    to_number = (request.values.get("To") or request.values.get("Called") or "").strip()
    from_number = (request.values.get("From") or request.values.get("Caller") or "").strip()

    contractor_key = to_number or "unknown"

    state = {
        "step": 0,
        "callback": from_number,
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
    gather.say(
        "First, please say your full name.",
        voice="Polly.Joanna",
        language="en-US",
    )

    vr.append(gather)

    vr.say(
        "Sorry, I didn’t catch that. Please call back and try again. Goodbye.",
        voice="Polly.Joanna",
        language="en-US",
    )

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

    print(
        "STEP DEBUG |",
        "CallSid:", call_sid,
        "| Step:", step,
        "| Digits:", digits,   
        "| Speech:", speech
    ) 

    to_number = (request.values.get("To") or "").strip()
    from_number = (request.values.get("From") or "").strip()

    # --- Resume / Alias logic (caller hung up and called back) ---

    # Keep the NEW CallSid so we can map it to the OLD one
    new_call_sid = call_sid

    # If this CallSid was already aliased earlier, follow it
    aliased = get_call_alias(new_call_sid)
    if aliased:
        call_sid = aliased

    # --- Lookup existing resume pointer BEFORE saving anything ---
    old_call_sid = None
    if step == 0 and redis_client and to_number and from_number:
        old_call_sid = get_resume_pointer(to_number, from_number)

    print("DEBUG resume pointer lookup:",
          "step=", step,
          "new=", new_call_sid,
          "old=", old_call_sid,
          "call_sid(before swap)=", call_sid)

    # If we found an older CallSid for this same caller, swap to it
    if old_call_sid and old_call_sid != new_call_sid:
        set_call_alias(new_call_sid, old_call_sid)   # NEW -> OLD mapping
        call_sid = old_call_sid
        print("DEBUG swapped call_sid to OLD:", call_sid)

    # Now load state for the FINAL chosen call_sid
    state = get_state(call_sid)


    # Always store CallSid
    state["call_sid"] = call_sid

    # Safe defaults (define keys first)
    state.setdefault("retries", 0)
    state.setdefault("name", "")
    state.setdefault("service_address", "")
    state.setdefault("job_description", "")
    state.setdefault("timing", "")
    state.setdefault("callback", "")
    state.setdefault("step", 0)

    # Always capture caller phone number (do not overwrite if already set)
    state["callback"] = state["callback"] or request.values.get("From", "")

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

    # Save back immediately (single save)
    set_state(call_sid, state)

    vr = VoiceResponse()

    # STEP 0: Client name (speech -> DTMF confirm)
    if step == 0:
        # If we are waiting for DTMF confirm, Digits will be present
        name_candidate = (state.get("name_candidate") or "").strip()

        # 0A) If we don't yet have a candidate name, ask for speech
        if not name_candidate and not speech:
            gather = Gather(
                input="speech",
                action="/voice-process?step=0",
                method="POST",
                timeout=8,
                speech_timeout="auto",
                profanity_filter=False,
                hints="first name last name full name",
            )
            gather.say(
                "Please say your full name now.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # 0B) If speech just came in, store it as candidate and ask for DTMF confirm
        if speech and not name_candidate:
            state["name_candidate"] = speech.strip()
            set_state(call_sid, state)

            g = Gather(
                input="dtmf",
                num_digits=1,
                action="/voice-process?step=0",
                method="POST",
                timeout=6,
            )
            g.say(
                f"I heard: {state['name_candidate']}. "
                "Press 1 to confirm, or press 2 to say it again.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(g)
            return Response(str(vr), mimetype="text/xml")

        # 0C) We have a candidate name; now we must have digits
        if not digits:
            g = Gather(
                input="dtmf",
                num_digits=1,
                action="/voice-process?step=0",
                method="POST",
                timeout=6,
            )
            g.say(
                "Press 1 to confirm, or press 2 to repeat.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(g)
            return Response(str(vr), mimetype="text/xml")

        # Press 2 => repeat name (but cap attempts to prevent infinite loops)
        if digits == "2":
            state["name_attempts"] = int(state.get("name_attempts") or 0) + 1

            # After 2 repeats, continue anyway with best guess (unconfirmed)
            if state["name_attempts"] >= 2:
                best_guess = (state.get("name_candidate") or "").strip()
                state["name"] = best_guess
                state["name_confirmed"] = False
                state["name_attempts"] = 0
                state.pop("name_candidate", None)

                state["step"] = 1
                state["retries"] = 0
                set_state(call_sid, state)

                if redis_client and to_number and from_number:
                    save_resume_pointer(to_number, from_number, call_sid)

                vr.say(
                    "Thanks. We'll continue and we can confirm spelling later.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.redirect("/voice-process?step=1", method="POST")
                return Response(str(vr), mimetype="text/xml")

            # Normal repeat (under the cap)
            state.pop("name_candidate", None)
            set_state(call_sid, state)
            vr.redirect("/voice-process?step=0", method="POST")
            return Response(str(vr), mimetype="text/xml")

        # Press 1 => commit name and move to Step 1
        if digits == "1":
            state["name"] = state.get("name_candidate", "").strip()
            state["name_confirmed"] = True
            state["name_attempts"] = 0
            state.pop("name_candidate", None)
            state["retries"] = 0
            state["step"] = 1
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)

            vr.say("Thanks.", voice="Polly.Joanna", language="en-US")
            vr.redirect("/voice-process?step=1", method="POST")
            return Response(str(vr), mimetype="text/xml")

        # Any other key => reprompt
        g = Gather(
            input="dtmf",
            num_digits=1,
            action="/voice-process?step=0",
            method="POST",
            timeout=6,
        )
        g.say(
            "Please press 1 to confirm, or press 2 to repeat.",
            voice="Polly.Joanna",
            language="en-US",
        )
        vr.append(g)
        return Response(str(vr), mimetype="text/xml")

    
    # STEP 1: Service address (The "Site Blueprint" — intro once, then collect in parts)
    if step == 1:
        # Intro: Say ONE time only
        if not state.get("address_intro_played"):
            state["address_intro_played"] = True
            state["retries"] = 0
            set_state(call_sid, state)

            vr.say(
                "Alright — let’s get the service address step by step. "
                "I’ll ask for the house number, then the street name, then the city, and finally the zip code.",
                voice="Polly.Joanna",
                language="en-US",
            )

            # If they hang up mid-address, resume should still work
            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)

            vr.redirect("/voice-process?step=1", method="POST")
            return Response(str(vr), mimetype="text/xml")

        # 1A) House / Building number (DTMF best, end with #)
        if not state.get("addr_number"):
            if not digits:
                gather = Gather(
                    input="dtmf",
                    action="/voice-process?step=1",
                    method="POST",
                    timeout=10,
                    finishOnKey="#",
                )
                gather.say(
                    "First, please enter the house or building number, then press pound. "
                    "For example: 4 5 1 5, then pound.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            house_num = "".join([c for c in digits if c.isdigit()]).strip()
            if len(house_num) < 1:
                vr.say(
                    "Sorry, I didn’t get the house number. Please try again, then press pound.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.redirect("/voice-process?step=1", method="POST")
                return Response(str(vr), mimetype="text/xml")

            state["addr_number"] = house_num
            state["retries"] = 0
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)

            vr.redirect("/voice-process?step=1", method="POST")
            return Response(str(vr), mimetype="text/xml")

        # 1B) Street name (speech)
        if not state.get("addr_street"):
            if not speech:
                gather = Gather(
                    input="speech",
                    action="/voice-process?step=1",
                    method="POST",
                    timeout=8,
                    speech_timeout="auto",
                    profanity_filter=False,
                    hints="Main Street, Oak Street, Pine Avenue, Court, Road, Drive, Lane",
                )
                gather.say(
                    "Great. Now please say the street name. "
                    "For example: Main Street, Oak Street, or Pine Avenue.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            state["addr_street"] = speech.strip()
            state["retries"] = 0
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)

            vr.redirect("/voice-process?step=1", method="POST")
            return Response(str(vr), mimetype="text/xml")

        # 1C) City (speech)
        if not state.get("addr_city"):
            if not speech:
                gather = Gather(
                    input="speech",
                    action="/voice-process?step=1",
                    method="POST",
                    timeout=8,
                    speech_timeout="auto",
                    profanity_filter=False,
                    hints="Bowie, Upper Marlboro, Lanham, Crofton, Washington, Baltimore",  
                )
                gather.say(
                    "Thanks. Now please say the city.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            state["addr_city"] = speech.strip()
            state["retries"] = 0
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)

            vr.redirect("/voice-process?step=1", method="POST")
            return Response(str(vr), mimetype="text/xml")

        # 1D) ZIP (DTMF)
        if not state.get("addr_zip"):
            if not digits:
                gather = Gather(
                    input="dtmf",
                    num_digits=5,
                    action="/voice-process?step=1",
                    method="POST",
                    timeout=10,
                )
                gather.say(
                    "Finally, please enter the five digit zip code.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            zip_digits = "".join([c for c in digits if c.isdigit()])
            if len(zip_digits) != 5:
                vr.say(
                    "Sorry, please enter a five digit zip code.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.redirect("/voice-process?step=1", method="POST")
                return Response(str(vr), mimetype="text/xml")

            state["addr_zip"] = zip_digits
            state["retries"] = 0
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)

            vr.redirect("/voice-process?step=1", method="POST")
            return Response(str(vr), mimetype="text/xml")

        # Done -> build full address and move to step 2
        state["service_address"] = f"{state['addr_number']} {state['addr_street']}, {state['addr_city']} {state['addr_zip']}"
        state["step"] = 2
        state["retries"] = 0
        set_state(call_sid, state)

        if redis_client and to_number and from_number:
            save_resume_pointer(to_number, from_number, call_sid)

        gather = Gather(
            input="speech",
            action="/voice-process?step=2",
            method="POST",
            timeout=8,
            speech_timeout="auto",
            profanity_filter=False,
        )
        gather.say(
            "Perfect. What service do you need today?",
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
                    profanity_filter=False,
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
            state["step"] = 2   # stay on step 2 until confirmed 
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)
                print("RESUME PTR SAVED (after job desc):", to_number, from_number, call_sid, "state.step=", state["step"])

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
            digits =  ""  # prevent stuck digits looping
            vr.redirect("/voice-process?step=2", method="POST")
            return Response(str(vr), mimetype="text/xml")

        if digits == "1":
            state["step"] = 3
            state["retries"] = 0
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)
                print("RESUME PTR SAVED (after confirm):", to_number, from_number, call_sid, "state.step=", state["step"])

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
                action="/voice-process?step=3",   # <-- FIXED
                method="POST",
                timeout=8,
                speech_timeout="auto",
                profanity_filter=False,
            )
            gather.say(
                "Please tell me when you need the service.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # Speech EXISTS -> save timing, move to step 4
        state["timing"] = speech.strip()
        state["retries"] = 0
        state["step"] = 4
        set_state(call_sid, state)

        if redis_client and to_number and from_number:
            save_resume_pointer(to_number, from_number, call_sid)
            print("RESUME PTR SAVED (after timing):", to_number, from_number, call_sid, "state.step=", state["step"])                  

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
        # Prefer DTMF if provided, otherwise use speech
        callback_val = (digits or speech or "").strip()

        # If still nothing usable, reprompt
        if not callback_val:
            gather = Gather(
                input="speech",
                action="/voice-process?step=4",
                method="POST",
                timeout=8,
                speech_timeout="auto",
                profanity_filter=False,
            )
            gather.say(
                "I didn't catch that. Please say the best callback phone number.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # Normalize to digits only (handles 240-555-1234, etc.)
        callback_digits = "".join([c for c in callback_val if c.isdigit()])

        # If caller spoke something too short, fall back to caller ID
        if len(callback_digits) < 7:
            callback_digits = (request.values.get("From", "") or "").strip()

        state["callback"] = callback_digits
        set_state(call_sid, state)
        
        if redis_client and to_number and from_number:
            save_resume_pointer(to_number, from_number, call_sid)
            print("RESUME PTR SAVED (after callback):", to_number, from_number, call_sid, "state.step=", state.get("step"))

        try:
            send_intake_summary(state)
        except Exception as e:
            print("send_intake_summary failed:", e)

        if redis_client and to_number and from_number:
            clear_resume_pointer(to_number, from_number)
            print("RESUME PTR CLEARED:", to_number, from_number)

        print(
            "CALL COMPLETE|",
            "CallSid:", call_sid,
            "| Name:", state.get("name"),
            "| Address:", state.get("service_address"),
            "| City:", state.get("city"),
            "| State:", state.get("state"),
            "| Zip:", state.get("zip")
        )

        unregister_live_call(state.get("contractor_key", "unknown"), call_sid)
        clear_state(call_sid)

        vr.say(
            "All set. We’ve received your request and our team will follow up shortly. Thanks for choosing us. Goodbye.",
            voice="Polly.Joanna",
            language="en-US",
            )
        
        vr.pause(length=1)
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
