"""
Diagnoser: groups classified MERCHANT failures into named breach clusters.

Each cluster represents one bug class with:
  - occurrence count + rupee value
  - hypothesis (root cause)
  - fix (code-level)
  - reproduction recipe (what API call to fire)
  - severity: CRITICAL / HIGH / MEDIUM / LOW

All bug_signature values used here must appear in BUG_SIGNATURE_MAP in classifier.py.
"""
import time
import uuid
import logging
from collections import defaultdict
from typing import List, Dict

log = logging.getLogger("diagnoser")

# ── Breach definitions ────────────────────────────────────────────────────────
# Keys must match BUG_SIGNATURE_MAP values in classifier.py exactly.

BREACH_DEFINITIONS: Dict[str, Dict] = {
    "float_amount_bug": {
        "title": "Float Amount Bug",
        "description": (
            "Amount sent as decimal rupees (e.g. 999.00) instead of integer paise (99900). "
            "Razorpay's API rejects any non-integer amount."
        ),
        "hypothesis": (
            "Merchant code computes amount = order_value * 100 where order_value is a float, "
            "producing 99900.0 (float) instead of 99900 (int). "
            "Python's * on floats does not auto-truncate."
        ),
        "fix": (
            "# Wrong — produces a float\n"
            "  amount = cart_total * 100\n"
            "\n"
            "# Right — explicit int conversion\n"
            "+ amount = int(round(cart_total * 100))\n"
            "\n"
            "# Also add an assertion on startup\n"
            "+ assert isinstance(amount, int), f'amount must be int, got {type(amount)}'"
        ),
        "reproduction_recipe": {
            "endpoint": "POST /v1/orders",
            "body": {"amount": 99900.0, "currency": "INR"},
            "expected_status": 400,
            "expected_reason": "input_validation_failed",
        },
        "severity": "CRITICAL",
        "docs_link": "https://razorpay.com/docs/api/orders/create/",
    },

    "amount_mismatch_bug": {
        "title": "Order/Payment Amount Mismatch",
        "description": (
            "Payment amount sent to Razorpay checkout differs from the amount in the order. "
            "Typically occurs when the cart is updated after order creation, "
            "or when currency conversion is applied a second time."
        ),
        "hypothesis": (
            "Order created for ₹X (50000p), checkout initiated with a freshly-computed amount ₹Y (55000p). "
            "Razorpay enforces exact equality between order.amount and the checkout amount."
        ),
        "fix": (
            "# Wrong — recalculates amount independently at checkout\n"
            "  options = { amount: computeCartTotal() * 100 }\n"
            "\n"
            "# Right — reads amount from the order object Razorpay returned\n"
            "+ const order = await razorpay.orders.create({ amount: finalAmount })\n"
            "+ options = { order_id: order.id, amount: order.amount }"
        ),
        "reproduction_recipe": {
            "endpoint": "POST /v1/orders",
            "body": {"amount": 50000, "currency": "INR"},
            "note": "Create order for 50000p, then initiate payment for 60000p — triggers mismatch at checkout.",
        },
        "severity": "HIGH",
        "docs_link": "https://razorpay.com/docs/api/orders/",
    },

    "webhook_signature_bug": {
        "title": "Webhook Signature Verification Failing",
        "description": (
            "Merchant's webhook handler computes an invalid HMAC because the request body "
            "has already been parsed (JSON-decoded) before the signature check. "
            "HMAC must be computed on the raw bytes."
        ),
        "hypothesis": (
            "Express/FastAPI JSON middleware decodes req.body before the webhook handler runs. "
            "Re-encoding the parsed JSON produces a different byte sequence than the original, "
            "so the HMAC never matches."
        ),
        "fix": (
            "# Node/Express — use raw body middleware on the webhook route only\n"
            "+ app.post('/webhook', express.raw({ type: 'application/json' }), handler)\n"
            "\n"
            "# Python/FastAPI — read raw bytes, not .json()\n"
            "+ body_bytes = await request.body()   # raw\n"
            "- body = await request.json()         # parsed — breaks HMAC\n"
            "\n"
            "+ razorpay.utility.verify_webhook_signature(\n"
            "+     body_bytes.decode(), signature, webhook_secret\n"
            "+ )"
        ),
        "reproduction_recipe": {
            "endpoint": "WEBHOOK_SIM",
            "note": (
                "POST to your /webhook endpoint with a valid Razorpay payload "
                "but a deliberately wrong X-Razorpay-Signature header. "
                "Your handler should return 400. If it returns 200, the bug is present."
            ),
        },
        "severity": "HIGH",
        "docs_link": "https://razorpay.com/docs/webhooks/validate-test/",
    },

    "stale_token_bug": {
        "title": "Stale/Expired Token Reuse",
        "description": (
            "Merchant caches a card token past its validity window and reuses it on subsequent "
            "recurring charges. In test mode tokens are valid for 3 days; live tokens expire too."
        ),
        "hypothesis": (
            "Token stored at subscription creation is being passed directly to the recurring charge "
            "endpoint without checking expiry. The retry path does not refresh the token."
        ),
        "fix": (
            "# Before each recurring charge, check token validity\n"
            "+ if token.get('dcc_enabled') or is_token_expired(token):\n"
            "+     token = refresh_token(customer_id, subscription_id)\n"
            "\n"
            "+ def is_token_expired(token: dict) -> bool:\n"
            "+     return token.get('expired_at', 0) < int(time.time())"
        ),
        "reproduction_recipe": {
            "endpoint": "POST /v1/payments/create/recurring",
            "body": {"token": "token_EXPIRED_PLACEHOLDER", "amount": 50000, "currency": "INR"},
            "expected_status": 400,
            "expected_reason": "invalid_token",
        },
        "severity": "HIGH",
        "docs_link": "https://razorpay.com/docs/payments/recurring-payments/",
    },

    "idempotency_bug": {
        "title": "Duplicate Order / Idempotency Failure",
        "description": (
            "The same order receipt is submitted multiple times — usually from a retry loop "
            "that doesn't check whether the original request already succeeded. "
            "Razorpay rejects duplicate receipts."
        ),
        "hypothesis": (
            "On network timeout the client retries unconditionally, reusing the same receipt. "
            "The first request may have succeeded server-side; the retry then hits a duplicate error."
        ),
        "fix": (
            "# 1. Check for existing order before creating\n"
            "+ existing = razorpay.orders.all({'receipt': receipt_id})\n"
            "+ if existing['count'] > 0: return existing['items'][0]\n"
            "\n"
            "# 2. Use idempotency key on creation\n"
            "+ razorpay.orders.create(data, headers={'Idempotency-Key': request_id})\n"
            "\n"
            "# 3. On 400 duplicate_order, fetch and return the existing order\n"
            "+ except RazorpayError as e:\n"
            "+     if 'duplicate' in str(e).lower(): return fetch_by_receipt(receipt_id)"
        ),
        "reproduction_recipe": {
            "endpoint": "POST /v1/orders",
            "body": {"amount": 50000, "currency": "INR", "receipt": "SAME_RECEIPT_TWICE"},
            "note": "Fire twice with identical receipt — second call returns 400 duplicate_order.",
        },
        "severity": "MEDIUM",
        "docs_link": "https://razorpay.com/docs/api/orders/create/",
    },

    "api_key_misconfiguration": {
        "title": "API Key Misconfiguration",
        "description": (
            "Wrong API key in use — test key deployed to production, live key in test, "
            "or key corrupted by trailing newline / truncation during env var loading."
        ),
        "hypothesis": (
            "RAZORPAY_KEY_ID loaded from environment with trailing whitespace or newline. "
            "key[:8] looks correct but the full value doesn't match Razorpay's records."
        ),
        "fix": (
            "# Validate key format on startup — fail fast\n"
            "+ key_id = os.getenv('RAZORPAY_KEY_ID', '').strip()\n"
            "+ secret  = os.getenv('RAZORPAY_KEY_SECRET', '').strip()\n"
            "+ assert key_id.startswith('rzp_'), f'Bad key format: {key_id[:12]}'\n"
            "+ assert len(secret) > 20, 'RAZORPAY_KEY_SECRET looks truncated'\n"
            "\n"
            "# Separate test and live keys at the environment level\n"
            "+ KEY_ID = rzp_test_xxx  # .env.test\n"
            "+ KEY_ID = rzp_live_xxx  # .env.production"
        ),
        "reproduction_recipe": {
            "endpoint": "GET /v1/payments",
            "headers": {"Authorization": "Basic DELIBERATELY_INVALID_KEY"},
            "expected_status": 401,
        },
        "severity": "CRITICAL",
        "docs_link": "https://razorpay.com/docs/api/authentication/",
    },

    "mandate_not_setup": {
        "title": "Subscription Mandate Not Authenticated",
        "description": (
            "Recurring charge attempted before the customer completed the mandate authentication "
            "step in Razorpay checkout. The first charge must always flow through checkout."
        ),
        "hypothesis": (
            "Merchant calls the recurring charge API immediately after subscription creation, "
            "without waiting for the subscription.activated webhook. "
            "The customer has never authenticated — no mandate exists."
        ),
        "fix": (
            "# Never charge until subscription.activated webhook is received\n"
            "+ @webhook_handler('subscription.activated')\n"
            "+ def on_activated(payload):\n"
            "+     sub_id = payload['payload']['subscription']['entity']['id']\n"
            "+     mark_subscription_ready(sub_id)  # now safe to charge\n"
            "\n"
            "# Guard in charge function\n"
            "+ if not subscription.is_active():\n"
            "+     raise ValueError('Cannot charge: mandate not authenticated')"
        ),
        "reproduction_recipe": {
            "endpoint": "POST /v1/payments/create/recurring",
            "note": "Attempt a recurring charge on a subscription ID that was never authenticated through checkout.",
        },
        "severity": "HIGH",
        "docs_link": "https://razorpay.com/docs/subscriptions/test-guide/",
    },

    "currency_misconfiguration": {
        "title": "Invalid Currency Code",
        "description": (
            "Payment or order created with a currency code in the wrong format — "
            "lowercase, a symbol, or a non-ISO code. Razorpay requires ISO 4217 uppercase."
        ),
        "hypothesis": (
            "Currency read from a locale/i18n library that returns 'inr' or '₹' "
            "instead of the ISO standard 'INR'."
        ),
        "fix": (
            "# Wrong\n"
            "- currency = locale.getCurrency()   # returns 'inr' or '₹'\n"
            "\n"
            "# Right\n"
            "+ CURRENCY = 'INR'  # hardcode or uppercase-normalise\n"
            "+ currency = raw_currency.upper().strip()"
        ),
        "reproduction_recipe": {
            "endpoint": "POST /v1/orders",
            "body": {"amount": 50000, "currency": "inr"},
            "expected_status": 400,
        },
        "severity": "MEDIUM",
        "docs_link": "https://razorpay.com/docs/api/orders/create/",
    },

    "malformed_request": {
        "title": "Malformed API Request",
        "description": (
            "Request body missing mandatory fields, or fields sent with wrong types or formats. "
            "Razorpay returns input_validation_failed with a field-level message."
        ),
        "hypothesis": (
            "Optional chaining or incomplete form data causes required fields (amount, currency, receipt) "
            "to be undefined/null when the API call is made."
        ),
        "fix": (
            "# Validate before calling Razorpay\n"
            "+ REQUIRED = ['amount', 'currency', 'receipt']\n"
            "+ missing = [k for k in REQUIRED if not payload.get(k)]\n"
            "+ if missing: raise ValueError(f'Missing fields: {missing}')\n"
            "\n"
            "# Use a schema validator (Pydantic, Joi, Zod) on order creation"
        ),
        "reproduction_recipe": {
            "endpoint": "POST /v1/orders",
            "body": {"currency": "INR"},  # missing amount and receipt
            "expected_status": 400,
        },
        "severity": "MEDIUM",
        "docs_link": "https://razorpay.com/docs/errors/common/",
    },

    "premature_cancel_bug": {
        "title": "Premature Payment Cancel",
        "description": (
            "Merchant cancels a payment before the customer completes it — "
            "usually from a server-side timeout that fires too early, "
            "or a double-submit handler that cancels the in-flight payment."
        ),
        "hypothesis": (
            "Server-side timeout (e.g. 10s) is shorter than Razorpay's checkout session (10 min). "
            "The server cancels the order, but the customer is still on the payment page."
        ),
        "fix": (
            "# Don't cancel until Razorpay's session expires (10 min default)\n"
            "- TIMEOUT = 10   # seconds — too short\n"
            "+ TIMEOUT = 700  # seconds — just over Razorpay's 10 min window\n"
            "\n"
            "# Or listen to payment.failed webhook instead of polling\n"
            "+ @webhook_handler('payment.failed')\n"
            "+ def on_failed(payload): cancel_order(payload['order_id'])"
        ),
        "reproduction_recipe": None,
        "severity": "MEDIUM",
        "docs_link": "https://razorpay.com/docs/payments/",
    },

    "merchant_integration_error": {
        "title": "Unknown Integration Error",
        "description": (
            "Merchant-attributed failure that doesn't match any specific known signature. "
            "Requires manual review of the raw error payload."
        ),
        "hypothesis": "Unclassified merchant-side error. Review error_reason and error_step.",
        "fix": (
            "# Enable detailed logging on Razorpay API calls\n"
            "+ logging.info('Razorpay error: %s', error_response)\n"
            "\n"
            "# Check Razorpay docs for the specific error_reason returned"
        ),
        "reproduction_recipe": None,
        "severity": "LOW",
        "docs_link": "https://razorpay.com/docs/errors/",
    },
}


def severity_rank(s: str) -> int:
    return {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(s, 0)


async def build_breach_clusters(failures: List[Dict]) -> List[Dict]:
    """
    Group MERCHANT-attributed failures by bug_signature → one breach card each.
    Returns list sorted by (severity desc, rupees desc).
    """
    merchant_failures = [f for f in failures if f.get("attribution") == "MERCHANT"]
    if not merchant_failures:
        log.info("No MERCHANT failures to cluster.")
        return []

    groups: Dict[str, List[Dict]] = defaultdict(list)
    for f in merchant_failures:
        sig = f.get("bug_signature") or "merchant_integration_error"
        # Ensure sig has a definition — fall back gracefully
        if sig not in BREACH_DEFINITIONS:
            log.warning(f"Unknown sig '{sig}' — routing to merchant_integration_error")
            sig = "merchant_integration_error"
        groups[sig].append(f)

    breaches = []
    for sig, members in groups.items():
        defn         = BREACH_DEFINITIONS[sig]
        total_paise  = sum(m.get("amount", 0) for m in members)
        rupees_lost  = round(total_paise / 100, 2)

        breach = {
            "id":               f"breach_{sig}_{uuid.uuid4().hex[:8]}",
            "bug_signature":    sig,
            "title":            defn["title"],
            "description":      defn["description"],
            "hypothesis":       defn["hypothesis"],
            "fix":              defn["fix"],
            "severity":         defn["severity"],
            "severity_rank":    severity_rank(defn["severity"]),
            "docs_link":        defn["docs_link"],
            "occurrence_count": len(members),
            "rupees_lost":      rupees_lost,
            "affected_payment_ids": [m["id"] for m in members if m.get("id")],
            "methods":          sorted({m.get("method", "unknown") for m in members}),
            "sparkline":        _build_sparkline(members),
            "status":           "investigating",
            "planted":          False,
            "reproduction": {
                "status":    "pending",
                "confirmed": False,
                "evidence":  None,
                "calls_used": 0,
                "audit_log":  [],
                "recipe":    defn.get("reproduction_recipe"),
            },
        }
        breaches.append(breach)

    breaches.sort(key=lambda b: (b["severity_rank"], b["rupees_lost"]), reverse=True)
    log.info(f"Built {len(breaches)} breach clusters from {len(merchant_failures)} failures")
    return breaches


def _build_sparkline(members: List[Dict]) -> List[int]:
    """24-bucket hourly failure count (oldest → newest)."""
    now    = int(time.time())
    hour   = 3600
    buckets = [0] * 24
    for m in members:
        ts        = m.get("created_at", 0)
        age_hours = int((now - ts) / hour)
        if 0 <= age_hours < 24:
            buckets[23 - age_hours] += 1   # index 23 = most recent hour
    return buckets
