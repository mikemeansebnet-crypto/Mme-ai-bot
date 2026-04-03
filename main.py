# main.py — ContractorOS AI Bot
# Architecture: ConversationRelay + Claude Haiku (conversation.py)
# Legacy step-based flow moved to branch: legacy-voice-stepflow

import os
import json
import math
import time
import urllib.parse
from datetime import datetime, timezone

from flask import Flask, request, jsonify, Response, session, redirect
from flask import render_template_string
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from google_auth_oauthlib.flow import Flow
import requests
import anthropic

from app.app.state import (
    get_state, set_state, clear_state,
    set_call_alias, get_call_alias, clear_call_alias,
    save_resume_pointer, get_resume_pointer, clear_resume_pointer,
    register_live_call, unregister_live_call, list_live_calls,
)
from app.app.config import redis_client
from app.app.cal_service import build_cal_booking_link, create_google_calendar_event
from app.app.mapbox_service import mapbox_address_candidates, mapbox_geocode_one
from app.app.crypto_service import encrypt_text
from app.app.airtable_service import (
    airtable_create_record,
    airtable_update_record,
    airtable_get_record,
    get_contractor_by_twilio_number,
    airtable_get_city_corrections,
    normalize_city,
)

# ─────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")

from app.app.conversation import conversation_bp, init_sock
app.register_blueprint(conversation_bp)
init_sock(app)


# ─────────────────────────────────────────────
# Geography helpers
# ─────────────────────────────────────────────

def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.7613
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lon2) - float(lon1))
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def address_in_service_area(contractor: dict, lat: float, lon: float) -> tuple[bool, str]:
    try:
        normalized = {str(k).strip(): v for k, v in contractor.items()}
        home_lat = normalized.get("Home Base Lat")
        home_lon = normalized.get("Home Base Lon")
        max_radius = normalized.get("Max Radius Miles")
        hard_max = normalized.get("Hard Max Miles")

        print(
            "SERVICE CONFIG |",
            "home_lat=", home_lat,
            "| home_lon=", home_lon,
            "| max_radius=", max_radius,
            "| hard_max=", hard_max,
            "| contractor_keys=", [repr(k) for k in contractor.keys()]
        )

        if home_lat in (None, "") or home_lon in (None, ""):
            return True, "no_home_base_config"

        miles = haversine_miles(home_lat, home_lon, lat, lon)
        limit = None
        if max_radius not in (None, ""):
            limit = float(max_radius)
        elif hard_max not in (None, ""):
            limit = float(hard_max)

        if limit is None:
            return True, f"no_radius_limit miles={miles:.2f}"

        return miles <= limit, f"miles={miles:.2f} limit={limit:.2f}"

    except Exception as e:
        print("SERVICE AREA CHECK ERROR |", e)
        return True, "service_check_error"


# ─────────────────────────────────────────────
# Twilio helpers
# ─────────────────────────────────────────────

def twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        return {"ok": False, "error": "Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN"}
    return {"ok": True, "client": Client(account_sid, auth_token)}


def record_calls_default() -> bool:
    return os.getenv("RECORD_CALLS_DEFAULT", "false").lower() == "true"


def start_call_recording(call_sid: str, contractor: dict) -> dict:
    record_calls = bool(contractor.get("RECORD_CALLS")) if contractor else False
    if not record_calls and not record_calls_default():
        return {"ok": False, "disabled": True}

    t = twilio_client()
    if not t.get("ok"):
        return t

    try:
        print("START RECORDING | CallSid:", call_sid)
        rec = t["client"].calls(call_sid).recordings.create(
            recording_status_callback_event=["completed"],
        )
        print("RECORDING STARTED | RecordingSid:", rec.sid)
        return {"ok": True, "recording_sid": rec.sid}
    except Exception as e:
        print("RECORDING ERROR |", str(e))
        return {"ok": False, "error": str(e)}


sms route
# -------------------------------------------------------------------------
 
@app.route("/sms", methods=["POST"])
def sms():
    """
    SMS intake handler — conversational lead capture via text.
    Uses Claude to collect name, address, job, timing.
    Stores conversation state in Redis between messages.
    """
    incoming_msg = (request.form.get("Body") or "").strip()
    from_number = (request.form.get("From") or "").strip()
    to_number = (request.form.get("To") or "").strip()
 
    print(f"SMS FROM {from_number} | TO {to_number} | MSG: {incoming_msg}")
 
    # Handle opt-out keywords immediately
    if incoming_msg.lower() in ["stop", "unsubscribe", "cancel", "quit", "end"]:
        return Response(
            "<Response><Message>You have been unsubscribed. Reply START to resubscribe.</Message></Response>",
            mimetype="text/xml"
        )
 
    if incoming_msg.lower() in ["start", "unstop"]:
        return Response(
            "<Response><Message>You are now subscribed to receive messages.</Message></Response>",
            mimetype="text/xml"
        )
 
    # Look up contractor
    contractor = {}
    try:
        contractor = get_contractor_by_twilio_number(to_number) or {}
    except Exception as e:
        print("SMS CONTRACTOR LOOKUP FAILED:", e)
 
    business_name = (contractor.get("Business Name") or "our office").strip()
 
    # Load or initialize SMS conversation state from Redis
    sms_state_key = f"sms_state:{to_number}:{from_number}"
    sms_state = {}
 
    if redis_client:
        try:
            raw = redis_client.get(sms_state_key)
            if raw:
                sms_state = json.loads(raw)
        except Exception as e:
            print("SMS STATE LOAD ERROR |", e)
 
    # Initialize state if new conversation
    if not sms_state:
        sms_state = {
            "name": "",
            "service_address": "",
            "job_description": "",
            "timing": "",
            "messages": [],
            "callback": from_number,
            "to_number": to_number,
            "from_number": from_number,
            "started_at": int(time.time()),
        }
 
    # Build system prompt based on current state
    system_prompt = build_sms_system_prompt(contractor, sms_state)
    messages = sms_state.get("messages", [])
 
    # Run Claude
    claude_response = run_sms_claude(system_prompt, messages, incoming_msg)
 
    if not claude_response:
        # Claude failed — send fallback
        reply = f"Thanks for contacting {business_name}! We received your message and will follow up shortly."
        return Response(
            f"<Response><Message>{reply}</Message></Response>",
            mimetype="text/xml"
        )
 
    print("SMS CLAUDE RESPONSE |", claude_response)
 
    # Handle emergency
    if "EMERGENCY" in claude_response:
        emergency_phone = (contractor.get("Emergency Phone") or "").strip()
        if emergency_phone:
            reply = f"This sounds urgent! Please call us directly at {emergency_phone} for immediate assistance."
        else:
            reply = f"This sounds urgent! Please call {business_name} directly for immediate assistance."
 
        if redis_client:
            redis_client.delete(sms_state_key)
 
        return Response(
            f"<Response><Message>{reply}</Message></Response>",
            mimetype="text/xml"
        )
 
    # Handle intake complete
    if "INTAKE_COMPLETE" in claude_response:
        try:
            import re
            json_match = re.search(r"\{.*\}", claude_response, re.DOTALL)
            if json_match:
                intake_data = json.loads(json_match.group(0))
 
                # Update state with final data
                sms_state["name"] = intake_data.get("name", sms_state.get("name", ""))
                sms_state["service_address"] = intake_data.get("service_address", "")
                sms_state["job_description"] = intake_data.get("job_description", "")
                sms_state["timing"] = intake_data.get("timing", "")
                sms_state["priority"] = intake_data.get("priority", "STANDARD")
                sms_state["call_sid"] = f"SMS-{from_number}-{int(time.time())}"
 
                # Save lead to Airtable + send email
                notify_email = (contractor.get("Notify Email") or os.getenv("TO_EMAIL") or "").strip()
                try:
                    send_intake_summary(sms_state, notify_email=notify_email)
                except Exception as e:
                    print("SMS INTAKE SUMMARY ERROR |", e)
 
                # Build and send booking link
                booking_link = build_cal_booking_link(contractor, sms_state)
 
                if booking_link:
                    reply = (
                        f"Got it! Book your estimate here: {booking_link} "
                        f"Reply STOP to opt out."
                    )
                else:
                    reply = (
                        f"Got it! We have all your details and will follow up shortly. "
                        f"Reply STOP to opt out."
                    )
 
                # Clear SMS state
                if redis_client:
                    redis_client.delete(sms_state_key)
 
                print("SMS INTAKE COMPLETE |", sms_state.get("name"), "|", sms_state.get("service_address"))
 
        except Exception as e:
            print("SMS INTAKE COMPLETE ERROR |", e)
            reply = f"Thanks! We have your details and will follow up shortly."
 
        return Response(
            f"<Response><Message>{reply}</Message></Response>",
            mimetype="text/xml"
        )
 
    # Normal response — save state and reply
    messages.append({"role": "user", "content": incoming_msg})
    messages.append({"role": "assistant", "content": claude_response})
    sms_state["messages"] = messages[-20:]  # Keep last 20 messages
 
    # Save state to Redis with 2 hour TTL
    if redis_client:
        try:
            redis_client.setex(sms_state_key, 7200, json.dumps(sms_state))
        except Exception as e:
            print("SMS STATE SAVE ERROR |", e)
 
    # Truncate reply to SMS safe length
    reply = claude_response[:320] if len(claude_response) > 320 else claude_response
 
    return Response(
        f"<Response><Message>{reply}</Message></Response>",
        mimetype="text/xml"
    )


def send_fallback_sms(to_number: str, body: str) -> dict:
    try:
        from_number = os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_FROM_NUMBER")
        if not from_number:
            return {"ok": False, "error": "missing_twilio_from_number"}
        return send_sms(to_number=to_number, body=body, from_number=from_number)
    except Exception as e:
        print("FALLBACK SMS ERROR |", str(e))
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────
# Email helpers
# ─────────────────────────────────────────────

def send_email(subject: str, body: str, to_email: str = None, reply_to: str = None):
    api_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("FROM_EMAIL")
    default_to = os.getenv("TO_EMAIL")
    to_email = (to_email or default_to or "").strip()

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
        plain_text_content=body,
    )
    if reply_to:
        message.reply_to = reply_to

    sg = SendGridAPIClient(api_key)
    response = sg.send(message)
    print("EMAIL SENT:", response.status_code)


def send_intake_summary(state: dict, notify_email: str = None, reply_to_email: str = None):
    print("EMAIL DEBUG | entering send_intake_summary")

    email_api_key = os.environ.get("SENDGRID_API_KEY", "")
    print("EMAIL DEBUG | SENDGRID KEY EXISTS:", bool(email_api_key))
    print("EMAIL DEBUG | SENDGRID KEY PREFIX:", email_api_key[:5] if email_api_key else "MISSING")

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

    airtable_fields = {
        "Client Name": state.get("name", ""),
        "Call Back Number": state.get("callback", ""),
        "Service Address": state.get("service_address", ""),
        "Job Description": state.get("job_description", ""),
        "Source": "AI Phone Call",
        "Call SID": state.get("call_sid", ""),
        "Appointment Requested": state.get("timing", ""),
        "Lead Status": "New Lead",
        "Priority": state.get("priority", "STANDARD"),
    }

    appt_datetime = state.get("appointment")
    if appt_datetime and "T" in appt_datetime:
        airtable_fields["Appointment Date and Time"] = appt_datetime

    airtable_result = airtable_create_record(airtable_fields)
    print("Airtable result:", airtable_result)

    send_email(subject, body, to_email=notify_email, reply_to=reply_to_email)


# ─────────────────────────────────────────────
# Contractor status monitoring
# ─────────────────────────────────────────────

def update_contractor_status(to_number: str, fields: dict):
    try:
        contractor = get_contractor_by_twilio_number(to_number)
        if not contractor:
            print("STATUS UPDATE SKIPPED | no contractor for", to_number)
            return

        record_id = (
            contractor.get("Contractor Record ID", "").strip()
            or contractor.get("airtable_id", "").strip()
        )
        if not record_id:
            print("STATUS UPDATE SKIPPED | no record_id for", to_number)
            return

        fields["Last Call Time"] = datetime.now(timezone.utc).isoformat()
        result = airtable_update_record(record_id, fields)
        print("STATUS UPDATED |", to_number, "|", list(fields.keys()), "| result:", result.get("ok"))
        return result

    except Exception as e:
        print("STATUS UPDATE ERROR |", str(e))
        pass

ction to main.py
# Place it after update_contractor_status() and before the routes
# =============================================================================
 
def run_sms_claude(system_prompt: str, messages: list, user_message: str) -> str:
    """
    Run a Claude turn for SMS conversation.
    Shorter responses than phone — SMS needs to be concise.
    """
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        client = anthropic.Anthropic(api_key=api_key)
 
        messages_to_send = messages + [{"role": "user", "content": user_message}]
 
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,  # Keep SMS responses short
            system=system_prompt,
            messages=messages_to_send,
        )
        return response.content[0].text.strip()
    except Exception as e:
        print("SMS CLAUDE ERROR |", e)
        return None
 
 
def build_sms_system_prompt(contractor: dict, state: dict) -> str:
    """
    System prompt for SMS intake — shorter and more text-friendly than phone.
    """
    business_name = (contractor.get("Business Name") or "our office").strip()
 
    name = (state.get("name") or "").strip()
    service_address = (state.get("service_address") or "").strip()
    job_description = (state.get("job_description") or "").strip()
    timing = (state.get("timing") or "").strip()
 
    already = []
    needed = []
 
    if name: already.append(f"Name: {name}")
    else: needed.append("full name")
 
    if service_address: already.append(f"Address: {service_address}")
    else: needed.append("full service address with zip code")
 
    if job_description: already.append(f"Job: {job_description}")
    else: needed.append("what work they need done")
 
    if timing: already.append(f"Timing: {timing}")
    else: needed.append("when they need it")
 
    already_str = "\n".join(f"- {x}" for x in already) if already else "Nothing yet"
    needed_str = "\n".join(f"- {x}" for x in needed) if needed else "All collected"
 
    return f"""You are a friendly SMS intake assistant for {business_name}.
Collect four pieces of info via text message to send a booking link.
 
ALREADY COLLECTED:
{already_str}
 
STILL NEEDED:
{needed_str}
 
RULES:
- This is SMS — keep ALL responses under 160 characters when possible
- Never use line breaks in responses — single flowing sentence only
- Ask for ONE piece of info at a time
- Accept the first answer given — never ask follow-ups
- For address: accept whatever they give, you will validate it
- If they say emergency (flood, burst pipe, no heat): reply exactly EMERGENCY
- If all four pieces collected: reply exactly INTAKE_COMPLETE followed by JSON on next line
- Be warm but extremely brief — this is text not email
 
WHEN COMPLETE output exactly:
INTAKE_COMPLETE
{{"name": "...", "service_address": "...", "job_description": "...", "timing": "...", "priority": "STANDARD"}}"""


# ─────────────────────────────────────────────
# Basic routes
# ─────────────────────────────────────────────

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

    return jsonify({
        "status": "ok" if redis_ok else "degraded",
        "redis_connected": redis_ok,
    }), 200 if redis_ok else 500


@app.get("/test-email")
def test_email():
    try:
        send_email("MME AI Bot Test", "If you got this, SendGrid is working ✅")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/test-google-event")
def test_google_event():
    contractor = get_contractor_by_twilio_number("+12408686702") or {}
    result = create_google_calendar_event(
        contractor=contractor,
        summary="ContractorOS Test Booking",
        start_time="2026-03-09T16:00:00-04:00",
        end_time="2026-03-09T16:30:00-04:00",
        description="Test event created by ContractorOS",
        location="Bowie, MD",
    )
    return jsonify(result)


@app.route("/book")
def book_redirect():
    contractor_key = (request.args.get("c") or "").strip()
    contractor = get_contractor_by_twilio_number(contractor_key) if contractor_key else {}
    base_url = (contractor.get("Intake URL") or "").strip()
    if not base_url:
        return "Booking link not configured for this contractor.", 404
    params = request.args.to_dict(flat=True)
    params.pop("c", None)
    query_string = urllib.parse.urlencode(params)
    separator = "&" if "?" in base_url else "?"
    return redirect(f"{base_url}{separator}{query_string}" if query_string else base_url, code=302)


# ─────────────────────────────────────────────
# SMS
# ─────────────────────────────────────────────

@app.route("/sms", methods=["POST"])
def sms():
    incoming_msg = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "")
    print(f"SMS from {from_number}: {incoming_msg}")
    reply = "MME AI Bot received your message."
    return Response(f"<Response><Message>{reply}</Message></Response>", mimetype="text/xml")


# ─────────────────────────────────────────────
# Fallback
# ─────────────────────────────────────────────

@app.route("/twilio-fallback", methods=["POST", "GET"])
def twilio_fallback():
    vr = VoiceResponse()
    vr.say(
        "Sorry, an application error occurred. Please try again later. Goodbye.",
        voice="Polly.Joanna",
        language="en-US",
    )
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


# ─────────────────────────────────────────────
# Voice entry — routes to ConversationRelay
# ─────────────────────────────────────────────

@app.route("/voice", methods=["POST", "GET"])
def voice():
    vr = VoiceResponse()
    vr.pause(length=1)

    to_number = (request.values.get("To") or "").strip()

    contractor = {}
    try:
        contractor = get_contractor_by_twilio_number(to_number) or {}
    except Exception as e:
        print("CONTRACTOR LOOKUP FAILED:", e)

    try:
        from_number_log = (request.values.get("From") or "").strip()
        update_contractor_status(to_number, {
            "Bot Status": "Active",
            "Last Call From": from_number_log,
            "Last Call Intent": "incoming",
        })
    except Exception:
        pass

    # Route to ConversationRelay
    vr.redirect("/voice-cr", method="POST")
    return Response(str(vr), mimetype="text/xml")


# ─────────────────────────────────────────────
# Emergency
# ─────────────────────────────────────────────

@app.route("/voice-emergency", methods=["POST", "GET"])
def voice_emergency():
    vr = VoiceResponse()
    to_number = (request.values.get("To") or "").strip()
    contractor = get_contractor_by_twilio_number(to_number) or {}
    emergency_phone = (contractor.get("Emergency Phone") or "").strip()
    business_name = (contractor.get("Business Name") or "your business").strip()

    if emergency_phone:
        vr.say("Okay. Connecting you now.", voice="Polly.Joanna", language="en-US")
        whisper_url = (
            request.url_root.rstrip("/")
            + "/emergency-whisper?biz="
            + urllib.parse.quote(business_name)
        )
        dial = vr.dial(timeout=20, caller_id=to_number, answer_on_bridge=True)
        dial.number(emergency_phone, url=whisper_url)
        return Response(str(vr), mimetype="text/xml")

    vr.say(
        "I'm sorry, we couldn't reach the on-call team. "
        "Please leave your name, address, and the nature of the emergency after the beep.",
        voice="Polly.Joanna",
        language="en-US",
    )
    vr.record(max_length=120, play_beep=True, action="/twilio/voicemail", method="POST")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


@app.route("/emergency-whisper", methods=["POST", "GET"])
def emergency_whisper():
    vr = VoiceResponse()
    biz_name = request.args.get("biz", "your business")
    gather = Gather(input="dtmf", num_digits=1, timeout=5, action="/emergency-whisper-connect", method="POST")
    gather.say(f"Emergency call for {biz_name}. Press any key to connect.", voice="Polly.Joanna", language="en-US")
    vr.append(gather)
    vr.say("No input received. Goodbye.", voice="Polly.Joanna", language="en-US")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


@app.route("/emergency-whisper-connect", methods=["POST"])
def emergency_whisper_connect():
    return Response(str(VoiceResponse()), mimetype="text/xml")


# ─────────────────────────────────────────────
# Voicemail
# ─────────────────────────────────────────────

@app.route("/twilio/voicemail", methods=["POST"])
def twilio_voicemail():
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    recording_url = request.values.get("RecordingUrl", "")
    recording_duration = request.values.get("RecordingDuration", "")
    to_number = (request.values.get("To") or "").strip()

    print("Voicemail received:", call_sid, from_number, recording_url, recording_duration)

    try:
        contractor = get_contractor_by_twilio_number(to_number) or {}
        greeting_name = (
            contractor.get("Greeting Name")
            or contractor.get("Business Name")
            or "our office"
        ).strip()
        send_fallback_sms(
            to_number=from_number,
            body=(
                f"Thanks for calling {greeting_name}. "
                "We received your voicemail and will follow up as soon as possible."
            ),
        )
    except Exception as e:
        print("VOICEMAIL SMS ERROR |", e)

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


# ─────────────────────────────────────────────
# Onboarding — Google Calendar OAuth
# ─────────────────────────────────────────────

@app.route("/onboard/<contractor_id>")
def onboard(contractor_id):
    # Force correct casing on Airtable record ID
    contractor_id = contractor_id[:3].lower() + contractor_id[3:].upper()
    session["oauth_contractor_key"] = contractor_id
    session.permanent = True
    print("ONBOARD | contractor_id stored in session:", contractor_id)
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    contractor_key = session.get("oauth_contractor_key")
    google_connected = False
    contractor_name = "Contractor"
    error_message = None

    if contractor_key:
        try:
            result = airtable_get_record(contractor_key, table_name="Contractors")
            print("DASHBOARD AIRTABLE RESULT:", result)
            if result.get("ok"):
                fields = result.get("fields", {})
                google_connected = bool(fields.get("Google Connected", False))
                contractor_name = (
                    fields.get("Business Name")
                    or fields.get("Greeting Name")
                    or "Contractor"
                )
            else:
                error_message = "Could not load your account. Please use your onboarding link again."
                print("DASHBOARD ERROR:", result)
        except Exception as e:
            print("DASHBOARD EXCEPTION:", e)
            error_message = "Something went wrong loading your account."
    else:
        error_message = "No account found. Please use your onboarding link."

    html = """
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>ContractorOS — Setup</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
                background: #f4f6f9; color: #111827;
                min-height: 100vh; display: flex;
                align-items: center; justify-content: center; padding: 20px;
            }
            .card {
                background: #fff; border-radius: 16px; padding: 40px;
                max-width: 520px; width: 100%;
                box-shadow: 0 4px 24px rgba(0,0,0,0.07);
            }
            .logo { font-size: 13px; font-weight: 700; letter-spacing: 0.1em;
                text-transform: uppercase; color: #2563eb; margin-bottom: 24px; }
            h1 { font-size: 28px; font-weight: 700; margin-bottom: 8px; line-height: 1.2; }
            .sub { font-size: 16px; color: #6b7280; margin-bottom: 28px; line-height: 1.5; }
            .status-badge {
                display: inline-flex; align-items: center; gap: 6px;
                padding: 6px 14px; border-radius: 999px;
                font-size: 14px; font-weight: 600; margin-bottom: 24px;
            }
            .status-badge.connected { background: #dcfce7; color: #166534; }
            .status-badge.not-connected { background: #fee2e2; color: #991b1b; }
            .btn {
                display: block; width: 100%; background: #2563eb; color: #fff;
                text-decoration: none; font-weight: 700; padding: 16px 20px;
                border-radius: 10px; font-size: 16px; text-align: center;
                margin-bottom: 12px;
            }
            .btn:hover { background: #1d4ed8; }
            .btn.done { background: #16a34a; cursor: default; }
            .note { font-size: 13px; color: #9ca3af; text-align: center; margin-bottom: 28px; }
            .divider { border: none; border-top: 1px solid #f3f4f6; margin: 28px 0; }
            .features h2 { font-size: 16px; font-weight: 700; margin-bottom: 12px; color: #374151; }
            .features ul { list-style: none; padding: 0; }
            .features li {
                font-size: 15px; color: #4b5563; padding: 6px 0;
                display: flex; align-items: center; gap: 8px;
            }
            .features li::before { content: "✓"; color: #16a34a; font-weight: 700; flex-shrink: 0; }
            .error-box {
                background: #fef2f2; border: 1px solid #fecaca;
                border-radius: 8px; padding: 14px;
                font-size: 14px; color: #991b1b; margin-bottom: 20px;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <div class="logo">ContractorOS</div>
            {% if error_message %}
                <div class="error-box">{{ error_message }}</div>
            {% endif %}
            <h1>Welcome, {{ contractor_name }}</h1>
            <p class="sub">
                {% if google_connected %}
                    Your AI receptionist is active and ready to take calls.
                {% else %}
                    One step left — connect your Google Calendar to start receiving bookings automatically.
                {% endif %}
            </p>
            {% if google_connected %}
                <div class="status-badge connected">&#10003; Google Calendar Connected</div>
                <a class="btn done">You're all set!</a>
                <p class="note">Calls are being answered and bookings are going to your calendar.</p>
            {% else %}
                <div class="status-badge not-connected">&#9679; Google Calendar Not Connected</div>
                <a href="/connect-google" class="btn">Connect Google Calendar</a>
                <p class="note">Takes less than 60 seconds. We never store your password.</p>
            {% endif %}
            <hr class="divider">
            <div class="features">
                <h2>What's included</h2>
                <ul>
                    <li>Calls answered 24/7 with your business name</li>
                    <li>Leads captured and emailed to you instantly</li>
                    <li>Customers get a booking link by text</li>
                    <li>Jobs booked directly to your Google Calendar</li>
                    <li>Emergency calls routed to your phone</li>
                    <li>Service area controls built in</li>
                </ul>
            </div>
        </div>
    </body>
    </html>
    """

    return render_template_string(
        html,
        contractor_name=contractor_name,
        google_connected=google_connected,
        error_message=error_message,
    )


@app.route("/connect-google")
def connect_google():
    contractor_key = session.get("oauth_contractor_key")
    if not contractor_key:
        return redirect("/dashboard")

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=[
            "openid",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
    )
    flow.redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["oauth_state"] = state
    session.permanent = True
    print("CONNECT GOOGLE | contractor_key:", contractor_key)
    return redirect(authorization_url)


@app.route("/oauth/google/callback")
def google_callback():
    state = session.get("oauth_state")
    contractor_key = session.get("oauth_contractor_key")

    print("GOOGLE CALLBACK | contractor_key:", contractor_key)

    if not contractor_key:
        print("GOOGLE CALLBACK ERROR | no contractor_key in session")
        return redirect("/dashboard")

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=[
            "openid",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        state=state,
    )
    flow.redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")

    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as e:
        print("GOOGLE CALLBACK TOKEN ERROR:", e)
        return redirect("/dashboard")

    credentials = flow.credentials

    try:
        profile = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {credentials.token}"},
            timeout=20,
        ).json()
        google_email = profile.get("email", "")
    except Exception as e:
        print("GOOGLE PROFILE ERROR:", e)
        google_email = ""

    refresh_token = encrypt_text(credentials.refresh_token or "")

    result = airtable_update_record(
        contractor_key,
        {
            "Google Connected": True,
            "Google Email": google_email,
            "Google Refresh Token": refresh_token or "",
            "Google Calendar ID": "primary",
        },
        table_name="Contractors",
    )
    print("GOOGLE OAUTH AIRTABLE UPDATE:", result)

    session["google_connected"] = True
    session.permanent = True
    return redirect("/dashboard")


# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
