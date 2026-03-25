# app/conversation.py
#
# ConversationRelay + Claude AI voice handler
# Replaces the rigid step-based voice flow in main.py
# All existing integrations (Mapbox, Airtable, Cal, SMS, Email) plug in unchanged.

import os
import json
import re
import anthropic
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
from app.app.airtable_service import (
    get_contractor_by_twilio_number,
    airtable_get_city_corrections,
    normalize_city,
)

# Import helpers from main.py (keep all existing integrations)
# These are imported at runtime to avoid circular imports
def _get_main_helpers():
    from main import (
        address_in_service_area,
        send_intake_summary,
        update_contractor_status,
        twilio_client,
        haversine_miles,
    )
    return {
        "address_in_service_area": address_in_service_area,
        "send_intake_summary": send_intake_summary,
        "update_contractor_status": update_contractor_status,
        "twilio_client": twilio_client,
    }

conversation_bp = Blueprint("conversation", __name__)

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

    # What we already have
    name = (state.get("name") or "").strip()
    service_address = (state.get("service_address") or "").strip()
    job_description = (state.get("job_description") or "").strip()
    timing = (state.get("timing") or "").strip()

    already_collected = []
    still_needed = []

    if name:
        already_collected.append(f"Name: {name}")
    else:
        still_needed.append("caller's full name")

    if service_address:
        already_collected.append(f"Service address: {service_address}")
    else:
        still_needed.append("service address (house number, street, city, zip)")

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
Your job is to collect four pieces of information from the caller so we can send them a booking link and schedule their estimate.

INFORMATION TO COLLECT:
1. Caller's full name (first and last)
2. Service address (full address including house number, street, city, and zip code)
3. Brief description of the work needed
4. When they need the service (timing)

ALREADY COLLECTED:
{already_str}

STILL NEEDED:
{needed_str}

CONVERSATION RULES:
- Be warm, brief, and professional — this is a phone call, keep responses short
- Collect information in whatever order the caller provides it naturally
- If the caller gives multiple pieces of info at once, capture all of them
- For the address, always confirm it back to the caller and ask them to confirm yes or no
- If they say the address is wrong, ask them to repeat it
- Once you have all four pieces confirmed, say exactly: "INTAKE_COMPLETE" followed by a JSON block with the collected data
- Never make up information — only use what the caller provides
- If caller mentions an emergency (flood, tree down, burst pipe, etc.), say exactly: "EMERGENCY_TRANSFER"
- If caller wants voicemail, say exactly: "VOICEMAIL_TRANSFER"
- Keep all responses under 30 words — callers are on a phone and impatient
- Do not repeat back all collected info unless asked
- Do not say "How can I assist you today" or other filler phrases

WHEN COMPLETE output exactly this format (no other text after):
INTAKE_COMPLETE
{{"name": "...", "service_address": "...", "job_description": "...", "timing": "..."}}

IMPORTANT: The service address must be a real, complete address with street number, street name, city, and zip."""


# ─────────────────────────────────────────────
# Address cleaning (carried over from old bot)
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
    """
    Validate and geocode address. Returns:
    {
        "ok": bool,
        "full_address": str,
        "lat": float,
        "lon": float,
        "in_service_area": bool,
        "reason": str
    }
    """
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

    # Geocode for service area check
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
    """
    Called once Claude signals INTAKE_COMPLETE.
    Fires: Airtable, Email, SMS, Cal booking link.
    """
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
                        f"Review your details and book your estimate here: {booking_link} "
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
    """
    Send conversation history + new caller input to Claude.
    Returns Claude's response text.
    """
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
# Parse INTAKE_COMPLETE from Claude response
# ─────────────────────────────────────────────

def parse_intake_complete(response_text: str) -> dict | None:
    """
    Extracts JSON from INTAKE_COMPLETE response.
    Returns dict or None if not found/invalid.
    """
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
# ConversationRelay TwiML entry point
# ─────────────────────────────────────────────

@conversation_bp.route("/voice-cr", methods=["POST", "GET"])
def voice_cr():
    """
    Entry point. Replaces /voice in main.py.
    Returns ConversationRelay TwiML to start the session.
    """
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

    # Initialize state
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
            "contractor_key": to_number,
            "started_at": int(__import__("time").time()),
            "messages": [],  # conversation history for Claude
        }

    set_state(effective_call_sid, state)
    register_live_call(to_number, effective_call_sid)

    # Build greeting
    already_have = []
    if state.get("name"):
        already_have.append(state["name"])
    if state.get("service_address"):
        already_have.append("address")
    if state.get("job_description"):
        already_have.append("job details")
    if state.get("timing"):
        already_have.append("timing")

    if already_have and resume_call_sid:
        greeting = f"Welcome back. I have your {', '.join(already_have)} already. Let me pick up where we left off."
    else:
        greeting = f"Thanks for calling {greeting_name}. How can I help you today?"

    # Webhook URL for conversation turns
    webhook_url = request.url_root.rstrip("/").replace("https://", "wss://") + "/conversation-turn"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <ConversationRelay
            url="{webhook_url}"
            voice="en-US-Neural2-F"
            language="en-US"
            transcriptionProvider="deepgram"
            speechModel="nova-3"
            dtmfDetection="true"
            interruptByDtmf="true"
        />
    </Connect>
</Response>"""

    # Store greeting so conversation-turn can send it first
    state["pending_greeting"] = greeting
    set_state(effective_call_sid, state)

    return Response(twiml, mimetype="text/xml")


# ─────────────────────────────────────────────
# ConversationRelay WebSocket / Webhook handler
# ─────────────────────────────────────────────

@conversation_bp.route("/conversation-turn", methods=["POST"])
def conversation_turn():
    """
    Receives each caller utterance from ConversationRelay.
    Sends to Claude, returns bot response.
    """
    data = request.get_json(force=True) or {}

    event_type = data.get("type", "")
    call_sid = data.get("callSid", "unknown")
    caller_input = (data.get("voicePrompt") or data.get("text") or "").strip()

    print("CR EVENT |", event_type, "| CallSid:", call_sid, "| Input:", caller_input)

    # Resolve aliased call_sid (resume flow)
    aliased = get_call_alias(call_sid)
    effective_call_sid = aliased if aliased else call_sid

    state = get_state(effective_call_sid) or {}
    to_number = state.get("to_number", "")
    from_number = state.get("callback", "")

    contractor = get_contractor_by_twilio_number(to_number) or {}

    # ── Session start — send greeting ──
    if event_type == "setup" or not caller_input:
        greeting = state.pop("pending_greeting", "How can I help you today?")
        set_state(effective_call_sid, state)
        return _cr_response(greeting)

    # ── End of call cleanup ──
    if event_type in ("end", "interrupt", "disconnect"):
        if to_number and from_number:
            save_resume_pointer(to_number, from_number, effective_call_sid)
        return _cr_response("")

    # ── Normal speech turn ──
    messages = state.get("messages", [])

    # Build system prompt with current state
    system_prompt = build_system_prompt(contractor, state)

    # Run Claude
    try:
        claude_response = run_claude_turn(system_prompt, messages, caller_input)
    except Exception as e:
        print("CLAUDE ERROR |", e)
        return _cr_response("I'm sorry, I had a technical issue. Please hold on a moment.")

    print("CLAUDE RESPONSE |", claude_response)

    # ── Check for special signals ──

    # Emergency transfer
    if "EMERGENCY_TRANSFER" in claude_response:
        if redis_client and to_number and from_number:
            save_resume_pointer(to_number, from_number, effective_call_sid)
        return _cr_response(
            "I'm connecting you to our emergency line right now. Please hold.",
            action="transfer",
            action_url="/voice-emergency",
        )

    # Voicemail transfer
    if "VOICEMAIL_TRANSFER" in claude_response:
        return _cr_response(
            "No problem. Please leave your message after the tone.",
            action="transfer",
            action_url="/twilio/voicemail",
        )

    # Intake complete
    intake_data = parse_intake_complete(claude_response)
    if intake_data:
        # Validate address via Mapbox
        raw_address = intake_data.get("service_address", "")
        addr_result = validate_address(raw_address, contractor)

        if not addr_result.get("ok"):
            # Address not found — ask Claude to re-collect it
            messages.append({"role": "user", "content": caller_input})
            messages.append({
                "role": "assistant",
                "content": "I need to verify that address. Could you please repeat your full service address including house number, street, city, and zip?"
            })
            state["messages"] = messages[-20:]  # keep last 20 turns
            set_state(effective_call_sid, state)
            return _cr_response("I need to verify that address. Could you please repeat your full service address including house number, street, city, and zip?")

        if not addr_result.get("in_service_area"):
            messages.append({"role": "user", "content": caller_input})
            messages.append({
                "role": "assistant",
                "content": "I'm sorry, that address is outside our service area. Do you have a different address, or is there anything else I can help with?"
            })
            state["messages"] = messages[-20:]
            set_state(effective_call_sid, state)
            return _cr_response("I'm sorry, that address is outside our service area. Do you have a different address?")

        # All good — save final state
        state["name"] = intake_data.get("name", state.get("name", ""))
        state["service_address"] = addr_result["full_address"]
        state["job_description"] = intake_data.get("job_description", state.get("job_description", ""))
        state["timing"] = intake_data.get("timing", state.get("timing", ""))
        state["callback"] = from_number

        set_state(effective_call_sid, state)

        # Fire all integrations
        try:
            finalize_lead(state, contractor, to_number, from_number, effective_call_sid)
        except Exception as e:
            print("FINALIZE ERROR |", e)

        business_name = (contractor.get("Business Name") or "our office").strip()
        return _cr_response(
            f"Perfect, I have everything I need. Keep an eye out for a text with your booking link. "
            f"Thanks for calling {business_name}. Goodbye!",
            action="hangup",
        )

    # ── Normal response — update conversation history ──
    # Extract any partial data Claude may have gleaned
    _extract_partial_data(claude_response, caller_input, state)

    messages.append({"role": "user", "content": caller_input})
    messages.append({"role": "assistant", "content": claude_response})
    state["messages"] = messages[-20:]  # rolling window, keeps tokens low
    set_state(effective_call_sid, state)

    # Save resume pointer after every turn
    if redis_client and to_number and from_number:
        save_resume_pointer(to_number, from_number, effective_call_sid)

    return _cr_response(claude_response)


# ─────────────────────────────────────────────
# Partial data extraction helper
# ─────────────────────────────────────────────

def _extract_partial_data(claude_response: str, caller_input: str, state: dict):
    """
    Not strictly needed since Claude tracks state, but useful for
    logging and resume accuracy. Looks for JSON fragments in response.
    """
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
# ConversationRelay response format
# ─────────────────────────────────────────────

def _cr_response(text: str, action: str = None, action_url: str = None) -> Response:
    """
    Returns JSON response for ConversationRelay.
    """
    payload = {"type": "text", "token": text}

    if action == "hangup":
        payload["type"] = "end"
    elif action == "transfer" and action_url:
        payload = {
            "type": "redirect",
            "redirectUrl": action_url,
        }

    return Response(json.dumps(payload), mimetype="application/json")
