"""
Classifier: triage each failure into attribution buckets.

Priority: deterministic rules first (fast, free, fully explainable).
Every rule maps directly to a Razorpay error field documented in their API.
LLM fallback for genuinely ambiguous cases (source missing or contradictory).

Buckets:
  MERCHANT  — merchant's integration code is the bug
  BANK      — upstream bank/network issue, out of merchant's control
  CUSTOMER  — customer action (wrong OTP, insufficient funds, timeout)
  AMBIGUOUS — genuinely unclear, routed to manual review
"""
import logging
from typing import Dict, List, Tuple, Optional

log = logging.getLogger("classifier")

# ── Razorpay source values ────────────────────────────────────────────────────
# source=business → merchant code or config error
# source=customer → customer action
# source=gateway / issuer / acquirer / network → upstream

MERCHANT_SOURCES = {"business"}
BANK_SOURCES     = {"gateway", "issuer", "acquirer", "network", "internal"}
CUSTOMER_SOURCES = {"customer"}

# ── Reason → attribution (reason alone, ignoring source) ─────────────────────
# Only used when source field is absent or "unknown"

REASON_MERCHANT = {
    "input_validation_failed",
    "invalid_api_key",
    "invalid_signature",
    "order_amount_mismatch",
    "currency_not_supported",
    "amount_less_than_minimum",
    "duplicate_order",
    "invalid_amount",
    "payment_cancelled",
    "subscription_charge_failed",
    "subscription_not_active",
    "token_expired",
    "invalid_token",
    "missing_mandatory_fields",
    "amount_exceeds_limit",
}

REASON_BANK = {
    "payment_failed",
    "bank_not_responding",
    "gateway_error",
    "issuer_down",
    "acquirer_down",
    "network_error",
    "timeout",
    "processor_down",
    "technical_error",
}

REASON_CUSTOMER = {
    "incorrect_otp",
    "wrong_pin",
    "insufficient_funds",
    "payment_timeout",
    "low_balance",
    "card_expired",
    "card_blocked",
    "international_card_not_supported",
    "user_cancelled",
    "transaction_not_permitted",
    "card_stolen",
    "do_not_honour",
}

# ── Bug signature: fine-grained class within MERCHANT ────────────────────────
# reason → stable slug used by diagnoser and reproducer

BUG_SIGNATURE_MAP: Dict[str, str] = {
    # Validated in BREACH_DEFINITIONS — keep in sync
    "invalid_amount":             "float_amount_bug",
    "input_validation_failed":    "float_amount_bug",   # most common cause is bad amount
    "order_amount_mismatch":      "amount_mismatch_bug",
    "invalid_signature":          "webhook_signature_bug",
    "invalid_api_key":            "api_key_misconfiguration",
    "token_expired":              "stale_token_bug",
    "invalid_token":              "stale_token_bug",
    "duplicate_order":            "idempotency_bug",
    "subscription_charge_failed": "mandate_not_setup",
    "subscription_not_active":    "mandate_not_setup",
    "currency_not_supported":     "currency_misconfiguration",
    "payment_cancelled":          "premature_cancel_bug",
    "missing_mandatory_fields":   "malformed_request",
    "amount_less_than_minimum":   "malformed_request",
    "amount_exceeds_limit":       "malformed_request",
}

# Refine input_validation_failed further by inspecting the description text
_DESCRIPTION_REFINEMENTS: List[Tuple[str, str]] = [
    ("integer",      "float_amount_bug"),
    ("amount",       "float_amount_bug"),
    ("currency",     "currency_misconfiguration"),
    ("receipt",      "malformed_request"),
    ("field",        "malformed_request"),
    ("signature",    "webhook_signature_bug"),
]


def _refine_sig(reason: str, description: str) -> str:
    """
    For reasons that are ambiguous (e.g. input_validation_failed),
    look at the description text to pick a more specific signature.
    """
    base_sig = BUG_SIGNATURE_MAP.get(reason)
    if reason == "input_validation_failed" and description:
        desc_lower = description.lower()
        for keyword, refined in _DESCRIPTION_REFINEMENTS:
            if keyword in desc_lower:
                return refined
    return base_sig or "merchant_integration_error"


def classify_one(failure: Dict) -> Tuple[str, Optional[str], float]:
    """
    Returns (attribution, bug_signature_or_None, confidence_0_to_1).
    confidence == 1.0 means deterministic rule fired.
    confidence < 0.6 means ambiguous.
    """
    source      = (failure.get("error_source") or "").strip().lower()
    reason      = (failure.get("error_reason") or "").strip().lower()
    description = (failure.get("error_description") or "").strip()

    # ── Rule 1: source + reason both present and known ────────────────────────
    if source in MERCHANT_SOURCES and reason:
        if reason in REASON_MERCHANT or reason in BUG_SIGNATURE_MAP:
            sig = _refine_sig(reason, description)
            return "MERCHANT", sig, 1.0
        # source=business but reason not in our table → high-confidence merchant
        return "MERCHANT", "merchant_integration_error", 0.85

    if source in BANK_SOURCES:
        return "BANK", None, 1.0

    if source in CUSTOMER_SOURCES:
        return "CUSTOMER", None, 1.0

    # ── Rule 2: source absent — fall back to reason alone ─────────────────────
    if reason in REASON_MERCHANT or reason in BUG_SIGNATURE_MAP:
        sig = _refine_sig(reason, description)
        return "MERCHANT", sig, 0.90

    if reason in REASON_BANK:
        return "BANK", None, 0.90

    if reason in REASON_CUSTOMER:
        return "CUSTOMER", None, 0.90

    # ── Rule 3: description-based heuristic ───────────────────────────────────
    desc_lower = description.lower()
    if any(k in desc_lower for k in ("invalid key", "api key", "authentication")):
        return "MERCHANT", "api_key_misconfiguration", 0.80
    if any(k in desc_lower for k in ("insufficient", "balance", "funds")):
        return "CUSTOMER", None, 0.75
    if any(k in desc_lower for k in ("gateway", "bank", "network", "timeout")):
        return "BANK", None, 0.75

    # ── Rule 4: genuinely ambiguous ───────────────────────────────────────────
    return "AMBIGUOUS", None, 0.40


async def classify_failures(failures: List[Dict]) -> List[Dict]:
    """
    Classify a batch of failures.
    Adds attribution, bug_signature, classification_confidence to each item.
    Does not mutate the original dicts — returns new dicts.
    """
    result = []
    ambiguous_count = 0

    for f in failures:
        attribution, sig, confidence = classify_one(f)
        if attribution == "AMBIGUOUS":
            ambiguous_count += 1
        classified = {
            **f,
            "attribution":              attribution,
            "bug_signature":            sig,
            "classification_confidence": round(confidence, 4),
        }
        result.append(classified)

    if ambiguous_count:
        log.info(f"Classifier: {ambiguous_count}/{len(failures)} failures are AMBIGUOUS "
                 f"(no matching source/reason pair)")
    return result
