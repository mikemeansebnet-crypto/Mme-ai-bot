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
        "name": name,
        "attendeePhoneNumber": callback,
        "service_address": service_address,
        "job_description": job_description,
    }  

    params = {k: v for k, v in params.items() if v}

    query_string = urllib.parse.urlencode(params)
    separator = "&" if "?" in redirect_base else "?"

    print("CAL PREFILL NAME:", repr(name))
    print("CAL PREFILL ADDRESS:", repr(service_address))
    print("CAL PREFILL JOB:", repr(job_description))
    print("CAL PREFILL CALLBACK:", repr(callback))
    print("CAL PARAMS:", params)
    print("CAL QUERY STRING:", query_string)
    print("CAL BOOKING LINK:", f"{redirect_base}{separator}{query_string}")

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

    if not timezone:
        timezone = "America/New_York"  # fallback only if contractor has no timezone set on file

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
        print(f"CALENDAR DEBUG | encrypted: {encrypted_refresh_token[:20]}... | looks_encrypted: {looks_encrypted(encrypted_refresh_token)} | refresh_token: {refresh_token[:20]}...")
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

from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, time as dtime


def _build_calendar_service(contractor: dict):
    """Shared helper - builds an authenticated Google Calendar client for a contractor."""
    encrypted_refresh_token = (contractor.get("Google Refresh Token") or "").strip()

    if not encrypted_refresh_token:
        refresh_token = ""
    elif looks_encrypted(encrypted_refresh_token):
        refresh_token = decrypt_text(encrypted_refresh_token)
    else:
        refresh_token = encrypted_refresh_token

    if not refresh_token:
        return None, None, None

    calendar_id = (contractor.get("Google Calendar ID") or "primary").strip() or "primary"

    raw_timezone = contractor.get("Timezone")
    if isinstance(raw_timezone, dict):
        timezone = (raw_timezone.get("name") or "").strip()
    else:
        timezone = str(raw_timezone or "").strip()
    if not timezone:
        timezone = "America/New_York"

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
        return service, calendar_id, timezone
    except Exception as e:
        print("CALENDAR SERVICE BUILD ERROR |", e)
        return None, None, None


def get_available_slots(contractor: dict, date_str: str, duration_minutes: int) -> list:
    """
    Open slots for a contractor on a given date (YYYY-MM-DD, contractor local time)
    long enough to fit duration_minutes without overlapping existing events.
    Working hours hardcoded for now: Mon-Sat 8AM-5PM local, closed Sunday.
    """
    service, calendar_id, timezone = _build_calendar_service(contractor)
    if not service:
        return []

    tz = ZoneInfo(timezone)

    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return []

    if day.weekday() == 6:  # Sunday
        return []

    work_start = datetime.combine(day, dtime(8, 0), tzinfo=tz)
    work_end = datetime.combine(day, dtime(17, 0), tzinfo=tz)
    earliest_allowed = datetime.now(tz) + timedelta(hours=1)

    try:
        fb = service.freebusy().query(body={
            "timeMin": work_start.astimezone(ZoneInfo("UTC")).isoformat(),
            "timeMax": work_end.astimezone(ZoneInfo("UTC")).isoformat(),
            "timeZone": "UTC",
            "items": [{"id": calendar_id}],
        }).execute()
        busy_raw = fb.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    except Exception as e:
        print("FREEBUSY ERROR |", e)
        busy_raw = []

    busy_intervals = []
    for b in busy_raw:
        try:
            busy_intervals.append((
                datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
            ))
        except Exception:
            continue

    slots = []
    slot_step = timedelta(minutes=30)
    slot_length = timedelta(minutes=duration_minutes)
    cursor = work_start

    while cursor + slot_length <= work_end:
        slot_end = cursor + slot_length
        if cursor < earliest_allowed:
            cursor += slot_step
            continue

        cursor_utc = cursor.astimezone(ZoneInfo("UTC"))
        slot_end_utc = slot_end.astimezone(ZoneInfo("UTC"))
        overlaps = any(cursor_utc < be and slot_end_utc > bs for bs, be in busy_intervals)

        if not overlaps:
            slots.append({
                "start_iso": cursor.isoformat(),
                "end_iso": slot_end.isoformat(),
                "label": cursor.strftime("%-I:%M %p"),
            })
        cursor += slot_step

    return slots
