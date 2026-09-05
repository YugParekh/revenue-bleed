"""
Shared in-memory store.
All keys documented here. For production: replace with Redis + asyncio.Lock per key.
"""
from typing import Any, Dict

store: Dict[str, Any] = {
    "bootstrapped":    False,
    "_bootstrapping":  False,
    "breaches":        [],          # List[Dict] — breach clusters
    "metrics":         {},          # Dict — eval metrics
    "pipeline_feed":   [],          # List[Dict] — particle data for UI
    "total_failed":    0,
    "merchant_caused": 0,
    "bank_caused":     0,
    "customer_caused": 0,
    "rupees_at_risk":  0.0,
}
