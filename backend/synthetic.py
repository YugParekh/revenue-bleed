"""
Synthetic generator: the eval core.

Creates a population of failures with planted bugs — answer key known.
Strips the ground truth labels before classification (simulates real inputs).
Compares classifier output against the answer key to compute honest metrics.

This is what makes the metrics table defensible.
"""
import random
import time
import uuid
import logging
from typing import List, Dict, Tuple

from classifier import classify_failures
from diagnoser import build_breach_clusters
from quantifier import quantify_breaches
from reproducer import reproduce_breach
from metrics import compute_metrics

log = logging.getLogger("synthetic")
random.seed(42)

# ── Bug taxonomy with ground truth ─────────────────────────────────────────────
# Each entry maps directly to a classifier rule and a breach definition.
# Count is calibrated so no single class dominates.

PLANTED_BUGS = [
    {
        "sig":   "float_amount_bug",
        "label": "MERCHANT",
        "error_source":      "business",
        "error_step":        "payment_initiation",
        "error_reason":      "invalid_amount",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "The amount must be an integer.",
        "method": "upi",
        "amount_range": (10000, 500000),
        "count": 45,
    },
    {
        "sig":   "amount_mismatch_bug",
        "label": "MERCHANT",
        "error_source":      "business",
        "error_step":        "payment_initiation",
        "error_reason":      "order_amount_mismatch",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "Payment amount does not match order amount.",
        "method": "card",
        "amount_range": (50000, 1000000),
        "count": 28,
    },
    {
        "sig":   "webhook_signature_bug",
        "label": "MERCHANT",
        "error_source":      "business",
        "error_step":        "payment_verification",
        "error_reason":      "invalid_signature",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "Razorpay payment signature verification failed.",
        "method": "netbanking",
        "amount_range": (20000, 300000),
        "count": 19,
    },
    {
        "sig":   "stale_token_bug",
        "label": "MERCHANT",
        "error_source":      "business",
        "error_step":        "payment_authentication",
        "error_reason":      "token_expired",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "The token provided has expired.",
        "method": "card",
        "amount_range": (50000, 200000),
        "count": 22,
    },
    {
        "sig":   "idempotency_bug",
        "label": "MERCHANT",
        "error_source":      "business",
        "error_step":        "order_creation",
        "error_reason":      "duplicate_order",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "An order with the same receipt already exists.",
        "method": "upi",
        "amount_range": (10000, 100000),
        "count": 31,
    },
    {
        "sig":   "mandate_not_setup",
        "label": "MERCHANT",
        "error_source":      "business",
        "error_step":        "payment_initiation",
        "error_reason":      "subscription_not_active",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "Subscription is not in active state.",
        "method": "card",
        "amount_range": (99900, 99900),
        "count": 16,
    },
    {
        "sig":   "malformed_request",
        "label": "MERCHANT",
        "error_source":      "business",
        "error_step":        "payment_initiation",
        "error_reason":      "missing_mandatory_fields",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "The amount field is required.",
        "method": "upi",
        "amount_range": (10000, 200000),
        "count": 12,
    },
    {
        "sig":   "premature_cancel_bug",
        "label": "MERCHANT",
        "error_source":      "business",
        "error_step":        "payment_initiation",
        "error_reason":      "payment_cancelled",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "Payment was cancelled.",
        "method": "card",
        "amount_range": (20000, 400000),
        "count": 14,
    },
    # ── Non-merchant (negative class — classifier must NOT label these MERCHANT)
    {
        "sig":   "_bank_downtime",
        "label": "BANK",
        "error_source":      "gateway",
        "error_step":        "payment_processing",
        "error_reason":      "gateway_error",
        "error_code":        "GATEWAY_ERROR",
        "error_description": "Payment failed due to gateway error.",
        "method": "upi",
        "amount_range": (10000, 500000),
        "count": 60,
    },
    {
        "sig":   "_bank_issuer",
        "label": "BANK",
        "error_source":      "issuer",
        "error_step":        "payment_processing",
        "error_reason":      "bank_not_responding",
        "error_code":        "GATEWAY_ERROR",
        "error_description": "Your bank is currently not responding.",
        "method": "netbanking",
        "amount_range": (10000, 300000),
        "count": 30,
    },
    {
        "sig":   "_customer_otp",
        "label": "CUSTOMER",
        "error_source":      "customer",
        "error_step":        "payment_authentication",
        "error_reason":      "incorrect_otp",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "Payment failed as customer entered incorrect OTP.",
        "method": "netbanking",
        "amount_range": (10000, 300000),
        "count": 85,
    },
    {
        "sig":   "_customer_funds",
        "label": "CUSTOMER",
        "error_source":      "customer",
        "error_step":        "payment_processing",
        "error_reason":      "insufficient_funds",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "Payment failed as customer has insufficient funds.",
        "method": "upi",
        "amount_range": (50000, 500000),
        "count": 72,
    },
    {
        "sig":   "_customer_cancel",
        "label": "CUSTOMER",
        "error_source":      "customer",
        "error_step":        "payment_authentication",
        "error_reason":      "user_cancelled",
        "error_code":        "BAD_REQUEST_ERROR",
        "error_description": "Payment was cancelled by the user.",
        "method": "upi",
        "amount_range": (10000, 200000),
        "count": 40,
    },
]


def _make_failure(bug: Dict, index: int) -> Dict:
    now = int(time.time())
    lo, hi = bug["amount_range"]
    # Ensure integer paise — round to nearest 100
    amount = random.randint(lo // 100, hi // 100) * 100

    return {
        "id":                  f"pay_syn_{bug['sig']}_{index:04d}_{uuid.uuid4().hex[:6]}",
        "amount":              amount,
        "currency":            "INR",
        "method":              bug["method"],
        "status":              "failed",
        "error_code":          bug["error_code"],
        "error_description":   bug["error_description"],
        "error_source":        bug["error_source"],
        "error_step":          bug["error_step"],
        "error_reason":        bug["error_reason"],
        "created_at":          now - random.randint(0, 86400),
        "order_id":            f"order_syn_{uuid.uuid4().hex[:12]}",
        "email":               f"test_{index}@example.com",
        "contact":             f"+9190{random.randint(10000000, 99999999)}",
        # Ground truth — stripped before classification
        "_ground_truth_label": bug["label"],
        "_ground_truth_sig":   bug["sig"],
    }


def generate_population() -> Tuple[List[Dict], Dict[str, Dict]]:
    """
    Build the full synthetic population.
    Returns (failures, answer_key).
    answer_key: payment_id → {true_label, true_sig}
    """
    failures   = []
    answer_key = {}

    for bug in PLANTED_BUGS:
        for i in range(bug["count"]):
            f = _make_failure(bug, len(failures))
            failures.append(f)
            answer_key[f["id"]] = {
                "true_label": bug["label"],
                "true_sig":   bug["sig"],
            }

    random.shuffle(failures)
    log.info(f"Generated {len(failures)} synthetic failures across {len(PLANTED_BUGS)} classes")
    return failures, answer_key


def _strip_ground_truth(failures: List[Dict]) -> List[Dict]:
    """Remove _ground_truth_* keys before passing to classifier."""
    return [{k: v for k, v in f.items() if not k.startswith("_")} for f in failures]


def build_pipeline_feed(classified: List[Dict], limit: int = 200) -> List[Dict]:
    """Build particle data for the animated pipeline UI."""
    now = int(time.time())
    return [
        {
            "id":            f["id"],
            "amount":        f["amount"] / 100,
            "method":        f["method"],
            "attribution":   f.get("attribution", "UNKNOWN"),
            "bug_signature": f.get("bug_signature"),
            "created_at":    f["created_at"],
            "age_seconds":   now - f["created_at"],
        }
        for f in classified[:limit]
    ]


async def run_synthetic_eval() -> Dict:
    """
    Full eval pipeline:
    1. Generate population with planted ground truth
    2. Strip labels → classify → diagnose → quantify
    3. Run reproductions on top breaches
    4. Score against answer key → metrics
    """
    log.info("Generating synthetic population...")
    failures, answer_key = generate_population()
    clean = _strip_ground_truth(failures)

    log.info("Classifying...")
    classified = await classify_failures(clean)

    log.info("Building breach clusters...")
    breaches = await build_breach_clusters(classified)

    for b in breaches:
        b["planted"] = True   # transparency flag for UI

    log.info("Quantifying...")
    breaches = await quantify_breaches(breaches, classified)

    # Update store summary stats
    from store import store
    merchant = sum(1 for f in classified if f.get("attribution") == "MERCHANT")
    bank     = sum(1 for f in classified if f.get("attribution") == "BANK")
    customer = sum(1 for f in classified if f.get("attribution") == "CUSTOMER")
    store["total_failed"]    = len(classified)
    store["merchant_caused"] = merchant
    store["bank_caused"]     = bank
    store["customer_caused"] = customer
    store["rupees_at_risk"]  = sum(b["rupees_lost"] for b in breaches)

    # Run reproductions on top 4 breaches (those with strategies)
    log.info("Running reproductions...")
    for breach in breaches[:4]:
        result = await reproduce_breach(breach)
        breach["reproduction"] = result
        breach["status"] = "confirmed" if result.get("confirmed") else "investigating"
        log.info(f"  {breach['bug_signature']}: {result['status']}")

    log.info("Computing metrics...")
    metrics = compute_metrics(classified, answer_key, breaches)

    log.info(
        f"Synthetic eval done — {len(breaches)} breaches | "
        f"P={metrics.get('overall_precision', 0):.1%} "
        f"R={metrics.get('overall_recall', 0):.1%} "
        f"FP cost=₹{metrics.get('false_positive_cost_rupees', 0):,.0f}"
    )

    return {
        "breaches":      breaches,
        "metrics":       metrics,
        "pipeline_feed": build_pipeline_feed(classified),
    }
