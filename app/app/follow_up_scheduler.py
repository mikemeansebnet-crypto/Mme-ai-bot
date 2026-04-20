# -----------------------------------------------
# FILE: app/app/follow_up_scheduler.py
# What it does: Runs every 2 min (test), checks
# Airtable for leads that need follow-up via Twilio
# -----------------------------------------------

from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import os
import requests

print("follow_up_scheduler.py loaded")

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")

AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    "Content-Type": "application/json"
}

FOLLOW_UP_MESSAGES = {
    1: "Hey {name}, just checking in — still interested in a quote? Reply YES to confirm or let us know a better time.",
    2: "Hi {name}, we have limited availability this week. Reply to hold your spot or we'll open it up for other customers.",
    3: "No worries {name}, whenever the timing is right just reply and we'll get you taken care of. — CrewCachePro"
}


def send_sms(to_number, message):
    from twilio.rest import Client
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(
        body=message,
        from_=TWILIO_PHONE_NUMBER,
        to=to_number
    )


def update_airtable_record(record_id, fields):
    requests.patch(
        f"{AIRTABLE_URL}/{record_id}",
        headers=HEADERS,
        json={"fields": fields}
    )


def fetch_leads_needing_followup():
    params = {
        "filterByFormula": "OR({Lead Status} = 'New Lead', {Lead Status} = 'Contacted')"
    }
    response = requests.get(AIRTABLE_URL, headers=HEADERS, params=params)
    print(f"Airtable status: {response.status_code} | Records raw: {response.text[:300]}")
    return response.json().get("records", [])


def run_follow_up_job():
    print(f"[{datetime.now()}] Running follow-up job...")
    records = fetch_leads_needing_followup()
    print(f"Records found: {len(records)}")

    for record in records:
        fields = record.get("fields", {})
        record_id = record.get("id")

        name = fields.get("Client Name", "there")
        phone = fields.get("Call Back Number")
        follow_up_count = int(fields.get("Follow Up Count", 0))
        created_time = record.get("createdTime")

        if not phone:
            print(f"No phone for record {record_id}, skipping")
            continue

        created_dt = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
        hours_elapsed = (datetime.now(created_dt.tzinfo) - created_dt).total_seconds() / 3600

        # TEMP TEST: 0.033 = ~2 minutes. Change back to 24 for production
        required_hours = (follow_up_count + 1) * 0.033

        print(f"Lead: {name} | Hours elapsed: {hours_elapsed:.2f} | Required: {required_hours:.2f} | Follow Up Count: {follow_up_count}")

        if hours_elapsed >= required_hours and follow_up_count < 3:
            message = FOLLOW_UP_MESSAGES[follow_up_count + 1].format(name=name.split()[0])
            try:
                send_sms(phone, message)
                new_count = follow_up_count + 1
                new_status = "Cold Lead" if new_count >= 3 else "Contacted"
                update_airtable_record(record_id, {
                    "Follow Up Count": new_count,
                    "Lead Status": new_status
                })
                print(f"Follow-up {new_count} sent to {name}")
            except Exception as e:
                print(f"Failed to send to {name}: {e}")


def start_scheduler():
    scheduler = BackgroundScheduler()
    # TEMP TEST: minutes=2. Change to hours=1 for production
    scheduler.add_job(run_follow_up_job, "interval", minutes=2)
    scheduler.start()
    print("Follow-up scheduler started.")
