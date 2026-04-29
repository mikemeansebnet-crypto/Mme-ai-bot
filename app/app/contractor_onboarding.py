# -----------------------------------------------
# FILE: app/app/contractor_onboarding.py
# What it does: Handles contractor signup and
# Stripe subscription creation for CrewCachePro
# -----------------------------------------------

import os
import stripe
import requests

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_CONTRACTORS_TABLE = os.environ.get("AIRTABLE_CONTRACTORS_TABLE")

CONTRACTORS_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_CONTRACTORS_TABLE}"
HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

PRICE_IDS = {
    "Basic": os.environ.get("STRIPE_BASIC_PRICE_ID"),
    "Pro": os.environ.get("STRIPE_PRO_PRICE_ID"),
}

# Base URL for redirects — set APP_BASE_URL in Render environment variables
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://mme-ai-bot.onrender.com")


def create_stripe_customer(business_name: str, email: str, phone: str) -> dict:
    """Creates a Stripe customer for a new contractor."""
    try:
        customer = stripe.Customer.create(
            name=business_name,
            email=email,
            phone=phone,
            metadata={"platform": "CrewCachePro"}
        )
        print(f"STRIPE CUSTOMER CREATED | {business_name} | {customer.id}")
        return {"ok": True, "customer_id": customer.id}
    except Exception as e:
        print(f"STRIPE CUSTOMER ERROR | {e}")
        return {"ok": False, "error": str(e)}


# NOTE: create_subscription is available for direct API subscription creation
# but is not used in the main onboarding flow — create_checkout_session is
# the active path. Keep for future use if needed.
def create_subscription(customer_id: str, tier: str) -> dict:
    """Creates a Stripe subscription directly for a contractor (not used in main flow)."""
    try:
        price_id = PRICE_IDS.get(tier)
        if not price_id:
            return {"ok": False, "error": f"No price ID found for tier: {tier}"}

        subscription = stripe.Subscription.create(
            customer=customer_id,
            items=[{"price": price_id}],
            payment_behavior="default_incomplete",
            payment_settings={"save_default_payment_method": "on_subscription"},
            expand=["latest_invoice.payment_intent"],
            metadata={"tier": tier, "platform": "CrewCachePro"}
        )

        client_secret = (
            subscription.latest_invoice.payment_intent.client_secret
            if subscription.latest_invoice and subscription.latest_invoice.payment_intent
            else None
        )

        print(f"STRIPE SUBSCRIPTION CREATED | {customer_id} | {tier} | {subscription.id}")
        return {
            "ok": True,
            "subscription_id": subscription.id,
            "client_secret": client_secret,
            "status": subscription.status
        }
    except Exception as e:
        print(f"STRIPE SUBSCRIPTION ERROR | {e}")
        return {"ok": False, "error": str(e)}


def create_checkout_session(tier: str, business_name: str, email: str, contractor_record_id: str) -> dict:
    """
    Creates a Stripe Checkout session for contractor signup.
    Contractor clicks link, enters card, done.
    """
    try:
        price_id = PRICE_IDS.get(tier)
        if not price_id:
            return {"ok": False, "error": f"No price ID for tier: {tier}"}

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=email,
            metadata={
                "tier": tier,
                "business_name": business_name,
                "contractor_record_id": contractor_record_id,
                "platform": "CrewCachePro"
            },
            # FIXED: Use APP_BASE_URL env var instead of hardcoded URL
            success_url=f"{APP_BASE_URL}/subscription-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_BASE_URL}/subscription-cancel",
        )

        print(f"CHECKOUT SESSION CREATED | {business_name} | {tier} | {session.id}")
        return {"ok": True, "url": session.url, "session_id": session.id}

    except Exception as e:
        print(f"CHECKOUT SESSION ERROR | {e}")
        return {"ok": False, "error": str(e)}


def update_contractor_subscription(record_id: str, customer_id: str, subscription_id: str, tier: str) -> dict:
    """Updates contractor Airtable record with Stripe subscription details."""
    try:
        response = requests.patch(
            f"{CONTRACTORS_URL}/{record_id}",
            headers=HEADERS,
            json={"fields": {
                "Stripe Customer ID": customer_id,
                "Stripe Subscription ID": subscription_id,
                "Subscription Tier": tier,
                # FIXED: Standardized to "Active" — matches subscription_service.py check
                "Subscription Status": "Active"
            }}
        )
        if response.status_code != 200:
            print(f"CONTRACTOR UPDATE FAILED | {record_id} | {response.status_code} | {response.text}")
            return {"ok": False, "error": response.text}
        print(f"CONTRACTOR SUBSCRIPTION UPDATED | {record_id} | {tier}")
        return {"ok": True}
    except Exception as e:
        print(f"CONTRACTOR UPDATE ERROR | {e}")
        return {"ok": False, "error": str(e)}


def cancel_contractor_subscription(subscription_id: str, record_id: str) -> dict:
    """Cancels a contractor's Stripe subscription at period end."""
    try:
        stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=True
        )
        response = requests.patch(
            f"{CONTRACTORS_URL}/{record_id}",
            headers=HEADERS,
            json={"fields": {
                # FIXED: "canceled" — matches Stripe's spelling and subscription_service.py
                "Subscription Status": "canceled"
            }}
        )
        if response.status_code != 200:
            print(f"CANCEL AIRTABLE UPDATE FAILED | {record_id} | {response.status_code}")
            return {"ok": False, "error": response.text}
        print(f"SUBSCRIPTION CANCELLED | {subscription_id} | {record_id}")
        return {"ok": True}
    except Exception as e:
        print(f"SUBSCRIPTION CANCEL ERROR | {e}")
        return {"ok": False, "error": str(e)}


def handle_subscription_event(event: dict) -> dict:
    """
    Handles Stripe subscription webhook events.
    Receives a pre-verified event from the webhook route — no signature check here.
    """
    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    try:
        if event_type == "checkout.session.completed":
            metadata = obj.get("metadata", {})
            contractor_record_id = metadata.get("contractor_record_id")
            tier = metadata.get("tier", "Basic")
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")

            if contractor_record_id and customer_id and subscription_id:
                result = update_contractor_subscription(
                    contractor_record_id, customer_id, subscription_id, tier
                )
                if not result.get("ok"):
                    return {"ok": False, "error": f"Airtable update failed: {result.get('error')}"}
                print(f"CONTRACTOR ONBOARDED | {contractor_record_id} | {tier}")
            else:
                print(f"ONBOARDING INCOMPLETE | Missing fields | record:{contractor_record_id} customer:{customer_id} sub:{subscription_id}")
                return {"ok": False, "error": "Missing metadata fields on checkout.session.completed"}

        elif event_type == "customer.subscription.deleted":
            customer_id = obj.get("customer")
            try:
                params = {"filterByFormula": f"{{Stripe Customer ID}} = '{customer_id}'"}
                response = requests.get(CONTRACTORS_URL, headers=HEADERS, params=params)
                records = response.json().get("records", [])
                for record in records:
                    requests.patch(
                        f"{CONTRACTORS_URL}/{record['id']}",
                        headers=HEADERS,
                        json={"fields": {
                            # FIXED: "canceled" matches Stripe spelling + subscription_service.py
                            "Subscription Status": "canceled",
                            "Subscription Tier": "Basic"
                        }}
                    )
                    print(f"SUBSCRIPTION DELETED | Contractor: {record['id']}")
            except Exception as e:
                print(f"SUBSCRIPTION DELETE WEBHOOK ERROR | {e}")
                return {"ok": False, "error": str(e)}

        elif event_type == "invoice.payment_failed":
            customer_id = obj.get("customer")
            try:
                params = {"filterByFormula": f"{{Stripe Customer ID}} = '{customer_id}'"}
                response = requests.get(CONTRACTORS_URL, headers=HEADERS, params=params)
                records = response.json().get("records", [])
                for record in records:
                    requests.patch(
                        f"{CONTRACTORS_URL}/{record['id']}",
                        headers=HEADERS,
                        json={"fields": {
                            # FIXED: "past_due" matches subscription_service.py check
                            "Subscription Status": "past_due"
                        }}
                    )
                    print(f"PAYMENT FAILED | Contractor: {record['id']}")
            except Exception as e:
                print(f"PAYMENT FAILED WEBHOOK ERROR | {e}")
                return {"ok": False, "error": str(e)}

        else:
            print(f"SUBSCRIPTION EVENT UNHANDLED | {event_type}")

        return {"ok": True}

    except Exception as e:
        print(f"SUBSCRIPTION EVENT HANDLER ERROR | {type(e).__name__} | {e}")
        return {"ok": False, "error": str(e)}


# --------------------------------------------------
# DEPRECATED — do not call this directly anymore.
# Replaced by handle_subscription_event(event).
# --------------------------------------------------
def handle_subscription_webhook(event: dict) -> dict:
    """DEPRECATED: Use handle_subscription_event(event) instead."""
    print("WARNING | handle_subscription_webhook called directly — this is deprecated.")
    return handle_subscription_event(event)
