# app/app/cal_service.py

import urllib.parse

def build_cal_booking_link(contractor: dict, state: dict) -> str:
    """
    Builds a clean redirect link that routes to the correct contractor's
    Cal.com Intake URL with prefilled fields.
    """

    redirect_base = "https://mme-ai-bot.onrender.com/book"

    contractor_key = (contractor.get("Twilio Number") or "").strip()
    if not contractor_key:
        return ""

    name = (state.get("name") or "").strip()

    callback = "".join(c for c in (state.get("callback") or "") if c.isdigit())
    if len(callback) == 10:
        callback = f"+1{callback}"
    elif len(callback) == 11 and callback.startswith("1"):
        callback = f"+{callback}"
    elif callback:
        callback = f"+{callback}"

    service_address = (state.get("service_address") or "").strip()
    job_description = (state.get("job_description") or "").strip()

    print("CAL PREFILL ADDRESS:", service_address)
    print("CAL PREFILL JOB:", job_description)

    params = {
        "c": contractor_key,
        "name": Name,
        "attendeePhoneNumber": callback,
        "service_address": service_address,
        "job_description": job_description,
    }  

    params = {k: v for k, v in params.items() if v}

    query_string = urllib.parse.urlencode(params)
    separator = "&" if "?" in redirect_base else "?"

    return f"{redirect_base}{separator}{query_string}" if query_string else redirect_base




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
