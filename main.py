# CHECKPOINT: state.py integrated and production verified before removing duplicates

import os
import requests
import json
import time
import re
import math
import urllib.parse
from flask import Flask, request, jsonify, Response, session, redirect, url_for 
from twilio.twiml.voice_response import VoiceResponse, Gather
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from twilio.rest import Client 
from google_auth_oauthlib.flow import Flow


from app.app.state import (
    get_state, set_state, clear_state,
    set_call_alias, get_call_alias, clear_call_alias,
    save_resume_pointer, get_resume_pointer, clear_resume_pointer,
    register_live_call, unregister_live_call, list_live_calls
)
from app.app.config import redis_client
from app.app.cal_service import build_cal_booking_link, create_google_calendar_event
from app.app.mapbox_service import mapbox_address_candidates, mapbox_geocode_one
from app.app.crypto_service import encrypt_text
from app.app.airtable_service import (
    airtable_create_record,
    get_contractor_by_twilio_number,
    airtable_get_city_corrections,
    normalize_city,
    airtable_update_record,
)



app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")


# Gather all environment variables 
airtable_token = os.getenv("AIRTABLE_TOKEN")
airtable_base_id = os.getenv("AIRTABLE_BASE_ID")
air_table_name = os.getenv("AIRTABLE_TABLE_NAME")
email_api_key = os.environ.get("SENDGRID_API_KEY")
from_email = os.environ.get("FROM_EMAIL")
to_email = os.environ.get("TO_EMAIL")



# helper functions

def init_conversation_data(state):
    if "conversation_data" not in state:
        state["conversation_data"] = {
            "name": None,
            "service": None,
            "address": None,
            "timing": None,
            "callback": None,
            "is_emergency": False,
        }
    return state

def extract_name_from_text(text):
    if not text:
        return None

    t = text.strip()

    patterns = [
        r"(?:this is|it's|it is|i am|i'm|my name is)\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,2})",
        r"^([A-Za-z]+(?:\s+[A-Za-z]+){0,2})\s+here\b",
    ]

    for pattern in patterns:
        m = re.search(pattern, t, re.IGNORECASE)
        if m:
            name = m.group(1).strip(" .,!?")
            if name:
                return name

    return None
    
def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.7613
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lon2) - float(lon1))

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c

def address_in_service_area(contractor: dict, lat: float, lon: float) -> tuple[bool, str]:
    """
    Returns (is_allowed, reason)
    Uses Home Base Lat/Lon + Max Radius Miles / Hard Max Miles.
    """
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

        allowed = miles <= limit
        return allowed, f"miles={miles:.2f} limit={limit:.2f}"

    except Exception as e:
        print("SERVICE AREA CHECK ERROR |", e)
        return True, "service_check_error"

# -------------------------
# INTENT HELPERS
# -------------------------

EMERGENCY_KEYWORDS = [
    "emergency",
    "urgent",
    "right away",
    "asap",
    "immediately",
    "tree fell",
    "tree down",
    "flood",
    "flooding",
    "water coming in",
    "storm damage",
    "burst pipe",
    "pipe burst",
    "sewer backup",
    "danger",
    "help",
    "hazard",
    "blocked."
]

VOICEMAIL_KEYWORDS = [
    "leave a message",
    "voicemail",
    "call me back",
    "callback",
    "have mike call me",
    "talk to mike",
    "speak to mike",
    "someone call me",
    "return my call",
    "busy",
    "not available",
    "reachout."
]

ESTIMATE_KEYWORDS = [
    "estimate",
    "quote",
    "pricing",
    "price",
]


def normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def detect_call_intent(text: str) -> str:
    t = normalize_text(text)

    if not t:
        return "unknown"

    for kw in EMERGENCY_KEYWORDS:
        if kw in t:
            return "emergency"

    for kw in VOICEMAIL_KEYWORDS:
        if kw in t:
            return "voicemail"

    for kw in ESTIMATE_KEYWORDS:
        if kw in t:
            return "estimate"

    # Default normal service requests to estimate
    return "estimate"


def send_fallback_sms(to_number: str, body: str) -> dict:
    """
    Optional SMS fallback using your existing twilio_client() helper.
    """
    try:
        from helpers import twilio_client

        tc = twilio_client()
        if not tc.get("ok"):
            return {"ok": False, "error": tc.get("error", "twilio_client_failed")}

        client = tc["client"]
        from_number = os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_FROM_NUMBER")

        if not from_number:
            return {"ok": False, "error": "missing_twilio_from_number"}

        msg = client.messages.create(
            body=body,
            from_=from_number,
            to=to_number
        )
        return {"ok": True, "sid": msg.sid}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# routes start here

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

def twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        return {"ok": False, "error": "Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN"}

    return {"ok": True, "client": Client(account_sid, auth_token)}

def record_calls_default() -> bool:
    return os.getenv("RECORD_CALLS_DEFAULT", "false").lower() == "true"


def start_call_recording(call_sid: str, contractor: dict) -> dict:
    """
    Starts a full-call recording in Twilio after consent.
    Returns {"ok": True, "recording_sid": "..."} or {"ok": False, "error": "..."}
    """
    # Per-contractor override (Airtable checkbox)
    record_calls = bool(contractor.get("RECORD_CALLS")) if contractor else False
    if not record_calls and not record_calls_default():
        return {"ok": False, "disabled": True}

    t = twilio_client()
    if not t.get("ok"):
        return t

    client = t["client"]

    try:
        rec = client.calls(call_sid).recordings.create(
            recording_channels="dual",
            recording_status_callback_event=["completed"],
            # OPTIONAL (recommended): add a callback URL in your app later
            # recording_status_callback=f"{os.getenv('PUBLIC_BASE_URL','')}/recording-status",
        )
        return {"ok": True, "recording_sid": rec.sid}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def sms_enabled() -> bool:
    return os.getenv("SMS_ENABLED", "false").lower() == "true"

def send_sms(to_number: str, body: str, from_number: str) -> dict:
    """
    Outbound SMS sender (feature-flagged).
    """
    if not sms_enabled():
        print("SMS_DISABLED | Would have sent:", to_number, "|", body)
        return {"ok": False, "disabled": True}

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        print("Missing Twilio credentials")
        return {"ok": False, "error": "missing_credentials"}

    client = Client(account_sid, auth_token)

    msg = client.messages.create(
        to=to_number,
        from_=from_number,  # <-- important
        body=body
    )

    print("SMS_SENT:", msg.sid)
    return {"ok": True, "sid": msg.sid}

def send_fallback_sms(to_number: str, body: str) -> dict:
    """
    Uses the same SMS engine as the rest of the system.
    """
    try:
        from_number = os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_FROM_NUMBER")

        if not from_number:
            print("FALLBACK SMS ERROR | missing_twilio_from_number")
            return {"ok": False, "error": "missing_twilio_from_number"}

        print("FALLBACK SMS DEBUG | sending from", from_number, "to", to_number)

        result = send_sms(
            to_number=to_number,
            body=body,
            from_number=from_number
        )

        print("FALLBACK SMS RESULT |", result)
        return result

    except Exception as e:
        print("FALLBACK SMS ERROR |", str(e))
        return {"ok": False, "error": str(e)}



def send_email(subject: str, body: str, to_email: str = None, reply_to: str = None):
    # Pull fresh every time (prevents refactor breakage)
    api_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("FROM_EMAIL")
    default_to_email = os.getenv("TO_EMAIL")
    to_email = (to_email or default_to_email or "").strip()

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

    if reply_to:
        message.reply_to = reply_to

    sg = SendGridAPIClient(api_key)
    response = sg.send(message)

    print("EMAIL SENT:", response.status_code)
    
def send_intake_summary(state: dict, notify_email: str = None, reply_to_email: str = None):
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

    send_email(subject, body, to_email=notify_email, reply_to=reply_to_email)
    # Optional: helpful in Render logs

    # Send booking link via SMS (if contractor has one)
    contractor = get_contractor_by_twilio_number(state.get("to_number"))

    from_number = state.get("from_number")
    to_number = state.get("to_number")

    booking_link = (contractor.get("CAL Booking URL") or "").strip()

    if booking_link and from_number and to_number:
        sms_body = f"Thanks for contacting us! Choose a date and time here: {booking_link}"
        send_sms(
            to_number=from_number,
            body=sms_body,
            from_number=to_number
        )
    


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



# -------------------------
# TEST GOOGLE EVENT
# -------------------------

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


# -------------------------
# CONTRACTOR DASHBOARD
# -------------------------


@app.route("/dashboard")
def dashboard():

    calendar_connected = session.get("google_connected", False)

    if calendar_connected:
        status_html = """
        <p><strong>Google Calendar Connected</strong> ✅</p>
        <p>Your booking system is active.</p>
        """
    else:
        status_html = """
        <p>Connect your Google Calendar to start receiving bookings.</p>
        <a href="/connect-google">
            <button>Connect Google Calendar</button>
        </a>
        """

    return f"""
    <html>
        <head>
            <title>ContractorOS Dashboard</title>
        </head>

        <body style="font-family: Arial; padding:40px">

            <h1>ContractorOS Dashboard</h1>

            {status_html}

        </body>
    </html>
    """

@app.route("/onboard/<contractor_id>")
def onboard(contractor_id):

    # store contractor id in session
    session["oauth_contractor_key"] = contractor_id

    # send user to dashboard
    return redirect("/dashboard")




@app.route("/connect-google")
def connect_google():

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
    return redirect(authorization_url)


@app.route("/oauth/google/callback")
def google_callback():

    state = session.get("oauth_state")
    contractor_key = session.get("oauth_contractor_key")

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

    flow.fetch_token(authorization_response=request.url)

    credentials = flow.credentials

    access_token = credentials.token

    profile = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20
    ).json()

    google_email = profile.get("email", "")

    

    refresh_token = encrypt_text(credentials.refresh_token or "")
    calendar_id = "primary"

    # mark session as connected
    session["google_connected"] = True

    # save connection to Airtable
    if contractor_key:
        result = airtable_update_record(
            contractor_key,
            {
                "Google Connected": True,
                "Google Email": google_email,
                "Google Refresh Token": refresh_token or "",
                "Google Calendar ID": calendar_id
            }
        )
        print("GOOGLE OAUTH AIRTABLE UPDATE:", result)
    

    return redirect("/dashboard")


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

@app.route("/voice-entry", methods=["POST", "GET"])
def voice_entry():
    vr = VoiceResponse()
    vr.redirect("/voice-menu", method="POST")
    return Response(str(vr), mimetype="text/xml")

# ------------------------------
# VOICE: 4-question intake
# ------------------------------


@app.route("/voice", methods=["POST", "GET"])
def voice():
    vr = VoiceResponse()
    vr.pause(length=2)

    to_number = (request.values.get("To") or "").strip()
    contractor = {}
    try:
        contractor = get_contractor_by_twilio_number(to_number) or {}
    except Exception as e:
        print("CONTRACTOR LOOKUP FAILED:", e)

    business_name = (contractor.get("Business Name") or "our office").strip()
    greeting_name = (contractor.get("Greeting Name") or business_name).strip()

    gather = Gather(
        input="speech",
        action="/voice-intent",
        method="POST",
        timeout=6,
        speech_timeout="auto",
        profanity_filter=False

    )
    

    gather.say(
        f"Hi thanks for calling {greeting_name}.",
        voice="Polly.Joanna",
        language="en-US",
    )
    gather.pause(length=1.2)
    gather.say(
        "How can we help you today?",
        voice="Polly.Joanna",
        language="en-US",
    )

    vr.append(gather)

    # If silence / no speech result, still send to intent handler
    vr.redirect("/voice-intent", method="POST")
    return Response(str(vr), mimetype="text/xml")

@app.route("/voice-intent", methods=["POST", "GET"])
def voice_intent():
    vr = VoiceResponse()

    speech = (
        request.values.get("SpeechResult")
        or request.values.get("UnstableSpeechResult")
        or ""
    ).strip()

    confidence = (request.values.get("Confidence") or "").strip()
    to_number = (request.values.get("To") or "").strip()
    from_number = (request.values.get("From") or "").strip()

    call_sid = (request.values.get("CallSid") or "").strip()

    # Keep your existing contractor lookup
    contractor = {}
    try:
        contractor = get_contractor_by_twilio_number(to_number) or {}
    except Exception as e:
        print("CONTRACTOR LOOKUP FAILED IN INTENT:", e)

    business_name = (contractor.get("Business Name") or "our office").strip()
    greeting_name = (contractor.get("Greeting Name") or business_name).strip()

    # Use caller number as resume lookup key
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

    # Determine low-confidence speech
    low_confidence = False
    try:
        if confidence:
            low_confidence = float(confidence) < 0.45
    except Exception:
        low_confidence = False

    text = normalize_text(speech)
    intent = detect_call_intent(text)

    # Save what the caller initially asked for
    state = get_state(call_sid) or {}

    state = init_conversation_data(state)
    conversation_data = state["conversation_data"]

    state["service_hint"] = text

    # Try to extract caller name from the first sentence
    extracted_name = extract_name_from_text(text)
    if extracted_name and not conversation_data.get("name"):
        conversation_data["name"] = extracted_name

    # Save likely service from the caller's first request
    if text and not conversation_data.get("service"):
        conversation_data["service"] = text

    # --- Early parsing block (lightweight slot detection) ---
    text_lower = text.lower()

    # detect simple timing phrases
    if "today" in text_lower:
        state["timing_hint"] = "today"
        if not conversation_data.get("timing"):
            conversation_data["timing"] = "today"

    elif "tomorrow" in text_lower:
        state["timing_hint"] = "tomorrow"
        if not conversation_data.get("timing"):
            conversation_data["timing"] = "tomorrow"

    elif "this week" in text_lower:
        state["timing_hint"] = "this week"
        if not conversation_data.get("timing"):
            conversation_data["timing"] = "this week"

    elif "next week" in text_lower:
        state["timing_hint"] = "next week"
        if not conversation_data.get("timing"):
            conversation_data["timing"] = "next week"

    state["conversation_data"] = conversation_data
    set_state(call_sid, state)

    # ---------------------------------------------------------

    print(
        "VOICE INTENT DEBUG |",
        "Speech:", speech,
        "| Confidence:", confidence,
        "| Low confidence:", low_confidence,
        "| Intent:", intent,
        "| Resume old_call_sid:", old_call_sid,
        "| Resume step:", inferred_step
    )

    # If no usable speech, send to voicemail fallback
    if not text or low_confidence:
        try:
            send_fallback_sms(
                to_number=from_number,
                body=(
                    f"Thanks for calling {greeting_name}. "
                    "We had trouble capturing your request by phone. "
                    "Reply with your name, address, and a brief description of the work needed, "
                    "or leave a voicemail when prompted."
                )
            )
        except Exception as e:
            print("FALLBACK SMS ERROR |", e)

        vr.say(
            "I’m sorry, I had trouble understanding. "
            "Please leave a detailed message after the tone, and we will follow up shortly.",
            voice="Polly.Joanna",
            language="en-US",
        )

        vr.record(
            maxLength=120,
            playBeep=True,
            action="/twilio/voicemail",
            method="POST"
        )

        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    # If caller clearly wants voicemail
    if intent == "voicemail":
        vr.say(
            "No problem. Please leave your name, phone number, address if applicable, "
            "and a brief message after the tone.",
            voice="Polly.Joanna",
            language="en-US",
        )

        vr.record(
            maxLength=120,
            playBeep=True,
            action="/twilio/voicemail",
            method="POST"
        )

        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    # If caller sounds like emergency, still use your existing emergency route
    if intent == "emergency":
        # 1. The Humanized Acknowledgement
        vr.say(
            "I’m very sorry to hear that. I'm going to get you connected to our on-call contractor immediately so they can assist you.",
            voice="Polly.Joanna",
            language="en-US",
        )

        # 2. The "Safety" Pause (reduces the jarring transition)
        vr.pause(length=1)

        # 3. The redirect to the actual dialing logic 
        vr.redirect("/voice-emergency", method="POST")
        return Response(str(vr), mimetype="text/xml")

    # Otherwise this is a normal estimate / service request
    # Keep your existing resume logic
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
        return Response(str(vr), mimetype="text/xml")

    # No resume progress -> continue into recording consent then intake
    vr.say(
        "Absolutely. I’ll collect a few quick details.",
        voice="Polly.Joanna",
        language="en-US",
    )
    vr.redirect("/recording-consent?next=/voice-intake", method="POST")
    return Response(str(vr), mimetype="text/xml")
        

@app.route("/recording-consent", methods=["POST", "GET"])
def recording_consent():
    # where to go next after consent decision
    next_url = request.args.get("next", "/resume-check")

    call_sid = request.values.get("CallSid", "unknown")
    to_number = (request.values.get("To") or "").strip()

    contractor = get_contractor_by_twilio_number(to_number) or {}

    # If recording not enabled for this contractor, skip gate
    if not (bool(contractor.get("RECORD_CALLS")) or record_calls_default()):
        vr = VoiceResponse()
        vr.redirect(next_url, method="POST")
        return Response(str(vr), mimetype="text/xml")

    digits = (request.values.get("Digits") or "").strip()
    vr = VoiceResponse()

    # If no digits yet, ASK the question
    if digits == "":
        g = Gather(
            num_digits=1,
            action=f"/recording-consent?next={next_url}",
            method="POST",
            timeout=6,
            actionOnEmptyResult=True,
        )
        g.say(
            "This call may be recorded. "
            "Press 1 to continue with recording, or press 2 to continue without recording.",
            voice="Polly.Joanna",
            language="en-US",

        )
            
        vr.append(g)
        # silence = continue without recording
        vr.redirect(next_url, method="POST")
        return Response(str(vr), mimetype="text/xml")

    # If they choose no recording
    if digits == "2":
        vr.say(
            "Great. I’ll collect a few quick details, then we’ll text you a secure booking link to schedule a time.",
            voice="Polly.Joanna",
            language="en-US",
        )
        vr.redirect(next_url, method="POST")
        return Response(str(vr), mimetype="text/xml")

    # If they choose recording (digits == "1")
    if digits == "1":
        try:
            tc = twilio_client()
            
            if tc.get("ok"):
                rec = tc["client"].calls(call_sid).recordings.create(
                    recording_channels="dual",
                )    
                print("RECORDING STARTED | CallSid:", call_sid, "| RecordingSid:", rec.sid)
                vr.say(
                    "Great. I’ll collect a few quick details, then we’ll text you a secure booking link to schedule a time.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
            else:
                print("RECORDING ERROR |", tc.get("error"))
                vr.say("Recording is currently unavailable. Continuing without recording.", voice="Polly.Joanna", language="en-US")
        except Exception as e:
            print("RECORDING EXCEPTION |", e)
            vr.say("Recording is currently unavailable. Continuing without recording.", voice="Polly.Joanna", language="en-US")

        vr.redirect(next_url, method="POST")
        return Response(str(vr), mimetype="text/xml")

    # Any other key
    vr.say("No valid input received. Continuing without recording.", voice="Polly.Joanna", language="en-US")
    vr.redirect(next_url, method="POST")
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
    vr.redirect("/recording-consent?next=/voice-intake", method="POST")
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
        # Clear resume pointer 
        if redis_client and to_number and from_number:
            clear_resume_pointer(to_number, from_number)
            print("RESUME PTR CLEARED (restart):", to_number, from_number)

        # Clear any alias mapping for this new call
        clear_call_alias(new_call_sid)

        # If we know the old call, wipe its state too (prevents stale resume + stale Mapbox candidates)
        if old_call_sid:
            clear_state(old_call_sid)
            clear_call_alias(old_call_sid)

        vr.say("No problem. We'll start over.", voice="Polly.Joanna", language="en-US")
        vr.redirect("/recording-consent?next=/voice-intake", method="POST")
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

    vr.redirect("/recording-consent?next=/voice-intake", method="POST")
    return Response(str(vr), mimetype="text/xml")


@app.route("/twilio/voicemail", methods=["POST"])
def twilio_voicemail():
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    recording_url = request.values.get("RecordingUrl", "")
    recording_duration = request.values.get("RecordingDuration", "")

    print("Voicemail received:", call_sid, from_number, recording_url, recording_duration)
        
    #SEND CONFIRMATION TEXT
    try:
        to_number = (request.values.get("To") or "").strip()
        contractor = get_contractor_by_twilio_number(to_number) or {}
        
        business_name = (contractor.get("Business Name") or "our office").strip()
        greeting_name = (contractor.get("Greeting Name") or business_name).strip()

        sms_result = send_fallback_sms(
            to_number=from_number,
            body=(
                f"Thanks for calling {greeting_name}. "
                "We received your voicemail and will follow up as soon as possible."
            )
        )
        print("VOICEMAIL SMS RESULT |", sms_result)

    except Exception as e:
        print("VOICEMAIL SMS ERROR |", e)

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

    # Normalize To/From
    to_number = (request.values.get("To") or request.values.get("Called") or "").strip()
    from_number = (request.values.get("From") or request.values.get("Caller") or "").strip()

    contractor_key = to_number or "unknown"

    # IMPORTANT: preserve anything already saved in /voice-intent
    existing_state = get_state(call_sid) or {}

    state = {
        **existing_state,
        "step": 0,
        "callback": existing_state.get("callback") or from_number,
        "retries": 0,
        "name": existing_state.get("name", ""),
        "service_address": existing_state.get("service_address", ""),
        "job_description": existing_state.get("job_description", ""),
        "timing": existing_state.get("timing", ""),
        "call_sid": call_sid,
        "to_number": to_number,
        "contractor_key": contractor_key,
        "started_at": existing_state.get("started_at") or int(time.time()),
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
        "What's your name?",
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

    to_number = (request.values.get("To") or "").strip()
    contractor = get_contractor_by_twilio_number(to_number) or {}

    emergency_phone = (contractor.get("Emergency Phone") or "").strip()
    business_name = (contractor.get("Business Name") or "your business").strip()

    print("DEBUG To number:", to_number)
    print("DEBUG contractor:", contractor)
    print("DEBUG emergency_phone:", emergency_phone)
    print("DEBUG business_name:", business_name)

    if emergency_phone:
        vr.say(
            "Okay. Connecting you now.",
            voice="Polly.Joanna",
            language="en-US"
        )

        whisper_url = (
            request.url_root.rstrip("/")
            + "/emergency-whisper?biz="
            + urllib.parse.quote(business_name)
        )

        print("DEBUG whisper_url:", whisper_url)

        dial = vr.dial(
            timeout=20,
            caller_id=to_number,
            answer_on_bridge=True
        )

        dial.number(
            emergency_phone,
            url=whisper_url
        )

        return Response(str(vr), mimetype="text/xml")

    vr.say(
        "I am sorry we couldn't reach the on-call team. "
        "Please leave your name, address, and the nature of the emergency after the beep.",
        voice="Polly.Joanna",
        language="en-US"
    )

    vr.record(
        max_length=120,
        play_beep=True,
        action="/twilio/voicemail",
        method="POST"
    )

    vr.hangup()
    return Response(str(vr), mimetype="text/xml")
    
@app.route("/emergency-whisper", methods=["POST", "GET"])
def emergency_whisper():
    vr = VoiceResponse()

    biz_name = request.args.get("biz", "your business")

    gather = Gather(
        input="dtmf",
        num_digits=1,
        timeout=5,
        action="/emergency-whisper-connect",
        method="POST"
    )
    gather.say(
        f"Emergency call for {biz_name}. Press any key to connect.",
        voice="Polly.Joanna",
        language="en-US"
    )
    vr.append(gather)

    vr.say(
        "No input received. Goodbye.",
        voice="Polly.Joanna",
        language="en-US"
    )
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")

@app.route("/emergency-whisper-connect", methods=["POST"])
def emergency_whisper_connect():
    vr = VoiceResponse()
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
    state = get_state(call_sid) or {}


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

        # Check if we already captured the caller's name earlier
        state = init_conversation_data(state)
        conversation_data = state.get("conversation_data", {})

        saved_name = (conversation_data.get("name") or "").strip()

        if saved_name:
            state["name"] = saved_name
            state["name_confirmed"] = True
            state["step"] = 1
            state["retries"] = 0
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)

            vr.say(
                f"Thanks {saved_name}. Let's get the service address.",
                voice="Polly.Joanna",
                language="en-US",
            )

            vr.redirect("/voice-process?step=1", method="POST")
            return Response(str(vr), mimetype="text/xml")

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
            vr.redirect("/voice-process?step=0", method="POST")
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

    
    # STEP 1: Service address (fast + momentum)
    if step == 1:
        # Optional: very short intro ONCE (keeps your "intro once" behavior but removes narration)
        if not state.get("address_intro_played"):
            state["address_intro_played"] = True
            state["retries"] = 0
            set_state(call_sid, state)

            vr.say(
                "Alright — let’s get the service address.",
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
                    timeout=5,                  # was 10
                    finishOnKey="#",            
                    barge_in=True,              # feels faster 
                    actionOnEmptyResult=True,   # don't stall on silence 
                )
                gather.say(
                    "Whats the house number? Enter it, then press pound.",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            house_num = "".join([c for c in digits if c.isdigit()]).strip()
            if len(house_num) < 1:
                vr.say(
                    "Sorry - house number only. Enter it, then press pound.",
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
                    timeout=6,                  # was 8
                    speech_timeout="auto",
                    barge_in=True,              # feels faster             
                    actionOnEmptyResult=True,
                    profanity_filter=False,
                    
                )
                gather.say(
                    "Okay, please say the street name?",
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
                    timeout=6,                 # was 8
                    speech_timeout="auto",
                    barge_in=True,              # feels faster
                    actionOnEmptyResult=True,
                    profanity_filter=False,
                    hints="Bowie, Upper Marlboro, Lanham, Crofton, Washington, Baltimore",  
                )
                gather.say(
                    "Perfect. What city is that in?",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            state["addr_city"] = speech.strip()

            corrections = airtable_get_city_corrections()
            state["addr_city"] = normalize_city(state["addr_city"], corrections)
            print("CITY NORMALIZED |", state["addr_city"])
            
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
                    timeout=5,                 # was 10
                    barge_in=True,             # feels faster
                    actionOnEmptyResult=True,
                )
                gather.say(
                    "Got it, please enter the five digit zip code.",
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


         # 1E) Mapbox resolve + confirm (DTMF)
        if not state.get("addr_confirmed"):
            # If we don't have candidates yet, fetch them once
            if not state.get("addr_candidates"):
                # Build query in US recommended format (include state if you want; MD helps)
                q = f"{state['addr_number']} {state['addr_street']} {state['addr_city']} MD {state['addr_zip']}"
                print("MAPBOX LOOKUP |", q)

                geo = mapbox_geocode_one(q, country="US")
                print("MAPBOX GEO ONE |", geo)

                state["addr_candidates"] = []

                if geo.get("ok") and geo.get("feature"):
                    feature = geo["feature"]

                    contractor = get_contractor_by_twilio_number(to_number) or {}
                    allowed, reason = address_in_service_area(
                        contractor,
                        feature.get("lat"),
                        feature.get("lon"),
                    )
                    print("SERVICE AREA CHECK |", allowed, "|", reason)

                    if allowed:
                        state["addr_candidates"] = [feature["place_name"]]
                    else:
                        vr.say(
                            "Sorry, that address appears to be outside our normal service area.",
                            voice="Polly.Joanna",
                            language="en-US",
                        )
                        state.pop("addr_candidates", None)
                        state.pop("addr_confirmed", None)
                        state.pop("addr_street", None)
                        state["retries"] = 0
                        set_state(call_sid, state)
                        vr.redirect("/voice-process?step=1", method="POST")
                        return Response(str(vr), mimetype="text/xml")

                print("MAPBOX CANDIDATES |", state["addr_candidates"])
                set_state(call_sid, state)

                # If none returned, do NOT auto-confirm 
                if not state["addr_candidates"]:
                    vr.say(
                        "Sorry, I could not confirm that address. Let's try the street name again.",
                        voice="Polly.Joanna",
                        language="en-US",
                    )

                    state.pop("addr_candidates", None)
                    state.pop("addr_confirmed", None)
                    state.pop("addr_street", None)
                    state["retries"] = 0
                    set_state(call_sid, state)
                    
                    vr.redirect("/voice-process?step=1", method="POST")
                    return Response(str(vr), mimetype="text/xml")

            # We have candidates; ask user to pick
            if not digits:
                opts = state["addr_candidates"]
                gather = Gather(
                    input="dtmf",
                    num_digits=1,
                    action="/voice-process?step=1",
                    method="POST",
                    timeout=10,
                )

                # Read top options
                prompt = "I found a few possible matches. "
                for i, a in enumerate(opts, start=1):
                    prompt += f"Press {i} for {a}. "
                prompt += "Or press 9 if none of these are correct."

                gather.say(prompt, voice="Polly.Joanna", language="en-US")
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            choice = "".join([c for c in digits if c.isdigit()]).strip()

            if choice == "9":
                # They said none match -> try again (do NOT confirm)
                state.pop("addr_candidates", None)
                state.pop("addr_confirmed", None)

                # re-ask street (best) - keep house number & zip since those are solid
                state.pop("addr_street", None)

                state["retries"] = 0
                set_state(call_sid, state)

                vr.say("No problem. Let's try the street name again.", voice="Polly.Joanna", language="en-US")
                vr.redirect("/voice-process?step=1", method="POST")
                return Response(str(vr), mimetype="text/xml")

            try:
                idx = int(choice) - 1
            except ValueError:
                idx = -1

            opts = state.get("addr_candidates") or []
            if idx < 0 or idx >= len(opts):
                vr.say("Sorry, I didn’t get that. Please press 1, 2, or 3. Or 9 for none.", voice="Polly.Joanna", language="en-US")
                vr.redirect("/voice-process?step=1", method="POST")
                return Response(str(vr), mimetype="text/xml")

            selected = opts[idx]

            geo = mapbox_geocode_one(selected, country="US")
            print("MAPBOX GEO ONE |", geo)

            if geo.get("ok") and geo.get("feature"):
                feature = geo["feature"]

                contractor = get_contractor_by_twilio_number(to_number) or {}
                allowed, reason = address_in_service_area(
                    contractor,
                    feature.get("lat"),
                    feature.get("lon"),
                )

                print("SERVICE AREA CHECK |", allowed, "|", reason)

                if not allowed:
                    vr.say(
                        "Sorry, that address appears to be outside our normal service area.",
                        voice="Polly.Joanna",
                        language="en-US",
                    )

                    state.pop("addr_candidates", None)
                    state.pop("addr_confirmed", None)
                    state.pop("addr_street", None)
                    state["retries"] = 0
                    set_state(call_sid, state)

                    vr.redirect("/voice-process?step=1", method="POST")
                    return Response(str(vr), mimetype="text/xml")

            state["service_address"] = selected
            state["addr_confirmed"] = True
            state["step"] = 1
            state["retries"] = 0
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)

            service_hint = (state.get("service_hint") or "").strip()
            timing_hint = (state.get("timing") or "").strip()
            callback_hint = (state.get("callback_number") or "").strip()

            # Address confirmed -> ask the next missing thing immediately
            if not service_hint:
                state["step"] = 2
                set_state(call_sid, state)

                gather = Gather(
                    input="speech",
                    action="/voice-process?step=2",
                    method="POST",
                    timeout=6,
                    speech_timeout="auto",
                    barge_in=True,
                    actionOnEmptyResult=True,
                    profanity_filter=False,
                )
                gather.say(
                    f"Thanks, I have the address as {selected}. Can you briefly describe the service you need?",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            if not timing_hint:
                state["step"] = 3
                set_state(call_sid, state)

                gather = Gather(
                    input="speech",
                    action="/voice-process?step=3",
                    method="POST",
                    timeout=6,
                    speech_timeout="auto",
                    barge_in=True,
                    actionOnEmptyResult=True,
                    profanity_filter=False,
                )
                gather.say(
                    f"Thanks, I have the address as {selected}. Got it — you're looking for {service_hint}. When would you like that done?",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            if not callback_hint:
                state["step"] = 4
                set_state(call_sid, state)

                gather = Gather(
                    input="speech dtmf",
                    action="/voice-process?step=4",
                    method="POST",
                    timeout=6,
                    speech_timeout="auto",
                    barge_in=True,
                    actionOnEmptyResult=True,
                    profanity_filter=False,
                    finishOnKey="#",
                )
                gather.say(
                    f"Thanks, I have the address as {selected}. Got it — you're looking for {service_hint}, and you need it {timing_hint}. What's the best callback number for you?",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            # If everything is already known, move to step 4 handler or finish logic
            state["step"] = 4
            set_state(call_sid, state)
            vr.redirect("/voice-process?step=4", method="POST")
            return Response(str(vr), mimetype="text/xml")



    # STEP 2: Job description + confirm/repeat
    if step == 2:
        # Pull inputs safely
        speech = (request.values.get("SpeechResult") or request.values.get("speech") or "").strip()
        digits = (request.values.get("Digits") or "").strip()

        # If we don't have a job description yet, ask for it
        if not state.get("job_description"):
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
                    action="/voice-process?step=2",
                    method="POST",
                    timeout=6,
                    speech_timeout="auto",
                    barge_in=True,
                    actionOnEmptyResult=True,
                    profanity_filter=False,
                    speech_model="phone_call",
                    hints="lawn care,mowing,cleanout,junk removal,mulch,landscaping,pressure washing,leaf cleanup,painting,drywall,plumbing,handyman",
                )
                gather.say(
                    "Can you briefly describe the service you need?",
                    voice="Polly.Joanna",
                    language="en-US",
                )
                vr.append(gather)
                return Response(str(vr), mimetype="text/xml")

            # Speech exists -> save it and confirm by DTMF
            state["job_description"] = speech
            state["service_hint"] = speech
            state["retries"] = 0
            state["step"] = 2
            set_state(call_sid, state)

            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, call_sid)
                print("RESUME PTR SAVED (after job desc):", to_number, from_number, call_sid, "state.step=", state["step"])

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
            vr.redirect("/voice-process?step=2", method="POST")
            return Response(str(vr), mimetype="text/xml")

        # We already have a job description -> waiting for DTMF confirm
        if not digits:
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
            vr.redirect("/voice-process?step=2", method="POST")
            return Response(str(vr), mimetype="text/xml")

        if digits == "2":
            state.pop("job_description", None)
            state.pop("service_hint", None)
            state["retries"] = 0
            set_state(call_sid, state)
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

        # Any other key -> reprompt
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
        vr.redirect("/voice-process?step=2", method="POST")
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

            service_hint = (state.get("service_hint") or state.get("job_description") or "").strip()

            gather = Gather(
                input="speech",
                action="/voice-process?step=3",
                method="POST",
                timeout=8,
                speech_timeout="auto",
                profanity_filter=False,
            )

            if service_hint:
                prompt = f"Got it — you're looking for {service_hint}. When would you like that done?"
            else:
                prompt = "Please tell me when you need the service."

            gather.say(
                prompt,
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # Speech exists -> save timing, move to step 4
        state["timing"] = speech.strip()
        state["timing_hint"] = speech.strip()
        state["retries"] = 0
        state["step"] = 4
        set_state(call_sid, state)

        if redis_client and to_number and from_number:
            save_resume_pointer(to_number, from_number, call_sid)
            print("RESUME PTR SAVED (after timing):", to_number, from_number, call_sid, "state.step=", state["step"])

        service_hint = (state.get("service_hint") or state.get("job_description") or "").strip()

        gather = Gather(
            input="speech dtmf",
            action="/voice-process?step=4",
            method="POST",
            timeout=8,
            speech_timeout="auto",
            finishOnKey="#",
        )

        if service_hint:
            prompt = f"Got it — you're looking for {service_hint}, and you need it {speech.strip()}. What's the best callback phone number?"
        else:
            prompt = "What is the best callback phone number?"

        gather.say(
            prompt,
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
                input="speech dtmf",
                action="/voice-process?step=4",
                method="POST",
                timeout=8,
                speech_timeout="auto",
                profanity_filter=False,
                finishOnKey="#",
            )
            gather.say(
                "I didn't catch that. Please say or enter the best callback phone number.",
                voice="Polly.Joanna",
                language="en-US",
            )
            vr.append(gather)
            return Response(str(vr), mimetype="text/xml")

        # Normalize to digits only
        callback_digits = "".join([c for c in callback_val if c.isdigit()])

        # If too short, fall back to caller ID
        if len(callback_digits) < 7:
            fallback_from = (request.values.get("From", "") or "").strip()
            callback_digits = "".join([c for c in fallback_from if c.isdigit()])

        state["callback"] = callback_digits
        state["callback_number"] = callback_digits
        set_state(call_sid, state)

        if redis_client and to_number and from_number:
            save_resume_pointer(to_number, from_number, call_sid)

        # Pull per-contractor email routing
        contractor = get_contractor_by_twilio_number(to_number) or {}
        business_name = (contractor.get("Business Name") or "our office").strip()

        notify_email = (contractor.get("Notify Email") or os.getenv("TO_EMAIL") or "").strip() or None
        reply_to_email = (contractor.get("Reply to Email") or "").strip() or None

        try:
            send_intake_summary(state, notify_email=notify_email, reply_to_email=reply_to_email)
        except Exception as e:
            print("send_intake_summary failed:", e)

        # Build booking link
        if not state.get("callback") and from_number:
            state["callback"] = from_number

        booking_link = build_cal_booking_link(contractor, state)
        print("CAL BOOKING LINK:", booking_link)

        # Send SMS confirmation
        try:
            sms_result = twilio_client()

            if not sms_result.get("ok"):
                print("SMS send skipped:", sms_result.get("error"))
            else:
                client = sms_result["client"]

                sms_to = state.get("callback", "") or from_number or ""
                sms_to_digits = "".join([c for c in sms_to if c.isdigit()])

                if sms_to_digits:
                    if len(sms_to_digits) == 10:
                        sms_to = f"+1{sms_to_digits}"
                    elif len(sms_to_digits) == 11 and sms_to_digits.startswith("1"):
                        sms_to = f"+{sms_to_digits}"
                    elif sms_to.startswith("+"):
                        sms_to = sms_to
                    else:
                        sms_to = f"+{sms_to_digits}"

                    if booking_link:
                        sms_body = (
                            f"Thanks for contacting {business_name}. "
                            "We've got your project details. "
                            f"Use this secure booking link to review your information, make any corrections, and choose a time for your estimate: {booking_link} "
                            "Reply STOP to opt out."
                        )
                    else:
                        sms_body = (
                            f"Thanks for contacting {business_name}. "
                            "We received your request and will follow up shortly. "
                            "Reply STOP to opt out."
                        )

                    msg = client.messages.create(
                        body=sms_body,
                        from_=to_number,
                        to=sms_to,
                    )

                    print("SMS SENT TO:", sms_to, "| SID:", msg.sid)
                else:
                    print("SMS SKIPPED: no valid callback number")
        except Exception as e:
            print("SMS send failed:", e)

        if redis_client and to_number and from_number:
            clear_resume_pointer(to_number, from_number)
            print("RESUME PTR CLEARED:", to_number, from_number)

        print(
            "CALL COMPLETE|",
            "CallSid:", call_sid,
            "| Name:", state.get("name"),
            "| Address:", state.get("service_address"),
            "| City:", state.get("addr_city"),
            "| Zip:", state.get("addr_zip"),
            "| Callback:", state.get("callback"),
        )

        unregister_live_call(state.get("contractor_key", "unknown"), call_sid)
        clear_state(call_sid)

        vr.say(
            "Perfect. I've recorded all those details. Keep an eye out for a text message "
            "shortly with your secure booking link. Thanks for choosing us. Goodbye!",
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
