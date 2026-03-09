# app/app/cal_service.py
from urllib.parse import urlencode

def build_cal_booking_link(contractor: dict, customer_name: str = "", customer_phone: str = "", customer_email: str = "") -> str:
    """
    Returns the contractor-specific Cal booking URL (optionally with prefill query params).
    If no URL exists, returns empty string.
    """
    base = (contractor.get("CAL Booking URL") or "").strip()
    if not base:
        return ""

    # Optional prefill. If Cal ignores any of these, no harm done.
    params = {}
    if customer_name:
        params["name"] = customer_name
    if customer_email:
        params["email"] = customer_email
    if customer_phone:
        params["phone"] = customer_phone

    if not params:
        return base

    joiner = "&" if "?" in base else "?"
    return base + joiner + urlencode(params)

import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


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

    refresh_token = (contractor.get("Google Refresh Token") or "").strip()
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
