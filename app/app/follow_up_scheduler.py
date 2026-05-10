# -----------------------------------------------
# FILE: app/app/follow_up_scheduler.py
# What it does: Checks Airtable hourly for leads
# that need follow-up and sends SMS via Twilio
# -----------------------------------------------

from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import os
import requests

print("follow_up_scheduler.py loaded")

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    "Content-Type": "application/json"
}


def get_contractor_info(twilio_number: str) -> dict:
    """Fetches contractor details from Airtable by Twilio number."""
    try:
        CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CONTRACTORS_TABLE}"
        params = {"filterByFormula": f"{{Twilio Number}} = '{twilio_number}'"}
        response = requests.get(url, headers=HEADERS, params=params)
        records = response.json().get("records", [])
        if records:
            return records[0].get("fields", {})
        return {}
    except Exception as e:
        print(f"CONTRACTOR LOOKUP ERROR | {e}")
        return {}


def send_sms(to_number: str, message: str, from_number: str) -> None:
    """Sends SMS from the contractor's Twilio number."""
    from twilio.rest import Client
    client = Client(TWILIO_ACCOUNT_SID, os.environ.get("TWILIO_AUTH_TOKEN"))
    client.messages.create(
        body=message,
        from_=from_number,
        to=to_number
    )


def update_airtable_record(record_id: str, fields: dict) -> None:
    requests.patch(
        f"{AIRTABLE_URL}/{record_id}",
        headers=HEADERS,
        json={"fields": fields}
    )


def fetch_leads_needing_followup():
    """
    Fetches leads that still need follow-up.
    FIXED: Excludes Booked and Cold Lead statuses so booked customers
    don't keep getting follow-up messages.
    """
    params = {
    "filterByFormula": (
        "AND("
        "OR({Lead Status} = 'New Lead', {Lead Status} = 'Contacted'), "
        "{Follow Up Count} < 3, "
        "{Call Back Number} != '', "
        "{Do Not Follow Up} = FALSE()"  # ADDED: Skip paused leads
        ")"
    )
}
    }
    response = requests.get(AIRTABLE_URL, headers=HEADERS, params=params)
    print(f"Airtable status: {response.status_code}")
    return response.json().get("records", [])


def run_follow_up_job():
    print(f"[{datetime.now()}] Running follow-up job...")
    records = fetch_leads_needing_followup()
    print(f"Records found: {len(records)}")

    for record in records:
        fields = record.get("fields", {})
        record_id = record.get("id")

        name = fields.get("Client Name", "there")
        first_name = name.split()[0] if name else "there"
        phone = fields.get("Call Back Number", "")
        # FIXED: Use contractor's Twilio number stored on the lead
        twilio_number = fields.get("Twilio Number", os.environ.get("TWILIO_PHONE_NUMBER", ""))
        follow_up_count = int(fields.get("Follow Up Count") or 0)
        created_time = record.get("createdTime")

        if not phone:
            print(f"No phone for record {record_id}, skipping")
            continue

        if not twilio_number:
            print(f"No Twilio number for record {record_id}, skipping")
            continue

        # Look up contractor for business name and booking URL
        contractor = get_contractor_info(twilio_number)
        business_name = contractor.get("Business Name", "your contractor")
        cal_booking_url = contractor.get("CAL Booking URL", "")

        # Build booking link
        import urllib.parse
        cal_params = urllib.parse.urlencode({
            "name": name,
            "attendeePhoneNumber": phone,
        })
        booking_link = f"{cal_booking_url}?{cal_params}" if cal_booking_url else ""

        created_dt = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
        hours_elapsed = (datetime.now(created_dt.tzinfo) - created_dt).total_seconds() / 3600
        required_hours = (follow_up_count + 1) * 24

        print(f"Lead: {name} | Hours: {hours_elapsed:.2f} | Required: {required_hours:.2f} | Count: {follow_up_count}")

        if hours_elapsed >= required_hours and follow_up_count < 3:
            # FIXED: Messages now include booking link and use contractor business name
            if follow_up_count == 0:
                message = (
                    f"Hey {first_name}, just checking in — still interested in a quote from {business_name}? "
                    f"Book a convenient time here: {booking_link}" if booking_link else
                    f"Hey {first_name}, just checking in — still interested in a quote from {business_name}? "
                    f"Reply YES and we'll get you scheduled."
                )
            elif follow_up_count == 1:
                message = (
                    f"Hi {first_name}, we have limited availability this week at {business_name}. "
                    f"Grab a spot before it's gone: {booking_link}" if booking_link else
                    f"Hi {first_name}, we have limited availability this week. "
                    f"Reply to hold your spot or we'll open it up for other customers."
                )
            else:
                # FIXED: Uses business name instead of hardcoded CrewCachePro
                message = (
                    f"No worries {first_name}, whenever the timing is right just reach out to {business_name} "
                    f"and we'll get you taken care of."
                )

            try:
                # FIXED: Sends from contractor's Twilio number
                send_sms(phone, message, twilio_number)
                new_count = follow_up_count + 1
                new_status = "Cold Lead" if new_count >= 3 else "Contacted"
                update_airtable_record(record_id, {
                    "Follow Up Count": new_count,
                    "Lead Status": new_status
                })
                print(f"Follow-up {new_count} sent to {name} | from {twilio_number}")
            except Exception as e:
                print(f"Failed to send to {name}: {e}")


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_follow_up_job, "interval", hours=1)
    scheduler.start()
    print("Follow-up scheduler started")
