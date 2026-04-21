# -----------------------------------------------
# FILE: app/app/cancel_reschedule.py
# What it does: Listens for CANCEL or RESCHEDULE
# replies from customers via Twilio SMS webhook,
# sends them the Cal.com booking link and updates
# Airtable lead status accordingly
# -----------------------------------------------

import os
import requests
from flask import request
from twilio.twiml.messaging_response import MessagingResponse

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME")
AIRTABLE_CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")

LEADS_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
CONTRACTORS_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_CONTRACTORS_TABLE}"

HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}


def get_contractor_booking_url():
    """Pull CAL Booking URL from the first active contractor record."""
    response = requests.get(CONTRACTORS_URL, headers=HEADERS)
    records = response.json().get("records", [])
    for record in records:
        cal_url = record.get("fields", {}).get("CAL Booking URL")
        if cal_url:
            return cal_url
    return None


def find_lead_by_phone(phone_number):
    """Find a lead record by their phone number."""
    params = {
        "filterByFormula": f"{{Call Back Number}} = '{phone_number}'"
    }
    response = requests.get(LEADS_URL, headers=HEADERS, params=params)
    records = response.json().get("records", [])
    if records:
        return records[0]
    return None


def update_lead_status(record_id, status):
    """Update the Lead Status field on a lead record."""
    requests.patch(
        f"{LEADS_URL}/{record_id}",
        headers=HEADERS,
        json={"fields": {"Lead Status": status}}
    )


def handle_cancel_reschedule():
    """
    Main webhook handler — call this from your Twilio SMS route in main.py.
    Returns a TwiML response string.
    """
    incoming_msg = request.form.get("Body", "").strip().upper()
    from_number = request.form.get("From", "")

    response = MessagingResponse()

    if incoming_msg in ["CANCEL", "RESCHEDULE", "CANCEL APPOINTMENT", "RESCHEDULE APPOINTMENT"]:
        # Get Cal.com booking URL from contractors table
        cal_url = get_contractor_booking_url()

        # Find the lead record by phone number
        lead = find_lead_by_phone(from_number)

        if lead:
            record_id = lead.get("id")
            name = lead.get("fields", {}).get("Client Name", "").split()[0]
            update_lead_status(record_id, "Rescheduled")
        else:
            name = "there"

        # Send booking link
        if cal_url:
            msg = (
                f"No problem {name}! You can pick a new day and time that works for you here:\n\n"
                f"{cal_url}\n\n"
                f"We look forward to hearing from you. — CrewCachePro"
            )
        else:
            msg = (
                f"No problem {name}! Please reply with your preferred day and time "
                f"and we'll get you rescheduled. — CrewCachePro"
            )

        response.message(msg)

    return str(response)
