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

from app.app.photo_service import (
    upload_photo,
    analyze_photos_with_claude,
    build_photo_upload_link,
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


def sms_enabled() -> bool:
    return os.getenv("SMS_ENABLED", "false").lower() == "true"


def send_sms(to_number: str, body: str, from_number: str) -> dict:
    if not sms_enabled():
        print("SMS_DISABLED | Would have sent:", to_number, "|", body)
        return {"ok": False, "disabled": True}

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        return {"ok": False, "error": "missing_credentials"}

    client = Client(account_sid, auth_token)
    msg = client.messages.create(to=to_number, from_=from_number, body=body)
    print("SMS_SENT:", msg.sid)
    return {"ok": True, "sid": msg.sid}


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
 
    # Save the new lead's Airtable record ID back to state
    # so finalize_lead() can use it for the photo upload link
    if airtable_result.get("ok"):
        lead_id = airtable_result.get("data", {}).get("id", "")
        state["lead_airtable_id"] = lead_id
        print("LEAD AIRTABLE ID SAVED |", lead_id)

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
        
    params = request.args.to_dict(flat=False)
    params.pop("c", None)
    query_string = urllib.parse.urlencode(params, doseq=True)
    separator = "&" if "?" in base_url else "?"

    
    print("BOOK DEBUG | raw request args:", request.args)
    print("BOOK DEBUG | params dict:", params)
    print("BOOK DEBUG | query string:", query_string)
    print("BOOK DEBUG | base url:", base_url)

    final_url = f"{base_url}{separator}{query_string}" if query_string else base_url

    print("BOOK DEBUG | redirect final:", final_url)
    
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

@app.route("/cal-webhook", methods=["POST"])
def cal_webhook():
    try:
        data = request.get_json(silent=True) or {}
        print("CAL WEBHOOK RECEIVED:", data)
        trigger = (data.get("triggerEvent") or data.get("event") or "").upper()
        payload = data.get("payload", {}) or {}
        responses = payload.get("responses", {}) or {}

        def response_value(key: str) -> str:
            item = responses.get(key, "")
            if isinstance(item, dict):
                return str(item.get("value") or "").strip()
            return str(item or "").strip()

        if trigger in {"BOOKING_CREATED", "BOOKING.CREATED"}:
            attendee = payload.get("attendees", [])
            attendee0 = attendee[0] if attendee and isinstance(attendee[0], dict) else {}
            name = (
                response_value("name")
                or str(attendee0.get("name") or "")
            ).strip()
            phone = (
                response_value("attendeePhoneNumber")
                or str(attendee0.get("phoneNumber") or "")
                or str(attendee0.get("phone") or "")
            ).strip()
            start_time = str(
                payload.get("startTime")
                or payload.get("start")
                or ""
            ).strip()

            print("WEBHOOK PARSED | name:", name, "| phone:", phone, "| start:", start_time)

            if phone:
                try:
                    from zoneinfo import ZoneInfo
                    dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                    eastern = dt.astimezone(ZoneInfo("America/New_York"))
                    formatted_time = eastern.strftime("%A, %B %-d at %-I:%M %p")
                except Exception:
                    formatted_time = start_time

                sms_result = send_fallback_sms(
                    to_number=phone,
                    body=f"Hi {name or 'there'}, your estimate is confirmed for {formatted_time}. We'll see you then!"
                )
                print("WEBHOOK SMS RESULT:", sms_result)
            else:
                print("WEBHOOK NOTICE | No phone found in payload")

        return "", 200

    except Exception as e:
        print("WEBHOOK ERROR:", e)
        return "", 500
  


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

# =============================================================================
# PHOTO  ROUTES


@app.route("/upload-photos/<lead_id>", methods=["GET"])
def photo_upload_page(lead_id):
    """
    Customer-facing photo upload page.
    Uses plain HTML form — no JavaScript fetch needed.
    Works on all browsers including iOS Safari.
    """
    html = """
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Upload Job Photos</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
                background: #f4f6f9;
                color: #111827;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }
            .card {
                background: #fff;
                border-radius: 16px;
                padding: 32px 24px;
                max-width: 480px;
                width: 100%;
                box-shadow: 0 4px 24px rgba(0,0,0,0.07);
            }
            .logo {
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.1em;
                text-transform: uppercase;
                color: #2563eb;
                margin-bottom: 20px;
            }
            h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; }
            .sub {
                font-size: 15px;
                color: #6b7280;
                margin-bottom: 28px;
                line-height: 1.5;
            }
            .file-label {
                display: block;
                border: 2px dashed #d1d5db;
                border-radius: 12px;
                padding: 32px 20px;
                text-align: center;
                margin-bottom: 20px;
                cursor: pointer;
                transition: border-color 0.2s;
            }
            .file-label:hover { border-color: #2563eb; }
            .file-icon { font-size: 40px; margin-bottom: 12px; }
            .file-text { font-size: 15px; color: #374151; font-weight: 500; margin-bottom: 6px; }
            .file-hint { font-size: 13px; color: #9ca3af; }
            input[type="file"] { display: none; }
            .btn {
                display: block;
                width: 100%;
                background: #2563eb;
                color: #fff;
                border: none;
                font-weight: 700;
                padding: 16px;
                border-radius: 10px;
                font-size: 16px;
                cursor: pointer;
                margin-bottom: 12px;
                text-align: center;
            }
            .btn:hover { background: #1d4ed8; }
            .btn-skip {
                display: block;
                width: 100%;
                background: none;
                color: #6b7280;
                border: none;
                font-size: 14px;
                cursor: pointer;
                padding: 8px;
                text-align: center;
                text-decoration: none;
            }
            .selected-info {
                background: #eff6ff;
                border-radius: 8px;
                padding: 10px 14px;
                font-size: 14px;
                color: #2563eb;
                margin-bottom: 16px;
                display: none;
            }
            .divider {
                border: none;
                border-top: 1px solid #f3f4f6;
                margin: 20px 0;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <div class="logo">ContractorOS</div>
            <h1>Upload Job Photos</h1>
            <p class="sub">
                Help us prepare a better estimate by sharing photos of the work area.
                Up to 5 photos accepted.
            </p>

            <form method="POST" action="/process-photos" enctype="multipart/form-data">
                <input type="hidden" name="lead_id" value="{{ lead_id }}">

                <label class="file-label" for="photos">
                    <div class="file-icon">📷</div>
                    <div class="file-text">Tap to select photos</div>
                    <div class="file-hint">JPG, PNG, HEIC — up to 5 photos</div>
                </label>

                <input
                    type="file"
                    id="photos"
                    name="photos"
                    accept="image/*"
                    multiple
                    onchange="showSelected(this)"
                >

                <div class="selected-info" id="selected-info"></div>

                <button type="submit" class="btn">Send Photos</button>
            </form>

            <hr class="divider">
            <a href="/upload-photos/{{ lead_id }}/skip" class="btn-skip">
                Skip — I'll discuss details at the estimate
            </a>
        </div>

        <script>
            function showSelected(input) {
                const info = document.getElementById('selected-info');
                if (input.files && input.files.length > 0) {
                    const count = Math.min(input.files.length, 5);
                    info.textContent = count + ' photo' + (count !== 1 ? 's' : '') + ' selected — tap Send Photos to upload';
                    info.style.display = 'block';
                }
            }
        </script>
    </body>
    </html>
    """
    return render_template_string(html, lead_id=lead_id)


@app.route("/upload-photos/<lead_id>/skip", methods=["GET"])
def photo_upload_skip(lead_id):
    """Caller chose to skip photo upload."""
    html = """
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>All Set</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
                background: #f4f6f9; min-height: 100vh;
                display: flex; align-items: center; justify-content: center; padding: 20px;
            }
            .card {
                background: #fff; border-radius: 16px; padding: 40px 24px;
                max-width: 480px; width: 100%; text-align: center;
                box-shadow: 0 4px 24px rgba(0,0,0,0.07);
            }
            .icon { font-size: 48px; margin-bottom: 16px; }
            h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; }
            p { font-size: 15px; color: #6b7280; line-height: 1.5; }
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon">✓</div>
            <h1>All set!</h1>
            <p>We'll be in touch to confirm your estimate appointment.</p>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)
        
                  
@app.route("/process-photos", methods=["POST"])
def process_photos():
    """
    Receives uploaded photos, saves to Cloudinary,
    runs Claude Vision analysis, emails contractor.
    """
    from app.app.photo_service import upload_photo, analyze_photos_with_claude
    from app.app.airtable_service import airtable_get_record, airtable_update_record

    lead_id = request.form.get("lead_id", "unknown")
    print("PHOTO UPLOAD | lead_id:", lead_id)

    # Collect uploaded files
    uploaded_urls = []
    
    photo_files = request.files.getlist("photos")
    for index, file in enumerate(photo_files[:5]):
        if file and file.filename:
            file_data = file.read()
            result = upload_photo(file_data, lead_id, index)
        if result.get("ok"):
                uploaded_urls.append(result["url"])

    print("PHOTOS UPLOADED |", len(uploaded_urls), "photos")

    if not uploaded_urls:
        return jsonify({"ok": False, "error": "no_photos_uploaded"})

    # Get lead details from Airtable
    lead = {}
    try:
        lead_result = airtable_get_record(lead_id, table_name="Leads")
        if lead_result.get("ok"):
            lead = lead_result.get("fields", {})
    except Exception as e:
        print("LEAD LOOKUP ERROR |", e)

    job_description = lead.get("Job Description", "general contractor work")
    client_name = lead.get("Client Name", "Customer")
    service_address = lead.get("Service Address", "")

    # Run Claude Vision analysis
    analysis = analyze_photos_with_claude(
        photo_urls=uploaded_urls,
        job_description=job_description,
        contractor_name=lead.get("Business Name", "our team"),
        client_name=client_name,
        service_address=service_address,
    
    )
    print("CLAUDE VISION ANALYSIS |", analysis)

    # Update lead in Airtable with AI analysis
    if analysis.get("ok"):
        try:
            airtable_update_record(
                lead_id,
                {
                    "AI Scope Summary": analysis.get("summary", ""),
                    "AI Estimate Range": analysis.get("estimate_range", ""),
                    "AI Full Analysis": analysis.get("full_analysis", ""),
                    "Photo URLs": "\n".join(uploaded_urls),
                },
                table_name="Leads",
            )
        except Exception as e:
            print("AIRTABLE UPDATE ERROR |", e)

 
    # Email contractor with photos + AI analysis
    try:
        photo_links = "\n".join([
            f"  Photo {i+1}: {url}"
            for i, url in enumerate(uploaded_urls)
        ])
 
        # Get contractor notify email from Airtable
        try:
            contractor = get_contractor_by_twilio_number(
                lead.get("Twilio Number", "") or ""
            )
            notify_email = (
                contractor.get("Notify Email")
                or os.getenv("TO_EMAIL", "")
            ).strip()
        except Exception:
            notify_email = os.getenv("TO_EMAIL", "")
 
        if analysis.get("ok"):
 
            # ── Email 1: Internal contractor analysis ──────────────────────
            internal_body = (
                f"Job photos received from {client_name}\n"
                f"Address: {service_address}\n"
                f"Job: {job_description}\n\n"
                f"{'━' * 40}\n"
                f"CONTRACTOR INTERNAL NOTES\n"
                f"{'━' * 40}\n\n"
                f"{analysis.get('internal_analysis', '')}\n\n"
                f"{'━' * 40}\n"
                f"PHOTO LINKS\n"
                f"{'━' * 40}\n"
                f"{photo_links}\n\n"
                f"Lead ID: {lead_id}"
            )
 
            send_email(
                subject=f"📸 Job Photos + AI Analysis — {client_name}",
                body=internal_body,
                to_email=notify_email,
            )
            print("INTERNAL ANALYSIS EMAIL SENT |", notify_email)
 
            # ── Email 2: Customer-ready estimate (separate email) ──────────
            customer_estimate = analysis.get("customer_estimate", "")
            customer_subject = analysis.get("customer_subject", f"Your Estimate — {job_description}")
 
            if customer_estimate:
                customer_ready_body = (
                    f"━━━ CUSTOMER-READY ESTIMATE ━━━\n"
                    f"Copy and forward this directly to {client_name}:\n\n"
                    f"Subject: {customer_subject}\n\n"
                    f"{customer_estimate}\n\n"
                    f"━━━ END OF CUSTOMER EMAIL ━━━"
                )
 
                send_email(
                    subject=f"📋 Forward to Customer — {client_name}",
                    body=customer_ready_body,
                    to_email=notify_email,
                )
                print("CUSTOMER ESTIMATE EMAIL SENT |", notify_email)
 
        else:
            # Analysis failed — send basic photo notification
            basic_body = (
                f"Job photos received from {client_name}\n"
                f"Address: {service_address}\n"
                f"Job: {job_description}\n\n"
                f"Photos:\n{photo_links}\n\n"
                f"Lead ID: {lead_id}"
            )
            send_email(
                subject=f"📸 Job Photos — {client_name}",
                body=basic_body,
                to_email=notify_email,
            )
            print("BASIC PHOTO EMAIL SENT |", notify_email)
 
    except Exception as e:
        print("PHOTO EMAIL ERROR |", e)
   

    return jsonify({
        "ok": True,
        "photos": len(uploaded_urls),
        "analysis": analysis.get("ok", False),
    })


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
