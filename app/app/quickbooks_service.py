"""
QuickBooks Online Integration for ContractorOS / CrewCachePro
Handles OAuth2 connection, token management, and invoice creation.
"""

import os
import json
import time
import requests
from base64 import b64encode

# ─────────────────────────────────────────────
# QuickBooks OAuth2 Config
# ─────────────────────────────────────────────

QB_CLIENT_ID     = os.getenv("QB_CLIENT_ID", "")
QB_CLIENT_SECRET = os.getenv("QB_CLIENT_SECRET", "")
QB_REDIRECT_URI  = os.getenv("QB_REDIRECT_URI", "")
QB_ENVIRONMENT   = os.getenv("QB_ENVIRONMENT", "production")

QB_AUTH_URL      = "https://appcenter.intuit.com/connect/oauth2"
QB_TOKEN_URL     = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QB_REVOKE_URL    = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
QB_SCOPES        = "com.intuit.quickbooks.accounting"

if QB_ENVIRONMENT == "sandbox":
    QB_API_BASE = "https://sandbox-quickbooks.api.intuit.com/v3/company"
else:
    QB_API_BASE = "https://quickbooks.api.intuit.com/v3/company"


# ─────────────────────────────────────────────
# Token Storage (Redis)
# ─────────────────────────────────────────────

def _get_redis():
    try:
        from app.app.config import redis_client
        return redis_client
    except Exception:
        return None


def save_qb_tokens(realm_id: str, access_token: str, refresh_token: str, expires_in: int = 3600):
    """Save QuickBooks tokens to Redis."""
    rc = _get_redis()
    if not rc:
        print("QB TOKEN SAVE ERROR | no redis client")
        return
    token_data = {
        "realm_id":     realm_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at":   int(time.time()) + expires_in,
    }
    rc.set("qb_tokens", json.dumps(token_data))
    print("QB TOKENS SAVED | realm_id:", realm_id)


def get_qb_tokens() -> dict | None:
    """Get QuickBooks tokens from Redis."""
    rc = _get_redis()
    if not rc:
        return None
    raw = rc.get("qb_tokens")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def get_valid_access_token() -> tuple[str, str] | tuple[None, None]:
    """
    Returns (access_token, realm_id) — refreshing if expired.
    Returns (None, None) if not connected.
    """
    tokens = get_qb_tokens()
    if not tokens:
        print("QB NOT CONNECTED | no tokens found")
        return None, None

    realm_id = tokens.get("realm_id", "")

    # Check if access token is still valid (with 60s buffer)
    if int(time.time()) < tokens.get("expires_at", 0) - 60:
        return tokens["access_token"], realm_id

    # Refresh the token
    print("QB TOKEN EXPIRED | refreshing...")
    refreshed = refresh_qb_token(tokens["refresh_token"])
    if refreshed:
        return refreshed["access_token"], realm_id

    print("QB TOKEN REFRESH FAILED | reconnect required")
    return None, None


def refresh_qb_token(refresh_token: str) -> dict | None:
    """Refresh QuickBooks access token using refresh token."""
    credentials = b64encode(f"{QB_CLIENT_ID}:{QB_CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type":  "application/x-www-form-urlencoded",
        "Accept":        "application/json",
    }
    data = {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }
    try:
        r = requests.post(QB_TOKEN_URL, headers=headers, data=data, timeout=15)
        if r.status_code != 200:
            print("QB REFRESH ERROR |", r.status_code, r.text)
            return None
        token_data = r.json()
        tokens = get_qb_tokens() or {}
        save_qb_tokens(
            realm_id=tokens.get("realm_id", ""),
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", refresh_token),
            expires_in=token_data.get("expires_in", 3600),
        )
        return token_data
    except Exception as e:
        print("QB REFRESH EXCEPTION |", e)
        return None


# ─────────────────────────────────────────────
# QuickBooks Customer Lookup / Create
# ─────────────────────────────────────────────

def find_or_create_qb_customer(access_token: str, realm_id: str, name: str, phone: str = "", address: str = "", email: str = "") -> str | None:
    """
    Find existing QuickBooks customer by name or create new one.
    Returns QuickBooks customer ID or None on failure.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }

    # Search for existing customer
    try:
        query = f"SELECT * FROM Customer WHERE DisplayName = '{name}' MAXRESULTS 1"
        r = requests.get(
            f"{QB_API_BASE}/{realm_id}/query",
            headers=headers,
            params={"query": query, "minorversion": "65"},
            timeout=15,
        )
        if r.status_code == 200:
            customers = r.json().get("QueryResponse", {}).get("Customer", [])
            if customers:
                cust_id = customers[0]["Id"]
                print("QB CUSTOMER FOUND |", name, "| ID:", cust_id)
                return cust_id
    except Exception as e:
        print("QB CUSTOMER SEARCH ERROR |", e)

    # Parse address
    addr_parts = address.split(",") if address else []
    street = addr_parts[0].strip() if len(addr_parts) > 0 else ""
    city   = addr_parts[1].strip() if len(addr_parts) > 1 else ""
    state_zip = addr_parts[2].strip().split() if len(addr_parts) > 2 else []
    state  = state_zip[0] if len(state_zip) > 0 else "MD"
    zipcode = state_zip[1] if len(state_zip) > 1 else ""

    # Create new customer
    customer_payload = {
        "DisplayName": name,
        "PrimaryPhone": {"FreeFormNumber": phone} if phone else None,
        "PrimaryEmailAddr": {"Address": email} if email else None,
        "BillAddr": {
            "Line1": street,
            "City":  city,
            "CountrySubDivisionCode": state,
            "PostalCode": zipcode,
            "Country": "US",
        } if street else None,
    }
    customer_payload = {k: v for k, v in customer_payload.items() if v is not None}

    try:
        r = requests.post(
            f"{QB_API_BASE}/{realm_id}/customer",
            headers=headers,
            params={"minorversion": "65"},
            json=customer_payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            cust_id = r.json()["Customer"]["Id"]
            print("QB CUSTOMER CREATED |", name, "| ID:", cust_id)
            return cust_id
        else:
            print("QB CUSTOMER CREATE ERROR |", r.status_code, r.text)
            return None
    except Exception as e:
        print("QB CUSTOMER CREATE EXCEPTION |", e)
        return None


# ─────────────────────────────────────────────
# Create QuickBooks Invoice
# ─────────────────────────────────────────────

def create_qb_invoice(state: dict) -> dict:
    access_token, realm_id = get_valid_access_token()
    if not access_token:
        return {"ok": False, "error": "QuickBooks not connected"}

    name        = state.get("name", "Unknown Customer")
    address     = state.get("service_address", "")
    job_desc    = state.get("job_description", "")
    callback    = state.get("callback", "")
    timing      = state.get("timing", "")
    email       = state.get("client_email", "")
    amount      = float(state.get("estimate_amount") or 0.00)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }

    customer_id = find_or_create_qb_customer(
        access_token, realm_id, name, callback, address, email
    )
    if not customer_id:
        return {"ok": False, "error": "Could not find or create QuickBooks customer"}

    invoice_payload = {
        "CustomerRef": {"value": customer_id},
        "BillAddr":    {"Line1": address},
        "BillEmail":   {"Address": email} if email else None,
        "CustomerMemo": {"value": f"Job: {job_desc}\nTiming: {timing}\nAddress: {address}"},
        "Line": [
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": amount,
                "Description": job_desc,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "1", "name": "Services"},
                    "Qty": 1,
                    "UnitPrice": amount,
                },
            }
        ],
        "DocNumber":   f"INV-{int(time.time())}",
        "PrivateNote": f"Auto-created by CrewCachePro. Callback: {callback}",
    }

    # Remove None values
    invoice_payload = {k: v for k, v in invoice_payload.items() if v is not None}

    try:
        r = requests.post(
            f"{QB_API_BASE}/{realm_id}/invoice",
            headers=headers,
            params={"minorversion": "65"},
            json=invoice_payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            invoice = r.json().get("Invoice", {})
            invoice_id  = invoice.get("Id", "")
            invoice_num = invoice.get("DocNumber", "")
            print("QB INVOICE CREATED | ID:", invoice_id, "| Num:", invoice_num, "| Customer:", name, "| Amount: $", amount)
            return {
                "ok":          True,
                "invoice_id":  invoice_id,
                "invoice_num": invoice_num,
                "customer_id": customer_id,
                "amount":      amount,
            }
        else:
            print("QB INVOICE CREATE ERROR |", r.status_code, r.text)
            return {"ok": False, "error": f"QB API error: {r.status_code}"}
    except Exception as e:
        print("QB INVOICE CREATE EXCEPTION |", e)
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────
# Connection Status Check
# ─────────────────────────────────────────────

def is_qb_connected() -> bool:
    """Check if QuickBooks is connected and tokens exist."""
    tokens = get_qb_tokens()
    return bool(tokens and tokens.get("refresh_token"))
