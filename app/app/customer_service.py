# -----------------------------------------------
# FILE: app/app/customer_service.py
# What it does: Handles post-booking customer
# questions via SMS — appointment status, reschedule,
# cancellation, job info — fully automated
# -----------------------------------------------

import os
import requests
import anthropic
from flask import Response

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
LEADS_TABLE_ID = "tbl6YL7BYY2vawIF1"

LEADS_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{LEADS_TABLE_ID}"
HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}


def lookup_lead_by_phone(from_number: str, twilio_number: str) -> dict | None:
    """
    Looks up an existing lead by customer phone number and contractor Twilio number.
    Returns the lead fields if found, None if new customer.
    Only returns leads that are Booked or Contacted — not brand new leads mid-intake.
    """
    try:
        # Normalize phone
        normalized = from_number.replace("+1", "").replace("-", "").replace(" ", "").strip()

        # Try multiple formats
        for fmt in [from_number, f"+1{normalized}", normalized]:
            params = {
                "filterByFormula": (
                    f"AND("
                    f"{{Callback Number}} = '{fmt}', "
                    f"{{Twilio Number}} = '{twilio_number}', "
                    f"OR({{Lead Status}} = 'Booked', {{Lead Status}} = 'Contacted', {{Lead Status}} = 'New Lead')"
                    f")"
                )
            }
            response = requests.get(LEADS_URL, headers=HEADERS, params=params)
            records = response.json().get("records", [])
            if records:
                # Return the most recent record
                latest = sorted(records, key=lambda r: r.get("createdTime", ""), reverse=True)[0]
                print(f"CUSTOMER SERVICE | Existing customer found | {from_number} | {latest['id']}")
                return latest.get("fields", {})

        return None

    except Exception as e:
        print(f"LOOKUP LEAD BY PHONE ERROR | {e}")
        return None


def handle_customer_service(
    incoming_msg: str,
    from_number: str,
    to_number: str,
    lead: dict,
    contractor: dict,
    business_name: str
) -> Response:
    """
    Handles customer service mode — answers questions from existing customers.
    Uses Claude to generate intelligent responses based on their lead data.
    Notifies contractor of all auto-responses.
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime

        customer_name = lead.get("Client Name", "there")
        first_name = customer_name.split()[0] if customer_name else "there"
        service_address = lead.get("Service Address", "")
        job_description = lead.get("Job Description", "")
        lead_status = lead.get("Lead Status", "")
        appointment_raw = lead.get("Appointment Date and Time", "")
        appointment_requested = lead.get("Appointment Requested", "")
        cal_booking_url = contractor.get("CAL Booking URL", "")
        notify_sms = contractor.get("Notify SMS", "")

        # Format appointment time if available
        appointment_display = ""
        if appointment_raw:
            try:
                eastern = ZoneInfo("America/New_York")
                dt = datetime.fromisoformat(appointment_raw.replace("Z", "+00:00"))
                dt_eastern = dt.astimezone(eastern)
                appointment_display = dt_eastern.strftime("%A, %B %-d at %-I:%M %p")
            except Exception:
                appointment_display = appointment_raw

        # Build Cal.com reschedule link
        import urllib.parse
        reschedule_link = ""
        if cal_booking_url:
            params = urllib.parse.urlencode({
                "name": customer_name,
                "attendeePhoneNumber": from_number,
            })
            reschedule_link = f"{cal_booking_url}?{params}"

        # Build Claude system prompt with customer context
        system_prompt = f"""You are a helpful customer service assistant for {business_name}.

Customer Information:
- Name: {customer_name}
- Service Address: {service_address}
- Job Requested: {job_description}
- Appointment Status: {lead_status}
- Confirmed Appointment: {appointment_display or 'Not yet scheduled'}
- Requested Timing: {appointment_requested}

Your job is to answer the customer's question helpfully and accurately based on their information above.

Rules:
- Be warm, professional and concise — this is an SMS response, keep it under 160 characters when possible
- If they ask about their appointment and it's booked: confirm the date and time
- If they ask to reschedule: provide this link: {reschedule_link}
- If they ask to cancel: tell them to reply CANCEL APPOINTMENT
- If they ask about pricing: tell them the contractor will confirm pricing at the estimate
- If you don't know the answer: say you'll have someone follow up shortly
- Never make up information you don't have
- Sign off with {business_name}
- Do NOT run a new intake — the customer is already in the system"""

        # Run Claude
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": incoming_msg}],
            system=system_prompt
        )
        reply = message.content[0].text.strip()

        print(f"CUSTOMER SERVICE REPLY | {customer_name} | Q: {incoming_msg[:50]} | A: {reply[:50]}")

        # Send reply to customer via Twilio
        from twilio.rest import Client as TwilioClient
        tc = TwilioClient(
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN")
        )
        tc.messages.create(
            body=reply,
            from_=to_number,
            to=from_number
        )

        # Notify contractor of auto-response
        if notify_sms and to_number:
            contractor_alert = (
                f"💬 Auto-reply sent to {first_name}\n"
                f"Q: \"{incoming_msg[:80]}\"\n"
                f"A: \"{reply[:80]}\""
            )
            try:
                tc.messages.create(
                    body=contractor_alert,
                    from_=to_number,
                    to=notify_sms
                )
                print(f"CONTRACTOR NOTIFIED | {notify_sms}")
            except Exception as e:
                print(f"CONTRACTOR NOTIFY ERROR | {e}")

        # Return empty TwiML — we already sent the SMS directly
        return Response(
            "<Response></Response>",
            mimetype="text/xml"
        )

    except Exception as e:
        print(f"CUSTOMER SERVICE ERROR | {type(e).__name__} | {e}")
        # Fallback — let them know we'll follow up
        fallback = f"Thanks for reaching out! Someone from {business_name} will follow up with you shortly."
        return Response(
            f"<Response><Message>{fallback}</Message></Response>",
            mimetype="text/xml"
        )
