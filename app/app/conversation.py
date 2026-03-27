# app/app/conversation.py
#
# ConversationRelay + Claude AI voice handler
# WebSocket mode — requires flask-sock in requirements.txt
# Gunicorn: --worker-class gthread --workers 1 --threads 10 --timeout 0

import os
import json
import re
import time
import urllib.parse
import xml.sax.saxutils as saxutils
import anthropic
from flask_sock import Sock
from datetime import datetime, timezone
from flask import Blueprint, request, Response

from app.app.state import (
    get_state, set_state, clear_state,
    set_call_alias, get_call_alias,
    save_resume_pointer, get_resume_pointer, clear_resume_pointer,
    register_live_call, unregister_live_call,
)
from app.app.config import redis_client
from app.app.mapbox_service import mapbox_address_candidates, mapbox_geocode_one
from app.app.airtable_service import get_contractor_by_twilio_number

# ─────────────────────────────────────────────
# Blueprint + WebSocket
# ─────────────────────────────────────────────

conversation_bp = Blueprint("conversation", __name__)
sock = Sock()

def init_sock(app):
    sock.init_app(app)


# ─────────────────────────────────────────────
# Claude client
# ─────────────────────────────────────────────

def get_claude_client():
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing ANTHROPIC_API_KEY environment variable")
    return anthropic.Anthropic(api_key=api_key)


# ─────────────────────────────────────────────
# System prompt builder
# ─────────────────────────────────────────────

def build_system_prompt(contractor: dict, state: dict) -> str:
    business_name = (contractor.get("Business Name") or "our office").strip()
    greeting_name = (contractor.get("Greeting Name") or business_name).strip()

    name = (state.get("name") or "").strip()
    service_address = (state.get("service_address") or "").strip()
    job_description = (state.get("job_description") or "").strip()
    timing = (state.get("timing") or "").strip()

    already_collected = []
    still_needed = []

    if name:
        already_collected.append(f"Name: {name}")
    else:
        still_needed.append("caller's full name (first and last)")

    if service_address:
        already_collected.append(f"Service address: {service_address}")
    else:
        still_needed.append("full service address (house number, street, city, zip)")

    if job_description:
        already_collected.append(f"Job description: {job_description}")
    else:
        still_needed.append("brief description of work needed")

    if timing:
        already_collected.append(f"Timing: {timing}")
    else:
        still_needed.append("when they need the service")

    already_str = "\n".join(f"- {x}" for x in already_collected) if already_collected else "Nothing yet"
    needed_str = "\n".join(f"- {x}" for x in still_needed) if still_needed else "All collected"

    return f"""You are a friendly, professional AI intake assistant for {greeting_name}, a contractor business.
Your job is to collect four pieces of information from the caller to send them a booking link.

INFORMATION TO COLLECT:
1. Caller's full name (first and last)
2. Full service address (house number, street, city, zip code)
3. Brief description of work needed
4. When they need the service

ALREADY COLLECTED:
{already_str}

STILL NEEDED:
{needed_str}

RULES:
- If caller mentions emergency (gushing, burst pipe, main line backup, sewage in house, sump pump failure, sparking, burning smell, smoking outlet, panel, exposed live wire, gas leak, carbon monoxide, no heat in winter, furnace whistling, tree on house, ceiling caved, major storm damage, active flooding) say exactly: EMERGENCY_TRANSFER
- If caller uses words like 'asap', 'urgent', 'quick', or 'immediately' but is NOT a catastrophic emergency, continue intake normally but set priority to 'URGENT'
- If job involves water heater, main panel, sump pump, HVAC out, main line, septic, panel upgrade, AC install, set priority to 'HIGH_PRIORITY'
- All other calls set priority to 'STANDARD'
- This is a phone call — keep ALL responses under 15 words
- Collect info in whatever order the caller provides it naturally
- If caller gives multiple pieces at once, capture all of them
- Accept the first answer given for any field — never ask follow-up questions
- For timing, accept anything they say as final (ASAP, first available, next week, etc.)
- For job description, accept their first description — do not ask for more details
- Only confirm the address — ask yes or no, nothing else
- If they say address is wrong, ask them to repeat it once
- If caller wants voicemail or to leave a message, say exactly: VOICEMAIL_TRANSFER
- Never make up or assume information
- Never ask clarifying questions about the job — the contractor will handle that at the estimate
- Do not use filler phrases like "How can I assist you today"
- Once ALL four pieces are confirmed output INTAKE_COMPLETE followed immediately by JSON
- Once the caller confirms the address with yes or correct, never ask about the address again
- Do not re-confirm any field that has already been confirmed 


WHEN ALL FOUR PIECES ARE COLLECTED output EXACTLY this (nothing after the JSON):
INTAKE_COMPLETE
{{"name": "...", "service_address": "...", "job_description": "...", "timing": "...", "priority": "..."}}"""


# ─────────────────────────────────────────────
# Address cleaning
# ─────────────────────────────────────────────

def clean_speech_field(text: str) -> str:
    text = re.sub(r"\bI'?m\b", "", text, flags=re.IGNORECASE)

    def collapse_spelled(m):
        return m.group(0).replace(" ", "")

    text = re.sub(r"\b([A-Za-z] ){1,}[A-Za-z]\b", collapse_spelled, text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ─────────────────────────────────────────────
# Mapbox validation
# ─────────────────────────────────────────────

def validate_address(raw_address: str, contractor: dict) -> dict:
    from main import address_in_service_area

    normalized = {str(k).strip(): v for k, v in contractor.items()}
    home_lat = normalized.get("Home Base Lat")
    home_lon = normalized.get("Home Base Lon")
    proximity = f"{home_lon},{home_lat}" if home_lat and home_lon else None

    cleaned = clean_speech_field(raw_address)
    candidates = mapbox_address_candidates(cleaned, limit=3, country="US", proximity=proximity)

    print("MAPBOX CANDIDATES V6 |", candidates)

    good = [c for c in candidates if c.get("confidence") in ("exact", "high", "medium", "low", None)]

    if not good:
        return {"ok": False, "reason": "no_candidates"}

    best = good[0]["full_address"]

    geo = mapbox_geocode_one(best, country="US", proximity=proximity)
    if not geo.get("ok") or not geo.get("feature"):
        return {"ok": False, "reason": "geocode_failed"}

    feature = geo["feature"]
    allowed, reason = address_in_service_area(contractor, feature.get("lat"), feature.get("lon"))

    return {
        "ok": True,
        "full_address": best,
        "lat": feature.get("lat"),
        "lon": feature.get("lon"),
        "in_service_area": allowed,
        "reason": reason,
    }


# ─────────────────────────────────────────────
# Finalize lead — fires all integrations
# ─────────────────────────────────────────────

def finalize_lead(state: dict, contractor: dict, to_number: str, from_number: str, call_sid: str):
    from main import send_intake_summary, update_contractor_status, twilio_client
    from app.app.cal_service import build_cal_booking_link

    state["to_number"] = to_number
    state["from_number"] = from_number
    state["call_sid"] = call_sid

    notify_email = (contractor.get("Notify Email") or os.getenv("TO_EMAIL") or "").strip() or None
    reply_to_email = (contractor.get("Reply to Email") or "").strip() or None

    try:
        send_intake_summary(state, notify_email=notify_email, reply_to_email=reply_to_email)
    except Exception as e:
        print("FINALIZE send_intake_summary ERROR |", e)

    try:
        booking_link = build_cal_booking_link(contractor, state)
        business_name = (contractor.get("Business Name") or "our office").strip()
        send_sms_enabled = bool(contractor.get("SMS", False))

        if send_sms_enabled:
            tc = twilio_client()
            if tc.get("ok"):
                client = tc["client"]
                callback = state.get("callback") or from_number or ""
                callback_digits = "".join(c for c in callback if c.isdigit())

                if len(callback_digits) == 10:
                    sms_to = f"+1{callback_digits}"
                elif len(callback_digits) == 11:
                    sms_to = f"+{callback_digits}"
                else:
                    sms_to = from_number

                if booking_link:
                    sms_body = (
                        f"Thanks for contacting {business_name}. "
                        f"Book your estimate here: {booking_link} "
                        "Reply STOP to opt out."
                    )
                else:
                    sms_body = (
                        f"Thanks for contacting {business_name}. "
                        "We received your request and will follow up shortly. "
                        "Reply STOP to opt out."
                    )

                msg = client.messages.create(body=sms_body, from_=to_number, to=sms_to)
                print("SMS SENT TO:", sms_to, "| SID:", msg.sid)

    except Exception as e:
        print("FINALIZE SMS ERROR |", e)

    try:
        update_contractor_status(to_number, {
            "Bot Status": "Healthy",
            "Last Good Address": state.get("service_address", ""),
        })
    except Exception:
        pass

    if redis_client and to_number and from_number:
        clear_resume_pointer(to_number, from_number)
        print("RESUME PTR CLEARED:", to_number, from_number)

    unregister_live_call(state.get("contractor_key", "unknown"), call_sid)
    clear_state(call_sid)

    print(
        "CALL COMPLETE |",
        "CallSid:", call_sid,
        "| Name:", state.get("name"),
        "| Address:", state.get("service_address"),
        "| Job:", state.get("job_description"),
        "| Timing:", state.get("timing"),
    )


# ─────────────────────────────────────────────
# Claude conversation turn
# ─────────────────────────────────────────────

def run_claude_turn(system_prompt: str, messages: list, caller_input: str) -> str:
    client = get_claude_client()
    messages_to_send = messages + [{"role": "user", "content": caller_input}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system_prompt,
        messages=messages_to_send,
    )

    return response.content[0].text.strip()


# ─────────────────────────────────────────────
# Parse INTAKE_COMPLETE
# ─────────────────────────────────────────────

def parse_intake_complete(response_text: str) -> dict | None:
    if "INTAKE_COMPLETE" not in response_text:
        return None

    try:
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print("INTAKE_COMPLETE JSON parse error |", e)

    return None


# ─────────────────────────────────────────────
# Partial data extraction helper
# ─────────────────────────────────────────────

def _extract_partial_data(claude_response: str, caller_input: str, state: dict):
    try:
        json_match = re.search(r"\{.*\}", claude_response, re.DOTALL)
        if json_match:
            partial = json.loads(json_match.group(0))
            if partial.get("name") and not state.get("name"):
                state["name"] = partial["name"]
            if partial.get("job_description") and not state.get("job_description"):
                state["job_description"] = partial["job_description"]
            if partial.get("timing") and not state.get("timing"):
                state["timing"] = partial["timing"]
    except Exception:
        pass


# ─────────────────────────────────────────────
# ConversationRelay TwiML entry point
# ─────────────────────────────────────────────

@conversation_bp.route("/voice-cr", methods=["POST", "GET"])
def voice_cr():
    call_sid = request.values.get("CallSid", "unknown")
    to_number = (request.values.get("To") or "").strip()
    from_number = (request.values.get("From") or "").strip()

    contractor = get_contractor_by_twilio_number(to_number) or {}
    business_name = (contractor.get("Business Name") or "our office").strip()
    greeting_name = (contractor.get("Greeting Name") or business_name).strip()

    # Check for resume
    resume_call_sid = None
    resume_state = {}
    if redis_client and to_number and from_number:
        resume_call_sid = get_resume_pointer(to_number, from_number)
        if resume_call_sid:
            resume_state = get_state(resume_call_sid) or {}

    # Initialize or resume state
    if resume_state and resume_call_sid:
        state = resume_state
        state["call_sid"] = resume_call_sid
        set_call_alias(call_sid, resume_call_sid)
        effective_call_sid = resume_call_sid
        print("RESUMING CALL |", resume_call_sid)
    else:
        effective_call_sid = call_sid
        state = {
            "step": 0,
            "callback": from_number,
            "retries": 0,
            "name": "",
            "service_address": "",
            "job_description": "",
            "timing": "",
            "call_sid": call_sid,
            "to_number": to_number,
            "from_number": from_number,
            "contractor_key": to_number,
            "started_at": int(time.time()),
            "messages": [],
        }

    set_state(effective_call_sid, state)
    register_live_call(to_number, effective_call_sid)

    # Build greeting
    if resume_state and resume_call_sid:
        have = []
        if state.get("name"): have.append(state["name"])
        if state.get("service_address"): have.append("your address")
        if state.get("job_description"): have.append("job details")
        if state.get("timing"): have.append("timing")
        greeting = f"Welcome back. Let me pick up where we left off."
    else:
        record_calls = bool(contractor.get("RECORD_CALLS")) or os.getenv("RECORD_CALLS_DEFAULT", "false").lower() == "true"
        if record_calls:
            greeting = f"Thanks for calling {greeting_name}. Just so you know, this call may be recorded. How can I help you today?"
        else:
            greeting = f"Thanks for calling {greeting_name}. How can I help you today?"

    # Store greeting for WebSocket setup event
    state["pending_greeting"] = greeting
    set_state(effective_call_sid, state)

    # WebSocket URL — must be wss://
    ws_url = (
        request.url_root.rstrip("/")
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        + "/conversation-turn?"
        + urllib.parse.urlencode({
            "to": to_number,
            "from": from_number,
            "call_sid": effective_call_sid
        })
    )

    escaped_url = saxutils.escape(ws_url)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <ConversationRelay
            url="{escaped_url}"
            voice="en-US-Neural2-F"
            language="en-US"
            transcriptionProvider="deepgram"
            speechModel="nova-3"
            dtmfDetection="true"
            interruptByDtmf="true"
        />
    </Connect>
</Response>"""


    return Response(twiml, mimetype="text/xml")


# ─────────────────────────────────────────────
# ConversationRelay WebSocket handler
# ─────────────────────────────────────────────

@sock.route("/conversation-turn")
def conversation_turn(ws):
    # Get to/from from query string passed by voice_cr
    to_number_qs = request.args.get("to", "")
    from_number_qs = request.args.get("from", "")
    call_sid_qs = request.args.get("call_sid", "unknown")

    while True:
        try:
            raw = ws.receive()
            if raw is None:
                break
            data = json.loads(raw)
        except Exception as e:
            print("WS RECEIVE ERROR |", e)
            break

        event_type = data.get("type", "")
        call_sid = data.get("callSid", "unknown")
        caller_input = (data.get("voicePrompt") or data.get("text") or "").strip()

        if call_sid == "unknown":
            call_sid = call_sid_qs

        print("CR EVENT |", event_type, "| CallSid:", call_sid, "| Input:", caller_input)

        # Resolve aliased call_sid
        aliased = get_call_alias(call_sid)
        effective_call_sid = aliased if aliased else call_sid

        state = get_state(effective_call_sid) or {}
        to_number = state.get("to_number", "") or to_number_qs
        from_number = state.get("from_number", "") or state.get("callback", "") or from_number_qs

        print("DEBUG STATE | effective_call_sid:", effective_call_sid, "| to_number:", to_number, "| from_number:", from_number)

        contractor = get_contractor_by_twilio_number(to_number) or {}

        # ── Session start ──
        if event_type == "setup":
            greeting = state.pop("pending_greeting", "How can I help you today?")
            set_state(effective_call_sid, state)
            ws.send(json.dumps({"type": "text", "token": greeting, "last": True}))
            continue

        # ── Call ended ──
        if event_type in ("end", "disconnect"):
            if to_number and from_number:
                save_resume_pointer(to_number, from_number, effective_call_sid)
            break

        # ── Skip empty input ──
        if not caller_input:
            continue

        # ── Run Claude ──
        messages = state.get("messages", [])
        system_prompt = build_system_prompt(contractor, state)

        try:
            claude_response = run_claude_turn(system_prompt, messages, caller_input)
        except Exception as e:
            print("CLAUDE ERROR |", e)
            ws.send(json.dumps({
                "type": "text",
                "token": "I'm sorry, I had a technical issue. One moment please.",
                "last": True
            }))
            continue

        print("CLAUDE RESPONSE |", claude_response)

        # ── Emergency transfer ──
        if "EMERGENCY_TRANSFER" in claude_response:
            if redis_client and to_number and from_number:
                save_resume_pointer(to_number, from_number, effective_call_sid)
            ws.send(json.dumps({
                "type": "text",
                "token": "Connecting you to our emergency line now. Please hold.",
                "last": True
            }))
            break

        # ── Voicemail transfer ──
        if "VOICEMAIL_TRANSFER" in claude_response:
            ws.send(json.dumps({
                "type": "text",
                "token": "Please leave your message after the tone.",
                "last": True
            }))
            break

        # ── Intake complete ──
        intake_data = parse_intake_complete(claude_response)
        if intake_data:
            raw_address = intake_data.get("service_address", "")
            addr_result = validate_address(raw_address, contractor)

            if not addr_result.get("ok"):
                retry_msg = "I need to verify that address. Could you repeat your full address including house number, street, city and zip?"
                messages.append({"role": "user", "content": caller_input})
                messages.append({"role": "assistant", "content": retry_msg})
                state["messages"] = messages[-20:]
                set_state(effective_call_sid, state)
                ws.send(json.dumps({"type": "text", "token": retry_msg, "last": True}))
                continue

            if not addr_result.get("in_service_area"):
                out_msg = "I'm sorry, that address is outside our service area. Do you have a different address?"
                messages.append({"role": "user", "content": caller_input})
                messages.append({"role": "assistant", "content": out_msg})
                state["messages"] = messages[-20:]
                set_state(effective_call_sid, state)
                ws.send(json.dumps({"type": "text", "token": out_msg, "last": True}))
                continue

            # Save final state
            state["name"] = intake_data.get("name", state.get("name", ""))
            state["service_address"] = addr_result["full_address"]
            state["job_description"] = intake_data.get("job_description", state.get("job_description", ""))
            state["timing"] = intake_data.get("timing", state.get("timing", ""))
            state["priority"] = intake_data.get("priority", "STANDARD")
            state["callback"] = from_number
            set_state(effective_call_sid, state)

            # Fire all integrations
            try:
                finalize_lead(state, contractor, to_number, from_number, effective_call_sid)
            except Exception as e:
                print("FINALIZE ERROR |", e)

            greeting_name = (contractor.get("Greeting Name") or contractor.get("Business Name") or "our office").strip()
            ws.send(json.dumps({
                "type": "text",
                "token": f"Perfect. Check your texts for the booking link. Thanks for calling {greeting_name}!",
                "last": True
            }))
            time.sleep(10)  # Give TTS time to finish speaking before closing
            break

        # ── Normal response ──
        _extract_partial_data(claude_response, caller_input, state)
        messages.append({"role": "user", "content": caller_input})
        messages.append({"role": "assistant", "content": claude_response})
        state["messages"] = messages[-20:]
        set_state(effective_call_sid, state)

        if redis_client and to_number and from_number:
            save_resume_pointer(to_number, from_number, effective_call_sid)

        ws.send(json.dumps({"type": "text", "token": claude_response, "last": True}))
