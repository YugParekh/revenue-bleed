"""
Quantifier: attaches financial projections to each breach.

Computes:
  rupees_recoverable  — estimated recovery after fix (based on known recovery rates)
  monthly_projection  — annualised at current 24h rate
  fp_cost_rupees      — passed in from metrics, stored here for the UI
"""
import logging
from typing import List, Dict

log = logging.getLogger("quantifier")

# Recovery rate per bug class: fraction of lost revenue recovered after the fix.
# Conservative estimates — some customers don't retry even after a fix.
RECOVERY_RATES: Dict[str, float] = {
    "float_amount_bug":          0.97,   # deterministic code fix → near-complete recovery
    "amount_mismatch_bug":       0.90,
    "webhook_signature_bug":     0.82,   # some payments already abandoned
    "stale_token_bug":           0.80,
    "idempotency_bug":           0.94,
    "api_key_misconfiguration":  0.99,
    "mandate_not_setup":         0.68,   # customer re-auth adds friction
    "currency_misconfiguration": 0.97,
    "malformed_request":         0.88,
    "premature_cancel_bug":      0.75,
    "merchant_integration_error": 0.60,  # unknown — conservative
}


async def quantify_breaches(breaches: List[Dict], all_failures: List[Dict]) -> List[Dict]:
    """
    Attach financial projections to each breach.
    `all_failures` is kept for future per-method breakdown — unused now but
    keeping the signature so callers don't need to change.
    """
    for b in breaches:
        sig    = b.get("bug_signature", "")
        rupees = b.get("rupees_lost", 0.0)
        rate   = RECOVERY_RATES.get(sig, 0.60)

        b["rupees_recoverable"]   = round(rupees * rate, 2)
        b["recovery_rate_pct"]    = round(rate * 100, 1)
        b["monthly_projection"]   = round(rupees * 30, 2)   # 24h window → 30d projection
        b["fp_cost_rupees"]       = 0.0                     # set by metrics.py after eval

    log.info(f"Quantified {len(breaches)} breaches")
    return breaches
