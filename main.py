# main.py — CrewCachePro AI Bot
# Architecture: ConversationRelay + Claude Haiku (conversation.py)
# Legacy step-based flow moved to branch: legacy-voice-stepflow

import os
import re
import json
import math
import time
import base64
import urllib.parse
import cloudinary
import cloudinary.uploader
from datetime import datetime, timezone, date

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

from app.app.photo_service import (
    upload_photo,
    analyze_photos_with_claude,
    build_photo_upload_link,
)

from app.app.quickbooks_service import (
    save_qb_tokens, get_valid_access_token, create_qb_invoice, 
    is_qb_connected, QB_CLIENT_ID, QB_REDIRECT_URI, QB_AUTH_URL, 
    QB_TOKEN_URL, QB_SCOPES, QB_CLIENT_SECRET,
)
from base64 import b64encode
import secrets

from follow_up_scheduler import start_scheduler
start_scheduler()

# ── Standard library & PDF imports ────────────────────────────────
import urllib.parse
import io

# ── ReportLab PDF generation ───────────────────────────────────────
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER

# ─────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")

from app.app.conversation import conversation_bp, init_sock, get_claude_client
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

# ─────────────────────────────────────────────
# Cache management
# ─────────────────────────────────────────────
@app.route("/flush-contractor-cache/<twilio_number>", methods=["GET"])
def flush_contractor_cache(twilio_number):
    cache_key = f"mmeai:contractor_cache:{twilio_number}"
    if redis_client:
        redis_client.delete(cache_key)
        return jsonify({"ok": True, "flushed": cache_key})
    return jsonify({"ok": False, "error": "no redis"})

@app.route("/flush-sms-state", methods=["GET"])
def flush_sms_state():
    to_number = request.args.get("to", "")
    from_number = request.args.get("from", "")
    key = f"sms_state:{to_number}:{from_number}"
    if redis_client:
        redis_client.delete(key)
        return jsonify({"ok": True, "flushed": key})
    return jsonify({"ok": False, "error": "no redis"})

@app.route("/flush-all-sms", methods=["GET"])
def flush_all_sms():
    if redis_client:
        keys = redis_client.keys("sms_state:*")
        if keys:
            redis_client.delete(*keys)
        return jsonify({"ok": True, "cleared": len(keys)})
    return jsonify({"ok": False, "error": "no redis"})


# ── Step 1: Initiate QuickBooks OAuth connection ──────────────────────────
@app.route("/quickbooks/connect", methods=["GET"])
def quickbooks_connect():
    state = secrets.token_urlsafe(16)
    if redis_client:
        redis_client.setex(f"qb_oauth_state:{state}", 600, "pending")
 
    params = urllib.parse.urlencode({
        "client_id":     QB_CLIENT_ID,
        "redirect_uri":  QB_REDIRECT_URI,
        "response_type": "code",
        "scope":         QB_SCOPES,
        "state":         state,
    })
    auth_url = f"{QB_AUTH_URL}?{params}"
    print("QB OAUTH REDIRECT |", auth_url)
    return redirect(auth_url, code=302)
 
 
# ── Step 2: QuickBooks OAuth callback ─────────────────────────────────────
@app.route("/quickbooks/callback", methods=["GET"])
def quickbooks_callback():
    code     = request.args.get("code", "")
    state    = request.args.get("state", "")
    realm_id = request.args.get("realmId", "")
    error    = request.args.get("error", "")
 
    if error:
        print("QB OAUTH ERROR |", error)
        return f"QuickBooks connection failed: {error}", 400
 
    # Verify state — log mismatch but don't block
    if redis_client:
        stored = redis_client.get(f"qb_oauth_state:{state}")
        if not stored:
            print("QB OAUTH STATE MISMATCH | continuing anyway |", state)
        else:
            redis_client.delete(f"qb_oauth_state:{state}")
 
    # Exchange code for tokens
    credentials = b64encode(f"{QB_CLIENT_ID}:{QB_CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type":  "application/x-www-form-urlencoded",
        "Accept":        "application/json",
    }
    data = {
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": QB_REDIRECT_URI,
    }
    try:
        r = requests.post(QB_TOKEN_URL, headers=headers, data=data, timeout=15)
        if r.status_code != 200:
            print("QB TOKEN EXCHANGE ERROR |", r.status_code, r.text)
            return f"Token exchange failed: {r.text}", 400
 
        token_data = r.json()
        save_qb_tokens(
            realm_id=realm_id,
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_in=token_data.get("expires_in", 3600),
        )
        print("QB CONNECTED | realm_id:", realm_id)
        return render_template_string('''
            <!doctype html><html><head>
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <style>
                body{font-family:Arial,sans-serif;display:flex;align-items:center;
                     justify-content:center;min-height:100vh;background:#f0f4f8;margin:0}
                .card{background:#fff;border-radius:16px;padding:40px;text-align:center;
                      max-width:400px;box-shadow:0 4px 24px rgba(0,0,0,.08)}
                .icon{font-size:48px;margin-bottom:16px}
                h1{color:#1A4D2E;margin:0 0 12px}
                p{color:#555;line-height:1.6}
            </style></head><body>
            <div class="card">
                <div class="icon">✅</div>
                <h1>QuickBooks Connected!</h1>
                <p>CrewCachePro is now linked to your QuickBooks Online account.
                   Invoices will be created automatically when jobs are marked complete.</p>
            </div></body></html>
        ''')
    except Exception as e:
        print("QB CALLBACK EXCEPTION |", e)
        return f"Connection error: {e}", 500
 
 
# ── Step 3: QuickBooks connection status ──────────────────────────────────
@app.route("/quickbooks/status", methods=["GET"])
def quickbooks_status():
    connected = is_qb_connected()
    return jsonify({
        "connected": connected,
        "message": "QuickBooks Online connected" if connected else "Not connected — visit /quickbooks/connect"
    })
 
 
# ── Step 4: Airtable webhook — auto-create invoice when job completed ─────
@app.route("/airtable/job-complete", methods=["POST"])
def airtable_job_complete():
    
    try:
        data = request.get_json(force=True) or {}
        print("AIRTABLE WEBHOOK | job complete |", data)
 
        # Airtable sends the record fields in the webhook body
        record  = data.get("record", {}) or data
        fields  = record.get("fields", {}) or record
 
        state = {
            "name":            fields.get("Client Name", ""),
            "service_address": fields.get("Service Address", ""),
            "job_description": fields.get("Job Description", ""),
            "callback":        fields.get("Call Back Number", ""),
            "timing":          fields.get("Appointment Date and Time", ""),
            "client_email":    fields.get("Client Email", ""),
            "estimate_amount": fields.get("Estimate Amount", 0.00),
            "contractor_key":  fields.get("Contractor Twilio Number", ""),
            "to_number":       fields.get("Contractor Twilio Number", ""),
        }
            
 
        if not state["name"]:
            return jsonify({"ok": False, "error": "No client name in webhook payload"}), 400
 
        # Create QuickBooks invoice
        result = create_qb_invoice(state)
        print("QB INVOICE RESULT |", result)
 
        if result.get("ok"):
            # SMS alert to contractor
            contractor_number = state.get("contractor_key", "") or state.get("to_number", "")
            notify_sms = ""
            if contractor_number:
                try:
                    contractor = get_contractor_by_twilio_number(contractor_number) or {}
                    notify_sms = (contractor.get("Notify SMS") or "").strip()
                except Exception:
                    pass
 
            if notify_sms and contractor_number:
                try:
                    Client(
                        os.getenv("TWILIO_ACCOUNT_SID"),
                        os.getenv("TWILIO_AUTH_TOKEN")
                    ).messages.create(
                        body=(
                            f"📋 Invoice Ready — {state['name']}\n"
                            f"Job: {state['job_description']}\n"
                            f"Invoice #{result.get('invoice_num', '')}\n"
                            f"Review and set amount in QuickBooks, then send."
                        ),
                        from_=contractor_number,
                        to=notify_sms
                    )
                    print("QB INVOICE SMS SENT |", notify_sms)
                except Exception as e:
                    print("QB INVOICE SMS ERROR |", e)
 
            return jsonify({"ok": True, "invoice_id": result.get("invoice_id")}), 200
        else:
            return jsonify({"ok": False, "error": result.get("error")}), 500
 
    except Exception as e:
        print("AIRTABLE WEBHOOK ERROR |", e)
        return jsonify({"ok": False, "error": str(e)}), 500



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

    # ── Pull contractor from Airtable ──────────────────────────────────
    twilio_number = state.get("contractor_key", "") or state.get("to_number", "")
    contractor = get_contractor_by_twilio_number(twilio_number) or {}
    print("CONTRACTOR FIELDS |", list(contractor.keys()))  # TEMP DEBUG
    print("CAL URL RAW |", contractor.get("CAL Booking URL"))  # TEMP DEBUG
    notify_email = contractor.get("Notify Email") or notify_email or os.getenv("TO_EMAIL")
    cal_booking_url = (contractor.get("CAL Booking URL") or "").strip()
    notify_sms = (contractor.get("Notify SMS") or "").strip()
    business_name = (contractor.get("Business Name") or "Your business").strip()

    subject = f"New Lead — {state.get('name', 'Unknown')}"
    body = (
        "New lead captured by MME AI Bot:\n\n"
        f"Client Name: {state.get('name', '')}\n"
        f"Service Address: {state.get('service_address', '')}\n"
        f"Job Requested: {state.get('job_description', '')}\n"
        f"Timing Needed: {state.get('timing', '')}\n"
        f"Callback Number: {state.get('callback', '')}\n"
        f"Call SID: {state.get('call_sid', '')}\n"
    )

    call_sid = state.get("call_sid", "")
    source = "AI SMS" if call_sid.startswith("SMS-") else "AI Phone Call"

    airtable_fields = {
        "Client Name": state.get("name", ""),
        "Call Back Number": state.get("callback", ""),
        "Service Address": state.get("service_address", ""),
        "Job Description": state.get("job_description", ""),
        "Source": source,
        "Call SID": call_sid,
        "Appointment Requested": state.get("timing", ""),
        "Lead Status": "New Lead",
        "Priority": state.get("priority", "STANDARD"),
    }

    appt_datetime = state.get("appointment")
    if appt_datetime and "T" in appt_datetime:
        airtable_fields["Appointment Date and Time"] = appt_datetime

    airtable_result = airtable_create_record(airtable_fields)
    print("Airtable result:", airtable_result)

    if airtable_result.get("ok"):
        lead_id = airtable_result.get("data", {}).get("id", "")
        state["lead_airtable_id"] = lead_id
        print("LEAD AIRTABLE ID SAVED |", lead_id)

    # ── Email contractor ───────────────────────────────────────────────
    send_email(subject, body, to_email=notify_email, reply_to=reply_to_email)

    # ── SMS notification to contractor ─────────────────────────────────
    twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if notify_sms and twilio_number:
        try:
            lead_msg = (
                f"🔔 New Lead — {business_name}\n"
                f"👤 {state.get('name', 'Unknown')}\n"
                f"📍 {state.get('service_address', '')}\n"
                f"🔧 {state.get('job_description', '')}\n"
                f"⏰ {state.get('timing', '')}\n"
                f"📞 {state.get('callback', '')}"
            )
            Client(twilio_account_sid, twilio_auth_token).messages.create(
                body=lead_msg,
                from_=twilio_number,
                to=notify_sms
            )
            print("CONTRACTOR SMS SENT |", notify_sms)
        except Exception as e:
            print("CONTRACTOR SMS ERROR |", e)
    else:
        print("CONTRACTOR SMS SKIPPED | notify_sms:", notify_sms)

    # ── SMS booking link to customer ───────────────────────────────────
    customer_number = state.get("callback", "")

    if customer_number and twilio_number and cal_booking_url:
        try:
            cal_params = urllib.parse.urlencode({
                "name": state.get("name", ""),
                "attendeePhoneNumber": customer_number,
                "service_address": state.get("service_address", ""),
                "job_description": state.get("job_description", ""),
            })
            booking_link = f"{cal_booking_url}?{cal_params}"
            first_name = state.get("name", "there").split()[0]
            booking_msg = (
                f"Hi {first_name}! Thanks for reaching out to {business_name}. "
                f"Click here to book your appointment: {booking_link}"
            )
            Client(twilio_account_sid, twilio_auth_token).messages.create(
                body=booking_msg,
                from_=twilio_number,
                to=customer_number
            )
            print("BOOKING LINK SMS SENT |", customer_number, "|", booking_link)
        except Exception as e:
            print("BOOKING LINK SMS ERROR |", e)
    else:
        print("BOOKING LINK SMS SKIPPED | customer:", customer_number,
              "| contractor:", twilio_number,
              "| cal_url:", cal_booking_url)


   

        
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
    email = (state.get("client_email") or "").strip()
 
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

    if email: already.append(f"Email: {email}")
 
    already_str = "\n".join(f"- {x}" for x in already) if already else "Nothing yet"
    needed_str = "\n".join(f"- {x}" for x in needed) if needed else "All collected"
 
    return f"""You are a friendly SMS intake assistant for {business_name}.
Collect five pieces of info via text message to send a booking link.

ALREADY COLLECTED:
{already_str}

STILL NEEDED:
{needed_str}

RULES:
- This is SMS - keep ALL responses under 160 characters
- Single sentence only - no line breaks
- Ask for ONE piece of info at a time
- Accept first answer given - never ask follow-ups
- If emergency: reply exactly EMERGENCY
- Be warm but brief
- NEVER tell a customer you don't offer a service - accept ALL job requests 
- The contractor decides what jobs to take - your job is only to collect information


RESPONSE FORMAT - you must ALWAYS reply with exactly two lines:
LINE 1: Your SMS message to the customer
LINE 2: JSON with what you just collected (use null if not collected this turn)

Example:
Thanks Mike! What's your service address including zip code?
{{"collected_name": "Mike Smith", "collected_address": null, "collected_job": null, "collected_timing": null, "ready": false}}

When all four fields are collected set ready to true:
Great! We have everything we need.
{{"collected_name": "Mike Smith", "collected_address": "123 Main St", "collected_job": "lawn mowing", "collected_timing": "next week", "ready": true}}"""



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
# Contractor on-site photo estimate flow
# ─────────────────────────────────────────────

def handle_contractor_photo_estimate(request, contractor, from_number, to_number, num_media, incoming_msg=""):
    """
    Contractor texts photos to their own Twilio number.
    Claude analyzes photos → generates PDF estimate → texts link back to contractor.
    """
   
    business_name = (contractor.get("Business Name") or "Your Business").strip()
    notify_sms = (contractor.get("Notify SMS") or "").strip()
    timestamp = int(time.time())

    print(f"CONTRACTOR PHOTO ESTIMATE | from: {from_number} | photos: {num_media}")

    # ── Step 1: Download photos from Twilio MMS ────────────────────────
    photo_urls = []
    cloudinary_urls = []
    twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    for i in range(num_media):
        media_url = request.form.get(f"MediaUrl{i}", "")
        if not media_url:
            continue
        try:
            r = requests.get(media_url, auth=(twilio_account_sid, twilio_auth_token), timeout=15)
            r.raise_for_status()
            photo_urls.append(r.content)
            print(f"PHOTO DOWNLOADED | {i+1} of {num_media} | {len(r.content)} bytes")
        except Exception as e:
            print(f"PHOTO DOWNLOAD ERROR | {i} |", e)

    if not photo_urls:
        tc = Client(twilio_account_sid, twilio_auth_token)
        tc.messages.create(
            body="Could not download your photos. Please try again.",
            from_=to_number,
            to=from_number
        )
        return Response("<Response></Response>", mimetype="text/xml")

    # ── Step 2: Upload to Cloudinary ───────────────────────────────────
    estimate_id = f"est_{timestamp}"
    for i, photo_data in enumerate(photo_urls):
        result = upload_photo(photo_data, estimate_id, i + 1)
        if result.get("ok"):
            cloudinary_urls.append(result["url"])
            print(f"CLOUDINARY SAVED | photo {i+1} | {result['url']}")

    # ── Step 3: Send photos to Claude Vision for analysis ──────────────
    try:
        claude_client = get_claude_client()

        # Build image content blocks
        image_blocks = []
        for photo_data in photo_urls[:5]:  # max 5
            b64 = base64.standard_b64encode(photo_data).decode("utf-8")
            image_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64
                }
            })

        image_blocks.append({
            "type": "text",
            "text": (
                f"You are an expert contractor estimator for {business_name}. "
                f"The contractor's description of the job: '{incoming_msg}'. "
                "Analyze these job site photos carefully along with the description. "
                "Identify the scope of work and provide a detailed estimate breakdown. "
                "IMPORTANT: Always provide realistic contractor pricing for each line item. "
                "Base prices on current US labor and material rates. Never use 0 as an amount. "
                "If a photo is unclear, use the contractor's text description to guide your estimate. "
                "Respond ONLY in this exact JSON format with no other text:\n"
                "{\n"
                '  "job_summary": "Brief description of what you see",\n'
                '  "line_items": [\n'
                '    {"description": "Item name", "detail": "Brief detail", "qty": "1", "unit": "Job", "amount": 150.00},\n'
                '    {"description": "Item name", "detail": "Brief detail", "qty": "500", "unit": "sq ft", "amount": 200.00}\n'
                "  ],\n"
                '  "notes": "Any important notes about the job"\n'
                "}"
            )
        })

        vision_response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": image_blocks}]
        )

        raw = vision_response.content[0].text.strip()
        print("CLAUDE VISION RESPONSE |", raw[:200])

        # Parse JSON
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON in Claude response")
        estimate_data = json.loads(json_match.group(0))

    except Exception as e:
        print("CLAUDE VISION ERROR |", e)
        tc = Client(twilio_account_sid, twilio_auth_token)
        tc.messages.create(
            body="Photos received but estimate generation failed. Please try again.",
            from_=to_number,
            to=from_number
        )
        return Response("<Response></Response>", mimetype="text/xml")

    # ── Step 4: Generate PDF estimate ──────────────────────────────────
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter

        pdf_buffer = io.BytesIO()
        c = canvas.Canvas(pdf_buffer, pagesize=letter)
        width, height = letter
        y = height - 60

        # Header bar
        c.setFillColor(colors.HexColor('#1A4D2E'))
        c.rect(0, height - 80, width, 80, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(40, height - 50, business_name)
        c.setFont("Helvetica-Bold", 28)
        c.setFillColor(colors.HexColor('#F4A828'))
        c.drawRightString(width - 40, height - 52, "ESTIMATE")

        y = height - 110

        # Date and estimate ID
        c.setFillColor(colors.HexColor('#333333'))
        c.setFont("Helvetica", 10)
        c.drawString(40, y, f"Date: {date.today().strftime('%B %d, %Y')}")
        c.drawRightString(width - 40, y, f"Estimate ID: {estimate_id}")
        y -= 20

        # Divider
        c.setStrokeColor(colors.HexColor('#2E7D4F'))
        c.setLineWidth(1.5)
        c.line(40, y, width - 40, y)
        y -= 20

        # Job summary
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(colors.HexColor('#2E7D4F'))
        c.drawString(40, y, "JOB SUMMARY")
        y -= 16
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.HexColor('#333333'))

        # Word wrap job summary
        summary = estimate_data.get("job_summary", "")
        words = summary.split()
        line = ""
        for word in words:
            test = f"{line} {word}".strip()
            if c.stringWidth(test, "Helvetica", 10) < width - 80:
                line = test
            else:
                c.drawString(40, y, line)
                y -= 14
                line = word
        if line:
            c.drawString(40, y, line)
        y -= 24

        # Line items header
        c.setFillColor(colors.HexColor('#1A4D2E'))
        c.rect(40, y - 4, width - 80, 22, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(48, y + 4, "DESCRIPTION")
        c.drawRightString(width - 160, y + 4, "QTY")
        c.drawRightString(width - 100, y + 4, "UNIT")
        c.drawRightString(width - 44, y + 4, "AMOUNT")
        y -= 26

        # Line items
        subtotal = 0.0
        line_items = estimate_data.get("line_items", [])
        for i, item in enumerate(line_items):
            amt = float(item.get("amount", 0))
            subtotal += amt

            # Alternating row background
            if i % 2 == 0:
                c.setFillColor(colors.HexColor('#E8F5ED'))
                c.rect(40, y - 6, width - 80, 20, fill=1, stroke=0)

            c.setFillColor(colors.HexColor('#333333'))
            c.setFont("Helvetica-Bold", 10)
            c.drawString(48, y + 4, item.get("description", "")[:45])
            c.setFont("Helvetica", 10)
            c.drawRightString(width - 160, y + 4, str(item.get("qty", "1")))
            c.drawRightString(width - 100, y + 4, str(item.get("unit", ""))[:10])
            c.drawRightString(width - 44, y + 4, f"${amt:,.2f}")

            # Detail text
            y -= 16
            c.setFont("Helvetica", 8)
            c.setFillColor(colors.HexColor('#666666'))
            detail = item.get("detail", "")[:80]
            c.drawString(48, y, detail)
            y -= 16

        # Divider before total
        y -= 8
        c.setStrokeColor(colors.HexColor('#CCCCCC'))
        c.setLineWidth(0.5)
        c.line(40, y, width - 40, y)
        y -= 18

        # Total
        c.setFillColor(colors.HexColor('#1A4D2E'))
        c.rect(40, y - 6, width - 80, 26, fill=1, stroke=0)
        c.setFillColor(colors.HexColor('#F4A828'))
        c.setFont("Helvetica-Bold", 14)
        c.drawRightString(width - 44, y + 6, f"${subtotal:,.2f}")
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(48, y + 6, "TOTAL ESTIMATE")
        y -= 40

        # Notes
        notes = estimate_data.get("notes", "")
        if notes:
            c.setFont("Helvetica-Bold", 9)
            c.setFillColor(colors.HexColor('#2E7D4F'))
            c.drawString(40, y, "NOTES")
            y -= 14
            c.setFont("Helvetica", 9)
            c.setFillColor(colors.HexColor('#333333'))
            words = notes.split()
            line = ""
            for word in words:
                test = f"{line} {word}".strip()
                if c.stringWidth(test, "Helvetica", 9) < width - 80:
                    line = test
                else:
                    c.drawString(40, y, line)
                    y -= 13
                    line = word
            if line:
                c.drawString(40, y, line)
            y -= 20

        # Disclaimer
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor('#888888'))
        c.drawString(40, y, "⚠ Ballpark estimate based on photos only. Final pricing may vary after on-site inspection.")

        # Footer
        c.setFillColor(colors.HexColor('#1A4D2E'))
        c.rect(0, 0, width, 36, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica", 9)
        c.drawCentredString(width / 2, 13, f"{business_name}  •  Professional Contractor Services")

        c.save()
        pdf_buffer.seek(0)
        pdf_bytes = pdf_buffer.read()
        print(f"PDF GENERATED | {len(pdf_bytes)} bytes")

       

    except Exception as e:
        import traceback
        print("PDF GENERATION ERROR |", e)
        traceback.print_exc()
        tc = Client(twilio_account_sid, twilio_auth_token)
        tc.messages.create(
            body="Photos analyzed but PDF generation failed. Please try again.",
            from_=to_number,
            to=from_number
        )
        return Response("<Response></Response>", mimetype="text/xml")
       
      
    # ── Step 5: Upload PDF to Cloudinary ───────────────────────────────
    try:
        pdf_public_id = f"contractoros/estimates/{estimate_id}/estimate"
        pdf_result = cloudinary.uploader.upload(
            pdf_bytes,
            public_id=pdf_public_id,
            resource_type="raw",
            format="pdf",
            overwrite=True,
        )
        raw_url = pdf_result.get("secure_url", "")
        pdf_url = raw_url.replace("/upload/", "/upload/fl_attachment/")
        print("PDF UPLOADED TO CLOUDINARY |", pdf_url)
    except Exception as e:
        print("PDF CLOUDINARY UPLOAD ERROR |", e)
        pdf_url = None

    # ── Step 6: Text PDF link to contractor ───────────────────────────
    try:
        tc = Client(twilio_account_sid, twilio_auth_token)

        if pdf_url:
            msg = (
                f"✅ Estimate ready — {business_name}\n"
                f"📋 {estimate_data.get('job_summary', '')[:80]}\n"
                f"💰 Total: ${subtotal:,.2f}\n\n"
                f"Review & adjust PDF here:\n{pdf_url}\n\n"
                f"Forward to customer when ready."
            )
        else:
            # PDF upload failed — send the line items as text fallback
            items_text = "\n".join([
                f"• {i.get('description')}: ${float(i.get('amount',0)):,.2f}"
                for i in line_items
            ])
            msg = (
                f"✅ Estimate — {business_name}\n"
                f"{estimate_data.get('job_summary', '')[:80]}\n\n"
                f"{items_text}\n\n"
                f"💰 Total: ${subtotal:,.2f}\n"
                f"(PDF upload failed — review and send manually)"
            )

        tc.messages.create(
            body=msg,
            from_=to_number,
            to=from_number
        )
        print("ESTIMATE SMS SENT TO CONTRACTOR |", from_number)

    except Exception as e:
        print("ESTIMATE SMS ERROR |", e)

    return Response("<Response></Response>", mimetype="text/xml")


# ─────────────────────────────────────────────
# SMS
# ─────────────────────────────────────────────

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

    # ── Contractor photo estimate flow ─────────────────────────────
    notify_sms = (contractor.get("Notify SMS") or "").strip()
    num_media = int(request.form.get("NumMedia", 0))
    if from_number == notify_sms and num_media > 0:
        return handle_contractor_photo_estimate(request, contractor, from_number, to_number, num_media, incoming_msg)


 
    # Load or initialize SMS conversation state from Redis
    sms_state_key = f"sms_state:{to_number}:{from_number}"
    sms_state = {}

    if redis_client:
        try:
            raw = redis_client.get(sms_state_key)
            if raw:
                sms_state = json.loads(raw)
                print("SMS STATE LOADED |", sms_state_key, "| name:", sms_state.get("name"), "| address:", sms_state.get("service_address"))
            else:
                print("SMS STATE NOT FOUND |", sms_state_key)
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
        reply = f"Thanks for contacting {business_name}! We received your message and will follow up shortly."
        return Response(
            f"<Response><Message>{reply}</Message></Response>",
            mimetype="text/xml"
        )

    print("SMS CLAUDE RESPONSE |", claude_response)

    # Parse Claude's two-line response
    lines = claude_response.strip().split("\n")
    reply = lines[0].strip() if lines else claude_response
    
    # Extract JSON from second line
    collected = {}
    if len(lines) > 1:
        try:
            import re
            json_match = re.search(r"\{.*\}", lines[1], re.DOTALL)
            if json_match:
                collected = json.loads(json_match.group(0))
        except Exception as e:
            print("SMS JSON PARSE ERROR |", e)

    # Update state with whatever Claude collected this turn
    if collected.get("collected_name"):
        sms_state["name"] = collected["collected_name"]
        print("NAME SAVED |", sms_state["name"])
    if collected.get("collected_address"):
        sms_state["service_address"] = collected["collected_address"]
        print("ADDRESS SAVED |", sms_state["service_address"])
    if collected.get("collected_job"):
        sms_state["job_description"] = collected["collected_job"]
        print("JOB SAVED |", sms_state["job_description"])
    if collected.get("collected_timing"):
        sms_state["timing"] = collected["collected_timing"]
        print("TIMING SAVED |", sms_state["timing"])

    print("SMS STATE SAVING |", sms_state_key, 
          "| name:", sms_state.get("name"), 
          "| address:", sms_state.get("service_address"))

    # Check if ready to complete
    if collected.get("ready") and sms_state.get("name") and sms_state.get("service_address"):
        # Fire INTAKE_COMPLETE flow
        sms_state["priority"] = "STANDARD"
        sms_state["call_sid"] = f"SMS-{from_number}-{int(time.time())}"

        notify_email = (contractor.get("Notify Email") or os.getenv("TO_EMAIL") or "").strip()
        try:
            send_intake_summary(sms_state, notify_email=notify_email)
        except Exception as e:
            print("SMS INTAKE SUMMARY ERROR |", e)

        booking_link = build_cal_booking_link(contractor, sms_state)

        first_name = sms_state.get('name', '').split()[0] if sms_state.get('name') else ''

        if booking_link:
            reply = (
                f"Perfect {first_name}! We have everything we need. "
                f"Book your estimate here: {booking_link} "
                f"We look forward to working with you! Reply STOP to opt out."
            )
        else:
            reply = (
                f"Got it {first_name}! We have all your details and "
                f"someone from {business_name} will be in touch shortly. "
                f"Reply STOP to opt out."
            )

        # Schedule photo SMS 6 minutes later
        try:
            messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
            lead_id = sms_state.get("lead_airtable_id", "")
            base_url = os.getenv("RENDER_EXTERNAL_URL", "https://mme-ai-bot.onrender.com").rstrip("/")
            photo_link = f"{base_url}/upload-photos/{lead_id}" if lead_id else ""

            if messaging_service_sid and photo_link:
                from datetime import timedelta
                photo_send_time = datetime.now(timezone.utc) + timedelta(minutes=6)
                tc = twilio_client()
                if tc.get("ok"):
                    tc["client"].messages.create(
                        body=(
                            f"One more thing {first_name} — send us photos "
                            f"of the job so we can prepare a better estimate: "
                            f"{photo_link} "
                            f"The more we see, the faster we can quote you."
                        ),
                        from_=to_number,
                        to=from_number,
                        send_at=photo_send_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        schedule_type="fixed",
                        messaging_service_sid=messaging_service_sid,
                    )
                    print("SMS PHOTO SCHEDULED | 6 min delay | to:", from_number)
        except Exception as e:
            print("SMS PHOTO SCHEDULE ERROR |", e)

        if redis_client:
            redis_client.delete(sms_state_key)

        print("SMS INTAKE COMPLETE |", sms_state.get("name"), "|", sms_state.get("service_address"))

        return Response(
            f"<Response><Message>{reply}</Message></Response>",
             mimetype="text/xml"
        )
                  

    # Handle emergency
    if "EMERGENCY" in reply.upper():
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

    # Save state and reply
    messages.append({"role": "user", "content": incoming_msg})
    messages.append({"role": "assistant", "content": reply})
    sms_state["messages"] = messages[-20:]

    if redis_client:
        try:
            redis_client.setex(sms_state_key, 7200, json.dumps(sms_state))
        except Exception as e:
            print("SMS STATE SAVE ERROR |", e)

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

                # Schedule photo upload SMS 6 minutes later
                try:
                    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
                    lead_id = sms_state.get("lead_airtable_id", "")
                    base_url = os.getenv("RENDER_EXTERNAL_URL", "https://mme-ai-bot.onrender.com").rstrip("/")
                    photo_link = f"{base_url}/upload-photos/{lead_id}" if lead_id else ""

                    if messaging_service_sid and photo_link:
                        from datetime import timedelta
                        photo_send_time = datetime.now(timezone.utc) + timedelta(minutes=6)
                        tc = twilio_client()
                        if tc.get("ok"):
                            tc["client"].messages.create(
                                body=(
                                    f"One more thing {first_name} — send us photos "
                                    f"of the job so we can prepare a better estimate: "
                                    f"{photo_link} "
                                    f"The more we see, the faster we can quote you."
                                ),
                                from_=to_number,
                                to=from_number,
                                send_at=photo_send_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                schedule_type="fixed",
                                messaging_service_sid=messaging_service_sid,
                            )
                            print("SMS PHOTO SCHEDULED | 6 min delay | to:", from_number)
                except Exception as e:
                    print("SMS PHOTO SCHEDULE ERROR |", e)

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
 
    # Extract collected fields — check what Claude just asked
    # messages[-1] is the last BOT message (what Claude asked before this response)
    all_bot_messages = " ".join([
        m.get("content", "") 
        for m in messages 
        if m.get("role") == "assistant"
    ]).lower()

    last_bot_message = next(
        (m.get("content", "") for m in reversed(messages) 
         if m.get("role") == "assistant"), ""
    ).lower()

    skip_words = {"yes", "no", "yeah", "nope", "correct", "yep", "ok", "okay", "sure"}
    is_confirmation = incoming_msg.lower().strip().rstrip(".!?") in skip_words

    # If customer confirmed address extract it from Claude's message
    if is_confirmation and not sms_state.get("service_address"):
        if any(w in last_bot_message for w in ["address", "confirm"]):
            import re
            addr_match = re.search(r"is (.+?) your service address", last_bot_message, re.IGNORECASE)
            if addr_match:
                sms_state["service_address"] = addr_match.group(1).strip()
                print("ADDRESS CONFIRMED |", sms_state["service_address"])

    if not sms_state.get("name") and not is_confirmation:
        if any(w in last_bot_message for w in ["name", "who"]):
            sms_state["name"] = incoming_msg.split(".")[0].strip()
            print("NAME EXTRACTED |", sms_state["name"])

    elif not sms_state.get("service_address") and not is_confirmation:
        if any(w in last_bot_message for w in ["address", "location", "zip"]):
            sms_state["service_address"] = incoming_msg.strip()
            print("ADDRESS EXTRACTED |", sms_state["service_address"])

    elif not sms_state.get("job_description") and not is_confirmation:
        if any(w in last_bot_message for w in ["work", "done", "need", "service"]):
            sms_state["job_description"] = incoming_msg.strip()
            print("JOB EXTRACTED |", sms_state["job_description"])

    elif not sms_state.get("timing") and not is_confirmation:
        if any(w in last_bot_message for w in ["when", "timing", "available"]):
            sms_state["timing"] = incoming_msg.split(".")[0].strip()
            print("TIMING EXTRACTED |", sms_state["timing"])

        # Normal response — save state and reply
        messages.append({"role": "user", "content": incoming_msg})
        messages.append({"role": "assistant", "content": claude_response})
        sms_state["messages"] = messages[-20:]
 
    # Save state to Redis with 2 hour TTL
    if redis_client:
        try:
            print("SMS STATE SAVING |", sms_state_key, "| name:", sms_state.get("name"), "| address:", sms_state.get("service_address"))
            redis_client.setex(sms_state_key, 7200, json.dumps(sms_state))
        except Exception as e:
            print("SMS STATE SAVE ERROR |", e)
 
    # Truncate reply to SMS safe length
    reply = claude_response[:320] if len(claude_response) > 320 else claude_response
 
    return Response(
        f"<Response><Message>{reply}</Message></Response>",
        mimetype="text/xml"
    )
 
 

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
