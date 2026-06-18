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
import hashlib
import secrets
import jwt as pyjwt
from functools import wraps

from flask import Flask, request, jsonify, Response, session, redirect, make_response, send_from_directory
from flask import render_template_string
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
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
from app.app.mapbox_service import haversine_miles, is_address_in_service_area
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
    QB_TOKEN_URL, QB_SCOPES, QB_CLIENT_SECRET, QB_API_BASE,
)
from base64 import b64encode
import secrets

from app.app.follow_up_scheduler import start_scheduler
start_scheduler() 

from app.app.cancel_reschedule import handle_cancel_reschedule

from app.app.stripe_service import create_payment_link

from app.app.subscription_service import has_feature, get_upgrade_message

from app.app.contractor_onboarding import create_checkout_session, handle_subscription_webhook

from app.app.customer_service import lookup_lead_by_phone, handle_customer_service


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

def send_push_notification(twilio_number, title, message, url="/dashboard"):
    """Send OneSignal push notification to contractor."""
    try:
        import requests as req
        ONESIGNAL_APP_ID = os.environ.get("ONESIGNAL_APP_ID")
        ONESIGNAL_API_KEY = os.environ.get("ONESIGNAL_API_KEY")

        if not ONESIGNAL_APP_ID or not ONESIGNAL_API_KEY:
            print("ONESIGNAL | Missing credentials")
            return False

        payload = {
            "app_id": ONESIGNAL_APP_ID,
            "filters": [
                {"field": "tag", "key": "twilio_number", "relation": "=", "value": twilio_number}
            ],
            "headings": {"en": title},
            "contents": {"en": message},
            "url": f"https://mme-ai-bot.onrender.com{url}",
            "chrome_web_icon": "https://res.cloudinary.com/dkfshn604/image/upload/IMG_1664_jukqma.jpg",
        }

        resp = req.post(
            "https://onesignal.com/api/v1/notifications",
            headers={
                "Authorization": f"Basic {ONESIGNAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=10
        )
        print(f"ONESIGNAL | Sent | {resp.status_code} | {title}")
        return resp.status_code in [200, 201]

    except Exception as e:
        print(f"ONESIGNAL ERROR | {e}")
        return False



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

@app.route("/airtable/send-invoice", methods=["POST"])
def airtable_send_invoice():
    """
    Triggered from Airtable when Send Invoice checkbox is checked.
    Creates QuickBooks invoice and emails it directly to customer.
    """
    try:
        data = request.get_json(force=True) or {}
        print("SEND INVOICE WEBHOOK |", data)

        record = data.get("record", {}) or data
        fields = record.get("fields", {}) or record

        customer_name = fields.get("Customer Name", "").strip()
        customer_email = fields.get("Client Email", "").strip()
        customer_phone = (fields.get("Phone Number") or "").strip()
        amount = float(fields.get("Amount", 0) or 0)
        job_description = fields.get("Notes", "").strip()
        record_id = fields.get("record_id", "")

        if not customer_name:
            return jsonify({"ok": False, "error": "No customer name"}), 400

        if not customer_email:
            return jsonify({"ok": False, "error": "No customer email — add email to Airtable record"}), 400

        # Build state for QB invoice creation
        state = {
            "name": customer_name,
            "service_address": "",
            "job_description": job_description,
            "callback": customer_phone,
            "timing": "",
            "client_email": customer_email,
            "estimate_amount": amount,
        }

        # Step 1 — Create QuickBooks invoice
        result = create_qb_invoice(state)
        print("QB INVOICE RESULT |", result)

        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error")}), 500

        invoice_id = result.get("invoice_id")

        # Step 2 — Email invoice to customer via QuickBooks
        access_token, realm_id = get_valid_access_token()
        if access_token and invoice_id and customer_email:
            try:
                email_url = f"{QB_API_BASE}/{realm_id}/invoice/{invoice_id}/send"
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/octet-stream",
                }
                email_resp = requests.post(
                    email_url,
                    headers=headers,
                    params={
                        "sendTo": customer_email,
                        "minorversion": "65"
                    },
                    timeout=15
                )
                if email_resp.status_code in [200, 201]:
                    print(f"QB INVOICE EMAILED | {customer_name} | {customer_email}")
                else:
                    print(f"QB INVOICE EMAIL ERROR | {email_resp.status_code} | {email_resp.text}")
            except Exception as e:
                print(f"QB INVOICE EMAIL EXCEPTION | {e}")

        # Step 3 — Update Airtable Payment Status to Invoiced
        if record_id:
            try:
                AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
                AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
                payments_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments"
                headers_at = {
                    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
                    "Content-Type": "application/json"
                }
                requests.patch(
                    f"{payments_url}/{record_id}",
                    headers=headers_at,
                    json={"fields": {"Payment Status": "Invoiced"}}
                )
                print(f"AIRTABLE PAYMENT STATUS | {record_id} | Invoiced")
            except Exception as e:
                print(f"AIRTABLE UPDATE ERROR | {e}")

        return jsonify({
            "ok": True,
            "invoice_id": invoice_id,
            "invoice_num": result.get("invoice_num"),
            "emailed_to": customer_email
        }), 200

    except Exception as e:
        print(f"SEND INVOICE ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/aerial-quote", methods=["POST"])
def aerial_quote():
    try:
        from app.app.aerial_service import run_aerial_quote
        from app.app.pdf_service import generate_quote_pdf

        data = request.get_json(silent=True) or {}
        address = data.get("address", "").strip()
        job_description = data.get("job_description", "").strip()
        lead_id = data.get("lead_id", "").strip()
        customer_name = data.get("customer_name", "").strip()
        twilio_number = data.get("twilio_number", "").strip()
        pdf_path = None  # ← MOVE IT HERE at the top

        if not address:
            return jsonify({"ok": False, "error": "Address required"}), 400

        print(f"AERIAL QUOTE ROUTE | {address} | {job_description}")

        result = run_aerial_quote(
            address=address,
            job_description=job_description,
            lead_id=lead_id,
            customer_name=customer_name
        )

        if not result.get("ok"):
            return jsonify(result), 500

        # Update Airtable
        if lead_id:
            try:
                AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
                AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
                leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"
                headers = {
                    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
                    "Content-Type": "application/json"
                }
                requests.patch(
                    f"{leads_url}/{lead_id}",
                    headers=headers,
                    json={"fields": {
                        "AI Scope Summary": result.get("analysis", "")[:1000],
                        "fldqt3c17hd20Xd9h": result.get("satellite_url", ""),
                        "fldk89m59h1lfHjN6": result.get("quote_range", ""),
                    }}
                )
                print(f"AERIAL | Airtable updated | {lead_id}")
            except Exception as e:
                print(f"AERIAL | Airtable update error | {e}")
                

        # SMS and email contractor
        if twilio_number:
            try:
                contractor = get_contractor_by_twilio_number(twilio_number) or {}

                pdf_path = generate_quote_pdf(
                    result=result,
                    contractor=contractor,
                    customer_name=customer_name,
                    job_description=job_description
                )
                print("AERIAL | PDF created |", pdf_path)

                notify_sms = contractor.get("Notify SMS", "")
                if notify_sms:
                    msg = (
                        f"🛰️ Aerial Quote — {customer_name}\n"
                        f"📍 {address}\n"
                        f"📐 ~{result.get('square_footage', 0):,} sq ft\n"
                        f"💰 Est: {result.get('quote_range')}\n"
                        f"🖼️ {result.get('satellite_url', '')}"
                    
                    )
                    
                    send_fallback_sms(to_number=notify_sms, body=msg)

                    # Send email with full analysis
                    try:
                        notify_email = contractor.get("Notify Email", "").strip()
                        if notify_email:
                            email_body = (
                                f"🛰️ Aerial Quote — {customer_name}\n"
                                f"{'='*50}\n\n"
                                f"📍 Address: {result.get('address')}\n"
                                f"🔧 Job: {job_description}\n"
                                f"📐 Estimated Work Area: ~{result.get('square_footage', 0):,} sq ft\n"
                                f"💰 Quote Range: {result.get('quote_range')}\n\n"
                                f"{'='*50}\n"
                                f"FULL AI ANALYSIS\n"
                                f"{'='*50}\n\n"
                                f"{result.get('analysis', '')}\n\n"
                                f"{'='*50}\n"
                                f"SATELLITE IMAGE\n"
                                f"{'='*50}\n"
                                f"{result.get('satellite_url', '')}\n\n"
                                f"{'='*50}\n"
                                f"CUSTOMER-READY ESTIMATE\n"
                                f"{'='*50}\n\n"
                                f"Dear {customer_name},\n\n"
                                f"Thank you for reaching out to {contractor.get('Business Name', 'us')}! "
                                f"Based on our initial review of your property at {result.get('address')}, "
                                f"here is our preliminary estimate for {job_description}:\n\n"
                                f"Estimated Investment: {result.get('quote_range')}\n\n"
                                f"This estimate is based on the approximate work area of {result.get('square_footage', 0):,} sq ft. "
                                f"Final pricing will be confirmed during our on-site visit.\n\n"
                                f"We look forward to working with you!\n\n"
                                f"Best regards,\n"
                                f"{contractor.get('Business Name', 'Your Contractor')}"
                            )
                            send_email(
                                subject=f"🛰️ Aerial Quote — {customer_name} | {result.get('quote_range')}",
                                body=email_body,
                                to_email=notify_email,
                                attachment_path=pdf_path,
                            )
                            print(f"AERIAL | Email sent | {notify_email}")
                    except Exception as e:
                        print(f"AERIAL | Email error | {e}")

                    print(f"AERIAL | SMS sent to contractor | {notify_sms}")
            except Exception as e:
                print(f"AERIAL | SMS error | {e}")

        return jsonify(result), 200

    except Exception as e:
        print(f"AERIAL QUOTE ERROR | {type(e).__name__} | {e}")
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

def send_email(subject: str, body: str, to_email: str = None, reply_to: str = None, attachment_path: str = None):
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

    # ✅ Attach PDF if provided
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()

        attachment = Attachment(
            FileContent(encoded),
            FileName(os.path.basename(attachment_path)),
            FileType("application/pdf"),
            Disposition("attachment")
        )

        message.attachment = attachment
        print("EMAIL | PDF attached:", attachment_path)
    else:
        print("EMAIL | No attachment found:", attachment_path)

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
        "Twilio Number": twilio_number,
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
When greeting a new customer for the first time, always start with: "Thanks for contacting {business_name}!"
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

import os

@app.route('/static/<path:filename>')
def static_files(filename):
    root_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(root_dir, filename)


@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "CrewCachePro",
        "short_name": "CrewCache",
        "start_url": "/dashboard",
        "display": "standalone",
        "background_color": "#f0f4f8",
        "theme_color": "#2563EB",
        "icons": [
            {
                "src": "/static/logo.png",
                "sizes": "1024x1024",
                "type": "image/png"
            }
        ]
    })

@app.route('/OneSignalSDKWorker.js')
def onesignal_worker():
    """Serves OneSignal service worker required for push notifications."""
    worker_content = """importScripts('https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.sw.js');"""
    from flask import Response
    return Response(
        worker_content,
        mimetype='application/javascript',
        headers={
            'Service-Worker-Allowed': '/',
            'Cache-Control': 'no-cache'
        }
    )



@app.route("/clear-cache")
def clear_cache():
    if redis_client:
        redis_client.flushall()
        return "Cache cleared"
    return "No Redis client"


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

    if not contractor:
        return "Booking link not configured for this contractor.", 404

    # Check if this contractor has native CrewCachePro booking configured
    try:
        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        services_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblX8znMQVi443I4U"
        svc_resp = requests.get(
            services_url,
            headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"},
            params={"filterByFormula": f"AND({{Twilio Number}} = '{contractor_key}', {{Active}} = TRUE())"}
        )
        active_services = svc_resp.json().get("records", [])
    except Exception as e:
        print("BOOK | services lookup error |", e)
        active_services = []

    if active_services:
        root_dir = os.path.dirname(os.path.abspath(__file__))
        return send_from_directory(root_dir, "Book.html")

    # Legacy fallback — existing Cal.com redirect, unchanged
    base_url = (contractor.get("Intake URL") or "").strip()
    if not base_url:
        return "Booking link not configured for this contractor.", 404

    params = request.args.to_dict(flat=False)
    params.pop("c", None)
    query_string = urllib.parse.urlencode(params, doseq=True)
    separator = "&" if "?" in base_url else "?"
    final_url = f"{base_url}{separator}{query_string}" if query_string else base_url
    return redirect(final_url, code=302)

@app.route("/book-services")
def book_services():
    contractor_key = (request.args.get("c") or "").strip()
    contractor = get_contractor_by_twilio_number(contractor_key) if contractor_key else {}
    if not contractor:
        return jsonify({"ok": False, "error": "Contractor not found"}), 404

    AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
    AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
    services_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblX8znMQVi443I4U"
    resp = requests.get(
        services_url,
        headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"},
        params={"filterByFormula": f"AND({{Twilio Number}} = '{contractor_key}', {{Active}} = TRUE())"}
    )
    records = resp.json().get("records", [])
    records.sort(key=lambda r: r.get("fields", {}).get("Sort Order", 999))

    services = [{
        "id": r["id"],
        "name": r.get("fields", {}).get("Service Name", ""),
        "duration": r.get("fields", {}).get("Duration Minutes", 30),
        "price_range": r.get("fields", {}).get("Price Range", ""),
    } for r in records]

    return jsonify({
        "ok": True,
        "business_name": contractor.get("Business Name", "Your Service Provider"),
        "services": services
    })


@app.route("/book-availability")
def book_availability():
    contractor_key = (request.args.get("c") or "").strip()
    service_id = (request.args.get("service_id") or "").strip()
    date_str = (request.args.get("date") or "").strip()
    contractor = get_contractor_by_twilio_number(contractor_key) if contractor_key else {}
    if not contractor:
        return jsonify({"ok": False, "error": "Contractor not found"}), 404

    from app.app.cal_service import get_available_slots, _build_calendar_service

    service, _, _ = _build_calendar_service(contractor)
    if not service:
        return jsonify({"ok": True, "slots": [], "calendar_connected": False})

    AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
    AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
    svc_resp = requests.get(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblX8znMQVi443I4U/{service_id}",
        headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    )
    duration = svc_resp.json().get("fields", {}).get("Duration Minutes", 30)
    slots = get_available_slots(contractor, date_str, duration)
    return jsonify({"ok": True, "slots": slots, "calendar_connected": True})


@app.route("/book-submit", methods=["POST"])
def book_submit():
    data = request.get_json(silent=True) or {}
    contractor_key = (data.get("c") or "").strip()
    contractor = get_contractor_by_twilio_number(contractor_key) if contractor_key else {}
    if not contractor:
        return jsonify({"ok": False, "error": "Contractor not found"}), 404

    service_id = data.get("service_id", "")
    customer_name = (data.get("customer_name") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()
    service_address = (data.get("service_address") or "").strip()
    job_description = (data.get("job_description") or "").strip()
    start_iso = data.get("start_iso", "")
    end_iso = data.get("end_iso", "")

    if not (customer_name and customer_phone and start_iso and end_iso):
        return jsonify({"ok": False, "error": "Missing required fields"}), 400

    AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
    AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
    business_name = contractor.get("Business Name", "Your Service Provider")

    svc_resp = requests.get(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblX8znMQVi443I4U/{service_id}",
        headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    )
    service_name = svc_resp.json().get("fields", {}).get("Service Name", job_description or "Service")

    from app.app.cal_service import create_google_calendar_event
    cal_result = create_google_calendar_event(
        contractor=contractor,
        summary=f"{business_name} - {service_name} ({customer_name})",
        start_time=start_iso,
        end_time=end_iso,
        description=f"Booked via CrewCachePro\nPhone: {customer_phone}\nService: {service_name}",
        location=service_address,
    )
    print("BOOK SUBMIT | calendar result |", cal_result)

    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(start_iso)
        formatted_display = dt.strftime("%A, %B %-d at %-I:%M %p")
    except Exception:
        formatted_display = start_iso

    at_headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json"
    }
    lead_resp = requests.post(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1",
        headers=at_headers,
        json={"fields": {
            "fldBktJv26lpFCZjg": customer_name,
            "fldfSFcMA4V5SLfjo": customer_phone,
            "fldo9GtQBLObByZs5": service_address,
            "fldxNwWbbMWF4cT47": service_name,
            "fldkTOouWuLx6JHly": formatted_display,
            "fldHL2tJs2egGKuI9": "Booked",
            "fldbtGSgcOrHHe6pO": "STANDARD",
            "fldAgsSlZfOLFCBrJ": contractor_key,
            "fldIfaFlPA4AyMntY": start_iso,
        }}
    )
    print("BOOK SUBMIT | lead created |", lead_resp.status_code)

    send_fallback_sms(
        to_number=customer_phone,
        body=f"Hi {customer_name.split()[0]}! Your {service_name} appointment with {business_name} is confirmed for {formatted_display}. Reply CANCEL APPOINTMENT to cancel."
    )

    notify_sms = (contractor.get("Notify SMS") or "").strip()
    if notify_sms:
        send_fallback_sms(
            to_number=notify_sms,
            body=f"New booking: {customer_name} - {service_name} - {formatted_display}"
        )

    return jsonify({"ok": True, "confirmation": formatted_display, "service_name": service_name})

# ─────────────────────────────────────────────
# Contractor on-site photo estimate flow
# ─────────────────────────────────────────────

def handle_contractor_photo_estimate(request, contractor, from_number, to_number, num_media, incoming_msg=""):
    """
    Contractor texts photos to their own Twilio number.
    Claude analyzes photos → generates PDF estimate → texts link back to contractor.
    Runs in background thread so Twilio doesn't timeout.
    """
    business_name = (contractor.get("Business Name") or "Your Business").strip()
    timestamp = int(time.time())
    twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    # Capture media URLs from request BEFORE threading
    media_urls_raw = []
    for i in range(num_media):
        url = request.form.get(f"MediaUrl{i}", "")
        if url:
            media_urls_raw.append(url)

    print(f"CONTRACTOR PHOTO ESTIMATE | from: {from_number} | photos: {num_media}")

    import threading

    def process_photos_background():
        with app.app_context():
            # ── Step 1: Download photos from Twilio MMS ────────────────────────
            photo_urls = []
            for i, media_url in enumerate(media_urls_raw):
                try:
                    r = requests.get(media_url, auth=(twilio_account_sid, twilio_auth_token), timeout=15)
                    r.raise_for_status()
                    photo_urls.append(r.content)
                    print(f"PHOTO DOWNLOADED | {i+1} of {len(media_urls_raw)} | {len(r.content)} bytes")
                except Exception as e:
                    print(f"PHOTO DOWNLOAD ERROR | {i} |", e)

            if not photo_urls:
                tc = Client(twilio_account_sid, twilio_auth_token)
                tc.messages.create(
                    body="Could not download your photos. Please try again.",
                    from_=to_number,
                    to=from_number
                )
                return

            # ── Step 2: Upload to Cloudinary ───────────────────────────────────
            estimate_id = f"est_{timestamp}"
            cloudinary_urls = []
            for i, photo_data in enumerate(photo_urls):
                result = upload_photo(photo_data, estimate_id, i + 1)
                if result.get("ok"):
                    cloudinary_urls.append(result["url"])
                    print(f"CLOUDINARY SAVED | photo {i+1} | {result['url']}")

            # ── Step 3: Send photos to Claude Vision for analysis ──────────────
            try:
                claude_client = get_claude_client()

                image_blocks = []
                for photo_data in photo_urls[:25]:
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
                        "Base all pricing on current regional US contractor labor and material rates. "
                        "Use the job location if mentioned in the description to determine local market rates. "
                        "For example: DC/MD/VA metro area rates are higher than rural markets. "
                        "California (especially LA/SF Bay Area) rates are among the highest in the US. "
                        "Texas, Florida, and Southeast rates are typically moderate. "
                        "Midwest and rural rates are generally lower. "
                        "If no location is mentioned, use national average contractor rates. "
                        "Never use 0 as an amount. "
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
                return

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

                c.setFillColor(colors.HexColor('#333333'))
                c.setFont("Helvetica", 10)
                c.drawString(40, y, f"Date: {date.today().strftime('%B %d, %Y')}")
                c.drawRightString(width - 40, y, f"Estimate ID: {estimate_id}")
                y -= 20

                c.setStrokeColor(colors.HexColor('#2E7D4F'))
                c.setLineWidth(1.5)
                c.line(40, y, width - 40, y)
                y -= 20

                c.setFont("Helvetica-Bold", 10)
                c.setFillColor(colors.HexColor('#2E7D4F'))
                c.drawString(40, y, "JOB SUMMARY")
                y -= 16
                c.setFont("Helvetica", 10)
                c.setFillColor(colors.HexColor('#333333'))

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

                c.setFillColor(colors.HexColor('#1A4D2E'))
                c.rect(40, y - 4, width - 80, 22, fill=1, stroke=0)
                c.setFillColor(colors.white)
                c.setFont("Helvetica-Bold", 9)
                c.drawString(48, y + 4, "DESCRIPTION")
                c.drawRightString(width - 160, y + 4, "QTY")
                c.drawRightString(width - 100, y + 4, "UNIT")
                c.drawRightString(width - 44, y + 4, "AMOUNT")
                y -= 26

                subtotal = 0.0
                line_items = estimate_data.get("line_items", [])
                for i, item in enumerate(line_items):
                    amt = float(item.get("amount", 0))
                    subtotal += amt

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

                y -= 16
                c.setFont("Helvetica", 8)
                c.setFillColor(colors.HexColor('#666666'))
                detail = item.get("detail", "")[:80]
                c.drawString(48, y, detail)
                y -= 16

                y -= 8
                c.setStrokeColor(colors.HexColor('#CCCCCC'))
                c.setLineWidth(0.5)
                c.line(40, y, width - 40, y)
                y -= 18

                c.setFillColor(colors.HexColor('#1A4D2E'))
                c.rect(40, y - 6, width - 80, 26, fill=1, stroke=0)
                c.setFillColor(colors.HexColor('#F4A828'))
                c.setFont("Helvetica-Bold", 14)
                c.drawRightString(width - 44, y + 6, f"${subtotal:,.2f}")
                c.setFillColor(colors.white)
                c.setFont("Helvetica-Bold", 11)
                c.drawString(48, y + 6, "TOTAL ESTIMATE")
                y -= 40

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

                c.setFont("Helvetica", 8)
                c.setFillColor(colors.HexColor('#888888'))
                c.drawString(40, y, "Ballpark estimate based on photos only. Final pricing may vary after on-site inspection.")

                c.setFillColor(colors.HexColor('#1A4D2E'))
                c.rect(0, 0, width, 36, fill=1, stroke=0)
                c.setFillColor(colors.white)
                c.setFont("Helvetica", 9)
                c.drawCentredString(width / 2, 13, f"{business_name}  •  Professional Contractor Services")

                c.save()
                pdf_buffer.seek(0)
                pdf_bytes = pdf_buffer.read()
                print(f"PDF GENERATED | {len(pdf_bytes)} bytes")

                # SAVE TO TEMP FILE FOR EMAIL ATTACHMENT
                import tempfile
                pdf_temp_path = f"/tmp/estimate_{estimate_id}.pdf"
                with open(pdf_temp_path, "wb") as f:
                    f.write(pdf_bytes)

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
                return

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

            # EMAIL PDF TO CONTRACTOR ← OUTSIDE the except block
            try:
                notify_email = contractor.get("Notify Email", "").strip()
                if notify_email and pdf_temp_path:
                    email_body = (
                        f"📋 Estimate Ready — {business_name}\n\n"
                        f"Job: {estimate_data.get('job_summary', '')}\n"
                        f"Total: ${subtotal:,.2f}\n\n"
                        f"PDF attached — review, adjust if needed, and forward to customer.\n\n"
                        f"Cloudinary link: {pdf_url or 'Not available'}"
                    )
                    send_email(
                        subject=f"📋 Estimate — {business_name} | ${subtotal:,.2f}",
                        body=email_body,
                        to_email=notify_email,
                        attachment_path=pdf_temp_path,
                    )
                    print(f"ESTIMATE EMAIL SENT | {notify_email}")
            except Exception as e:
                print(f"ESTIMATE EMAIL ERROR | {e}")

            # ── Step 6: Text PDF link to contractor ────────────────────────────
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
                    items_text = "\n".join([
                        f"• {i.get('description')}: ${float(i.get('amount', 0)):,.2f}"
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

    # Start background thread — return immediately to Twilio
    thread = threading.Thread(target=process_photos_background)
    thread.daemon = True
    thread.start()

    return Response(
        "<Response><Message>📸 Got your photos! Generating your estimate now — you'll receive it in about 30 seconds.</Message></Response>",
        mimetype="text/xml"
    )

def handle_sms_job_complete(incoming_msg: str, from_number: str, to_number: str):
    """
    Handles SMS job completion flow.
    Contractor texts DONE 1 or DONE 1 75 to mark a job complete and send payment.
    """
    import re
    from twilio.rest import Client as TwilioClient

    tc = TwilioClient(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))

    def reply(msg):
        tc.messages.create(body=msg, from_=to_number, to=from_number)
        return Response("<Response></Response>", mimetype="text/xml")

    # Check if this is a payment method selection (reply to previous DONE)
    pending_key = f"pending_complete:{to_number}:{from_number}"
    if redis_client:
        pending_raw = redis_client.get(pending_key)
        if pending_raw and incoming_msg.strip() in ["1", "2", "3", "4"]:
            pending = json.loads(pending_raw)
            return handle_payment_selection(
                incoming_msg.strip(), pending, from_number, to_number, tc, pending_key
            )

    # Parse DONE command
    parts = incoming_msg.strip().split()
    if len(parts) < 2:
        return reply("Send COMPLETED followed by job number. Example: DONE 1\nOr include amount: DONE 1 75")

    try:
        job_num = int(parts[1])
    except ValueError:
        return reply("Invalid job number. Example: DONE 1")

    # Get custom amount if provided
    custom_amount = None
    if len(parts) >= 3:
        try:
            custom_amount = float(parts[2])
        except ValueError:
            pass

    # Look up today's jobs from Redis
    from zoneinfo import ZoneInfo
    from datetime import datetime
    eastern = ZoneInfo("America/New_York")
    today_str = datetime.now(eastern).strftime("%Y-%m-%d")
    job_key = f"daily_jobs:{to_number}:{today_str}"

    if not redis_client:
        return reply("System error — Redis not available.")

    jobs_raw = redis_client.get(job_key)
    if not jobs_raw:
        return reply("No jobs found for today. Make sure you received your daily briefing at 6 AM.")

    jobs = json.loads(jobs_raw)

    if job_num < 1 or job_num > len(jobs):
        return reply(f"Invalid job number. You have {len(jobs)} job(s) today. Send DONE 1 through DONE {len(jobs)}.")

    job = jobs[job_num - 1]
    customer_name = job.get("name", "Customer")
    customer_phone = job.get("phone", "")
    job_description = job.get("job", "")
    record_id = job.get("record_id", "")
    is_regular = job.get("is_regular", False)
    # Try to get amount from Regular Clients table
    amount = custom_amount
    if not amount and is_regular:
        try:
            import requests as req
            AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
            AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
            headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
            regular_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl3LAJzXa6Vsexry"
            resp = req.get(regular_url, headers=headers, params={
                "filterByFormula": f"{{Phone}} = '{customer_phone}'"
            })
            records = resp.json().get("records", [])
            if records:
                amount = records[0].get("fields", {}).get("Monthly Amount", 0)
                # Also advance Next Appointment for regular client
                freq = records[0].get("fields", {}).get("Frequency Days", 7)
                from datetime import timedelta
                next_date = (datetime.now(eastern) + timedelta(days=int(freq))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                req.patch(
                    f"{regular_url}/{records[0].get('id')}",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"fields": {"Next Appointment": next_date, "Last Completed": datetime.now(eastern).strftime("%Y-%m-%dT%H:%M:%S.000Z")}}
                )
                print(f"REGULAR CLIENT ADVANCED | {customer_name} | Next: {next_date}")
        except Exception as e:
            print(f"REGULAR CLIENT LOOKUP ERROR | {e}")

    # Store pending completion in Redis (expires in 5 minutes)
    pending = {
        "record_id": record_id,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "job_description": job_description,
        "amount": amount or 0,
        "twilio_number": to_number,
    }
    redis_client.setex(pending_key, 300, json.dumps(pending))

    # Ask for payment method
    amount_str = f"${amount:.2f}" if amount else "amount TBD"
    msg = (
        f"Job {job_num}: {customer_name}\n"
        f"{job_description}\n"
        f"Amount: {amount_str}\n\n"
        f"Select payment:\n"
        f"1 - 💳 Stripe\n"
        f"2 - 🏦 Zelle\n"
        f"3 - 📚 QuickBooks\n"
        f"4 - 💵 Cash"
    )
    return reply(msg)


def handle_payment_selection(
    choice: str,
    pending: dict,
    from_number: str,
    to_number: str,
    tc,
    pending_key: str
):
    """Processes the payment method selection after DONE command."""
    import requests as req

    def reply(msg):
        tc.messages.create(body=msg, from_=to_number, to=from_number)
        return Response("<Response></Response>", mimetype="text/xml")

    record_id = pending.get("record_id", "")
    customer_name = pending.get("customer_name", "")
    customer_phone = pending.get("customer_phone", "")
    job_description = pending.get("job_description", "")
    amount = float(pending.get("amount", 0))
    twilio_number = pending.get("twilio_number", "")

    AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
    AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json"
    }

    # Mark lead as complete
    if record_id:
        req.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1/{record_id}",
            headers=headers,
            json={"fields": {"Lead Status": "Completed"}}
        )

    # Get contractor record ID
    contractor = get_contractor_by_twilio_number(twilio_number) or {}
    business_name = contractor.get("Business Name", "your contractor")
    contractor_record_id = contractor.get("airtable_id", "")

    payment_methods = {"1": "Stripe", "2": "Zelle ", "3": "QuickBooks", "4": "Cash"}
    payment_method = payment_methods.get(choice, "Cash")
    today = __import__('datetime').datetime.now().strftime("%Y-%m-%d")

    # Create payment record
    payment_fields = {
        "fldAZ5Qr0NCU11J0A": customer_name,
        "fld8bUzdzFeeXLrlD": customer_phone,
        "fld596bZM5ZCI7ga8": amount,
        "fldeROEzoyhWKJ36y": job_description,
        "fldWg6gGv6dKFb853": "Unpaid",
        "fldUFO1PfTeiLA3UR": payment_method,
        "fldYNu0gpLuiCsF6Z": today,
    }

    if contractor_record_id:
        payment_fields["fldxdSy7mICyTo50P"] = [contractor_record_id]

    if payment_method in ["Stripe", "Zelle "]:
        payment_fields["fldEifNosHbfRIzwu"] = True  # Send Payment Request

    if payment_method == "QuickBooks":
        # Get customer email from Recurring Customers table
        try:
            recurring_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblxGfrifBiGRk80M"
            resp = req.get(recurring_url, {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}, params={
                "filterByFormula": f"OR({{Phone}} = '{customer_phone}', {{Client Name}} = '{customer_name}')"
            })
            records = resp.json().get("records", [])
            if records:
                email = records[0].get("fields", {}).get("Email", "")
                if email:
                    payment_fields["fld1J5DuxJVcreFKk"] = email
                    payment_fields["fldmTaAGMRf5aafaE"] = True  # Send Invoice
        except Exception as e:
            print(f"QB EMAIL LOOKUP ERROR | {e}")

    req.post(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments",
        headers=headers,
        json={"fields": payment_fields}
    )

    # Clear pending state
    if redis_client:
        redis_client.delete(pending_key)

    method_labels = {
        "1": f"Stripe link sent to {customer_name}",
        "2": f"Zelle request sent to {customer_name}",
        "3": f"QuickBooks invoice sent",
        "4": "Cash payment recorded"
    }

    return reply(f"✅ Job complete!\n{method_labels.get(choice, 'Payment processed')}\n\nHave a great rest of the day! 💪")
   


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
    if incoming_msg.lower() in ["stop", "unsubscribe", "quit", "end"]:
        return Response(
            "<Response><Message>You have been unsubscribed. Reply START to resubscribe.</Message></Response>",
            mimetype="text/xml"
        )

    if incoming_msg.lower() in ["start", "unstop"]:
        return Response(
            "<Response><Message>You are now subscribed to receive messages.</Message></Response>",
            mimetype="text/xml"
        )

    # ── Cancel / Reschedule handler ────────────────────────────────
    if incoming_msg.upper() in ["CANCEL APPOINTMENT", "RESCHEDULE", "RESCHEDULE APPOINTMENT"]:
        from app.app.cancel_reschedule import handle_cancel_reschedule
        return handle_cancel_reschedule()

    # ── Job completion via SMS ─────────────────────────────────────
    if incoming_msg.upper().startswith("COMPLETED"):
        return handle_sms_job_complete(incoming_msg, from_number, to_number)

    # Look up contractor
    contractor = {}
    try:
        contractor = get_contractor_by_twilio_number(to_number) or {}
    except Exception as e:
        print("SMS CONTRACTOR LOOKUP FAILED:", e)

    business_name = (contractor.get("Business Name") or "our office").strip()

    # ── Subscription tier check ────────────────────────────────────
    from app.app.subscription_service import has_feature, get_upgrade_message
    subscription_active = has_feature(contractor, "sms_intake")
    if contractor and not subscription_active:
        return Response(
            "<Response><Message>This service is currently unavailable. Please contact your contractor directly.</Message></Response>",
            mimetype="text/xml"
        )

    # ── Contractor photo estimate flow ─────────────────────────────
    notify_sms = (contractor.get("Notify SMS") or "").strip()
    num_media = int(request.form.get("NumMedia", 0))
    num_media = min(num_media, 25)  # cap at 25
    if from_number == notify_sms and num_media > 0:
        if not has_feature(contractor, "photo_estimates"):
            return Response(
                f"<Response><Message>{get_upgrade_message('photo_estimates')}</Message></Response>",
                mimetype="text/xml"
            )
        return handle_contractor_photo_estimate(request, contractor, from_number, to_number, num_media, incoming_msg)

    # ── CUSTOMER SERVICE MODE ──────────────────────────────────────
    # Check if this customer already exists in our system
    # If yes — handle their question intelligently instead of running intake
    try:
        existing_lead = lookup_lead_by_phone(from_number, to_number)
        if existing_lead:
            return handle_customer_service(
                incoming_msg, from_number, to_number,
                existing_lead, contractor, business_name
            )
    except Exception as e:
        print(f"CUSTOMER SERVICE LOOKUP ERROR | {e}")
    # ── END CUSTOMER SERVICE MODE ──────────────────────────────────

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
        from app.app.mapbox_service import is_address_in_service_area
        addr = collected["collected_address"]
        home_lat = float(contractor.get("Home Base Lat") or 38.9427)
        home_lon = float(contractor.get("Home Base Lon") or -76.7300)
        max_miles = float(contractor.get("Max Radius Miles") or 50.0)
        hard_max = float(contractor.get("Hard Max Miles") or 35.0)

        check = is_address_in_service_area(addr, home_lat, home_lon, max_miles, hard_max)

        if check.get("ok") and check.get("in_range"):
            sms_state["service_address"] = check.get("place_name") or addr
            print("ADDRESS VALIDATED |", addr, "|", check.get("distance_miles"), "miles away")
        elif check.get("ok") and not check.get("in_range"):
            miles = check.get("distance_miles")
            reply = (
                f"Thanks for reaching out! Unfortunately that address is outside our "
                f"service area ({miles} miles away — we currently serve within "
                f"{int(hard_max)} miles)."
            )
            return Response(
                f"<Response><Message>{reply}</Message></Response>",
                mimetype="text/xml"
            )
        else:
            sms_state["service_address"] = addr
            print("MAPBOX FAILED | Accepting address anyway |", addr)
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
        sms_state["priority"] = "STANDARD"
        sms_state["call_sid"] = f"SMS-{from_number}-{int(time.time())}"

        notify_email = (contractor.get("Notify Email") or os.getenv("TO_EMAIL") or "").strip()
        try:
            send_intake_summary(sms_state, notify_email=notify_email)
        except Exception as e:
            print("SMS INTAKE SUMMARY ERROR |", e)

        # After lead created in SMS route
        try:
            send_push_notification(
                twilio_number=to_number,
                title="🔔 New Lead!",
                
                message=f"{sms_state.get('name', 'New Customer')} — {sms_state.get('job_description', '')[:60]}",
                url="/dashboard"
            )
        except Exception as e:
            print(f"PUSH NOTIFICATION ERROR | {e}")

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

        if has_feature(contractor, "photo_estimates"):
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

    # Normal mid-conversation — save state and reply
    messages.append({"role": "user", "content": incoming_msg})
    messages.append({"role": "assistant", "content": reply})
    sms_state["messages"] = messages[-20:]

    if redis_client:
        try:
            redis_client.setex(sms_state_key, 7200, json.dumps(sms_state))
            print("SMS STATE SAVED |", sms_state_key, "| name:", sms_state.get("name"), "| address:", sms_state.get("service_address"))
        except Exception as e:
            print("SMS STATE SAVE ERROR |", e)

    return Response(
        f"<Response><Message>{reply}</Message></Response>",
        mimetype="text/xml"
    )
  

@app.route("/create-payment-link", methods=["POST"])
def create_payment_link_route():
    try:
        data = request.get_json(silent=True) or {}
        record_id = data.get("record_id")
        amount = data.get("amount")
        customer_name = data.get("customer_name")
        job_description = data.get("job_description")
        customer_phone = data.get("customer_phone")
        twilio_number = data.get("twilio_number", "")
        business_name = data.get("business_name", "")

        # Look up contractor once - used for business name fallback AND Stripe Connect routing
        contractor = {}
        if twilio_number:
            try:
                contractor = get_contractor_by_twilio_number(twilio_number) or {}
            except Exception as e:
                print(f"CONTRACTOR LOOKUP ERROR | {e}")

        if not business_name or business_name.strip() == "" or business_name == "Your Contractor":
            business_name = contractor.get("Business Name", "").strip()

        if not business_name:
            try:
                AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
                AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
                payments_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments"
                headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
                resp = requests.get(f"{payments_url}/{record_id}", headers=headers)
                fields = resp.json().get("fields", {})
                contractor_links = fields.get("Contractor", [])
                if contractor_links:
                    CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")
                    contractors_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CONTRACTORS_TABLE}"
                    contractor_id = contractor_links[0]
                    c_resp = requests.get(f"{contractors_url}/{contractor_id}", headers=headers)
                    business_name = c_resp.json().get("fields", {}).get("Business Name", "").strip()
            except Exception as e:
                print(f"PAYMENT RECORD LOOKUP ERROR | {e}")

        if not business_name:
            business_name = "MME Lawn Care And More"
            print(f"WARNING | business_name fell through all lookups for record {record_id}")

        print(f"CREATE PAYMENT LINK | business_name: {business_name} | customer: {customer_name}")

        stripe_account_id = (contractor.get("Stripe Account ID") or "").strip()
        stripe_charges_enabled = bool(contractor.get("Stripe Charges Enabled"))

        if stripe_account_id and stripe_charges_enabled:
            from app.app.stripe_service import create_connect_payment_link
            result = create_connect_payment_link(
                amount, customer_name, job_description, record_id, business_name,
                contractor_stripe_account_id=stripe_account_id,
                application_fee_percent=1.0,
            )
            print(f"CREATE PAYMENT LINK | Routed via Connect | account: {stripe_account_id}")
        else:
            from app.app.stripe_service import create_payment_link
            result = create_payment_link(amount, customer_name, job_description, record_id, business_name)
            print(f"CREATE PAYMENT LINK | Routed via platform account (no Connect) | twilio: {twilio_number}")

        if result.get("ok"):
            msg = (
                f"Hi {customer_name.split()[0]}! Your job is complete. "
                f"Please pay your balance of ${amount} here: {result['url']} "
                f"Thank you for choosing {business_name}!"
            )
            send_fallback_sms(to_number=customer_phone, body=msg)
        return result
    except Exception as e:
        print(f"CREATE PAYMENT LINK ERROR | {e}")
        return {"ok": False, "error": str(e)}



@app.route("/stripe-connect-return")
def stripe_connect_return():
    account_id = request.args.get("account_id", "")
    try:
        from app.app.stripe_service import check_account_status
        is_enabled = check_account_status(account_id)
        if is_enabled:
            AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
            AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
            resp = requests.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{os.environ.get('AIRTABLE_CONTRACTORS_TABLE')}",
                headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"},
                params={"filterByFormula": f"{{Stripe Account ID}} = '{account_id}'"}
            )
            records = resp.json().get("records", [])
            if records:
                requests.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{os.environ.get('AIRTABLE_CONTRACTORS_TABLE')}/{records[0]['id']}",
                    headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Stripe Charges Enabled": True}}
                )
    except Exception as e:
        print(f"STRIPE CONNECT RETURN ERROR | {e}")

    return """
    <html><body style="font-family:sans-serif;text-align:center;padding:60px;background:#0f172a;color:white;">
        <h2>Stripe account connected!</h2>
        <p>You can close this window and return to your dashboard.</p>
    </body></html>
    """


@app.route("/stripe-connect-refresh")
def stripe_connect_refresh():
    account_id = request.args.get("account_id", "")
    from app.app.stripe_service import create_account_onboarding_link
    result = create_account_onboarding_link(account_id)
    if result.get("ok"):
        return redirect(result["url"])
    return "Could not refresh onboarding link.", 500
    
   


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    # Verify signature ONCE here — do not re-verify inside handlers
    try:
        import stripe
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError as e:
        print(f"STRIPE WEBHOOK | Invalid signature | {e}")
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        print(f"STRIPE WEBHOOK | Payload error | {e}")
        return jsonify({"error": "Bad payload"}), 400

    # Route to the correct handler — pass the already-verified event
    try:
        from app.app.stripe_service import handle_stripe_event
        from app.app.subscription_service import handle_subscription_event

        # Payment events (charges, payment intents)
        payment_result = handle_stripe_event(event)
        if not payment_result.get("ok"):
            print(f"STRIPE WEBHOOK | Payment handler error | {payment_result}")

        # Subscription lifecycle events
        sub_result = handle_subscription_event(event)
        if not sub_result.get("ok"):
            print(f"STRIPE WEBHOOK | Subscription handler error | {sub_result}")

        return jsonify({"ok": True}), 200

    except Exception as e:
        import traceback
        print(f"STRIPE WEBHOOK ERROR | {type(e).__name__} | {e}")
        print(traceback.format_exc())
        # Return 500 so Stripe retries — do NOT return 200 on a crash
        return jsonify({"ok": False}), 500


@app.route("/payment-success")
def payment_success():
    return """
    <html>
    <body style="font-family:sans-serif;text-align:center;padding:60px;">
        <h2>✅ Payment received!</h2>
        <p>Thank you for your business. Your contractor will be in touch shortly.</p>
        <p style="color:#888;font-size:13px;">Powered by CrewCachePro</p>
    </body>
    </html>
    """


@app.route("/subscribe/<tier>", methods=["GET"])
def subscribe(tier):
    """Generates a Stripe checkout link for contractor signup."""
    from app.app.contractor_onboarding import create_checkout_session

    business_name = request.args.get("business", "New Contractor")
    email = request.args.get("email", "")
    record_id = request.args.get("record_id", "")
    tier = tier.capitalize()

    # Trial signups do not go through this route
    if tier == "Trial":
        return jsonify({"error": "Trial signups are handled separately."}), 400

    if tier not in ["Basic", "Pro"]:
        return jsonify({"error": "Invalid tier. Choose Basic or Pro."}), 400

    result = create_checkout_session(tier, business_name, email, record_id)
    if result.get("ok"):
        return redirect(result["url"])

    print(f"SUBSCRIBE | Checkout session failed | {result}")
    return jsonify(result), 500


@app.route("/subscription-success")
def subscription_success():
    return """
    <html>
    <body style="font-family:sans-serif;text-align:center;padding:60px;">
        <h2>✅ You're in!</h2>
        <p>Welcome to CrewCachePro. Your subscription is active.</p>
        <p>We'll be in touch shortly to get your account set up.</p>
        <p style="color:#888;font-size:13px;">CrewCachePro</p>
    </body>
    </html>
    """


@app.route("/subscription-cancel")
def subscription_cancel():
    return """
    <html>
    <body style="font-family:sans-serif;text-align:center;padding:60px;">
        <h2>No problem!</h2>
        <p>If you have questions about CrewCachePro, reply to this message anytime.</p>
        <p style="color:#888;font-size:13px;">CrewCachePro</p>
    </body>
    </html>
    """
 
        
  


 
 

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

            # ADDED: Write appointment date back to Airtable lead record
            if phone and start_time:
                try:
                    update_lead_appointment_date(phone, start_time, name)
                except Exception as e:
                    print("CAL WEBHOOK AIRTABLE UPDATE ERROR |", e)

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

        elif trigger in {"BOOKING_CANCELLED", "BOOKING.CANCELLED"}:
            # ADDED: Update lead status to Cancelled in Airtable
            attendee = payload.get("attendees", [])
            attendee0 = attendee[0] if attendee and isinstance(attendee[0], dict) else {}
            phone = (
                response_value("attendeePhoneNumber")
                or str(attendee0.get("phoneNumber") or "")
                or str(attendee0.get("phone") or "")
            ).strip()
            if phone:
                try:
                    update_lead_status_by_phone(phone, "Cancelled")
                    print("CAL WEBHOOK | Booking cancelled | phone:", phone)
                except Exception as e:
                    print("CAL WEBHOOK CANCEL UPDATE ERROR |", e)

        elif trigger in {"BOOKING_RESCHEDULED", "BOOKING.RESCHEDULED"}:
            # ADDED: Update appointment date in Airtable when rescheduled
            attendee = payload.get("attendees", [])
            attendee0 = attendee[0] if attendee and isinstance(attendee[0], dict) else {}
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
            name = str(attendee0.get("name") or "").strip()
            if phone and start_time:
                try:
                    update_lead_appointment_date(phone, start_time, name)
                    print("CAL WEBHOOK | Booking rescheduled | phone:", phone)
                except Exception as e:
                    print("CAL WEBHOOK RESCHEDULE UPDATE ERROR |", e)

        return "", 200

    except Exception as e:
        print("WEBHOOK ERROR:", e)
        return "", 500


@app.route("/cal-booking-notify", methods=["POST"])
def cal_booking_notify():
    try:
        data = request.get_json(silent=True) or {}
        trigger = data.get("triggerEvent", "")
        payload = data.get("payload", {})

        attendees = payload.get("attendees", [])
        customer_name = attendees[0].get("name", "Unknown") if attendees else "Unknown"
        customer_phone = attendees[0].get("phoneNumber", "") if attendees else ""

        raw_time = payload.get("startTime", "")
        try:
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            eastern = dt.astimezone(ZoneInfo("America/New_York"))
            start_time = eastern.strftime("%A %B %d at %I:%M %p EST")
        except Exception:
            start_time = raw_time

        title = payload.get("title", "Appointment")

        if trigger == "BOOKING_CREATED":
            emoji = "📅"
            action = "New Booking"
        elif trigger == "BOOKING_CANCELLED":
            emoji = "❌"
            action = "Booking Cancelled"
        elif trigger == "BOOKING_RESCHEDULED":
            emoji = "🔄"
            action = "Booking Rescheduled"
        else:
            return {"ok": True}

        msg = (
            f"{emoji} {action}\n"
            f"Customer: {customer_name}\n"
            f"Phone: {customer_phone}\n"
            f"Time: {start_time}\n"
            f"Event: {title}"
        )

        # FIXED: Look up contractor by Twilio number properly
        try:
            contractor = get_contractor_by_twilio_number(os.getenv("TWILIO_PHONE_NUMBER")) or {}
        except Exception:
            contractor = {}

        notify_sms = contractor.get("Notify SMS") or os.getenv("NOTIFY_SMS")
        if notify_sms:
            send_fallback_sms(to_number=notify_sms, body=msg)

        return {"ok": True}

    except Exception as e:
        print("CAL BOOKING NOTIFY ERROR |", e)
        return {"ok": False}


def update_lead_appointment_date(phone: str, start_time: str, name: str = "") -> None:
    """
    Finds a lead in Airtable by phone number and updates
    the Appointment Date field with the confirmed booking time.
    """
    try:
        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        LEADS_TABLE = "tbl6YL7BYY2vawIF1"

        leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{LEADS_TABLE}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        # Try multiple phone formats to match however Airtable stores it
        phone_formats = [
            phone,                                                  # +17632132731
            phone.replace("+1", ""),                               # 7632132731
            phone.replace("+", ""),                                # 17632132731
            f"+1{phone.replace('+1', '').replace('+', '')}"       # +17632132731 normalized
        ]

        records = []
        for fmt in phone_formats:
            params = {"filterByFormula": f"{{fldfSFcMA4V5SLfjo}} = '{fmt}'"}
            response = requests.get(leads_url, headers=headers, params=params)
            records = response.json().get("records", [])
            if records:
                print(f"CAL WEBHOOK | Lead found with format: {fmt}")
                break

        if not records:
            # Try FIND as fallback
            normalized = phone.replace("+1", "").replace("-", "").replace(" ", "").strip()
            params = {"filterByFormula": f"FIND('{normalized}', {{Callback Number}})"}
            response = requests.get(leads_url, headers=headers, params=params)
            records = response.json().get("records", [])

        if not records:
            print(f"CAL WEBHOOK | No lead found for phone {phone}")
            return

        record_id = records[0]["id"]

        try:
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            # Store in UTC — Airtable displays in the user's local timezone
            airtable_date = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            # For display in logs only — convert to Eastern
            eastern = dt.astimezone(ZoneInfo("America/New_York"))
            formatted_display = eastern.strftime("%A, %B %-d at %-I:%M %p ET")
        except Exception:
            airtable_date = start_time
            formatted_display = start_time
            

        update_response = requests.patch(
            f"{leads_url}/{record_id}",
            headers=headers,
            json={"fields": {
                "Appointment Date and Time": airtable_date,
                "Lead Status": "Booked"
            }}
        )

        if update_response.status_code == 200:
            print(f"CAL WEBHOOK | Appointment Date updated | {record_id} | {formatted_display}")
        else:
            print(f"CAL WEBHOOK | Airtable update failed | {update_response.status_code} | {update_response.text}")

    except Exception as e:
        print(f"UPDATE LEAD APPOINTMENT ERROR | {e}")


def update_lead_status_by_phone(phone: str, status: str) -> None:
    """Updates Lead Status in Airtable when a booking is cancelled."""
    try:
        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        LEADS_TABLE = "Leads"

        leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{LEADS_TABLE}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        # Try multiple phone formats to match however Airtable stores it
        phone_formats = [
            phone,
            phone.replace("+1", ""),
            phone.replace("+", ""),
            f"+1{phone.replace('+1', '').replace('+', '')}"
        ]

        records = []
        for fmt in phone_formats:
            params = {"filterByFormula": f"{{Callback Number}} = '{fmt}'"}
            response = requests.get(leads_url, headers=headers, params=params)
            records = response.json().get("records", [])
            if records:
                print(f"CAL WEBHOOK | Lead found with format: {fmt}")
                break

        if not records:
            normalized = phone.replace("+1", "").replace("-", "").replace(" ", "").strip()
            params = {"filterByFormula": f"FIND('{normalized}', {{Callback Number}})"}
            response = requests.get(leads_url, headers=headers, params=params)
            records = response.json().get("records", [])

        if not records:
            print(f"CAL WEBHOOK | No lead found for cancel | phone {phone}")
            return

        record_id = records[0]["id"]
        requests.patch(
            f"{leads_url}/{record_id}",
            headers=headers,
            json={"fields": {"Lead Status": status}}
        )
        print(f"LEAD STATUS UPDATED | {record_id} | {status}")

    except Exception as e:
        print(f"UPDATE LEAD STATUS ERROR | {e}")

@app.route("/send-job-reminders", methods=["POST", "GET"])
def send_job_reminders():
    """
    Runs nightly at 7 PM Eastern via Render cron job.
    Finds all leads AND regular clients with appointments tomorrow
    and sends SMS reminders to customers.
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        import requests as req

        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        tomorrow = now + timedelta(days=1)
        tomorrow_date = tomorrow.strftime("%Y-%m-%d")

        print(f"JOB REMINDER | Running for date: {tomorrow_date}")

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        sent = 0
        failed = 0
        reminders = []  # unified list of jobs to remind

        # ── Pull from Leads table ──────────────────────────────
        leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"
        filter_formula = (
            f"AND("
            f"{{Lead Status}} = 'Booked', "
            f"{{Archived}} != TRUE(), "
            f"IS_SAME({{Appointment Date and Time}}, '{tomorrow_date}', 'day')"
            f")"
        )
        response = req.get(leads_url, headers=headers, params={"filterByFormula": filter_formula})
        lead_records = response.json().get("records", [])
        print(f"JOB REMINDER | Leads table: {len(lead_records)} appointments for tomorrow")

        for record in lead_records:
            fields = record.get("fields", {})
            appointment_dt = fields.get("Appointment Date and Time", "")
            try:
                dt = datetime.fromisoformat(appointment_dt.replace("Z", "+00:00"))
                dt_eastern = dt.astimezone(eastern)
                formatted_time = dt_eastern.strftime("%-I:%M %p")
            except Exception:
                formatted_time = "your scheduled time"

            reminders.append({
                "name": fields.get("Client Name", "there"),
                "phone": fields.get("Call Back Number", ""),
                "twilio_number": (fields.get("Twilio Number") or [""])[0] if isinstance(fields.get("Twilio Number"), list) else fields.get("Twilio Number", ""),
                "formatted_time": formatted_time,
            })

        # ── Pull from Regular Clients table ───────────────────
        regular_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl3LAJzXa6Vsexry"
        regular_resp = req.get(regular_url, headers=headers, params={
            "filterByFormula": f"AND({{Active}} = TRUE(), {{Next Appointment}} != '')"
        })
        regular_records = regular_resp.json().get("records", [])
        print(f"JOB REMINDER | Regular clients table: checking {len(regular_records)} records")

        for record in regular_records:
            fields = record.get("fields", {})
            next_appt = fields.get("Next Appointment", "")
            if not next_appt:
                continue
            try:
                dt = datetime.fromisoformat(next_appt.replace("Z", "+00:00"))
                dt_eastern = dt.astimezone(eastern)
                if dt_eastern.strftime("%Y-%m-%d") != tomorrow_date:
                    continue
                formatted_time = dt_eastern.strftime("%-I:%M %p")
            except Exception:
                continue

            twilio_number = (fields.get("Twilio Number") or "").strip()
            customer_phone = (fields.get("Phone") or "").strip()
            if not customer_phone or not twilio_number:
                continue

            reminders.append({
                "name": fields.get("Client Name", "there"),
                "phone": customer_phone,
                "twilio_number": twilio_number,
                "formatted_time": formatted_time,
            })

        print(f"JOB REMINDER | Total reminders to send: {len(reminders)}")

        # ── Send all reminders ─────────────────────────────────
        for job in reminders:
            client_name = job["name"]
            first_name = client_name.split()[0] if client_name else "there"
            customer_phone = job["phone"]
            twilio_number = job["twilio_number"]

            if not customer_phone or not twilio_number:
                failed += 1
                continue

            contractor = get_contractor_by_twilio_number(twilio_number) or {}
            business_name = contractor.get("Business Name", "Your contractor")
            notify_sms = contractor.get("Notify SMS", twilio_number)

            msg = (
                f"Hi {first_name}! Just a reminder that {business_name} "
                f"is scheduled for tomorrow at {job['formatted_time']}. "
                f"We look forward to seeing you! "
                f"To reschedule please call or text {notify_sms}."
            )

            try:
                from twilio.rest import Client as TwilioClient
                tc = TwilioClient(
                    os.environ.get("TWILIO_ACCOUNT_SID"),
                    os.environ.get("TWILIO_AUTH_TOKEN")
                )
                tc.messages.create(
                    body=msg,
                    from_=twilio_number,
                    to=customer_phone
                )
                print(f"JOB REMINDER SENT | {client_name} | {customer_phone} | {job['formatted_time']}")
                sent += 1
            except Exception as e:
                print(f"JOB REMINDER SMS ERROR | {client_name} | {e}")
                failed += 1

        # Send summary to each contractor whose clients got reminders
        try:
            from collections import defaultdict
            contractor_reminders = defaultdict(list)
            for job in reminders:
                contractor_reminders[job["twilio_number"]].append(job["name"].split()[0])

            for twilio_num, names in contractor_reminders.items():
                contractor = get_contractor_by_twilio_number(twilio_num) or {}
                notify_sms = (contractor.get("Notify SMS") or "").strip()
                if not notify_sms:
                    continue
                summary = (
                    f"7PM Reminder Summary - {tomorrow_date}\n"
                    f"Sent reminders to {len(names)} client{'s' if len(names) != 1 else ''}:\n"
                    f"{', '.join(names)}"
                )
                send_fallback_sms(to_number=notify_sms, body=summary)
                print(f"JOB REMINDER SUMMARY | {twilio_num} | {len(names)} clients")
        except Exception as e:
            print(f"JOB REMINDER SUMMARY ERROR | {e}")

        return jsonify({
            "ok": True,
            "date": tomorrow_date,
            "sent": sent,
            "failed": failed,
            "total": len(reminders)
        }), 200

    except Exception as e:
        print(f"JOB REMINDER ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/send-daily-briefing", methods=["POST"])
def send_daily_briefing():
    # Verify secret token
    secret = request.headers.get("X-Briefing-Secret") or request.args.get("secret")
    if secret != os.environ.get("BRIEFING_SECRET"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    """
    Sends daily job briefing SMS to each contractor at 6 AM Eastern.
    Shows today's jobs, open leads count, and upcoming regular clients.
    """
    try:
        import requests as req
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta

        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        today_str = now.strftime("%Y-%m-%d")
        today_display = now.strftime("%A, %B %-d")

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

        # Get all active contractors
        contractors_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CONTRACTORS_TABLE}"
        contractors_resp = req.get(contractors_url, headers=headers, params={
            "filterByFormula": "{Active} = TRUE()"
        })
        contractors = contractors_resp.json().get("records", [])

        sent = 0
        failed = 0

        for contractor_record in contractors:
            try:
                fields = contractor_record.get("fields", {})
                twilio_number = (fields.get("Twilio Number") or "").strip()
                notify_sms = (fields.get("Notify SMS") or "").strip()
                business_name = (fields.get("Business Name") or "your business").strip()
                contractor_id = contractor_record.get("id", "")

                if not twilio_number or not notify_sms:
                    continue

                # Get today's booked jobs
                leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"
                # REPLACE WITH
                jobs_resp = req.get(leads_url, headers=headers, params={
                    "filterByFormula": (
                        f"AND("
                        f"{{Lead Status}} = 'Booked', "
                        f"{{Archived}} != TRUE(), "
                        f"{{Twilio Number}} = '{twilio_number}', "
                        f"{{Appointment Date and Time}} != ''"
                        f")"
                    )
                })
                job_records = jobs_resp.json().get("records", [])

                # Filter for today's jobs
                todays_jobs = []
                for r in job_records:
                    f = r.get("fields", {})
                    appt = f.get("Appointment Date and Time", "")
                    try:
                        dt = datetime.fromisoformat(appt.replace("Z", "+00:00"))
                        dt_eastern = dt.astimezone(eastern)
                        if dt_eastern.strftime("%Y-%m-%d") == today_str:
                            todays_jobs.append({
                                "record_id": r.get("id", ""),  # ADD THIS
                                "name": f.get("Client Name", "Unknown"),
                                "address": f.get("Service Address", ""),
                                "time": dt_eastern.strftime("%-I:%M %p"),
                                "job": f.get("Job Description", ""),
                                "phone": f.get("Call Back Number", ""),
                            })
                    except Exception:
                        pass
                        

                # Sort by time
                todays_jobs.sort(key=lambda x: x["time"])

                # Get open leads count
                leads_resp = req.get(leads_url, headers=headers, params={
                    "filterByFormula": (
                        f"AND("
                        f"OR({{Lead Status}} = 'New Lead', {{Lead Status}} = 'Contacted'), "
                        f"{{Twilio Number}} = '{twilio_number}'"
                        f")"
                    )
                })
                open_leads_count = len(leads_resp.json().get("records", []))

                # Get regular clients due this week
                regular_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl3LAJzXa6Vsexry"
                regular_resp = req.get(regular_url, headers=headers, params={
                    "filterByFormula": (
                        f"AND("
                        f"{{Active}} = TRUE(), "
                        f"{{Twilio Number}} = '{twilio_number}'"
                        f")"
                    )
                })
                regular_records = regular_resp.json().get("records", [])

                # Merge regular clients due TODAY into todays_jobs
                for r in regular_records:
                    f = r.get("fields", {})
                    next_appt = f.get("Next Appointment", "")
                    if next_appt:
                        try:
                            dt = datetime.fromisoformat(next_appt.replace("Z", "+00:00"))
                            dt_eastern = dt.astimezone(eastern)
                            days_until = (dt_eastern.date() - now.date()).days
                            if days_until == 0:
                                todays_jobs.append({
                                    "record_id": r.get("id", ""),
                                    "name": f.get("Client Name", ""),
                                    "address": f.get("Service Address", ""),
                                    "time": f.get("Preferred Time", "9:00 AM"),
                                    "job": f.get("Service Description", "Lawn Service"),
                                    "phone": f.get("Phone", ""),
                                    "is_regular": True
                                })
                        except Exception:
                            pass

                # Re-sort now that regular clients have been merged in
                todays_jobs.sort(key=lambda x: x["time"])

                # Find regular clients due within 3 days
                # Exclude anyone already merged into todays_jobs to avoid duplicates
                already_in_jobs = {j["name"].strip().lower() for j in todays_jobs}
                due_soon = []
                for r in regular_records:
                    f = r.get("fields", {})
                    next_appt = f.get("Next Appointment", "")
                    client_name = (f.get("Client Name", "") or "").strip()
                    if not next_appt:
                        continue
                    if client_name.lower() in already_in_jobs:
                        continue
                    try:
                        dt = datetime.fromisoformat(next_appt.replace("Z", "+00:00"))
                        dt_eastern = dt.astimezone(eastern)
                        days_until = (dt_eastern.date() - now.date()).days
                        if 0 <= days_until <= 3:
                            due_soon.append({
                                "name": client_name,
                                "days": days_until
                            })
                    except Exception:
                        pass

                # Get outstanding payments
                outstanding_total = 0
                outstanding_count = 0
                try:
                    payments_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments"
                    payments_resp = req.get(payments_url, headers=headers, params={
                        "filterByFormula": f"AND({{Payment Status}} = 'Unpaid', FIND('{contractor_id}', ARRAYJOIN({{Contractor}})))"
                    })
                    payment_records = payments_resp.json().get("records", [])
                    for p in payment_records:
                        pf = p.get("fields", {})
                        amount = float(pf.get("Amount", 0) or 0)
                        outstanding_total += amount
                        outstanding_count += 1
                    print(f"DAILY BRIEFING | Outstanding | {outstanding_count} payments | ${outstanding_total}")
                except Exception as e:
                    print(f"DAILY BRIEFING | Outstanding error | {e}")

                # Build briefing message
                lines = [f"📋 Good morning! {today_display}"]
                lines.append(f"━━━━━━━━━━━━━━")

                if todays_jobs:
                    lines.append(f"📅 TODAY'S JOBS ({len(todays_jobs)})")
                    for i, job in enumerate(todays_jobs, 1):
                        lines.append(f"{i}. {job['time']} — {job['name']}")
                        if job['address']:
                            lines.append(f"   📍 {job['address'][:40]}")
                else:
                    lines.append("📅 No jobs scheduled today")

                if open_leads_count > 0:
                    lines.append(f"━━━━━━━━━━━━━━")
                    lines.append(f"🔔 {open_leads_count} open lead{'s' if open_leads_count != 1 else ''} need follow-up")

                if due_soon:
                    lines.append(f"━━━━━━━━━━━━━━")
                    lines.append("⏰ REGULAR CLIENTS DUE SOON")
                    for c in due_soon:
                        label = "Today" if c['days'] == 0 else f"In {c['days']} day{'s' if c['days'] != 1 else ''}"
                        lines.append(f"• {c['name']} — {label}")

                lines.append(f"━━━━━━━━━━━━━━")
                if outstanding_count > 0:
                    lines.append(f"━━━━━━━━━━━━━━")
                    lines.append(f"💵 {outstanding_count} outstanding payment{'s' if outstanding_count != 1 else ''} — ${outstanding_total:,.2f} owed")
                lines.append("Have a great day! 💪")

                msg = "\n".join(lines)

                # Send SMS
                send_fallback_sms(to_number=notify_sms, body=msg)
                print(f"DAILY BRIEFING SENT | {business_name} | {notify_sms} | {len(todays_jobs)} jobs")
                
                # At the end of the contractor loop in send_daily_briefing
                # Store today's jobs in Redis for SMS completion
                if redis_client and todays_jobs:
                    job_key = f"daily_jobs:{twilio_number}:{today_str}"
                    redis_client.setex(job_key, 86400, json.dumps(todays_jobs))
                    print(f"DAILY JOBS CACHED | {twilio_number} | {len(todays_jobs)} jobs")
                    
                sent += 1

            except Exception as e:
                print(f"DAILY BRIEFING ERROR | contractor | {e}")
                failed += 1

        return jsonify({"ok": True, "sent": sent, "failed": failed})

    except Exception as e:
        print(f"DAILY BRIEFING ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/send-regular-client-reminders", methods=["POST"])
def send_regular_client_reminders():
    """
    Runs daily at 6 AM — checks Regular Clients due in 2 days
    and automatically books them, sends confirmation SMS and adds to Google Calendar.
    """
    try:
        secret = request.headers.get("X-Briefing-Secret")
        if secret != os.environ.get("BRIEFING_SECRET"):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        import requests as req
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta

        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        target_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        today_str = now.strftime("%Y-%m-%d")

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")
        headers_at = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

        # Get all active contractors
        contractors_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CONTRACTORS_TABLE}"
        contractors_resp = req.get(contractors_url, headers=headers_at, params={
            "filterByFormula": "{Active} = TRUE()"
        })
        contractors = contractors_resp.json().get("records", [])

        booked = 0
        skipped = 0

        for contractor_record in contractors:
            try:
                fields = contractor_record.get("fields", {})
                twilio_number = (fields.get("Twilio Number") or "").strip()
                notify_sms = (fields.get("Notify SMS") or "").strip()
                business_name = (fields.get("Business Name") or "your contractor").strip()
                contractor_id = contractor_record.get("id", "")

                if not twilio_number:
                    continue

                contractor = fields

                # Get active regular clients for this contractor
                regular_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl3LAJzXa6Vsexry"
                regular_resp = req.get(regular_url, headers=headers_at, params={
                    "filterByFormula": f"AND({{Active}} = TRUE(), {{Twilio Number}} = '{twilio_number}')"
                })
                regular_records = regular_resp.json().get("records", [])
                print(f"REGULAR CLIENT QUERY | twilio: {twilio_number} | found: {len(regular_records)} records")

                for r in regular_records:
                    f = r.get("fields", {})
                    record_id = r.get("id", "")
                    next_appt_raw = f.get("Next Appointment", "")
                    client_name = f.get("Client Name", "")
                    client_phone = f.get("Phone", "")
                    service_address = f.get("Service Address", "")
                    service_desc = f.get("Service Description", "")
                    preferred_time = f.get("Preferred Time", "09:00")
                    frequency_days = int(f.get("Frequency Days", 14) or 14)

                    if not next_appt_raw or not client_phone:
                        print(f"SKIP REGULAR CLIENT | {client_name} | missing next appointment or phone | next={next_appt_raw} phone={client_phone}")
                        skipped += 1
                        continue

                    try:
                        dt = datetime.fromisoformat(next_appt_raw.replace("Z", "+00:00"))
                        dt_eastern = dt.astimezone(eastern)
                        appt_date_str = dt_eastern.strftime("%Y-%m-%d")
                    except Exception:
                        continue

                    # Only book if appointment is exactly 2 days away
                    if appt_date_str != target_date:
                        print(f"SKIP REGULAR CLIENT | {client_name} | not due | appt={appt_date_str} target={target_date}")
                        skipped += 1
                        continue

                    print(f"REGULAR CLIENT AUTO-BOOK | {client_name} | {appt_date_str} | {twilio_number}")

                    # Format appointment display
                    formatted_display = dt_eastern.strftime("%A, %B %-d at %-I:%M %p")

                    # Create lead in Airtable
                    leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"
                    at_headers = {
                        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
                        "Content-Type": "application/json"
                    }

                    lead_resp = req.post(
                        leads_url,
                        headers=at_headers,
                        json={"fields": {
                            "fldBktJv26lpFCZjg": client_name,
                            "fldfSFcMA4V5SLfjo": client_phone,
                            "fldo9GtQBLObByZs5": service_address,
                            "fldxNwWbbMWF4cT47": service_desc,
                            "fldkTOouWuLx6JHly": formatted_display,
                            "fldHL2tJs2egGKuI9": "Booked",
                            "fldbtGSgcOrHHe6pO": "STANDARD",
                            "fldAgsSlZfOLFCBrJ": twilio_number,
                            "fldIfaFlPA4AyMntY": dt_eastern.isoformat(),
                        }}
                    )
                    print(f"REGULAR CLIENT LEAD | {lead_resp.status_code} | {client_name}")

                    # Add to Google Calendar
                    try:
                        from app.app.cal_service import create_google_calendar_event
                        dt_end = dt_eastern + timedelta(hours=1)
                        create_google_calendar_event(
                            contractor=contractor,
                            summary=f"{business_name} - {service_desc} ({client_name})",
                            start_time=dt_eastern.isoformat(),
                            end_time=dt_end.isoformat(),
                            description=f"Regular client - every {frequency_days} days\nPhone: {client_phone}",
                            location=service_address,
                        )
                        print(f"REGULAR CLIENT CALENDAR | {client_name}")
                    except Exception as e:
                        print(f"REGULAR CLIENT CALENDAR ERROR | {e}")

                    # Send confirmation SMS to customer
                    first_name = client_name.split()[0] if client_name else "there"
                    msg = (
                        f"Hi {first_name}! Your appointment with {business_name} is confirmed for "
                        f"{formatted_display}. "
                        f"Reply CANCEL APPOINTMENT to cancel."
                    )
                    send_fallback_sms(to_number=client_phone, body=msg)
                    print(f"REGULAR CLIENT SMS | {client_name} | {client_phone}")

                    # Notify contractor
                    if notify_sms:
                        contractor_msg = (
                            f"Auto-booked: {client_name}\n"
                            f"{formatted_display}\n"
                            f"{service_address}\n"
                            f"{service_desc}"
                        )
                        send_fallback_sms(to_number=notify_sms, body=contractor_msg)

                    booked += 1

            except Exception as e:
                print(f"REGULAR CLIENT AUTO-BOOK ERROR | contractor | {e}")
                skipped += 1

        return jsonify({"ok": True, "booked": booked, "skipped": skipped})

    except Exception as e:
        print(f"REGULAR CLIENT REMINDERS ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/send-payment-reminders", methods=["POST", "GET"])
def send_payment_reminders():
    """
    Runs daily via Render cron job.
    Finds unpaid invoices and sends SMS reminders at 3, 7, and 14 days.
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        import requests as req

        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        payments_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        # Fetch all unpaid records
        params = {
            "filterByFormula": "AND({Payment Status} = 'Unpaid', {Phone Number} != '')"
        }
        response = req.get(payments_url, headers=headers, params=params)
        records = response.json().get("records", [])

        print(f"PAYMENT REMINDER | Found {len(records)} unpaid records")

        sent = 0
        failed = 0

        for record in records:
            fields = record.get("fields", {})
            record_id = record.get("id")

            customer_name = fields.get("Customer Name", "there")
            first_name = customer_name.split()[0] if customer_name else "there"
            customer_phone = fields.get("Phone Number", "")
            amount = fields.get("Amount", 0)
            notes = fields.get("Notes", "services rendered")
            payment_date_str = fields.get("Payment Date", "")
            reminder_count = int(fields.get("Reminder Count") or 0)

            # Get contractor from linked record
            contractor_links = fields.get("Contractor", [])
            contractor_record_id = contractor_links[0] if contractor_links else None

            twilio_number = ""
            business_name = "your contractor"
            notify_sms = ""

            if contractor_record_id:
                try:
                    CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")
                    contractors_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CONTRACTORS_TABLE}"
                    c_response = req.get(f"{contractors_url}/{contractor_record_id}", headers=headers)
                    contractor = c_response.json().get("fields", {})
                    twilio_number = contractor.get("Twilio Number", "")
                    business_name = contractor.get("Business Name", "your contractor")
                    notify_sms = contractor.get("Notify SMS", "")
                except Exception as e:
                    print(f"PAYMENT REMINDER | Contractor lookup failed | {e}")

            if not twilio_number or not customer_phone:
                print(f"PAYMENT REMINDER | Skipping {customer_name} — missing phone or Twilio number")
                failed += 1
                continue

            # Calculate days since invoice
            try:
                payment_date = datetime.fromisoformat(payment_date_str)
                if payment_date.tzinfo is None:
                    payment_date = payment_date.replace(tzinfo=eastern)
                days_elapsed = (now - payment_date).days
            except Exception:
                print(f"PAYMENT REMINDER | Invalid payment date for {customer_name}")
                continue

            # Determine if reminder should fire
            should_remind = False
            reminder_num = reminder_count + 1

            if days_elapsed >= 14 and reminder_count < 3:
                should_remind = True
                new_status = "Overdue"
            elif days_elapsed >= 7 and reminder_count < 2:
                should_remind = True
                new_status = "Unpaid"
            elif days_elapsed >= 3 and reminder_count < 1:
                should_remind = True
                new_status = "Unpaid"
            else:
                continue

            if not should_remind:
                continue

            # Build message
            if days_elapsed >= 14:
                msg = (
                    f"Hi {first_name}, this is a final notice from {business_name}. "
                    f"Your balance of ${amount:.2f} for {notes} is now overdue. "
                    f"Please arrange payment at your earliest convenience."
                )
            elif days_elapsed >= 7:
                msg = (
                    f"Hi {first_name}, a friendly reminder from {business_name} — "
                    f"your balance of ${amount:.2f} for {notes} is still outstanding. "
                    f"Please take a moment to complete your payment."
                )
            else:
                msg = (
                    f"Hi {first_name}, this is {business_name} reminding you of "
                    f"your outstanding balance of ${amount:.2f} for {notes}. "
                    f"Please complete your payment at your earliest convenience."
                )

            # Send SMS to customer
            try:
                from twilio.rest import Client as TwilioClient
                tc = TwilioClient(
                    os.environ.get("TWILIO_ACCOUNT_SID"),
                    os.environ.get("TWILIO_AUTH_TOKEN")
                )
                tc.messages.create(
                    body=msg,
                    from_=twilio_number,
                    to=customer_phone
                )
                print(f"PAYMENT REMINDER SENT | {customer_name} | Day {days_elapsed} | ${amount}")

                # Update Airtable — increment reminder count and status
                update_fields = {
                    "Reminder Count": reminder_num,
                    "Payment Status": new_status
                }
                req.patch(
                    f"{payments_url}/{record_id}",
                    headers=headers,
                    json={"fields": update_fields}
                )

                # Alert contractor if overdue
                if days_elapsed >= 14 and notify_sms and twilio_number:
                    tc.messages.create(
                        body=(
                            f"⚠️ Overdue Payment — {customer_name}\n"
                            f"Amount: ${amount:.2f}\n"
                            f"Job: {notes}\n"
                            f"Phone: {customer_phone}\n"
                            f"Days outstanding: {days_elapsed}"
                        ),
                        from_=twilio_number,
                        to=notify_sms
                    )
                    print(f"OVERDUE ALERT SENT TO CONTRACTOR | {customer_name}")

                sent += 1

            except Exception as e:
                print(f"PAYMENT REMINDER SMS ERROR | {customer_name} | {e}")
                failed += 1

        return jsonify({
            "ok": True,
            "sent": sent,
            "failed": failed,
            "total": len(records)
        }), 200

    except Exception as e:
        print(f"PAYMENT REMINDER ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# -----------------------------------------------
# DASHBOARD ROUTES
# -----------------------------------------------

DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "crewcachepro-dashboard-secret")

def generate_dashboard_password() -> str:
    """Generates a secure random password for contractor dashboard."""
    return secrets.token_urlsafe(10)

def hash_password(password: str) -> str:
    """Hashes a password for storage."""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    """Verifies a password against its hash."""
    return hashlib.sha256(password.encode()).hexdigest() == hashed

def create_dashboard_token(contractor_id: str, twilio_number: str) -> str:
    """Creates a JWT token for dashboard session."""
    from datetime import datetime, timedelta  # ADD THIS LINE
    payload = {
        "contractor_id": contractor_id,
        "twilio_number": twilio_number,
        "exp": datetime.utcnow() + timedelta(days=30)
    }
    return pyjwt.encode(payload, DASHBOARD_SECRET, algorithm="HS256")

def verify_dashboard_token(token: str) -> dict:
    """Verifies a JWT token and returns payload."""
    try:
        return pyjwt.decode(token, DASHBOARD_SECRET, algorithms=["HS256"])
    except Exception:
        return {}

def dashboard_auth_required(f):
    """Decorator to protect dashboard routes."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("dashboard_token") or request.headers.get("X-Dashboard-Token")
        if not token:
            return redirect("/dashboard/login")
        payload = verify_dashboard_token(token)
        if not payload:
            return redirect("/dashboard/login")
        request.contractor_id = payload.get("contractor_id")
        request.twilio_number = payload.get("twilio_number")
        return f(*args, **kwargs)
    return decorated


def setup_contractor_dashboard_password(contractor_record_id: str, twilio_number: str, notify_sms: str, business_name: str) -> str:
    """
    Generates a password for a new contractor, stores hashed version in Airtable,
    and SMS's the plain text password to the contractor.
    """
    try:
        plain_password = generate_dashboard_password()
        hashed = hash_password(plain_password)

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")
        contractors_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CONTRACTORS_TABLE}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        requests.patch(
            f"{contractors_url}/{contractor_record_id}",
            headers=headers,
            json={"fields": {"Dashboard Password": hashed}}
        )

        # SMS password to contractor
        if notify_sms and twilio_number:
            try:
                from twilio.rest import Client as TwilioClient
                tc = TwilioClient(
                    os.environ.get("TWILIO_ACCOUNT_SID"),
                    os.environ.get("TWILIO_AUTH_TOKEN")
                )
                tc.messages.create(
                    body=(
                        f"Welcome to CrewCachePro! 🎉\n"
                        f"Your dashboard is ready at:\n"
                        f"https://mme-ai-bot.onrender.com/dashboard\n\n"
                        f"Login: {twilio_number}\n"
                        f"Password: {plain_password}\n\n"
                        f"Save this message — you'll need it to log in."
                    ),
                    from_=twilio_number,
                    to=notify_sms
                )
                print(f"DASHBOARD PASSWORD SMS SENT | {notify_sms}")
            except Exception as e:
                print(f"DASHBOARD PASSWORD SMS ERROR | {e}")

        return plain_password

    except Exception as e:
        print(f"SETUP DASHBOARD PASSWORD ERROR | {e}")
        return ""

@app.route("/dashboard/debug-login")
def debug_login():
    import hashlib
    test_password = "otLig2masittL!!!"
    test_hash = hashlib.sha256(test_password.encode()).hexdigest()
    
    contractor = get_contractor_by_twilio_number("+12408686702") or {}
    stored_hash = contractor.get("Dashboard Password", "NO FIELD FOUND")
    
    return jsonify({
        "expected_hash": test_hash,
        "stored_hash": stored_hash,
        "match": test_hash == stored_hash
    })


@app.route("/dashboard/login", methods=["GET", "POST"])
def dashboard_login():
    """Dashboard login page."""
    if request.method == "GET":
        return '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>CrewCachePro Login</title>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: DM Sans, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #0d2137 50%, #0f2818 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .login-card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 20px;
            padding: 40px 32px;
            width: 100%;
            max-width: 380px;
            box-shadow: 0 8px 40px rgba(0,0,0,0.4);
        }
        .logo { text-align: center; margin-bottom: 32px; }
        .logo h1 { font-size: 26px; font-weight: 700; color: white; }
        .logo span { color: #22c55e; }
        .logo p { color: #94a3b8; font-size: 13px; margin-top: 4px; font-family: DM Mono, monospace; }
        label { display: block; color: #94a3b8; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 6px; font-family: DM Mono, monospace; }
        input { width: 100%; background: #0f172a; border: 1px solid #334155; border-radius: 10px; padding: 14px 16px; color: white; font-size: 16px; margin-bottom: 16px; outline: none; font-family: DM Sans, sans-serif; }
        input:focus { border-color: #2563EB; }
        button { width: 100%; background: linear-gradient(135deg, #2563EB, #16a34a); color: white; border: none; border-radius: 10px; padding: 16px; font-size: 16px; font-weight: 700; cursor: pointer; font-family: DM Sans, sans-serif; }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo">
            <h1>Crew<span>Cache</span>Pro</h1>
            <p>Contractor Dashboard</p>
        </div>
        <form method="POST" action="/dashboard/login">
            <label>Your Twilio Number</label>
            <input type="tel" name="twilio_number" placeholder="+12408686702" required>
            <label>Password</label>
            <input type="password" name="password" placeholder="password" required>
            <button type="submit">Sign In</button>
        </form>
    </div>
</body>
</html>
        '''

    # POST — handle login
    twilio_number = request.form.get("twilio_number", "").strip()

    # POST — handle login
    twilio_number = request.form.get("twilio_number", "").strip()
    password = request.form.get("password", "").strip()

    if not twilio_number or not password:
        return dashboard_login_error("Please enter your Twilio number and password.")

    # Look up contractor
    try:
        contractor = get_contractor_by_twilio_number(twilio_number)
        if not contractor:
            return dashboard_login_error("No account found for that number.")

        stored_hash = contractor.get("Dashboard Password", "")
        if not stored_hash or not verify_password(password, stored_hash):
            return dashboard_login_error("Incorrect password.")

        # Find contractor record ID — use same lookup, get ID from records
        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")
        contractors_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CONTRACTORS_TABLE}"
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
        params = {"filterByFormula": f"{{Twilio Number}} = '{twilio_number}'"}
        response = requests.get(contractors_url, headers=headers, params=params)
        records = response.json().get("records", [])

        if not records:
            return dashboard_login_error("Could not find contractor record.")

        contractor_record_id = records[0]["id"]

        print(f"DASHBOARD LOGIN | contractor_id: {contractor_record_id} | twilio: {twilio_number}")
        
        token = create_dashboard_token(contractor_record_id, twilio_number)
        resp = make_response(redirect(f"/dashboard?token={token}"))
        resp.set_cookie("dashboard_token", token, max_age=30*24*3600, httponly=False, samesite="Lax")
        
        return resp

    except Exception as e:
        print(f"DASHBOARD LOGIN ERROR | {e}")
        return dashboard_login_error("Something went wrong. Please try again.")


def dashboard_login_error(message: str):
    """Returns login page with error message."""
    return f'''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>CrewCachePro — Login</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: "Georgia", serif;
            background: #0a0a0a;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .login-card {{
            background: #111;
            border: 1px solid #222;
            border-radius: 16px;
            padding: 40px 32px;
            width: 100%;
            max-width: 380px;
        }}
        .logo {{
            text-align: center;
            margin-bottom: 32px;
        }}
        .logo h1 {{ font-size: 24px; color: #fff; letter-spacing: -0.5px; }}
        .logo span {{ color: #22c55e; }}
        .logo p {{ color: #555; font-size: 13px; margin-top: 4px; font-family: monospace; }}
        label {{ display: block; color: #888; font-size: 12px; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 8px; font-family: monospace; }}
        input {{ width: 100%; background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 14px 16px; color: #fff; font-size: 16px; margin-bottom: 20px; outline: none; }}
        button {{ width: 100%; background: #22c55e; color: #000; border: none; border-radius: 8px; padding: 16px; font-size: 16px; font-weight: 700; cursor: pointer; }}
        .error {{ background: #1a0a0a; border: 1px solid #ef4444; color: #ef4444; padding: 12px 16px; border-radius: 8px; font-size: 14px; margin-bottom: 20px; font-family: monospace; }}
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo">
            <h1>Crew<span>Cache</span>Pro</h1>
            <p>Contractor Dashboard</p>
        </div>
        <div class="error">⚠ {message}</div>
        <form method="POST" action="/dashboard/login">
            <label>Your Twilio Number</label>
            <input type="tel" name="twilio_number" placeholder="+12408686702" required>
            <label>Password</label>
            <input type="password" name="password" placeholder="••••••••••" required>
            <button type="submit">Sign In →</button>
        </form>
    </div>
</body>
</html>
    '''


@app.route("/dashboard")
@dashboard_auth_required
def dashboard():
    """Main dashboard page — mobile optimized."""
    return '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <link rel="apple-touch-icon" href="https://res.cloudinary.com/dkfshn604/image/upload/IMG_1664_jukqma.jpg">
    <link rel="icon" type="image/png" href="https://res.cloudinary.com/dkfshn604/image/upload/IMG_1664_jukqma.jpg">
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#2563EB">
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>
    <script>
        window.OneSignalDeferred = window.OneSignalDeferred || [];
        OneSignalDeferred.push(async function(OneSignal) {
            await OneSignal.init({
                appId: "8c26bbef-107f-430b-9aef-f3b5137467fa",
                notifyButton: { enable: false },
                allowLocalhostAsSecureOrigin: true,
            });
        });
    </script>
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
    <title>CrewCachePro Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }

        :root {
            --blue: #2563EB;
            --green: #16a34a;
            --gradient: linear-gradient(135deg, #2563EB, #16a34a);
            --bg: #0f172a;
            --card: #1e293b;
            --card-border: #334155;
            --text: #f1f5f9;
            --text-muted: #94a3b8;
            --text-light: #64748b;
            --success: #22c55e;
            --danger: #ef4444;
            --warning: #f59e0b;
        }

        body {
            font-family: 'DM Sans', sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #0d2137 50%, #0f2818 100%);
            color: var(--text);
            min-height: 100vh;
            padding-bottom: 80px;
        }

        header {
            background: linear-gradient(135deg, #2563EB, #16a34a);
            padding: 14px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 2px 20px rgba(37,99,235,0.4);
        }

        header h1 {
            font-size: 20px;
            font-weight: 700;
            color: white;
        }

        .business-name {
            font-size: 11px;
            color: rgba(255,255,255,0.7);
            font-family: 'DM Mono', monospace;
            margin-top: 2px;
        }

        .logout-btn {
            background: rgba(255,255,255,0.15);
            border: 1px solid rgba(255,255,255,0.3);
            color: white;
            padding: 8px 14px;
            border-radius: 8px;
            font-size: 12px;
            cursor: pointer;
            text-decoration: none;
            font-family: 'DM Mono', monospace;
        }

        .content { padding: 16px; }

        .revenue-bar {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-bottom: 16px;
        }

        .revenue-card {
            background: linear-gradient(135deg, #1e3a5f, #1a3a2a);
            border: 1px solid #334155;
            border-radius: 14px;
            padding: 14px 16px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3);
        }

        .revenue-label {
            font-size: 10px;
            color: var(--text-muted);
            letter-spacing: 1px;
            text-transform: uppercase;
            font-family: 'DM Mono', monospace;
            margin-bottom: 6px;
        }

        .revenue-amount {
            font-size: 22px;
            font-weight: 700;
            color: #22c55e;
        }

        .revenue-amount.red { color: var(--danger); }
        .revenue-amount.white { color: white; }

        .revenue-sub {
            font-size: 11px;
            color: var(--text-light);
            font-family: 'DM Mono', monospace;
            margin-top: 2px;
        }

        .calendar-card {
            background: var(--card);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            padding: 20px;
            margin-bottom: 16px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3);
        }

        .calendar-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .calendar-title {
            font-size: 16px;
            font-weight: 700;
            color: white;
        }

        .calendar-nav { display: flex; gap: 8px; }

        .cal-btn {
            background: #334155;
            border: 1px solid #475569;
            color: white;
            width: 32px;
            height: 32px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .cal-grid {
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 4px;
        }

        .cal-day-header {
            text-align: center;
            font-size: 10px;
            color: var(--text-muted);
            font-family: 'DM Mono', monospace;
            padding: 4px 0;
            letter-spacing: 1px;
        }

        .cal-day {
            aspect-ratio: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 8px;
            font-size: 13px;
            cursor: pointer;
            color: var(--text-muted);
            transition: background 0.15s;
        }

        .cal-day:hover { background: #334155; }

        .cal-day.has-job {
            background: rgba(37,99,235,0.3);
            color: #22c55e;
            font-weight: 700;
            border: 1px solid #22c55e;
        }

        .cal-day.today {
            background: var(--gradient);
            color: white;
            font-weight: 700;
        }

        .cal-day.today.has-job {
            background: var(--gradient);
            color: white;
        }

        .cal-day.other-month { color: #334155; }

        .section { margin-bottom: 16px; }

        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }

        .section-title {
            font-size: 11px;
            color: var(--text-muted);
            letter-spacing: 2px;
            text-transform: uppercase;
            font-family: 'DM Mono', monospace;
            font-weight: 500;
        }

        .section-count {
            background: rgba(37,99,235,0.2);
            color: #60a5fa;
            font-size: 11px;
            padding: 3px 8px;
            border-radius: 20px;
            font-family: 'DM Mono', monospace;
        }

        .job-card {
            background: var(--card);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 10px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3);
            transition: box-shadow 0.2s;
        }

        .job-card:active { box-shadow: 0 8px 24px rgba(37,99,235,0.2); }
        .job-card.urgent { border-left: 3px solid var(--danger); }
        .job-card.today-job { border-left: 3px solid #22c55e; }

        .job-time {
            font-size: 12px;
            color: #60a5fa;
            font-family: 'DM Mono', monospace;
            margin-bottom: 4px;
            font-weight: 500;
        }

        .job-name {
            font-size: 16px;
            font-weight: 700;
            margin-bottom: 3px;
            color: white;
        }

        .job-address {
            font-size: 13px;
            color: var(--text-muted);
            margin-bottom: 3px;
        }

        .job-type {
            font-size: 12px;
            color: var(--text-light);
            font-family: 'DM Mono', monospace;
        }

        .job-actions {
            display: flex;
            gap: 8px;
            margin-top: 12px;
        }

        .action-btn {
            flex: 1;
            padding: 10px;
            border-radius: 10px;
            border: none;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            text-align: center;
            text-decoration: none;
            display: block;
            font-family: 'DM Sans', sans-serif;
            transition: opacity 0.15s;
        }

        .action-btn:active { opacity: 0.85; }
        .btn-call { background: var(--gradient); color: white; }
        .btn-sms { background: #334155; border: 1px solid #475569; color: var(--text); }

        .invoice-card {
            background: var(--card);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 10px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3);
        }

        .invoice-amount { font-size: 20px; font-weight: 700; color: var(--danger); }
        .invoice-days { font-size: 11px; color: var(--text-light); font-family: 'DM Mono', monospace; margin-top: 2px; }

        .empty-state {
            text-align: center;
            color: var(--text-light);
            font-size: 14px;
            padding: 24px;
            font-family: 'DM Mono', monospace;
            background: var(--card);
            border-radius: 14px;
            border: 1px dashed var(--card-border);
        }

        .loading {
            text-align: center;
            color: var(--text-light);
            font-size: 13px;
            padding: 20px;
            font-family: 'DM Mono', monospace;
        }

        .refresh-btn {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: var(--gradient);
            color: white;
            border: none;
            width: 52px;
            height: 52px;
            border-radius: 50%;
            font-size: 20px;
            cursor: pointer;
            box-shadow: 0 4px 20px rgba(37,99,235,0.4);
        }

        .badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 20px;
            font-size: 10px;
            font-family: 'DM Mono', monospace;
            letter-spacing: 1px;
            margin-left: 8px;
        }

        .badge-new { background: rgba(34,197,94,0.2); color: #22c55e; }
        .badge-urgent { background: rgba(239,68,68,0.2); color: var(--danger); }
        .badge-overdue { background: rgba(239,68,68,0.2); color: var(--danger); }

        .payment-method-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-bottom: 16px;
        }

        .payment-method-btn {
            padding: 12px;
            border-radius: 10px;
            border: 2px solid var(--card-border);
            background: var(--card);
            color: var(--text-muted);
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            text-align: center;
            transition: all 0.15s;
            font-family: 'DM Sans', sans-serif;
        }

        .payment-method-btn.selected {
            border-color: #2563EB;
            background: rgba(37,99,235,0.2);
            color: #60a5fa;
        }

        .email-field { display: none; }
        .email-field.visible { display: block; }

        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.8);
            z-index: 200;
            align-items: flex-end;
            justify-content: center;
            backdrop-filter: blur(4px);
        }

        .modal-overlay.active { display: flex; }

        .booking-modal {
            background: #1e293b;
            border-radius: 24px 24px 0 0;
            padding: 24px 20px 40px;
            width: 100%;
            max-width: 500px;
            max-height: 90vh;
            overflow-y: auto;
            box-shadow: 0 -8px 40px rgba(37,99,235,0.3);
            border-top: 1px solid #334155;
        }

        .modal-title { font-size: 18px; font-weight: 700; margin-bottom: 6px; color: white; }
        .modal-date { font-size: 13px; color: #60a5fa; font-family: 'DM Mono', monospace; margin-bottom: 20px; }
        .form-group { margin-bottom: 14px; }

        .form-label {
            display: block;
            font-size: 11px;
            color: var(--text-muted);
            letter-spacing: 1px;
            text-transform: uppercase;
            font-family: 'DM Mono', monospace;
            margin-bottom: 6px;
        }

        .form-input {
            width: 100%;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 12px 14px;
            color: white;
            font-size: 16px;
            outline: none;
            font-family: 'DM Sans', sans-serif;
            transition: border-color 0.15s;
        }

        .form-input:focus { border-color: #2563EB; }

        .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

        .modal-actions { display: flex; gap: 10px; margin-top: 20px; }

        .btn-book {
            flex: 1;
            background: var(--gradient);
            color: white;
            border: none;
            border-radius: 12px;
            padding: 16px;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            font-family: 'DM Sans', sans-serif;
        }

        .btn-cancel-modal {
            background: #334155;
            border: 1px solid #475569;
            color: var(--text-muted);
            border-radius: 12px;
            padding: 16px 20px;
            font-size: 14px;
            cursor: pointer;
            font-family: 'DM Sans', sans-serif;
        }

        .crew-logo { display: flex; align-items: center; }
    </style>
</head>
<body>

    <!-- Splash Screen -->
    <div id="splashScreen" style="
        position: fixed;
        top: 0; left: 0; right: 0; bottom: 0;
        background: linear-gradient(135deg, #2563EB, #16a34a);
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        z-index: 9999;
        animation: splashFade 0.5s ease 2s forwards;
    ">
        <style>
            @keyframes splashFade {
                0% { opacity: 1; pointer-events: all; }
                100% { opacity: 0; pointer-events: none; display: none; }
            }
        </style>
        <img src="https://res.cloudinary.com/dkfshn604/image/upload/IMG_1664_jukqma.jpg" 
             style="width: 140px; height: 140px; border-radius: 32px; margin-bottom: 24px; box-shadow: 0 8px 32px rgba(0,0,0,0.3);">
        <div style="color: white; font-family: 'DM Sans', sans-serif; font-size: 28px; font-weight: 700;">
            Crew<span style="color: #bbf7d0">Cache</span>Pro
        </div>
    </div>
    
    <header>
        <div>
            <div style="display:flex; align-items:center; gap:10px;">

  <div class="crew-logo">
    <svg viewBox="0 0 500 500" width="70" height="70">
      <defs>
        <linearGradient id="crewGradient" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#005B9A"/>
          <stop offset="100%" stop-color="#39B54A"/>
        </linearGradient>
      </defs>

      <path d="M120 180 A150 150 0 0 1 385 140"
        fill="none"
        stroke="url(#crewGradient)"
        stroke-width="25"
        stroke-linecap="round"
      />
      <polygon points="390,120 430,175 365,180"
        fill="url(#crewGradient)"
      />

      <path d="M380 320 A150 150 0 0 1 115 360"
        fill="none"
        stroke="url(#crewGradient)"
        stroke-width="25"
        stroke-linecap="round"
      />
      <polygon points="110,380 70,325 135,320"
        fill="url(#crewGradient)"
      />
    </svg>
  </div>

  <div>
    <h1 style="margin:0;">
      <span style="color:#005B9A;">Crew</span><span style="color:#39B54A;">CachePro</span>
    </h1>
    <div class="business-name" id="businessName">Loading...</div>
  </div>

</div>

        <div style="display:flex;gap:8px;align-items:center">
            <button onclick="startVoiceInput()" id="voiceBtn" style="background:var(--bg);border:1px solid var(--card-border);color:var(--text);border-radius:10px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;font-family:'DM Sans',sans-serif">
                Voice
            </button>

            <button onclick="window.location.href='/walkthrough'" style="background:var(--bg);border:1px solid var(--card-border);color:var(--text);border-radius:10px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;font-family:DM Sans,sans-serif">
                Video
            </button>
            

            <button onclick="openAddContractorModal()" style="background:var(--bg);border:1px solid var(--card-border);color:var(--text);border-radius:10px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;font-family:DM Sans,sans-serif">
                + Contractor
            </button>

            <button onclick="connectStripe()" style="background:var(--bg);border:1px solid var(--card-border);color:var(--text);border-radius:10px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;font-family:DM Sans,sans-serif">
                Connect Stripe
            </button>
            
            <button onclick="openBookingModal('')" style="background:var(--gradient);color:white;border:none;border-radius:10px;padding:8px 16px;font-size:13px;font-weight:700;cursor:pointer;font-family:'DM Sans',sans-serif">+ Add Job</button>
            <a href="/dashboard/logout" class="logout-btn">Sign out</a>
        </div>
           

        
    </header>

    <div class="content">
        <!-- Revenue Summary -->
        <div class="revenue-bar" id="revenueBar">
            <div class="revenue-card">
                <div class="revenue-label">This Week</div>
                <div class="revenue-amount" id="revWeek">--</div>
            </div>
            <div class="revenue-card">
                <div class="revenue-label" id="revMonthLabel">This Month</div>
                <div class="revenue-amount" id="revMonth">--</div>
                <div class="revenue-sub" id="revJobs"></div>
            </div>
            <div class="revenue-card">
                <div class="revenue-label">This Year</div>
                <div class="revenue-amount white" id="revYear">--</div>
            </div>
            <div class="revenue-card">
                <div class="revenue-label">Outstanding</div>
                <div class="revenue-amount red" id="revOutstanding">--</div>
            </div>
        </div>

        <!-- Booking Link -->
        <div style="background:var(--card);border:1px solid var(--card-border);border-radius:14px;padding:14px 16px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,0.04)">
            <div style="font-size:11px;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;font-family:'DM Mono',monospace;margin-bottom:8px">Your Booking Link</div>
            <div style="display:flex;gap:8px;align-items:center">
                <input type="text" id="bookingLinkInput" readonly style="flex:1;background:var(--bg);border:1px solid var(--card-border);border-radius:8px;padding:10px 12px;font-size:12px;color:var(--text);font-family:'DM Mono',monospace">
                <button onclick="copyBookingLink()" id="copyBookingBtn" style="background:var(--gradient);color:white;border:none;border-radius:8px;padding:10px 16px;font-size:13px;font-weight:600;cursor:pointer;font-family:'DM Sans',sans-serif;white-space:nowrap">Copy</button>
            </div>
            <div style="font-size:11px;color:var(--text-light);margin-top:8px">Share this on Google Business Profile, Facebook, or your website so customers can book themselves.</div>
        </div>

        <!-- Today's Summary -->
        <div class="today-summary">
            <h2>Today</h2>
            <div class="job">No jobs scheduled today.</div>
        </div>
        
        <!-- Calendar -->
        <div class="calendar-card">
            <div class="calendar-header">
                <div class="calendar-title" id="calTitle">May 2026</div>
                <div class="calendar-nav">
                    <button class="cal-btn" onclick="changeMonth(-1)">‹</button>
                    <button class="cal-btn" onclick="changeMonth(1)">›</button>
                </div>
            </div>
            <div class="cal-grid" id="calGrid">
                <div class="cal-day-header">SUN</div>
                <div class="cal-day-header">MON</div>
                <div class="cal-day-header">TUE</div>
                <div class="cal-day-header">WED</div>
                <div class="cal-day-header">THU</div>
                <div class="cal-day-header">FRI</div>
                <div class="cal-day-header">SAT</div>
            </div>
        </div>

        <!-- Today's Jobs -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Today's Jobs</div>
                <div class="section-count" id="todayCount">0</div>
            </div>
            <div id="todayJobs"><div class="loading">Loading...</div></div>
        </div>

        <!-- Tomorrow's Jobs -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Tomorrow</div>
                <div class="section-count" id="tomorrowCount">0</div>
            </div>
            <div id="tomorrowJobs"><div class="loading">Loading...</div></div>
        </div>

        <!-- Open Leads -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Open Leads</div>
                <div class="section-count" id="leadsCount">0</div>
            </div>
            <div id="openLeads"><div class="loading">Loading...</div></div>
        </div>

        <!-- Unpaid Invoices -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Unpaid Invoices</div>
                <div class="section-count" id="invoicesCount">0</div>
            </div>
            <div id="unpaidInvoices"><div class="loading">Loading...</div></div>
        </div>

        <!-- Recent Bookings -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Recent Bookings</div>
            </div>
            <div id="recentBookings"><div class="loading">Loading...</div></div>
        </div>
    </div>

        <!-- Regular Clients -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Regular Clients</div>
                <div class="section-count" id="regularCount">0</div>
            </div>
            <div id="regularClients"><div class="loading">Loading...</div></div>
        </div>

        <!-- Seasonal Campaigns -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">📣 Seasonal Campaigns</div>
            </div>
            <div id="seasonalCampaigns"><div class="loading">Loading...</div></div>
        </div>

        <!-- Recurring Invoices -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Recurring Invoices</div>
                <div class="section-count" id="recurringCount">0</div>
            </div>
            <div id="recurringCustomers"><div class="loading">Loading...</div></div>
        </div>
        
    </div>
    
    <button class="refresh-btn" onclick="loadDashboard()" title="Refresh">↻</button>

    <script>
        let dashboardData = {};
        let currentMonth = new Date().getMonth();
        let currentYear = new Date().getFullYear();

        async function dashboardAction(endpoint, payload, successMsg) {
            try {
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Dashboard-Token': getCookie('dashboard_token')
                    },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (data.ok) {
                    alert(successMsg || 'Done!');
                    loadDashboard(); // Refresh dashboard
                } else {
                    alert('Error: ' + (data.error || 'Something went wrong'));
                }
            } catch(e) {
                alert('Request failed. Please try again.');
            }
        }

        function jobCard(job, isToday) {
            const phone = job.phone ? job.phone.replace(/\D/g,'') : '';
            const recordId = job.record_id || '';
            const appointmentTime = job.time || 'your scheduled time';
            const customerPhone = job.phone || '';
            const customerName = job.name || '';

            return `
            <div class="job-card ${isToday ? 'today-job' : ''}">
                <div class="job-time">${job.time || 'Time TBD'}</div>
                <div class="job-name">${job.name || 'Unknown'}</div>
                <div class="job-address">${job.address || ''}</div>
                <div class="job-type">${job.job_type || ''}</div>
                ${phone ? `
                <div class="job-actions">
                    <a href="tel:+1${phone}" class="action-btn btn-call">📞 Call</a>
                    <a href="sms:+1${phone}" class="action-btn btn-sms">💬 Text</a>
                </div>
                <div class="job-actions" style="margin-top:8px">
                    <button onclick="dashboardAction('/dashboard/action/send-confirmation', {customer_name:'${customerName}', customer_phone:'${customerPhone}', appointment_time:'${appointmentTime}'}, 'Confirmation sent!')" class="action-btn btn-sms">✅ Send Confirmation</button>
                    <button onclick="dashboardAction('/dashboard/action/on-my-way', {customer_name:'${customerName}', customer_phone:'${customerPhone}'}, 'On my way message sent!')" class="action-btn btn-sms">🚗 On My Way</button>
                </div>
                <div class="job-actions" style="margin-top:8px">
                    <button onclick="openCompletePayModal('${recordId}', '${customerName}', '${customerPhone}', '${job.job_type||''}')" class="action-btn" style="background:#22c55e;color:#000;flex:1">✓ Complete & Pay</button>
                </div>` : ''}
            </div>`;
        }

        function renderOpenLeads() {
            const leads = dashboardData.open_leads || [];
            document.getElementById('leadsCount').textContent = leads.length;
            const el = document.getElementById('openLeads');

            if (!leads.length) {
                el.innerHTML = '<div class="empty-state">No open leads</div>';
                return;
            }

            el.innerHTML = leads.map(lead => {
                const phone = lead.phone ? lead.phone.replace(/\D/g,'') : '';
                const recordId = lead.record_id || '';
                const customerName = lead.name || '';
                const badge = lead.priority === 'URGENT' || lead.priority === 'HIGH_PRIORITY'
                    ? '<span class="badge badge-urgent">URGENT</span>'
                    : '<span class="badge badge-new">NEW</span>';

                return `
                    <div class="job-card ${lead.priority !== 'STANDARD' ? 'urgent' : ''}">
                        <div class="job-name">${lead.name || 'Unknown'}${badge}</div>
                        <div class="job-address">${lead.address || ''}</div>
                        <div class="job-type">${lead.job_type || ''} · ${lead.timing || ''}</div>

                        ${phone ? `
                        <div class="job-actions" style="margin-top:8px">
                            <button 
                                data-record-id="${recordId}"
                                data-address="${lead.address || ''}"
                                data-job-type="${lead.job_type || ''}"
                                data-customer="${customerName}"
                                data-twilio="${dashboardData.twilio_number || ''}"
                                onclick="runAerialQuote(this.dataset.recordId, this.dataset.address, this.dataset.jobType, this.dataset.customer, this.dataset.twilio)"
                                class="action-btn btn-sms" 
                                style="width:100%">
                                🛰️ Aerial Quote
                            </button>
                        </div>
                        ` : ''}
                    </div>
                 `;
            }).join('');
        }
        


        async function runAerialQuote(recordId, address, jobType, customerName, twilioNumber) {
            if (!address) {
                alert('No address on file for this lead.');
                return;
            }
            const btn = event.target;
            btn.textContent = '🛰️ Analyzing...';
            btn.disabled = true;

            try {
                const res = await fetch('/aerial-quote', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Dashboard-Token': getCookie('dashboard_token')
                    },
                    body: JSON.stringify({
                        lead_id: recordId,
                        address: address,
                        job_description: jobType,
                        customer_name: customerName,
                        twilio_number: twilioNumber
                    })
                });

                const data = await res.json();
                if (data.ok) {
                    alert(
                        `🛰️ Aerial Quote Complete!\n\n` +
                        `📐 ~${data.square_footage?.toLocaleString()} sq ft\n` +
                        `💰 Estimate: ${data.quote_range}\n\n` +
                        `Satellite image and full analysis saved to lead record.\n` +
                        `SMS sent to your notify number.`
                    );
                    loadDashboard();
                } else {
                    alert('Error: ' + (data.error || 'Something went wrong'));
                }
            } catch(e) {
                alert('Request failed. Please try again.');
            } finally {
                btn.textContent = '🛰️ Aerial Quote';
                btn.disabled = false;
            }
        }

        function renderUnpaidInvoices() {
            const invoices = dashboardData.unpaid_invoices || [];
            document.getElementById('invoicesCount').textContent = invoices.length;
            const el = document.getElementById('unpaidInvoices');
            if (!invoices.length) { el.innerHTML = '<div class="empty-state">All paid up ✓</div>'; return; }
            el.innerHTML = invoices.map(inv => {
                const recordId = inv.record_id || '';
                const customerPhone = inv.phone || '';
                const customerName = inv.name || '';
                return `
                <div class="invoice-card" style="flex-direction:column;align-items:stretch">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                        <div>
                            <div class="job-name">${inv.name || 'Unknown'}</div>
                            <div class="job-type">${inv.job_type || ''}</div>
                            ${inv.days_outstanding > 0 ? `<div class="invoice-days">${inv.days_outstanding} days outstanding</div>` : ''}
                        </div>
                        <div style="text-align:right">
                            <div class="invoice-amount">$${inv.amount}</div>
                            ${inv.days_outstanding >= 14 ? '<span class="badge badge-overdue">OVERDUE</span>' : ''}
                        </div>
                    </div>
                    <div class="job-actions">
                        <button onclick="if(confirm('Mark $${inv.amount} from ${customerName} as paid?')) dashboardAction('/dashboard/action/mark-paid', {record_id:'${recordId}'}, 'Invoice marked paid!')" class="action-btn" style="background:#22c55e;color:#000;flex:1">✓ Mark Paid</button>
                        <button onclick="dashboardAction('/dashboard/action/send-reminder', {customer_name:'${customerName}', customer_phone:'${customerPhone}', amount:'${inv.amount}', job_type:'${inv.job_type||''}'}, 'Reminder sent!')" class="action-btn btn-sms">💬 Send Reminder</button>
                    </div>
                </div>`;
            }).join('');
        }

        
        function renderRecentBookings() {
            const bookings = dashboardData.recent_bookings || [];
            const el = document.getElementById('recentBookings');
            if (!bookings.length) { el.innerHTML = '<div class="empty-state">No recent bookings</div>'; return; }
            el.innerHTML = bookings.map(b => {
                const recordId = b.record_id || '';
                const customerName = b.name || '';
                const customerPhone = b.phone || '';
                const jobType = b.job_type || '';
                return `
                <div class="job-card">
                    <div class="job-time">${b.date || ''}</div>
                    <div class="job-name">${b.name || 'Unknown'}</div>
                    <div class="job-address">${b.address || ''}</div>
                    <div class="job-type">${b.job_type || ''}</div>
                    ${recordId ? `
                    <div class="job-actions" style="margin-top:8px">
                        <button onclick="openCompletePayModal('${recordId}', '${customerName}', '${customerPhone}', '${jobType}')" class="action-btn" style="background:#22c55e;color:#000;flex:1">✓ Complete & Pay</button>
                    </div>` : ''}
               </div>`;
            }).join('');
        }

        async function loadDashboard() {
            try {
                const token = getCookie('dashboard_token');
                if (!token) {
                    window.location.href = '/dashboard/login';
                    return;
                }

                const res = await fetch('/dashboard/data', {
                    headers: { 'X-Dashboard-Token': token }
                });
                if (res.status === 401) {
                    localStorage.removeItem('dashboard_token');
                    window.location.href = '/dashboard/login';
                    return;
                }
                dashboardData = await res.json();
                renderAll();
                loadRecurringCustomers();
                loadRevenue();
                loadRegularClients();
            } catch(e) {
                console.error('Dashboard load error:', e);
            }
        }
            

        function getCookie(name) {
            // Check URL param first (PWA login redirect)
            try {
                const urlParams = new URLSearchParams(window.location.search);
                const urlToken = urlParams.get('token');
                if (urlToken) {
                    try { localStorage.setItem('dashboard_token', urlToken); } catch(e) {}
                    window.history.replaceState({}, '', '/dashboard');
                    return urlToken;
                }
                // Check localStorage
                try {
                    const localToken = localStorage.getItem('dashboard_token');
                    if (localToken) return localToken;
                } catch(e) {}
            } catch(e) {}
            // Fall back to cookie
            const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
            return match ? match[2] : '';
        }

        function renderAll() {
            document.getElementById('businessName').textContent = dashboardData.business_name || '';
            renderCalendar();
            renderTodayJobs();
            renderTomorrowJobs();
            renderOpenLeads();
            renderUnpaidInvoices();
            renderRecentBookings();
            renderBookingLink();
        }

        function renderBookingLink() {
            const twilio = dashboardData.twilio_number || "";
            if (!twilio) return;
            const link = "https://mme-ai-bot.onrender.com/book?c=" + encodeURIComponent(twilio);
            document.getElementById("bookingLinkInput").value = link;
        }

        function copyBookingLink() {
            const input = document.getElementById("bookingLinkInput");
            navigator.clipboard.writeText(input.value).then(function() {
                const btn = document.getElementById("copyBookingBtn");
                btn.textContent = "Copied!";
                setTimeout(function() { btn.textContent = "Copy"; }, 2000);
            }).catch(function() {
                input.select();
                document.execCommand("copy");
            });
        }

        function renderTodayJobs() {
            const jobs = dashboardData.today_jobs || [];
            document.getElementById('todayCount').textContent = jobs.length;
            const el = document.getElementById('todayJobs');
            if (!jobs.length) { el.innerHTML = '<div class="empty-state">No jobs today</div>'; return; }
            el.innerHTML = jobs.map(job => jobCard(job, true)).join('');
        }

        function renderTomorrowJobs() {
            const jobs = dashboardData.tomorrow_jobs || [];
            document.getElementById('tomorrowCount').textContent = jobs.length;
            const el = document.getElementById('tomorrowJobs');
            if (!jobs.length) { el.innerHTML = '<div class="empty-state">No jobs tomorrow</div>'; return; }
            el.innerHTML = jobs.map(job => jobCard(job, false)).join('');
        }

        function renderCalendar() {
            const jobDates = new Set((dashboardData.all_jobs || []).map(j => j.date));
            const today = new Date();
            const firstDay = new Date(currentYear, currentMonth, 1);
            const lastDay = new Date(currentYear, currentMonth + 1, 0);
            const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];

            document.getElementById('calTitle').textContent = `${monthNames[currentMonth]} ${currentYear}`;

            const grid = document.getElementById('calGrid');
            const headers = Array.from(grid.querySelectorAll('.cal-day-header'));
            grid.innerHTML = '';
            headers.forEach(h => grid.appendChild(h));

            for (let i = 0; i < firstDay.getDay(); i++) {
                const empty = document.createElement('div');
                empty.className = 'cal-day other-month';
                grid.appendChild(empty);
            }

            for (let d = 1; d <= lastDay.getDate(); d++) {
                const dateStr = `${currentYear}-${String(currentMonth+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
                const isToday = today.getDate() === d && today.getMonth() === currentMonth && today.getFullYear() === currentYear;
                const hasJob = jobDates.has(dateStr);

                const day = document.createElement('div');
                day.className = `cal-day${hasJob ? ' has-job' : ''}${isToday ? ' today' : ''}`;
                day.textContent = d;
                // ADDED: Click to open booking modal
                day.onclick = () => openBookingModal(dateStr);
                day.style.cursor = 'pointer';
                if (hasJob) {
                    day.title = 'Jobs scheduled — tap to add another';
                } else {
                    day.title = 'Tap to add a job';
                }
                grid.appendChild(day);
            }
        }    

        function changeMonth(dir) {
            currentMonth += dir;
            if (currentMonth > 11) { currentMonth = 0; currentYear++; }
            if (currentMonth < 0) { currentMonth = 11; currentYear--; }
            renderCalendar();
        }

        // ── BOOKING MODAL ──────────────────────────
        function openBookingModal(dateStr) {
            document.getElementById('bookDate').value = dateStr || '';
            document.getElementById('bookName').value = '';
            document.getElementById('bookPhone').value = '';
            document.getElementById('bookAddress').value = '';
            document.getElementById('bookJob').value = '';
            document.getElementById('bookTime').value = '09:00';

            if (dateStr) {
                const d = new Date(dateStr + 'T12:00:00');
                const options = { weekday: 'long', month: 'long', day: 'numeric' };
                document.getElementById('modalDate').textContent = d.toLocaleDateString('en-US', options);
            }

            document.getElementById('bookingModal').classList.add('active');
        }

        function closeBookingModal() {
            document.getElementById('bookingModal').classList.remove('active');
        }

        async function submitBooking() {
            const name = document.getElementById('bookName').value.trim();
            const phone = document.getElementById('bookPhone').value.trim();
            const address = document.getElementById('bookAddress').value.trim();
            const job = document.getElementById('bookJob').value.trim();
            const date = document.getElementById('bookDate').value;
            const time = document.getElementById('bookTime').value;

            if (!name || !phone || !date) {
                alert('Please fill in Name, Phone and Date.');
                return;
            }

            const btn = document.querySelector('.btn-book');
            btn.textContent = 'Booking...';
            btn.disabled = true;

            try {
                const res = await fetch('/dashboard/add-job', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Dashboard-Token': getCookie('dashboard_token')
                    },
                    body: JSON.stringify({
                        customer_name: name,
                        customer_phone: phone,
                        service_address: address,
                        job_description: job,
                        appointment_date: date,
                        appointment_time: time
                    })
                });

                const data = await res.json();
                if (data.ok) {
                    alert(`✅ Job booked for ${data.appointment}!\nConfirmation SMS sent to customer.`);
                    closeBookingModal();
                    loadDashboard();
                } else {
                    alert('Error: ' + (data.error || 'Something went wrong'));
                }
            } catch(e) {
                alert('Request failed. Please try again.');
            } finally {
                btn.textContent = 'Add Job →';
                btn.disabled = false;
            }
        }

        // ── COMPLETE & PAY MODAL ────────────────────────
        let completePayData = {};
        let selectedPayMethod = '';

        function openCompletePayModal(recordId, customerName, customerPhone, jobType) {
            completePayData = { recordId, customerName, customerPhone, jobType };
            selectedPayMethod = '';
            document.getElementById('completePayCustomer').textContent = customerName + ' — ' + jobType;
            document.getElementById('payAmount').value = '';
            document.getElementById('payEmail').value = '';
            document.getElementById('emailField').classList.remove('visible');
            // Reset method buttons
            document.querySelectorAll('.payment-method-btn').forEach(b => b.classList.remove('selected'));
            document.getElementById('completePayModal').classList.add('active');
        }

        function closeCompletePayModal() {
            document.getElementById('completePayModal').classList.remove('active');
        }

        function selectPayMethod(method, btn) {
            selectedPayMethod = method;
            document.querySelectorAll('.payment-method-btn').forEach(b => b.classList.remove('selected'));
            btn.classList.add('selected');
            // Show email field for QuickBooks
            const emailField = document.getElementById('emailField');
            if (method === 'QuickBooks') {
                emailField.classList.add('visible');
            } else {
                emailField.classList.remove('visible');
            }
        }

        async function submitCompleteAndPay() {
            const amount = parseFloat(document.getElementById('payAmount').value);
            const email = document.getElementById('payEmail').value.trim();

            if (!amount || amount <= 0) {
                alert('Please enter a valid amount.');
                return;
            }
            if (!selectedPayMethod) {
                alert('Please select a payment method.');
                return;
            }
            if (selectedPayMethod === 'QuickBooks' && !email) {
                alert('Please enter the customer email for QuickBooks invoice.');
                return;
            }

            const btn = document.getElementById('completePayBtn');
            btn.textContent = 'Processing...';
            btn.disabled = true;

            try {
                const res = await fetch('/dashboard/action/complete-and-pay', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Dashboard-Token': getCookie('dashboard_token')
                    },
                    body: JSON.stringify({
                        record_id: completePayData.recordId,
                        customer_name: completePayData.customerName,
                        customer_phone: completePayData.customerPhone,
                        job_description: completePayData.jobType,
                        amount: amount,
                        payment_method: selectedPayMethod,
                        customer_email: email
                    })
                });

                const data = await res.json();
                if (data.ok) {
                    alert('✅ ' + data.message);
                    closeCompletePayModal();
                    loadDashboard();
                } else {
                    alert('Error: ' + (data.error || 'Something went wrong'));
                }
            } catch(e) {
                alert('Request failed. Please try again.');
            } finally {
                btn.textContent = 'Send Payment →';
                btn.disabled = false;
            }
        }

        // ── RECURRING INVOICES ──────────────────────────
        async function loadRecurringCustomers() {
            try {
                const res = await fetch('/dashboard/recurring', {
                    headers: { 'X-Dashboard-Token': getCookie('dashboard_token') }
                });
                const data = await res.json();
                if (data.ok) renderRecurringCustomers(data.customers);
            } catch(e) {
                console.error('Recurring load error:', e);
            }
        }

        function renderRecurringCustomers(customers) {
            document.getElementById('recurringCount').textContent = customers.length;
            const el = document.getElementById('recurringCustomers');
            if (!customers.length) {
                el.innerHTML = '<div class="empty-state">No recurring customers — add them in Airtable</div>';
                return;
            }
            el.innerHTML = customers.map(c => {
                const methodIcon = {
                    'QuickBooks': '📚',
                    'EFT': '🏦',
                    'Check': '📝',
                    'Stripe': '💳',
                    'Zelle': '🏦'
                }[c.payment_method] || '💰';

                return `
                <div class="job-card">
                    <div class="job-name">${c.name || 'Unknown'}</div>
                    <div class="job-address">${c.email || c.phone || ''}</div>
                    <div class="job-type">${c.service || ''}</div>
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
                        <div style="color:#22c55e;font-size:18px;font-weight:700">$${c.amount}</div>
                        <div style="color:#555;font-size:12px;font-family:monospace">${methodIcon} ${c.payment_method}</div>
                    </div>
                    <div class="job-actions" style="margin-top:8px">
                        <button onclick="sendRecurringInvoice('${c.record_id}', '${c.name}', '${c.email}', '${c.phone}', ${c.amount}, '${c.service}', '${c.payment_method}')"
                            class="action-btn" style="background:#22c55e;color:#000;flex:1">
                            📤 Send Invoice
                        </button>
                    </div>
                    ${c.notes ? `<div class="job-type" style="margin-top:6px;color:#444">${c.notes}</div>` : ''}
                </div>`;
            }).join('');
        }

        async function sendRecurringInvoice(recordId, name, email, phone, amount, service, paymentMethod) {
            let invoiceAmount = amount;
            let invoiceService = service;

            if (!invoiceAmount || invoiceAmount <= 0) {
                const input = prompt("Enter invoice amount for " + name + ":");
                if (!input) return;
                invoiceAmount = parseFloat(input);
                if (isNaN(invoiceAmount) || invoiceAmount <= 0) {
                    alert("Invalid amount.");
                    return;
                }
            }

            if (!invoiceService || invoiceService.trim() === "") {
                const svcInput = prompt("Enter service description for " + name + ":");
                if (!svcInput) return;
                invoiceService = svcInput.trim();
            }

            if (!confirm("Send Stripe invoice to " + name + " at " + email + " for $" + invoiceAmount + "? They will receive a PDF invoice by email with a 30-day payment link.")) return;

            try {
                const res = await fetch("/dashboard/action/send-recurring-invoice", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-Dashboard-Token": getCookie("dashboard_token")
                    },
                    body: JSON.stringify({
                        customer_name: name,
                        customer_email: email,
                        customer_phone: phone,
                        amount: invoiceAmount,
                        service: invoiceService,
                        payment_method: paymentMethod
                    })
                });
                const data = await res.json();
                if (data.ok) {
                    alert("Invoice sent to " + email + "!\nInvoice #: " + data.invoice_number + "\nAmount: $" + invoiceAmount);
                } else {
                    alert("Error: " + (data.error || "Something went wrong"));
                }
            } catch(e) {
                alert("Request failed. Please try again.");
            }
        }

        // ── REVENUE SUMMARY ──────────────────────────
        async function loadRevenue() {
            try {
                const res = await fetch('/dashboard/revenue', {
                    headers: { 'X-Dashboard-Token': getCookie('dashboard_token') }
                });
                const data = await res.json();
                if (data.ok) {
                    document.getElementById('revWeek').textContent = '$' + data.week_revenue.toLocaleString();
                    document.getElementById('revMonth').textContent = '$' + data.month_revenue.toLocaleString();
                    document.getElementById('revMonthLabel').textContent = data.month_name;
                    document.getElementById('revJobs').textContent = data.jobs_this_month + ' jobs paid';
                    document.getElementById('revYear').textContent = '$' + data.year_revenue.toLocaleString();
                    document.getElementById('revOutstanding').textContent = '$' + data.outstanding.toLocaleString();
                }
            } catch(e) {
                console.error('Revenue load error:', e);
            }
        }

        

        // ── REGULAR CLIENTS ──────────────────────────
        let regularBookData = {};

        async function loadRegularClients() {
            try {
                const res = await fetch('/dashboard/regular-clients', {
                    headers: { 'X-Dashboard-Token': getCookie('dashboard_token') }
                });
                const data = await res.json();
                if (data.ok) renderRegularClients(data.clients);
            } catch(e) {
                console.error('Regular clients load error:', e);
            }
        }

        function renderRegularClients(clients) {
            document.getElementById('regularCount').textContent = clients.length;
            const el = document.getElementById('regularClients');
            if (!clients.length) {
                el.innerHTML = '<div class="empty-state">No regular clients — add them in Airtable</div>';
                return;
            }
            el.innerHTML = clients.map(c => {
                const urgency = c.days_until !== null && c.days_until <= 2
                    ? 'border-left: 3px solid #ef4444;'
                    : c.days_until !== null && c.days_until <= 5
                    ? 'border-left: 3px solid #f59e0b;'
                    : '';

                const daysLabel = c.days_until !== null
                    ? c.days_until === 0 ? '🔴 Today!'
                    : c.days_until === 1 ? '🟡 Tomorrow'
                    : c.days_until < 0 ? `🔴 Overdue ${Math.abs(c.days_until)}d`
                    : `🟢 In ${c.days_until} days`
                    : '';

                return `
                <div class="job-card" style="${urgency}">
                    <div class="job-name">${c.name || 'Unknown'}</div>
                    <div class="job-address">${c.address || ''}</div>
                    <div class="job-type">${c.service || ''} · Every ${c.frequency_days} days</div>
                    ${c.next_appointment ? `
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
                        <div style="color:#22c55e;font-size:13px;font-family:monospace">${c.next_appointment}</div>
                        <div style="font-size:12px">${daysLabel}</div>
                    </div>` : '<div style="color:#555;font-size:12px;margin-top:8px;font-family:monospace">No appointment scheduled</div>'}
                    <div class="job-actions" style="margin-top:10px">
                        <button onclick="openRegularBookModal('${c.record_id}', '${c.name}', '${c.phone}', '${c.address}', '${c.service}', ${c.frequency_days}, '${c.preferred_time}')"
                            class="action-btn btn-sms">📅 Book</button>
                        <button onclick="completeRegularClient('${c.record_id}', '${c.name}', ${c.frequency_days}, '${c.phone}', '${c.service}')"
                            class="action-btn" style="background:#22c55e;color:#000">Done</button>
                    </div>
                    ${c.phone ? `
                    <div class="job-actions" style="margin-top:8px">
                        <a href="tel:${c.phone}" class="action-btn btn-call">📞 Call</a>
                        <a href="sms:${c.phone}" class="action-btn btn-sms">💬 Text</a>
                    </div>` : ''}
                </div>`;
            }).join('');
        }

        function openRegularBookModal(recordId, name, phone, address, service, frequencyDays, preferredTime) {
            regularBookData = { recordId, name, phone, address, service, frequencyDays };
            document.getElementById('regularBookCustomer').textContent = name + ' — Every ' + frequencyDays + ' days';
            document.getElementById('regularBookTime').value = preferredTime || '09:00';

            // Pre-fill date with today
            const today = new Date();
            const dateStr = today.toISOString().split('T')[0];
            document.getElementById('regularBookDate').value = dateStr;

            document.getElementById('regularBookModal').classList.add('active');
        }

        function closeRegularBookModal() {
            document.getElementById('regularBookModal').classList.remove('active');
        }

        async function submitRegularBooking() {
            const date = document.getElementById('regularBookDate').value;
            const time = document.getElementById('regularBookTime').value;

            if (!date) {
                alert('Please select a date.');
                return;
            }

            const btn = document.getElementById('regularBookBtn');
            btn.textContent = 'Booking...';
            btn.disabled = true;

            try {
                const res = await fetch('/dashboard/action/book-regular-client', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Dashboard-Token': getCookie('dashboard_token')
                    },
                    body: JSON.stringify({
                        record_id: regularBookData.recordId,
                        customer_name: regularBookData.name,
                        customer_phone: regularBookData.phone,
                        service_address: regularBookData.address,
                        job_description: regularBookData.service,
                        appointment_date: date,
                        appointment_time: time,
                        frequency_days: regularBookData.frequencyDays,
                        twilio_number: dashboardData.twilio_number
                    })
                });

                const data = await res.json();
                if (data.ok) {
                    alert(`✅ Booked for ${data.appointment}!\nNext appointment auto-set for ${data.next_appointment}.\nConfirmation SMS sent to customer.`);
                    closeRegularBookModal();
                    loadRegularClients();
                    loadDashboard();
                } else {
                    alert('Error: ' + (data.error || 'Something went wrong'));
                }
            } catch(e) {
                alert('Request failed. Please try again.');
            } finally {
                btn.textContent = 'Book & Confirm →';
                btn.disabled = false;
            }
        }

        async function completeRegularClient(recordId, name, frequencyDays, phone, jobType) {
            if (!confirm("Mark " + name + " visit as complete?")) return;

            try {
                const res = await fetch("/dashboard/action/complete-regular-client", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-Dashboard-Token": getCookie("dashboard_token")
                    },
                    body: JSON.stringify({
                        record_id: recordId,
                        frequency_days: frequencyDays
                    })
                });

                const data = await res.json();
                if (data.ok) {
                    // Open Complete & Pay modal after marking done
                    openCompletePayModal(recordId, name, phone, jobType);
                } else {
                    alert("Error: " + (data.error || "Something went wrong"));
                }
            } catch(e) {
                alert("Request failed. Please try again.");
            }
        }

        // ── SEASONAL CAMPAIGNS ──────────────────────────
        async function loadSeasonalCampaigns() {
            try {
                const res = await fetch('/dashboard/seasonal-campaigns', {
                    headers: { 'X-Dashboard-Token': getCookie('dashboard_token') }
                });
                const data = await res.json();
                if (data.ok) renderSeasonalCampaigns(data.campaigns);
            } catch(e) {
                console.error('Seasonal campaigns load error:', e);
            }
        }

        function renderSeasonalCampaigns(campaigns) {
            const el = document.getElementById('seasonalCampaigns');
            if (!campaigns.length) {
                el.innerHTML = '<div class="empty-state">No active campaigns — add one in Airtable</div>';
                return;
            }
            el.innerHTML = campaigns.map(c => `
                <div class="job-card">
                    <div class="job-name">${c.name}</div>
                    <div class="job-type">${c.message_type} · ${c.season || 'All seasons'}</div>
                    <div style="font-size:12px;color:#888;margin-top:6px">${c.message_body.substring(0, 80)}...</div>
                    <div style="font-size:11px;color:#22c55e;margin-top:6px">Sent ${c.send_count || 0} times</div>
                    <div class="job-actions" style="margin-top:10px">
                        <button onclick="sendSeasonalBlast('${c.name}')" class="action-btn" style="background:#22c55e;color:#000">📤 Send Blast</button>
                    </div>
                </div>
            `).join('');
        }

        async function sendSeasonalBlast(campaignName) {
            if (!confirm(`Send "${campaignName}" to all active regular clients?`)) return;

            try {
                const res = await fetch('/send-seasonal-blast', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Dashboard-Token': getCookie('dashboard_token')
                    },
                    body: JSON.stringify({
                        twilio_number: dashboardData.twilio_number,
                        campaign_name: campaignName
                    })
                });
                const data = await res.json();
                if (data.ok) {
                    alert(`✅ Sent to ${data.sent} clients (${data.failed} failed)`);
                    loadSeasonalCampaigns();
                } else {
                    alert('Error: ' + (data.error || 'Something went wrong'));
                }
            } catch(e) {
                alert('Request failed. Please try again.');
            }
        }

        

        // ── VOICE INPUT ──────────────────────────
        let voiceRecognition = null;
        let isListening = false;

        function startVoiceInput() {
            if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
                alert('Voice input is not supported on this browser. Please use Safari on iPhone/iPad or Chrome on desktop.');
                return;
            }

            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            const btn = document.getElementById('voiceBtn');

            if (isListening) {
                voiceRecognition.stop();
                return;
            }

            voiceRecognition = new SpeechRecognition();
            voiceRecognition.continuous = false;
            voiceRecognition.interimResults = false;
            voiceRecognition.lang = 'en-US';

            voiceRecognition.onstart = () => {
                isListening = true;
                btn.textContent = '🔴 Listening...';
                btn.style.background = '#fee2e2';
                btn.style.borderColor = '#dc2626';
                btn.style.color = '#dc2626';
            };

            voiceRecognition.onresult = async (event) => {
                const transcript = event.results[0][0].transcript;
                console.log('Voice transcript:', transcript);
                btn.textContent = '⏳ Processing...';
                btn.style.background = '#fef9c3';
                btn.style.borderColor = '#d97706';
                btn.style.color = '#d97706';
                await parseVoiceTranscript(transcript);
            };

            voiceRecognition.onerror = (event) => {
                console.error('Voice error:', event.error);
                resetVoiceBtn();
                if (event.error === 'not-allowed') {
                    alert('Microphone access denied. Please allow microphone access in your browser settings.');
                } else {
                    alert('Voice recognition error: ' + event.error);
                }
            };

            voiceRecognition.onend = () => {
                isListening = false;
                if (btn.textContent === '🔴 Listening...') {
                    resetVoiceBtn();
                }
            };

            voiceRecognition.start();
        }

        function resetVoiceBtn() {
            const btn = document.getElementById('voiceBtn');
            btn.textContent = '🎤 Voice';
            btn.style.background = '';
            btn.style.borderColor = '';
            btn.style.color = '';
            isListening = false;
        }

        async function parseVoiceTranscript(transcript) {
            try {
                const res = await fetch('/dashboard/voice-parse', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Dashboard-Token': getCookie('dashboard_token')
                    },
                    body: JSON.stringify({ transcript })
                });

                const data = await res.json();

                if (data.ok && data.fields) {
                    const f = data.fields;

                    // Open booking modal and fill fields
                    openBookingModal(f.date || '');

                    // Fill in parsed fields with slight delay for modal to open
                    setTimeout(() => {
                        if (f.name) document.getElementById('bookName').value = f.name;
                        if (f.phone) document.getElementById('bookPhone').value = f.phone;
                        if (f.address) document.getElementById('bookAddress').value = f.address;
                        if (f.job) document.getElementById('bookJob').value = f.job;
                        if (f.date) document.getElementById('bookDate').value = f.date;
                        if (f.time) document.getElementById('bookTime').value = f.time;
                    }, 100);

                    resetVoiceBtn();

                    // Show what was parsed
                    const filled = [];
                    if (f.name) filled.push(`Name: ${f.name}`);
                    if (f.date) filled.push(`Date: ${f.date}`);
                    if (f.time) filled.push(`Time: ${f.time}`);
                    if (filled.length) {
                        console.log('Voice parsed:', filled.join(', '));
                    }

                } else {
                    resetVoiceBtn();
                    alert('Could not parse your voice input. Please try again or fill in manually.');
                }
            } catch(e) {
                resetVoiceBtn();
                alert('Voice processing failed. Please try again.');
            }
        }

        // -- WALKTHROUGH RECORDING --
        let wtRecognition = null;
        let wtIsRecording = false;
        let wtTranscriptFull = '';
        let wtTimerInterval = null;
        let wtSeconds = 0;

        function openWalkthroughModal() {
            wtTranscriptFull = '';
            wtSeconds = 0;
            document.getElementById('wtCustomer').value = '';
            document.getElementById('wtAddress').value = '';
            document.getElementById('wtTranscript').value = '';
            document.getElementById('wtStatus').textContent = 'Tap to start recording';
            document.getElementById('wtTimer').style.display = 'none';
            document.getElementById('wtRecordBtn').textContent = 'REC';
            document.getElementById('wtRecordBtn').style.background = 'var(--gradient)';
            document.getElementById('wtSubmitBtn').disabled = false;
            document.getElementById('wtSubmitBtn').textContent = 'Generate Estimate';
            document.getElementById('walkthroughModal').classList.add('active');
        }

        function closeWalkthroughModal() {
            stopWalkthroughRecording();
            document.getElementById('walkthroughModal').classList.remove('active');
        }

        function toggleWalkthroughRecording() {
            if (wtIsRecording) {
                stopWalkthroughRecording();
            } else {
                startWalkthroughRecording();
            }
        }

        function startWalkthroughRecording() {
            if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
                alert(`Voice recording not supported. Use Safari on iPhone or Chrome on desktop.`);
                return;
            }
            const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
            wtRecognition = new SR();
            wtRecognition.continuous = true;
            wtRecognition.interimResults = true;
            wtRecognition.lang = 'en-US';
            let interim = '';
            wtRecognition.onresult = function(event) {
                interim = '';
                for (let i = event.resultIndex; i < event.results.length; i++) {
                    if (event.results[i].isFinal) {
                        wtTranscriptFull += event.results[i][0].transcript + ' ';
                    } else {
                        interim += event.results[i][0].transcript;
                    }
                }
                document.getElementById('wtTranscript').value = wtTranscriptFull + interim;
            };
            wtRecognition.onerror = function(e) {
                if (e.error !== 'no-speech') console.error('Walkthrough error:', e.error);
            };
            wtRecognition.onend = function() {
                if (wtIsRecording) wtRecognition.start();
            };
            wtRecognition.start();
            wtIsRecording = true;
            document.getElementById('wtRecordBtn').textContent = 'STOP';
            document.getElementById('wtRecordBtn').style.background = '#dc2626';
            document.getElementById('wtStatus').textContent = 'Recording - speak as you walk the property';
            document.getElementById('wtTimer').style.display = 'block';
            wtSeconds = 0;
            wtTimerInterval = setInterval(function() {
                wtSeconds++;
                var m = Math.floor(wtSeconds / 60);
                var s = wtSeconds % 60;
                document.getElementById('wtTimer').textContent = m + ':' + (s < 10 ? '0' : '') + s;
            }, 1000);
        }

        function stopWalkthroughRecording() {
            if (wtRecognition) {
                wtIsRecording = false;
                try { wtRecognition.abort(); } catch(e) {}
                wtRecognition = null;
            }
            if (wtTimerInterval) {
                clearInterval(wtTimerInterval);
                wtTimerInterval = null;
            }
            var btn = document.getElementById('wtRecordBtn');
            if (btn) {
                btn.textContent = 'REC';
                btn.style.background = 'var(--gradient)';
            }
            document.getElementById('wtStatus').textContent = 'Recording stopped - review transcript below';
            document.getElementById('wtTimer').style.display = 'none';
        }

        async function submitVoiceWalkthrough() {
            var customer = document.getElementById('wtCustomer').value.trim();
            var address = document.getElementById('wtAddress').value.trim();
            var projectType = document.getElementById('wtProjectType').value;
            var transcript = document.getElementById('wtTranscript').value.trim();

            if (!transcript) {
                alert('Please record a walkthrough or type notes first.');
                return;
            }

            var btn = document.getElementById('wtSubmitBtn');
            btn.textContent = 'Generating estimate...';
            btn.disabled = true;

            try {
                var res = await fetch('/dashboard/walkthrough', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Dashboard-Token': getCookie('dashboard_token')
                    },
                    body: JSON.stringify({
                        transcript: transcript,
                        customer_name: customer,
                        property_address: address,
                        project_type: projectType
                    })
                });
                var data = await res.json();
                if (data.ok) {
                    closeWalkthroughModal();
                    alert(`Walkthrough estimate generated!\n\nEstimate: ${data.estimate_range}\nTimeline: ${data.timeline}\n\nPDF emailed to you!`);
                } else {
                    alert('Error: ' + (data.error || 'Something went wrong'));
                }
            } catch(e) {
                alert('Request failed. Please try again.');
            } finally {
                btn.textContent = 'Generate Estimate';
                btn.disabled = false;
            }
        }

        // ── ONESIGNAL PUSH NOTIFICATIONS ──────────────────────────
        async function registerOneSignal() {
            try {
                if (typeof OneSignalDeferred === 'undefined') {
                    console.log('OneSignal not loaded');
                    return;
                }
                OneSignalDeferred.push(async function(OneSignal) {
                    try {
                        const permission = await OneSignal.Notifications.permission;
                        if (!permission) {
                            await OneSignal.Notifications.requestPermission();
                        }
                        const playerId = OneSignal.User.PushSubscription.id;
                        if (!playerId) return;
                        await fetch('/onesignal/register', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-Dashboard-Token': getCookie('dashboard_token')
                            },
                            body: JSON.stringify({ player_id: playerId })
                        });
                    } catch(e) {
                        console.log('OneSignal inner error:', e);
                    }
                });
            } catch(e) {
                console.log('OneSignal error:', e);
            }
        }

        async function loadDashboard() {
            try {
                const token = getCookie('dashboard_token');
                if (!token) {
                    window.location.href = '/dashboard/login';
                    return;
                }
                const res = await fetch('/dashboard/data', {
                    headers: { 'X-Dashboard-Token': token }
                });
                if (res.status === 401) {
                    localStorage.removeItem('dashboard_token');
                    window.location.href = '/dashboard/login';
                    return;
                }
                dashboardData = await res.json();
                renderAll();
                loadRecurringCustomers();
                loadRevenue();
                loadRegularClients();
                loadSeasonalCampaigns();
                setTimeout(registerOneSignal, 3000);  // ← delay 3 seconds, never blocks
            } catch(e) {
                console.error('Dashboard load error:', e);
            }
        }

        // -- ADD CONTRACTOR --
        function openAddContractorModal() {
            document.getElementById("cName").value = "";
            document.getElementById("cBusiness").value = "";
            document.getElementById("cPhone").value = "";
            document.getElementById("cEmail").value = "";
            document.getElementById("cTwilio").value = "";
            document.getElementById("cPassword").value = "";
            document.getElementById("addContractorModal").classList.add("active");
        }

        function closeAddContractorModal() {
            document.getElementById("addContractorModal").classList.remove("active");
        }

        async function submitAddContractor() {
            var name = document.getElementById("cName").value.trim();
            var business = document.getElementById("cBusiness").value.trim();
            var phone = document.getElementById("cPhone").value.trim();
            var email = document.getElementById("cEmail").value.trim();
            var twilio = document.getElementById("cTwilio").value.trim();
            var password = document.getElementById("cPassword").value.trim();

            if (!name || !phone || !twilio || !password) {
                alert("Please fill in Name, Phone, Twilio Number and Password.");
                return;
            }

            var btn = document.getElementById("addContractorBtn");
            btn.textContent = "Adding...";
            btn.disabled = true;

            try {
                var res = await fetch("/dashboard/action/add-contractor", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-Dashboard-Token": getCookie("dashboard_token")
                    },
                    body: JSON.stringify({
                        contractor_name: name,
                        business_name: business,
                        phone: phone,
                        email: email,
                        twilio_number: twilio,
                        password: password
                    })
                });
                var data = await res.json();
                if (data.ok) {
                    closeAddContractorModal();
                    alert("Contractor added! SMS and email sent. Record ID: " + data.record_id);
                } else {
                    alert("Error: " + (data.error || "Something went wrong"));
                }
            } catch(e) {
                alert("Request failed. Please try again.");
            } finally {
                btn.textContent = "Add and Send Invite";
                btn.disabled = false;
            }
        }

        async function connectStripe() {
            try {
                const res = await fetch("/dashboard/action/connect-stripe", {
                    method: "POST",
                    headers: { "X-Dashboard-Token": getCookie("dashboard_token") }
                });
                const data = await res.json();
                if (data.ok && data.url) {
                    window.location.href = data.url;
                } else {
                    alert("Error: " + (data.error || "Could not start Stripe setup"));
                }
            } catch(e) {
                alert("Request failed. Please try again.");
            }
        }

        // Load on startup
        loadDashboard();
        // Auto-refresh every 5 minutes
        setInterval(loadDashboard, 5 * 60 * 1000);
    </script>
    <!-- Booking Modal -->
    <div class="modal-overlay" id="bookingModal">
        <div class="booking-modal">
        <div class="modal-title">➕ Add Job</div>
        <div class="modal-date" id="modalDate"></div>

        <div class="form-group">
            <label class="form-label">Customer Name *</label>
            <input type="text" class="form-input" id="bookName" placeholder="John Doe">
        </div>
        <div class="form-group">
            <label class="form-label">Phone Number *</label>
            <input type="tel" class="form-input" id="bookPhone" placeholder="+12025551234">
        </div>
        <div class="form-group">
            <label class="form-label">Service Address</label>
            <input type="text" class="form-input" id="bookAddress" placeholder="123 Main St, Bowie MD">
        </div>
        <div class="form-group">
            <label class="form-label">Job Description</label>
            <input type="text" class="form-input" id="bookJob" placeholder="Lawn mowing, edging...">
        </div>
        <div class="form-row">
            <div class="form-group">
                <label class="form-label">Date *</label>
                <input type="date" class="form-input" id="bookDate">
            </div>
            <div class="form-group">
                <label class="form-label">Time</label>
                <input type="time" class="form-input" id="bookTime" value="09:00">
            </div>
        </div>

        <div class="modal-actions">
            <button class="btn-cancel-modal" onclick="closeBookingModal()">Cancel</button>
            <button class="btn-book" onclick="submitBooking()">Add Job →</button>
        </div>
    </div>
</div>
    <!-- Complete & Pay Modal -->
    <div class="modal-overlay" id="completePayModal">
        <div class="booking-modal">
            <div class="modal-title">✓ Mark Job Complete</div>
            <div class="modal-date" id="completePayCustomer"></div>

            <div class="form-group">
                <label class="form-label">Amount *</label>
                <input type="number" class="form-input" id="payAmount" placeholder="0.00" step="0.01" min="0">
            </div>

            <div class="form-group">
                <label class="form-label">Payment Method *</label>
                <div class="payment-method-grid">
                    <button class="payment-method-btn" onclick="selectPayMethod('Stripe', this)">💳 Stripe</button>
                    <button class="payment-method-btn" onclick="selectPayMethod('Zelle', this)">🏦 Zelle</button>
                    <button class="payment-method-btn" onclick="selectPayMethod('QuickBooks', this)">📚 QuickBooks</button>
                    <button class="payment-method-btn" onclick="selectPayMethod('Cash', this)">💵 Cash</button>
                </div>
            </div>

            <div class="form-group email-field" id="emailField">
                <label class="form-label">Customer Email (QuickBooks)</label>
                <input type="email" class="form-input" id="payEmail" placeholder="customer@email.com">
            </div>

            <div class="modal-actions">
                <button class="btn-cancel-modal" onclick="closeCompletePayModal()">Cancel</button>
                <button class="btn-book" id="completePayBtn" onclick="submitCompleteAndPay()">Send Payment →</button>
            </div>
        </div>
</div>

    <!-- Regular Client Booking Modal -->
<div class="modal-overlay" id="regularBookModal">
    <div class="booking-modal">
        <div class="modal-title">📅 Book Appointment</div>
        <div class="modal-date" id="regularBookCustomer"></div>
        <div class="form-row">
            <div class="form-group">
                <label class="form-label">Date *</label>
                <input type="date" class="form-input" id="regularBookDate">
            </div>
            <div class="form-group">
                <label class="form-label">Time</label>
                <input type="time" class="form-input" id="regularBookTime" value="09:00">
            </div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel-modal" onclick="closeRegularBookModal()">Cancel</button>
            <button class="btn-book" id="regularBookBtn" onclick="submitRegularBooking()">Book & Confirm →</button>
        </div>
    </div>
</div>

    <!-- Walkthrough Recording Modal -->
    <div class="modal-overlay" id="walkthroughModal">
        <div class="booking-modal">
            <div class="modal-title">Walkthrough Estimate</div>
            <div class="modal-date">Walk the property and describe what you see</div>
            <div class="form-group">
                <label class="form-label">Customer Name</label>
                <input type="text" class="form-input" id="wtCustomer" placeholder="John Smith">
            </div>
            <div class="form-group">
                <label class="form-label">Property Address</label>
                <input type="text" class="form-input" id="wtAddress" placeholder="123 Main St, Bowie MD">
            </div>
            <div class="form-group">
                <label class="form-label">Project Type</label>
                <select class="form-input" id="wtProjectType">
                    <option>Lawn and Landscaping</option>
                    <option>Home Remodel</option>
                    <option>Exterior Project</option>
                    <option>HVAC</option>
                    <option>Electrical</option>
                    <option>Carpentry</option>
                    <option>Pressure Washing</option>
                    <option>Snow and Ice</option>
                    <option>General Contracting</option>
                    <option>Other</option>
                </select>
            </div>
            <div style="background:var(--bg);border:1px solid var(--card-border);border-radius:12px;padding:16px;margin-bottom:16px;text-align:center">
                <button id="wtRecordBtn" onclick="toggleWalkthroughRecording()" style="background:var(--gradient);color:white;border:none;border-radius:50%;width:72px;height:72px;font-size:16px;font-weight:700;cursor:pointer;display:block;margin:0 auto 12px">
                    REC
                </button>
                <div id="wtStatus" style="font-size:13px;color:var(--text-muted)">Tap to start recording</div>
                <div id="wtTimer" style="font-size:24px;font-weight:700;color:var(--blue);margin-top:8px;display:none">0:00</div>
            </div>
            <div class="form-group">
                <label class="form-label">Transcript (auto-filled or type notes)</label>
                <textarea class="form-input" id="wtTranscript" rows="4" placeholder="Your walkthrough notes will appear here as you speak." style="resize:none;font-size:13px"></textarea>
            </div>
            <div class="modal-actions">
                <button class="btn-cancel-modal" onclick="closeWalkthroughModal()">Cancel</button>
                <button class="btn-book" id="wtSubmitBtn" onclick="submitVoiceWalkthrough()">Generate Estimate</button>
            </div>
        </div>
    </div>

    <!-- Add Contractor Modal -->
    <div class="modal-overlay" id="addContractorModal">
        <div class="booking-modal">
            <div class="modal-title">Add Contractor</div>
            <div class="modal-date">New contractor onboarding</div>
            <div class="form-group">
                <label class="form-label">Contractor Name *</label>
                <input type="text" class="form-input" id="cName" placeholder="John Smith">
            </div>
            <div class="form-group">
                <label class="form-label">Business Name</label>
                <input type="text" class="form-input" id="cBusiness" placeholder="Smith Lawn Care LLC">
            </div>
            <div class="form-group">
                <label class="form-label">Their Phone Number *</label>
                <input type="tel" class="form-input" id="cPhone" placeholder="+12025551234">
            </div>
            <div class="form-group">
                <label class="form-label">Email</label>
                <input type="email" class="form-input" id="cEmail" placeholder="john@smithlawn.com">
            </div>
            <div class="form-group">
                <label class="form-label">Their Twilio Number *</label>
                <input type="tel" class="form-input" id="cTwilio" placeholder="+12405551234">
            </div>
            <div class="form-group">
                <label class="form-label">Dashboard Password *</label>
                <input type="text" class="form-input" id="cPassword" placeholder="Create a password for them">
            </div>
            <div class="modal-actions">
                <button class="btn-cancel-modal" onclick="closeAddContractorModal()">Cancel</button>
                <button class="btn-book" id="addContractorBtn" onclick="submitAddContractor()">Add and Send Invite</button>
            </div>
        </div>
    </div>

    </body>
</html>
    '''


@app.route("/dashboard/data")
@dashboard_auth_required
def dashboard_data():
    """Returns JSON data for the dashboard."""
    try:
        import requests as req
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta

        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        twilio_number = request.twilio_number
        contractor_record_id = request.contractor_id

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        business_name = contractor.get("Business Name", "")
        cal_booking_url = contractor.get("CAL Booking URL", "")

        leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"

        all_jobs_resp = req.get(leads_url, headers=headers, params={
            "filterByFormula": f"AND({{Lead Status}} = 'Booked', {{Twilio Number}} = '{twilio_number}', {{Appointment Date and Time}} != '')"
        })
        all_job_records = all_jobs_resp.json().get("records", [])

        def parse_job(record):
            fields = record.get("fields", {})
            appt = fields.get("Appointment Date and Time", "")
            date_str = ""
            time_str = ""
            try:
                dt = datetime.fromisoformat(appt)

                # If no timezone, assume it's already Eastern
                if dt.tzinfo is None:
                    dt_eastern = dt.replace(tzinfo=eastern)
                else:
                    dt_eastern = dt.astimezone(eastern)

                date_str = dt_eastern.strftime("%Y-%m-%d")
                time_str = dt_eastern.strftime("%-I:%M %p")

            except Exception as e:
                print("TIME PARSE ERROR:", appt, e)
                pass
            return {
                # ADDED: record_id for action buttons
                "record_id": record.get("id", ""),
                "name": fields.get("Customer Name") or fields.get("Client Name") or "Unknown",
                "phone": fields.get("Call Back Number", ""),
                "address": fields.get("Service Address", ""),
                "job_type": fields.get("Job Description", ""),
                "date": date_str,
                "time": time_str,
                "priority": fields.get("Priority", "STANDARD"),
                "timing": fields.get("Appointment Requested", "")
            }

        all_jobs = [parse_job(r) for r in all_job_records]
        today_jobs = [j for j in all_jobs if j["date"] == today_str]
        tomorrow_jobs = [j for j in all_jobs if j["date"] == tomorrow_str]

        open_leads_resp = req.get(leads_url, headers=headers, params={
            "filterByFormula": (
                f"AND("
                f"OR({{Lead Status}} = 'New Lead', {{Lead Status}} = 'Contacted'), "
                f"{{Twilio Number}} = '{twilio_number}'"
                f")"
            )
        })
        open_lead_records = open_leads_resp.json().get("records", [])
        open_leads = []
        for r in open_lead_records:
            f = r.get("fields", {}) 
            open_leads.append({
                # ADDED: record_id for action buttons
                "record_id": r.get("id", ""),
                "name": f.get("Customer Name") or f.get("Client Name") or "Unknown",
                "phone": f.get("Call Back Number", ""),
                "address": f.get("Service Address", ""),
                "job_type": f.get("Job Description", ""),
                "timing": f.get("Appointment Requested", ""),
                "priority": f.get("Priority", "STANDARD")
            })

        payments_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments"
        unpaid_resp = req.get(payments_url, headers=headers, params={
            "filterByFormula": "AND({Payment Status} = 'Unpaid', {Phone Number} != '', {Send Invoice} = FALSE())"
        })
        unpaid_records = unpaid_resp.json().get("records", [])
        unpaid_invoices = []
        for r in unpaid_records:
            f = r.get("fields", {})
            print(f"UNPAID PAYMENT FIELDS | {f}")
            contractor_links = f.get("Contractor", [])
            if contractor_record_id and contractor_record_id not in str(contractor_links):
                continue
            payment_date = f.get("Payment Date", "")
            days_outstanding = 0
            try:
                pd = datetime.fromisoformat(payment_date)
                if pd.tzinfo is None:
                    pd = pd.replace(tzinfo=eastern)
                days_outstanding = (now - pd).days
            except Exception:
                pass
            unpaid_invoices.append({
                # ADDED: record_id for action buttons
                "record_id": r.get("id", ""),
                "name": f.get("Customer Name") or f.get("Customer Name ") or f.get("Client Name") or f.get("Name") or "Unknown",
                "phone": f.get("Phone Number", ""),
                "amount": f.get("Amount", 0),
                "job_type": f.get("Notes", ""),
                "days_outstanding": days_outstanding
            }) 

        recent_bookings = sorted(
            [j for j in all_jobs if j["date"]],
            key=lambda x: x["date"],
            reverse=True
        )[:10]

        return jsonify({
            "ok": True,
            "business_name": business_name,
            "cal_booking_url": cal_booking_url,
            "twilio_number": twilio_number,
            "today_jobs": today_jobs,
            "tomorrow_jobs": tomorrow_jobs,
            "all_jobs": all_jobs,
            "open_leads": open_leads,
            "unpaid_invoices": unpaid_invoices,
            "recent_bookings": recent_bookings
        })

    except Exception as e:
        print(f"DASHBOARD DATA ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/walkthrough')
@dashboard_auth_required
def walkthrough_page():
    """Serves the video walkthrough page."""
    root_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(root_dir, 'Walkthrough.html')

@app.route("/onesignal/register", methods=["POST"])
@dashboard_auth_required
def onesignal_register():
    """Saves OneSignal player ID with contractor's twilio number as a tag."""
    try:
        data = request.get_json(silent=True) or {}
        player_id = data.get("player_id", "")
        twilio_number = request.twilio_number

        if not player_id:
            return jsonify({"ok": False}), 400

        import requests as req
        ONESIGNAL_APP_ID = os.environ.get("ONESIGNAL_APP_ID")
        ONESIGNAL_API_KEY = os.environ.get("ONESIGNAL_API_KEY")

        # Tag the device with the contractor's twilio number
        resp = req.put(
            f"https://onesignal.com/api/v1/players/{player_id}",
            headers={
                "Authorization": f"Basic {ONESIGNAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "app_id": ONESIGNAL_APP_ID,
                "tags": {"twilio_number": twilio_number}
            },
            timeout=10
        )
        print(f"ONESIGNAL | Registered | {twilio_number} | {player_id}")
        return jsonify({"ok": True})

    except Exception as e:
        print(f"ONESIGNAL REGISTER ERROR | {e}")
        return jsonify({"ok": False}), 500

@app.route("/dashboard/seasonal-campaigns", methods=["GET"])
@dashboard_auth_required
def dashboard_seasonal_campaigns():
    """Returns active seasonal campaigns for the logged-in contractor."""
    try:
        import requests as req
        twilio_number = request.twilio_number

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

        campaigns_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblSrBFioDKG0uKIU"
        resp = req.get(campaigns_url, headers=headers, params={
            "filterByFormula": f"AND({{Twilio Number}} = '{twilio_number}', {{Active}} = TRUE())"
        })
        records = resp.json().get("records", [])

        campaigns = []
        for r in records:
            f = r.get("fields", {})
            campaigns.append({
                "name": f.get("Campaign Name", ""),
                "message_type": f.get("Message Type", ""),
                "season": f.get("Season", ""),
                "message_body": f.get("Message Body", ""),
                "send_count": f.get("Send Count", 0),
            })

        return jsonify({"ok": True, "campaigns": campaigns})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500





@app.route("/dashboard/logout")
def dashboard_logout():
    """Clears dashboard session."""
    resp = make_response(redirect("/dashboard/login"))
    resp.delete_cookie("dashboard_token")
    return resp

# ── DASHBOARD ACTION BUTTONS ────────────────────────────────────

@app.route("/dashboard/action/send-confirmation", methods=["POST"])
@dashboard_auth_required
def dashboard_send_confirmation():
    """Sends appointment confirmation SMS to customer."""
    try:
        data = request.get_json(silent=True) or {}
        customer_name = data.get("customer_name", "there")
        customer_phone = data.get("customer_phone", "")
        appointment_time = data.get("appointment_time", "")
        twilio_number = request.twilio_number

        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        business_name = contractor.get("Business Name", "your contractor")
        first_name = customer_name.split()[0] if customer_name else "there"

        msg = (
            f"Hi {first_name}! This is a confirmation from {business_name}. "
            f"Your appointment is confirmed for {appointment_time}. "
            f"We look forward to seeing you! Reply CANCEL APPOINTMENT to cancel."
        )

        send_fallback_sms(to_number=customer_phone, body=msg)
        print(f"DASHBOARD | Confirmation sent | {customer_name} | {customer_phone}")
        return jsonify({"ok": True})

    except Exception as e:
        print(f"DASHBOARD CONFIRMATION ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/on-my-way", methods=["POST"])
@dashboard_auth_required
def dashboard_on_my_way():
    """Sends on my way SMS to customer."""
    try:
        data = request.get_json(silent=True) or {}
        customer_name = data.get("customer_name", "there")
        customer_phone = data.get("customer_phone", "")
        twilio_number = request.twilio_number

        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        business_name = contractor.get("Business Name", "your contractor")
        first_name = customer_name.split()[0] if customer_name else "there"

        msg = (
            f"Hi {first_name}! Your {business_name} contractor is on the way. "
            f"We'll see you shortly!"
        )

        print("DASHBOARD | ON MY WAY DEBUG")
        print("Customer Name:", customer_name)
        print("Customer Phone:", customer_phone)
        print("Message:", msg)

        send_fallback_sms(to_number=customer_phone, body=msg)

        print(f"DASHBOARD | On my way sent | {customer_name} | {customer_phone}")
        return jsonify({"ok": True})
           

    except Exception as e:
        print(f"DASHBOARD ON MY WAY ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/mark-complete", methods=["POST"])
@dashboard_auth_required
def dashboard_mark_complete():
    """Marks a job as complete in Airtable."""
    try:
        data = request.get_json(silent=True) or {}
        record_id = data.get("record_id", "")

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        response = requests.patch(
            f"{leads_url}/{record_id}",
            headers=headers,
            json={"fields": {"Lead Status": "Completed"}}
        )

        if response.status_code == 200:
            print(f"DASHBOARD | Job marked complete | {record_id}")
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": response.text}), 500

    except Exception as e:
        print(f"DASHBOARD MARK COMPLETE ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/dashboard/action/complete-and-pay", methods=["POST"])
@dashboard_auth_required
def dashboard_complete_and_pay():
    """
    Marks job complete AND sends payment request in one tap.
    Handles Stripe, Zelle, QuickBooks, and Cash.
    """
    try:
        data = request.get_json(silent=True) or {}
        record_id = data.get("record_id", "")
        customer_name = data.get("customer_name", "")
        customer_phone = data.get("customer_phone", "")
        job_description = data.get("job_description", "")
        amount = float(data.get("amount", 0))
        payment_method = data.get("payment_method", "")
        payment_method_airtable = "Zelle " if payment_method == "Zelle" else payment_method
        customer_email = data.get("customer_email", "")
        twilio_number = request.twilio_number
        contractor_record_id = request.contractor_id

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }
        leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"
        payments_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments"

        # Step 1 — Mark lead as Completed
        if record_id:
            requests.patch(
                f"{leads_url}/{record_id}",
                headers=headers,
                json={"fields": {"Lead Status": "Completed"}}
            )
            print(f"COMPLETE AND PAY | Lead marked complete | {record_id}")

        # Step 2 — Handle Cash — just mark paid, no payment record needed
        if payment_method == "Cash":
            print(f"COMPLETE AND PAY | Cash payment | {customer_name} | ${amount}")
            return jsonify({"ok": True, "message": "Job marked complete. Cash payment recorded."})

        # Step 3 — Create Payment record in Airtable
        today = datetime.now().strftime("%Y-%m-%d")
        payment_fields = {
            "fldAZ5Qr0NCU11J0A": customer_name,      # Customer Name
            "fld8bUzdzFeeXLrlD": customer_phone,       # Phone Number
            "fld596bZM5ZCI7ga8": amount,               # Amount
            "fldeROEzoyhWKJ36y": job_description,      # Notes
            "fldWg6gGv6dKFb853": "Unpaid",            # Payment Status
            "fldUFO1PfTeiLA3UR": payment_method_airtable,       # Payment Method
            "fldYNu0gpLuiCsF6Z": today,               # Payment Date
            "fldxdSy7mICyTo50P": [contractor_record_id],  # Contractor
        }

        # Add email for QuickBooks
        if customer_email:
            payment_fields["fld1J5DuxJVcreFKk"] = customer_email  # Client Email

        # For Stripe/Zelle — check Send Payment Request to trigger automation
        if payment_method in ["Stripe", "Zelle"]:
            payment_fields["fldEifNosHbfRIzwu"] = True  # Send Payment Request

        # For QuickBooks — check Send Invoice to trigger automation
        if payment_method == "QuickBooks":
            payment_fields["fldmTaAGMRf5aafaE"] = True  # Send Invoice

        payment_resp = requests.post(
            payments_url,
            headers=headers,
            json={"fields": payment_fields}
        )

        if payment_resp.status_code in [200, 201]:
            payment_record_id = payment_resp.json().get("id", "")
            print(f"COMPLETE AND PAY | Payment record created | {payment_record_id} | {payment_method} | ${amount}")
        else:
            print(f"COMPLETE AND PAY | Payment record error | {payment_resp.text}")
            return jsonify({"ok": False, "error": "Failed to create payment record"}), 500

        # Step 4 — QuickBooks direct flow (creates and emails invoice immediately)
        if payment_method == "QuickBooks":
            try:
                state = {
                    "name": customer_name,
                    "service_address": "",
                    "job_description": job_description,
                    "callback": customer_phone,
                    "timing": "",
                    "client_email": customer_email,
                    "estimate_amount": amount,
                }
                qb_result = create_qb_invoice(state)
                if qb_result.get("ok"):
                    invoice_id = qb_result.get("invoice_id")
                    access_token, realm_id = get_valid_access_token()
                    if access_token and invoice_id and customer_email:
                        email_url = f"{QB_API_BASE}/{realm_id}/invoice/{invoice_id}/send"
                        qb_headers = {
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/octet-stream",
                        }
                        requests.post(
                            email_url,
                            headers=qb_headers,
                            params={"sendTo": customer_email, "minorversion": "65"},
                            timeout=15
                        )
                        print(f"COMPLETE AND PAY | QB invoice emailed | {customer_email}")
                    # Update payment status to Invoiced
                    requests.patch(
                        f"{payments_url}/{payment_record_id}",
                        headers=headers,
                        json={"fields": {"Payment Status": "Invoiced"}}
                    )
            except Exception as e:
                print(f"COMPLETE AND PAY | QB error | {e}")

        method_messages = {
            "Stripe": f"Job complete! Stripe payment link sent to {customer_name}.",
            "Zelle": f"Job complete! Zelle payment request sent to {customer_name}.",
            "QuickBooks": f"Job complete! QuickBooks invoice emailed to {customer_email}.",
            "Cash": f"Job complete! Cash payment recorded."
        }

        return jsonify({
            "ok": True,
            "message": method_messages.get(payment_method, "Job marked complete!"),
            "payment_record_id": payment_record_id
        })

    except Exception as e:
        print(f"COMPLETE AND PAY ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/dashboard/voice-parse", methods=["POST"])
@dashboard_auth_required
def dashboard_voice_parse():
    """Parses voice transcript into job fields using Claude."""
    try:
        data = request.get_json(silent=True) or {}
        transcript = data.get("transcript", "").strip()

        if not transcript:
            return jsonify({"ok": False, "error": "No transcript"}), 400

        from zoneinfo import ZoneInfo
        from datetime import datetime
        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        today_str = now.strftime("%Y-%m-%d")
        today_day = now.strftime("%A")

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": f"""Extract job booking details from this voice transcript.
Today is {today_day}, {today_str}.

Transcript: "{transcript}"

Return ONLY valid JSON with these fields:
{{
  "name": "customer full name or empty string",
  "phone": "phone number with dashes or empty string",
  "address": "service address or empty string",
  "job": "job description or empty string",
  "date": "YYYY-MM-DD format",
  "time": "HH:MM in 24hr format or 09:00 if not mentioned"
}}

Only return the JSON object, nothing else."""
            }]
        )

        import json as json_lib
        raw = response.content[0].text.strip()
        parsed = json_lib.loads(raw)
        print(f"VOICE PARSE | {transcript[:50]} | {parsed}")
        return jsonify({"ok": True, "fields": parsed})

    except Exception as e:
        print(f"VOICE PARSE ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/dashboard/walkthrough", methods=["POST"])
@dashboard_auth_required
def dashboard_walkthrough():
    """
    Processes a property walkthrough VIDEO recording.
    Video → Gemini Vision → structured estimate → PDF → email to contractor.
    """
    print("=== WALKTHROUGH HIT ===", flush=True)
    print("FILES:", list(request.files.keys()), flush=True)
    print("FORM:", list(request.form.keys()), flush=True)
    print("CONTENT TYPE:", request.content_type, flush=True)
    
    try:
        from google import genai
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        import tempfile
        import mimetypes

        twilio_number = request.twilio_number
        contractor_record_id = request.contractor_id
        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        business_name = contractor.get("Business Name", "Your Business")
        notify_email = contractor.get("Notify Email", "").strip()

        # Get form data
        customer_name = request.form.get("customer_name", "").strip()
        contractor_notes = request.form.get("contractor_notes", "").strip()
        property_address = request.form.get("property_address", "").strip()
        project_type = request.form.get("project_type", "General Contracting").strip()
        video_file = request.files.get("video")

        if not video_file:
            return jsonify({"ok": False, "error": "No video file provided"}), 400

        print(f"WALKTHROUGH | {customer_name} | {project_type} | video: {video_file.filename}")

        # Step 1 — Save video to temp file
        suffix = ".mp4"
        if video_file.filename.endswith(".mov"):
            suffix = ".mov"
        elif video_file.filename.endswith(".webm"):
            suffix = ".webm"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            video_file.save(tmp.name)
            tmp_path = tmp.name

        print(f"WALKTHROUGH | Video saved to temp | {tmp_path}")

        # Step 2 — Upload to Cloudinary for storage
        video_url = ""
        try:
            import cloudinary.uploader as cloud_upload
            timestamp = int(time.time())
            safe_name = (customer_name or "walkthrough").replace(" ", "_")[:20]
            upload_result = cloud_upload.upload(
                tmp_path,
                resource_type="video",
                public_id=f"contractoros/walkthroughs/{safe_name}_{timestamp}",
                overwrite=True
            )
            video_url = upload_result.get("secure_url", "")
            print(f"WALKTHROUGH | Video uploaded to Cloudinary | {video_url}")
        except Exception as e:
            print(f"WALKTHROUGH | Cloudinary video upload error | {e}")

        # Step 3 — Send video to Gemini for analysis
        prompt = f"""You are an expert contractor estimator for {business_name}.
Analyze this property walkthrough video carefully. Watch every frame and listen to all audio.
Customer: {customer_name}
Property: {property_address}
Project Type: {project_type}
Based on what you SEE in the video and HEAR from the contractor narration:
1. Identify every area, room, or surface that needs work
2. Note the condition, size, and complexity of each area
3. Generate a complete professional contractor estimate
Return ONLY this JSON format:
{{
  "project_summary": "2-3 sentence overview of what you observed",
  "scope_of_work": "detailed paragraph of all work needed",
  "line_items": [
    {{"description": "Specific task", "detail": "Location and specifics", "labor": 0.00, "materials": 0.00, "total": 0.00}}
  ],
  "timeline": "estimated completion time",
  "notes": "important observations, concerns, or recommendations",
  "estimate_total": 0.00,
  "estimate_range": "$X,XXX - $X,XXX",
  "areas_identified": ["list of areas/rooms identified in video"]
}}
Base pricing on current US contractor rates for the {property_address} region.
Be thorough - price every single item you observe needs attention."""

        if contractor_notes:
            prompt += f"\n\nCONTRACTOR NOTES (spoken during walkthrough):\n{contractor_notes}\n\nIncorporate these notes into your estimate."

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        print(f"WALKTHROUGH | Reading video for Gemini...")

        with open(tmp_path, "rb") as vf:
            video_bytes = vf.read()

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(
                    data=video_bytes,
                    mime_type="video/mp4" if suffix == ".mp4" else "video/webm" if suffix == ".webm" else "video/quicktime"
                ),
                prompt
            ]
        )
        raw = response.text.strip()
        print(f"WALKTHROUGH | Gemini analysis complete | {raw[:200]}")

        # Parse JSON
        import json as json_lib
        import re as re_lib
        json_match = re_lib.search(r'\{.*\}', raw, re_lib.DOTALL)
        if not json_match:
            raise ValueError("No JSON in Gemini response")
        estimate_data = json_lib.loads(json_match.group(0))
        print(f"WALKTHROUGH | Estimate generated | ${estimate_data.get('estimate_total', 0)}")

        # Step 5 — Generate PDF
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.lib.units import inch
        from datetime import datetime

        os.makedirs("/tmp/walkthroughs", exist_ok=True)
        safe_customer = (customer_name or "estimate").replace(" ", "_")[:20]
        pdf_path = f"/tmp/walkthroughs/{safe_customer}_walkthrough_{int(time.time())}.pdf"

        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=letter,
            rightMargin=0.6*inch,
            leftMargin=0.6*inch,
            topMargin=0.6*inch,
            bottomMargin=0.6*inch
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("T", parent=styles["Title"], fontSize=22,
            textColor=colors.HexColor("#111111"), spaceAfter=4)
        brand_style = ParagraphStyle("B", parent=styles["Normal"], fontSize=10,
            textColor=colors.HexColor("#22c55e"), spaceAfter=16)
        heading_style = ParagraphStyle("H", parent=styles["Heading2"], fontSize=13,
            textColor=colors.HexColor("#111111"), spaceBefore=14, spaceAfter=6)
        normal_style = ParagraphStyle("N", parent=styles["Normal"], fontSize=10, leading=15)
        small_style = ParagraphStyle("S", parent=styles["Normal"], fontSize=8.5,
            textColor=colors.gray, leading=12)

        story = []

        story.append(Paragraph(business_name, title_style))
        story.append(Paragraph("Professional Estimate", brand_style))
        story.append(Paragraph("Video Walkthrough Estimate", heading_style))

        # Info table
        info_data = [
            ["Customer", customer_name or "—"],
            ["Property", property_address or "—"],
            ["Project Type", project_type],
            ["Date Prepared", datetime.now().strftime("%B %d, %Y")],
            ["Estimate Range", estimate_data.get("estimate_range", "TBD")],
        ]
        info_table = Table(info_data, colWidths=[1.8*inch, 4.7*inch])
        info_table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#f3f4f6")),
            ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 9.5),
            ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#dddddd")),
            ("PADDING", (0,0), (-1,-1), 8),
        ]))
        story.append(info_table)
        story.append(Spacer(1, 12))

        # Areas identified
        areas = estimate_data.get("areas_identified", [])
        if areas:
            story.append(Paragraph("Areas Identified in Walkthrough", heading_style))
            areas_text = " · ".join(areas)
            story.append(Paragraph(areas_text, normal_style))

        # Project Summary
        story.append(Paragraph("Project Summary", heading_style))
        story.append(Paragraph(estimate_data.get("project_summary", ""), normal_style))

        # Scope of Work
        story.append(Paragraph("Scope of Work", heading_style))
        story.append(Paragraph(estimate_data.get("scope_of_work", ""), normal_style))

        # Line Items
        story.append(Paragraph("Estimate Breakdown", heading_style))
        line_items = estimate_data.get("line_items", [])
        if line_items:
            li_data = [["Description", "Labor", "Materials", "Total"]]
            for item in line_items:
                li_data.append([
                    Paragraph(f"<b>{item.get('description', '')}</b><br/>{item.get('detail', '')}", normal_style),
                    f"${float(item.get('labor', 0)):,.2f}",
                    f"${float(item.get('materials', 0)):,.2f}",
                    f"${float(item.get('total', 0)):,.2f}",
                ])
            total = float(estimate_data.get("estimate_total", 0))
            li_data.append(["TOTAL ESTIMATE", "", "", f"${total:,.2f}"])

            li_table = Table(li_data, colWidths=[3.8*inch, 1.0*inch, 1.0*inch, 0.9*inch])
            li_table.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1A4D2E")),
                ("TEXTCOLOR", (0,0), (-1,0), colors.white),
                ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTSIZE", (0,0), (-1,-1), 9),
                ("GRID", (0,0), (-1,-2), 0.5, colors.HexColor("#dddddd")),
                ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#0f2a1a")),
                ("TEXTCOLOR", (0,-1), (-1,-1), colors.HexColor("#22c55e")),
                ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
                ("FONTSIZE", (0,-1), (-1,-1), 11),
                ("PADDING", (0,0), (-1,-1), 8),
                ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.white, colors.HexColor("#f9fafb")]),
                ("VALIGN", (0,0), (-1,-1), "TOP"),
            ]))
            story.append(li_table)

        story.append(Spacer(1, 12))

        if estimate_data.get("timeline"):
            story.append(Paragraph("Timeline", heading_style))
            story.append(Paragraph(estimate_data.get("timeline"), normal_style))

        if estimate_data.get("notes"):
            story.append(Paragraph("Important Notes", heading_style))
            story.append(Paragraph(estimate_data.get("notes"), normal_style))


        story.append(Spacer(1, 16))
        story.append(Paragraph(
            f"{business_name} · {contractor.get('Notify SMS', '')} · {notify_email}",
            small_style
        ))

        doc.build(story)
        print(f"WALKTHROUGH | PDF generated | {pdf_path}")

        # Step 6 — Save to Airtable
        import requests as req
        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        at_headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }
        at_resp = req.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblAlqryj3Vhw5JaK",
            headers=at_headers,
            json={"fields": {
                "fldSvpPnDN4Ge4fTl": f"{customer_name} — {project_type}",
                "fldB2KMaHLg60sfAP": customer_name,
                "fldcCpcv3WRaSqBzT": property_address,
                "flddiv0HZH3kxaJWP": project_type,
                "fldLtbD7qbheLVbnG": estimate_data.get("project_summary", "") + "\n\n" + estimate_data.get("scope_of_work", ""),
                "fldErOKh8ZVxDAnbn": json_lib.dumps(estimate_data, indent=2),
                "fldo5WzLTsgo2QTTD": float(estimate_data.get("estimate_total", 0)),
                "fldyLFd8XVT6iAcgh": "Draft",
                "fldknADF18Bq2mQdH": twilio_number,
                "fldltvDOf7ZTSPMg1": video_url,
            }}
        )
        record_id = at_resp.json().get("id", "")
        print(f"WALKTHROUGH | Airtable saved | {record_id}")

        # Step 7 — Email PDF to contractor
        if notify_email:
            email_body = (
                f"🎥 Video Walkthrough Estimate — {customer_name}\n\n"
                f"Property: {property_address}\n"
                f"Project Type: {project_type}\n"
                f"Areas Identified: {', '.join(areas[:5])}\n"
                f"Estimate Range: {estimate_data.get('estimate_range', 'TBD')}\n"
                f"Total: ${float(estimate_data.get('estimate_total', 0)):,.2f}\n\n"
                f"Summary: {estimate_data.get('project_summary', '')}\n\n"
                f"Timeline: {estimate_data.get('timeline', '')}\n\n"
                f"PDF attached — review, adjust if needed, and send to customer.\n"
                f"Video recording: {video_url}"
            )
            send_email(
                subject=f"🎥 Video Estimate — {customer_name} | {estimate_data.get('estimate_range', '')}",
                body=email_body,
                to_email=notify_email,
                attachment_path=pdf_path,
            )
            print(f"WALKTHROUGH | Email sent | {notify_email}")

        # Cleanup temp file
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "estimate_range": estimate_data.get("estimate_range", ""),
            "estimate_total": estimate_data.get("estimate_total", 0),
            "project_summary": estimate_data.get("project_summary", ""),
            "timeline": estimate_data.get("timeline", ""),
            "areas_identified": areas,
            "record_id": record_id,
            "video_url": video_url,
        })

    except Exception as e:
        print(f"WALKTHROUGH ERROR | {type(e).__name__} | {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/dashboard/recurring")
@dashboard_auth_required
def dashboard_recurring():
    """Returns active recurring customers for this contractor."""
    try:
        import requests as req
        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

        twilio_number = request.twilio_number

        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblxGfrifBiGRk80M"
        resp = req.get(url, headers=headers, params={
            "filterByFormula": f"AND({{Active}} = TRUE(), {{Twilio Number}} = '{twilio_number}')"
        })
        records = resp.json().get("records", [])
        customers = []
        for r in records:
            f = r.get("fields", {})
            customers.append({
                "record_id": r.get("id", ""),
                "name": f.get("Customer Name", ""),
                "email": f.get("Email", ""),
                "phone": f.get("Phone", ""),
                "service": f.get("Service Description", ""),
                "amount": f.get("Monthly Amount", 0),
                "payment_method": f.get("Payment Method", ""),
                "notes": f.get("Notes", ""),
            })
        return jsonify({"ok": True, "customers": customers})
    except Exception as e:
        print(f"RECURRING CUSTOMERS ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/dashboard/regular-clients")
@dashboard_auth_required
def dashboard_regular_clients():
    """Returns active regular clients with upcoming appointments for this contractor."""
    try:
        import requests as req
        from zoneinfo import ZoneInfo
        from datetime import datetime

        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)

        twilio_number = request.twilio_number

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl3LAJzXa6Vsexry"

        resp = req.get(url, headers=headers, params={
            "filterByFormula": f"AND({{Active}} = TRUE(), {{Contractor}} = '{twilio_number}')"
        })
        records = resp.json().get("records", [])

        clients = []
        for r in records:
            f = r.get("fields", {})
            next_appt = f.get("Next Appointment", "")
            next_appt_display = ""
            days_until = None
            try:
                if next_appt:
                    dt = datetime.fromisoformat(next_appt.replace("Z", "+00:00"))
                    dt_eastern = dt.astimezone(eastern)
                    next_appt_display = dt_eastern.strftime("%a %b %-d at %-I:%M %p")
                    days_until = (dt_eastern.date() - now.date()).days
            except Exception:
                pass

            clients.append({
                "record_id": r.get("id", ""),
                "name": f.get("Client Name", ""),
                "phone": f.get("Phone", ""),
                "email": f.get("Email", ""),
                "address": f.get("Service Address", ""),
                "service": f.get("Service Description", ""),
                "frequency_days": f.get("Frequency Days", 0),
                "preferred_time": f.get("Preferred Time", "09:00"),
                "next_appointment": next_appt_display,
                "next_appointment_raw": next_appt,
                "days_until": days_until,
                "notes": f.get("Notes", ""),
            })

        clients.sort(key=lambda x: x.get("next_appointment_raw") or "9999")

        return jsonify({"ok": True, "clients": clients})

    except Exception as e:
        print(f"REGULAR CLIENTS ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/book-regular-client", methods=["POST"])
@dashboard_auth_required
def dashboard_book_regular_client():
    """
    Books the next appointment for a regular client.
    Creates Airtable lead, adds to Google Calendar, SMS confirmation.
    Updates Next Appointment in Regular Clients table.
    """
    try:
        from app.app.cal_service import create_google_calendar_event
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta

        data = request.get_json(silent=True) or {}
        record_id = data.get("record_id", "")
        customer_name = data.get("customer_name", "")
        customer_phone = data.get("customer_phone", "")
        service_address = data.get("service_address", "")
        job_description = data.get("job_description", "")
        appointment_date = data.get("appointment_date", "")
        appointment_time = data.get("appointment_time", "09:00")
        frequency_days = int(data.get("frequency_days", 14))
        twilio_number = request.twilio_number

        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        business_name = contractor.get("Business Name", "your contractor")

        eastern = ZoneInfo("America/New_York")
        dt_str = f"{appointment_date}T{appointment_time}:00"
        dt_start = datetime.fromisoformat(dt_str).replace(tzinfo=eastern)
        dt_end = dt_start + timedelta(hours=1)
        formatted_display = dt_start.strftime("%A, %B %-d at %-I:%M %p")

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        # Create lead in Airtable
        leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"
        airtable_resp = requests.post(
            leads_url,
            headers=headers,
            json={"fields": {
                "fldBktJv26lpFCZjg": customer_name,
                "fldfSFcMA4V5SLfjo": customer_phone,
                "fldo9GtQBLObByZs5": service_address,
                "fldxNwWbbMWF4cT47": job_description,
                "fldkTOouWuLx6JHly": formatted_display,
                "fldHL2tJs2egGKuI9": "Booked",
                "fldbtGSgcOrHHe6pO": "STANDARD",
                "fldn2cCGDP4WimUMh": "Regular Client",
                "fldAgsSlZfOLFCBrJ": twilio_number,
                "fldeKVCUvdkVswD4V": f"REGULAR-{twilio_number}-{int(dt_start.timestamp())}",
                "fldIfaFlPA4AyMntY": dt_start.isoformat(),
            }}
        )
        print(f"REGULAR CLIENT BOOKED | {customer_name} | {formatted_display}")

        # Add to Google Calendar
        cal_result = create_google_calendar_event(
            contractor=contractor,
            summary=f"{business_name} — {job_description} ({customer_name})",
            start_time=dt_start.isoformat(),
            end_time=dt_end.isoformat(),
            description=f"Regular client — every {frequency_days} days\nPhone: {customer_phone}\nAddress: {service_address}",
            location=service_address,
        )
        print(f"REGULAR CLIENT CALENDAR | {cal_result.get('ok')} | {customer_name}")

        # SMS confirmation to customer
        first_name = customer_name.split()[0] if customer_name else "there"
        msg = (
            f"Hi {first_name}! Your appointment with {business_name} is confirmed for "
            f"{formatted_display}. "
            f"Reply CANCEL APPOINTMENT to cancel."
        )
        send_fallback_sms(to_number=customer_phone, body=msg)

        # Update Next Appointment and Last Completed in Regular Clients table
        next_appt_dt = dt_start + timedelta(days=frequency_days)
        regular_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl3LAJzXa6Vsexry"
        requests.patch(
            f"{regular_url}/{record_id}",
            headers=headers,
            json={"fields": {
                "fldrQYykMd28OcYUI": next_appt_dt.isoformat(),  # Next Appointment
            }}
        )
        print(f"REGULAR CLIENT NEXT APPT | {customer_name} | {next_appt_dt.strftime('%Y-%m-%d')}")

        return jsonify({
            "ok": True,
            "appointment": formatted_display,
            "next_appointment": next_appt_dt.strftime("%B %-d")
        })

    except Exception as e:
        print(f"BOOK REGULAR CLIENT ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/complete-regular-client", methods=["POST"])
@dashboard_auth_required
def dashboard_complete_regular_client():
    """
    Marks regular client visit complete.
    Updates Last Completed and calculates Next Appointment using the
    client's Preferred Time, not the time the Done button was tapped.
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        data = request.get_json(silent=True) or {}
        record_id = data.get("record_id", "")
        frequency_days = int(data.get("frequency_days", 14))
        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }
        regular_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl3LAJzXa6Vsexry"

        # Look up the client's actual Preferred Time before calculating next appointment
        record_resp = requests.get(f"{regular_url}/{record_id}", headers=headers)
        preferred_time_str = (record_resp.json().get("fields", {}).get("Preferred Time") or "9:00 AM").strip()

        hour, minute = 9, 0
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                parsed_time = datetime.strptime(preferred_time_str, fmt)
                hour, minute = parsed_time.hour, parsed_time.minute
                break
            except Exception:
                continue

        next_appt_date = (now + timedelta(days=frequency_days)).date()
        next_appt = datetime(
            next_appt_date.year, next_appt_date.month, next_appt_date.day,
            hour, minute, tzinfo=eastern
        )

        requests.patch(
            f"{regular_url}/{record_id}",
            headers=headers,
            json={"fields": {
                "fldad0GDluY6VLeAX": now.isoformat(),
                "fldrQYykMd28OcYUI": next_appt.isoformat(),
            }}
        )
        print(f"REGULAR CLIENT COMPLETE | {record_id} | Next: {next_appt.isoformat()}")
        return jsonify({
            "ok": True,
            "next_appointment": next_appt.strftime("%B %-d at %-I:%M %p")
        })
    except Exception as e:
        print(f"COMPLETE REGULAR CLIENT ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/dashboard/revenue")
@dashboard_auth_required
def dashboard_revenue():
    """Returns revenue summary stats for the contractor."""
    try:
        import requests as req
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta

        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)

        # Date ranges
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        month_start = now.strftime("%Y-%m-01")
        year_start = now.strftime("%Y-01-01")

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
        payments_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments"
        contractor_record_id = request.contractor_id

        # Fetch ALL payment records — filter by contractor in Python
        all_records = []
        params = {"pageSize": 100}
        while True:
            resp = req.get(payments_url, headers=headers, params=params)
            data = resp.json()
            all_records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break
            params["offset"] = offset

        print(f"REVENUE | Total records fetched: {len(all_records)} | contractor: {contractor_record_id}")

        # Calculate stats
        week_revenue = 0
        month_revenue = 0
        year_revenue = 0
        outstanding = 0
        jobs_this_month = 0

        for r in all_records:
            f = r.get("fields", {})
            amount = float(f.get("Amount", 0) or 0)

            # Check contractor match
            contractor_links = f.get("Contractor", [])
            contractor_ids = [
                c.get("id", "") if isinstance(c, dict) else str(c)
                for c in contractor_links
            ]
            if contractor_record_id and contractor_record_id not in contractor_ids:
                continue

            # Get status name from singleSelect object
            status_field = f.get("Payment Status", {})
            status = status_field.get("name", "") if isinstance(status_field, dict) else str(status_field)

            payment_date = f.get("Payment Date", "") or ""

            if status in ["Paid", "Invoiced"]:
                if payment_date >= week_start:
                    week_revenue += amount
                if payment_date >= month_start:
                    month_revenue += amount
                    jobs_this_month += 1
                if payment_date >= year_start:
                    year_revenue += amount

            if status == "Unpaid":
                outstanding += amount

        print(f"REVENUE | Week: ${week_revenue} | Month: ${month_revenue} | Year: ${year_revenue} | Outstanding: ${outstanding}")

        return jsonify({
            "ok": True,
            "week_revenue": round(week_revenue, 2),
            "month_revenue": round(month_revenue, 2),
            "year_revenue": round(year_revenue, 2),
            "outstanding": round(outstanding, 2),
            "jobs_this_month": jobs_this_month,
            "month_name": now.strftime("%B")
        })

    except Exception as e:
        print(f"REVENUE ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/add-contractor", methods=["POST"])
@dashboard_auth_required
def dashboard_add_contractor():
    """
    Onboards a new contractor — creates Airtable record,
    hashes password, sends SMS with login link and Google Calendar setup link.
    """
    try:
        import hashlib
        import requests as req

        data = request.get_json(silent=True) or {}
        contractor_name = data.get("contractor_name", "").strip()
        business_name = data.get("business_name", "").strip()
        phone = data.get("phone", "").strip()
        email = data.get("email", "").strip()
        twilio_number = data.get("twilio_number", "").strip()
        password = data.get("password", "").strip()

        if not contractor_name or not phone or not twilio_number or not password:
            return jsonify({"ok": False, "error": "Name, phone, Twilio number and password required"}), 400

        # Hash the password
        password_hash = hashlib.sha256(password.encode()).hexdigest()

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }
        contractors_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CONTRACTORS_TABLE}"

        # Create contractor record in Airtable
        resp = req.post(
            contractors_url,
            headers=headers,
            json={"fields": {
                "Business Name": business_name or contractor_name,
                "Notify SMS": phone,
                "Notify Email": email,
                "Twilio Number": twilio_number,
                "Dashboard Password": password_hash,
                "Active": True,
            }}
        )

        if resp.status_code not in [200, 201]:
            print(f"ADD CONTRACTOR | Airtable error | {resp.text}")
            return jsonify({"ok": False, "error": "Failed to create contractor in Airtable"}), 500
        

        if resp.status_code not in [200, 201]:
            print(f"ADD CONTRACTOR | Airtable error | {resp.text}")
            return jsonify({"ok": False, "error": "Failed to create contractor in Airtable"}), 500

        record_id = resp.json().get("id", "")
        print(f"ADD CONTRACTOR | Created | {record_id} | {business_name}")

        # Send SMS to new contractor with login and onboarding links
        base_url = os.environ.get("APP_BASE_URL", "https://mme-ai-bot.onrender.com")
        onboard_link = f"{base_url}/onboard/{record_id}"
        dashboard_link = f"{base_url}/dashboard/login"

        welcome_msg = (
            f"Welcome to CrewCachePro, {contractor_name}!\n\n"
            f"Your dashboard login:\n"
            f"{dashboard_link}\n"
            f"Phone: {twilio_number}\n"
            f"Password: {password}\n\n"
            f"Connect Google Calendar here:\n"
            f"{onboard_link}\n\n"
            f"Questions? Reply to this message."
        )
        send_fallback_sms(to_number=phone, body=welcome_msg)
        print(f"ADD CONTRACTOR | Welcome SMS sent | {phone}")

        # Email welcome if email provided
        if email:
            send_email(
                subject=f"Welcome to CrewCachePro - Your Login Details",
                body=welcome_msg,
                to_email=email,
            )
            print(f"ADD CONTRACTOR | Welcome email sent | {email}")

        return jsonify({
            "ok": True,
            "record_id": record_id,
            "onboard_link": onboard_link,
            "message": f"Contractor {contractor_name} added! Login details sent via SMS."
        })

    except Exception as e:
        print(f"ADD CONTRACTOR ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/dashboard/action/connect-stripe", methods=["POST"])
@dashboard_auth_required
def dashboard_connect_stripe():
    try:
        from app.app.stripe_service import create_connect_account, create_account_onboarding_link
        twilio_number = request.twilio_number
        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        existing_account_id = (contractor.get("Stripe Account ID") or "").strip()

        if not existing_account_id:
            result = create_connect_account(
                contractor_record_id=request.contractor_id,
                email=contractor.get("Notify Email", ""),
                business_name=contractor.get("Business Name", "Contractor"),
            )
            if not result.get("ok"):
                return jsonify({"ok": False, "error": result.get("error")}), 500

            account_id = result["account_id"]
            AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
            AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
            requests.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{os.environ.get('AIRTABLE_CONTRACTORS_TABLE')}/{request.contractor_id}",
                headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": {"Stripe Account ID": account_id}}
            )
        else:
            account_id = existing_account_id

        link_result = create_account_onboarding_link(account_id)
        if not link_result.get("ok"):
            return jsonify({"ok": False, "error": link_result.get("error")}), 500

        return jsonify({"ok": True, "url": link_result["url"]})
    except Exception as e:
        print(f"CONNECT STRIPE ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/send-recurring-invoice", methods=["POST"])
@dashboard_auth_required
def dashboard_send_recurring_invoice():
    """
    Sends a Stripe invoice to a recurring commercial client.
    Replaces QuickBooks invoicing entirely.
    """
    try:
        data = request.get_json(silent=True) or {}
        customer_name = (data.get("customer_name") or "").strip()
        customer_email = (data.get("customer_email") or "").strip()
        customer_phone = (data.get("customer_phone") or "").strip()
        amount = float(data.get("amount") or 0)
        service = (data.get("service") or "Lawn Service").strip()
        payment_method = (data.get("payment_method") or "Stripe").strip()
        twilio_number = request.twilio_number
        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        business_name = contractor.get("Business Name", "Your Contractor")

        if not customer_email:
            return jsonify({"ok": False, "error": "Customer email required for invoice"}), 400
        if not amount or amount <= 0:
            return jsonify({"ok": False, "error": "Valid amount required"}), 400

        from app.app.stripe_service import create_stripe_invoice
        stripe_account_id = (contractor.get("Stripe Account ID") or "").strip()
        stripe_charges_enabled = bool(contractor.get("Stripe Charges Enabled"))

        result = create_stripe_invoice(
            customer_email=customer_email,
            customer_name=customer_name,
            amount=amount,
            service_description=service,
            business_name=business_name,
            due_days=30,
            contractor_stripe_account_id=stripe_account_id if stripe_charges_enabled else "",
            application_fee_percent=1.0,
        )

        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error")}), 500

        # Create payment record in Airtable
        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        at_headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }
        today = datetime.now().strftime("%Y-%m-%d")
        requests.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments",
            headers=at_headers,
            json={"fields": {
                "fldAZ5Qr0NCU11J0A": customer_name,
                "fld8bUzdzFeeXLrlD": customer_phone,
                "fld596bZM5ZCI7ga8": amount,
                "fldeROEzoyhWKJ36y": service,
                "fldWg6gGv6dKFb853": "Unpaid",
                "fldUFO1PfTeiLA3UR": "Stripe",
                "fldYNu0gpLuiCsF6Z": today,
                "fldxdSy7mICyTo50P": [request.contractor_id],
                "fldngufZKDk8G0bZ2": result.get("invoice_number", ""),
            }}
        )

        print(f"RECURRING INVOICE | {customer_name} | ${amount} | {result.get('invoice_number')}")
        return jsonify({
            "ok": True,
            "message": f"Invoice sent to {customer_email} for ${amount:,.2f}. PDF delivered automatically.",
            "invoice_number": result.get("invoice_number", ""),
            "invoice_url": result.get("invoice_url", ""),
        })

    except Exception as e:
        print(f"SEND RECURRING INVOICE ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/mark-paid", methods=["POST"])
@dashboard_auth_required
def dashboard_mark_paid():
    """Marks an invoice as paid in Airtable."""
    try:
        data = request.get_json(silent=True) or {}
        record_id = data.get("record_id", "")

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        payments_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Payments"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        response = requests.patch(
            f"{payments_url}/{record_id}",
            headers=headers,
            json={"fields": {"Payment Status": "Paid"}}
        )

        if response.status_code == 200:
            print(f"DASHBOARD | Invoice marked paid | {record_id}")
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": response.text}), 500

    except Exception as e:
        print(f"DASHBOARD MARK PAID ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/send-reminder", methods=["POST"])
@dashboard_auth_required
def dashboard_send_reminder():
    """Manually sends a payment reminder SMS to customer."""
    try:
        data = request.get_json(silent=True) or {}
        customer_name = data.get("customer_name", "there")
        customer_phone = data.get("customer_phone", "")
        amount = data.get("amount", 0)
        job_type = data.get("job_type", "services rendered")
        twilio_number = request.twilio_number

        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        business_name = contractor.get("Business Name", "your contractor")
        first_name = customer_name.split()[0] if customer_name else "there"

        msg = (
            f"Hi {first_name}! A friendly reminder from {business_name} — "
            f"your balance of ${amount} for {job_type} is still outstanding. "
            f"Please complete your payment at your earliest convenience."
        )

        send_fallback_sms(to_number=customer_phone, body=msg)
        print(f"DASHBOARD | Payment reminder sent | {customer_name} | {customer_phone}")
        return jsonify({"ok": True})

    except Exception as e:
        print(f"DASHBOARD SEND REMINDER ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/send-booking-link", methods=["POST"])
@dashboard_auth_required
def dashboard_send_booking_link():
    """Sends Cal.com booking link to a lead."""
    try:
        data = request.get_json(silent=True) or {}
        customer_name = data.get("customer_name", "")
        customer_phone = data.get("customer_phone", "")
        job_type = data.get("job_type", "")
        address = data.get("address", "")
        twilio_number = request.twilio_number

        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        business_name = contractor.get("Business Name", "your contractor")
        cal_booking_url = contractor.get("CAL Booking URL", "")
        first_name = customer_name.split()[0] if customer_name else "there"

        import urllib.parse
        params = urllib.parse.urlencode({
            "name": customer_name,
            "attendeePhoneNumber": customer_phone,
            "service_address": address,
            "job_description": job_type,
        })
        booking_link = f"{cal_booking_url}?{params}" if cal_booking_url else ""

        msg = (
            f"Hi {first_name}! This is {business_name}. "
            f"Click here to book your appointment: {booking_link}"
        )

        send_fallback_sms(to_number=customer_phone, body=msg)
        print(f"DASHBOARD | Booking link sent | {customer_name} | {customer_phone}")
        return jsonify({"ok": True})

    except Exception as e:
        print(f"DASHBOARD SEND BOOKING LINK ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dashboard/action/mark-contacted", methods=["POST"])
@dashboard_auth_required
def dashboard_mark_contacted():
    """Marks a lead as contacted in Airtable."""
    try:
        data = request.get_json(silent=True) or {}
        record_id = data.get("record_id", "")

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        response = requests.patch(
            f"{leads_url}/{record_id}",
            headers=headers,
            json={"fields": {"Lead Status": "Contacted"}}
        )

        if response.status_code == 200:
            print(f"DASHBOARD | Lead marked contacted | {record_id}")
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": response.text}), 500

    except Exception as e:
        print(f"DASHBOARD MARK CONTACTED ERROR | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/dashboard/add-job", methods=["POST"])
@dashboard_auth_required
def dashboard_add_job():
    """
    Creates a new job booking from the dashboard.
    - Creates lead in Airtable
    - Adds to Google Calendar
    - Sends confirmation SMS to customer
    """
    try:
        data = request.get_json(silent=True) or {}
        customer_name = data.get("customer_name", "").strip()
        customer_phone = data.get("customer_phone", "").strip()
        service_address = data.get("service_address", "").strip()
        job_description = data.get("job_description", "").strip()
        appointment_date = data.get("appointment_date", "").strip()
        appointment_time = data.get("appointment_time", "").strip()

        if not customer_name or not customer_phone or not appointment_date:
            return jsonify({"ok": False, "error": "Name, phone and date are required"}), 400

        twilio_number = request.twilio_number
        contractor = get_contractor_by_twilio_number(twilio_number) or {}
        business_name = contractor.get("Business Name", "your contractor")

        # Build ISO datetime strings for Google Calendar
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta

        eastern = ZoneInfo("America/New_York")

        # Parse date and time
        time_str = appointment_time or "09:00"
        dt_str = f"{appointment_date}T{time_str}:00"
        dt_start = datetime.fromisoformat(dt_str).replace(tzinfo=eastern)
        dt_end = dt_start + timedelta(hours=1)

        start_iso = dt_start.isoformat()
        end_iso = dt_end.isoformat()
        formatted_display = dt_start.strftime("%A, %B %-d at %-I:%M %p")

        # 1 — Create Airtable lead record
        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        leads_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl6YL7BYY2vawIF1"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }

        airtable_resp = requests.post(
            leads_url,
            headers=headers,
            json={"fields": {
                "Client Name": customer_name,
                "Call Back Number": customer_phone,
                "Service Address": service_address,
                "Job Description": job_description,
                "Appointment Date and Time": dt_start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "Lead Status": "Booked",
                "Priority": "STANDARD",
                "Source": "Manual Entry",
                "Twilio Number": twilio_number,
                "Call SID": f"MANUAL-{twilio_number}-{int(dt_start.timestamp())}",
                "Appointment Requested": formatted_display,
            }}
        )

        if airtable_resp.status_code not in [200, 201]:
            print(f"DASHBOARD ADD JOB | Airtable error | {airtable_resp.text}")
            return jsonify({"ok": False, "error": "Failed to create Airtable record"}), 500

        lead_record_id = airtable_resp.json().get("id", "")
        print(f"DASHBOARD ADD JOB | Lead created | {lead_record_id} | {customer_name}")

        # After lead is created in Airtable
        try:  
            send_push_notification(
                twilio_number=to_number,
                title="🔔 New Lead!",
                message=f"{caller_name} — {job_description[:60]}",
                url="/dashboard"
            )
        except Exception as e:
            print(f"PUSH NOTIFICATION ERROR | {e}")

        # 2 — Add to Google Calendar
        from app.app.cal_service import create_google_calendar_event

        cal_result = create_google_calendar_event(
            contractor=contractor,
            summary=f"{business_name} — {job_description or 'Service'} ({customer_name})",
            start_time=start_iso,
            end_time=end_iso,
            description=(
                f"Customer: {customer_name}\n"
                f"Phone: {customer_phone}\n"
                f"Address: {service_address}\n"
                f"Job: {job_description}\n"
                f"Booked via CrewCachePro Dashboard"
            ),
            location=service_address,
        )

        if cal_result.get("ok"):
            print(f"DASHBOARD ADD JOB | Google Calendar event created | {cal_result.get('event_id')}")
        else:
            print(f"DASHBOARD ADD JOB | Google Calendar failed | {cal_result.get('error')}")

        # 3 — Send confirmation SMS to customer
        first_name = customer_name.split()[0] if customer_name else "there"
        msg = (
            f"Hi {first_name}! Your appointment with {business_name} is confirmed for "
            f"{formatted_display} at {service_address}. "
            f"Reply CANCEL APPOINTMENT to cancel or RESCHEDULE to reschedule."
        )
        send_fallback_sms(to_number=customer_phone, body=msg)
        print(f"DASHBOARD ADD JOB | Confirmation SMS sent | {customer_phone}")

        return jsonify({
            "ok": True,
            "lead_id": lead_record_id,
            "calendar": cal_result.get("ok", False),
            "appointment": formatted_display
        })

    except Exception as e:
        print(f"DASHBOARD ADD JOB ERROR | {type(e).__name__} | {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


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

    # ── Subscription tier check ────────────────────────────────────
    from app.app.subscription_service import has_feature, get_upgrade_message
    if contractor and not has_feature(contractor, "voice_intake"):
        vr2 = VoiceResponse()
        vr2.say(get_upgrade_message("voice_intake"))
        vr2.hangup()
        return Response(str(vr2), mimetype="text/xml")

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
    # REMOVE the casing transformation — use the ID as-is
    session["oauth_contractor_key"] = contractor_id
    session.permanent = True
    print("ONBOARD | contractor_id stored in session:", contractor_id)
    return redirect("/setup")


@app.route("/setup")
def setup():
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
    print(f"GOOGLE CALLBACK | contractor_key: {contractor_key} | state: {state}")

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

@app.route("/send-seasonal-blast", methods=["POST"])
def send_seasonal_blast():
    """
    Sends a seasonal campaign SMS blast to all active Regular Clients
    for the given contractor. Logs every send to Message Log.
    """
    try:
        import requests as req
        from datetime import datetime
        from zoneinfo import ZoneInfo

        data = request.get_json(force=True)
        twilio_number = (data.get("twilio_number") or "").strip()
        campaign_name = (data.get("campaign_name") or "").strip()

        if not twilio_number or not campaign_name:
            return jsonify({"ok": False, "error": "twilio_number and campaign_name required"}), 400

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}

        # Step 1 — Find the campaign
        campaigns_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblSrBFioDKG0uKIU"
        camp_resp = req.get(campaigns_url, headers=headers, params={
            "filterByFormula": (
                f"AND({{Campaign Name}} = '{campaign_name}', "
                f"{{Twilio Number}} = '{twilio_number}', "
                f"{{Active}} = TRUE())"
            )
        })
        camp_records = camp_resp.json().get("records", [])
        if not camp_records:
            return jsonify({"ok": False, "error": "Active campaign not found"}), 404

        campaign = camp_records[0].get("fields", {})
        message_template = campaign.get("Message Body", "")
        message_type = campaign.get("Message Type", "Promo")

        if not message_template:
            return jsonify({"ok": False, "error": "Campaign has no Message Body"}), 400

        # Step 2 — Get active Regular Clients for this contractor
        regular_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tbl3LAJzXa6Vsexry"
        clients_resp = req.get(regular_url, headers=headers, params={
            "filterByFormula": (
                f"AND({{Active}} = TRUE(), {{Twilio Number}} = '{twilio_number}')"
            )
        })
        clients = clients_resp.json().get("records", [])

        eastern = ZoneInfo("America/New_York")
        sent_count = 0
        failed_count = 0
        log_table_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblnX5APzYjgXtO6t"

        for c in clients:
            f = c.get("fields", {})
            client_name = f.get("Client Name", "")
            client_phone = f.get("Phone", "")

            if not client_phone:
                continue

            first_name = client_name.split()[0] if client_name else "there"
            personalized_msg = message_template.replace("{name}", first_name)

            status = "Sent"
            error_msg = ""
            twilio_sid = ""

            try:
                from twilio.rest import Client
                twilio_client = Client(
                    os.environ.get("TWILIO_ACCOUNT_SID"),
                    os.environ.get("TWILIO_AUTH_TOKEN")
                )
                msg = twilio_client.messages.create(
                    body=personalized_msg,
                    from_=twilio_number,
                    to=client_phone
                )
                twilio_sid = msg.sid
                sent_count += 1
            except Exception as e:
                status = "Failed"
                error_msg = str(e)
                failed_count += 1

            # Log every send
            try:
                req.post(log_table_url, headers=headers, json={"fields": {
                    "Sent At": datetime.now(eastern).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "Twilio Number": twilio_number,
                    "Client Name": client_name,
                    "Client Phone": client_phone,
                    "Message Type": message_type,
                    "Campaign Name": campaign_name,
                    "Message Body": personalized_msg,
                    "Status": status,
                    "Error": error_msg,
                    "Twilio SID": twilio_sid,
                }})
            except Exception as e:
                print(f"MESSAGE LOG ERROR | {e}")

        # Step 3 — Update Send Count on campaign
        try:
            current_send_count = int(campaign.get("Send Count", 0) or 0)
            req.patch(
                f"{campaigns_url}/{camp_records[0].get('id')}",
                headers=headers,
                json={"fields": {"Send Count": current_send_count + sent_count}}
            )
        except Exception as e:
            print(f"SEND COUNT UPDATE ERROR | {e}")

        return jsonify({
            "ok": True,
            "campaign": campaign_name,
            "sent": sent_count,
            "failed": failed_count,
            "total_clients": len(clients)
        })

    except Exception as e:
        print(f"SEASONAL BLAST ERROR | {type(e).__name__} | {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/message-log", methods=["GET"])
def get_message_log():
    """Returns recent message log entries for a contractor."""
    try:
        import requests as req
        twilio_number = request.args.get("twilio_number", "").strip()

        AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
        AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

        log_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/tblnX5APzYjgXtO6t"
        params = {"sort[0][field]": "Sent At", "sort[0][direction]": "desc"}
        if twilio_number:
            params["filterByFormula"] = f"{{Twilio Number}} = '{twilio_number}'"

        resp = req.get(log_url, headers=headers, params=params)
        records = resp.json().get("records", [])

        results = [r.get("fields", {}) for r in records]
        return jsonify({"ok": True, "messages": results})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
