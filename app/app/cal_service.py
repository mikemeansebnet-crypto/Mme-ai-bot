# app/app/cal_service.py

import urllib.parse

def build_cal_booking_link(contractor: dict, state: dict) -> str:
    """
    Builds a Cal.com booking link with prefilled fields so the customer
    can review/correct details instead of re-entering everything.

    Expected Cal.com identifiers:
    - name
    - phone
    - service_address
    - service_needed
    - notes
    """

    base_url = (contractor.get("CAL Booking URL") or "").strip()
    if not base_url:
        return ""

    name = (state.get("name") or "").strip()
    callback = (state.get("callback") or "").strip()
    service_address = (state.get("service_address") or "").strip()
    job_description = (state.get("job_description") or "").strip()

    params = {
        "name": name,
        "phone": callback,
        "service_address": service_address,
        "service_needed": job_description,
        "notes": f"Address: {service_address}\nService needed: {job_description}",
    }

    params = {k: v for k, v in params.items() if v}

    query_string = urllib.parse.urlencode(params)
    separator = "&" if "?" in base_url else "?"

    return f"{base_url}{separator}{query_string}" if query_string else base_url

import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from app.app.crypto_service import decrypt_text, looks_encrypted 


def create_google_calendar_event(
    contractor: dict,
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    location: str = "",
) -> dict:
    """
    Creates a Google Calendar event for the given contractor.

    start_time and end_time should be ISO 8601 strings, for example:
    2026-03-09T15:00:00-04:00
    """

    encrypted_refresh_token = (contractor.get("Google Refresh Token") or "").strip()

    if not encrypted_refresh_token:
        refresh_token = ""
    elif looks_encrypted(encrypted_refresh_token):
        refresh_token = decrypt_text(encrypted_refresh_token)
    else:
        refresh_token = encrypted_refresh_token
        
    calendar_id = (contractor.get("Google Calendar ID") or "primary").strip() or "primary"
    
    raw_timezone = contractor.get("Timezone")

    if isinstance(raw_timezone, dict):
        timezone = (raw_timezone.get("name") or "").strip()
    else:
        timezone = str(raw_timezone or "").strip()

    print("RAW TIMEZONE:", raw_timezone)
    print("NORMALIZED TIMEZONE:", timezone)

    if timezone != "America/New_York":
        timezone = "America/New_York"

    if not refresh_token:
        return {"ok": False, "error": "missing_google_refresh_token"}

    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.getenv("GOOGLE_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            scopes=[
                "openid",
                "https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
        )

        service = build("calendar", "v3", credentials=creds)

        event_body = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {
                "dateTime": start_time,
                "timeZone": timezone,
            },
            "end": {
                "dateTime": end_time,
                "timeZone": timezone,
            },
        }

        created_event = service.events().insert(
            calendarId=calendar_id,
            body=event_body
        ).execute()

        return {
            "ok": True,
            "event_id": created_event.get("id"),
            "html_link": created_event.get("htmlLink"),
            "data": created_event,
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}
