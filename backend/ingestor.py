"""
Ingestor: pulls real failed payment records from Razorpay test-mode API.

Normalises the raw Razorpay payment object into the schema the rest of
the pipeline expects. Returns an empty list (not an error) if keys are
not configured or test mode has no failed payments yet.
"""
import os
import base64
import logging
from typing import List, Dict

import httpx

log = logging.getLogger("ingestor")

RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
BASE_URL            = "https://api.razorpay.com/v1"
TIMEOUT             = 20   # seconds


def _keys_configured() -> bool:
    return bool(
        RAZORPAY_KEY_ID
        and RAZORPAY_KEY_SECRET
        and not RAZORPAY_KEY_ID.startswith("rzp_test_YOUR")
        and len(RAZORPAY_KEY_SECRET) > 10
    )


def _auth_header() -> str:
    raw = f"{RAZORPAY_KEY_ID.strip()}:{RAZORPAY_KEY_SECRET.strip()}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


async def _fetch_raw(count: int) -> List[Dict]:
    """Pull up to `count` payments from Razorpay, filtered to failed status."""
    headers = {"Authorization": _auth_header()}
    # Razorpay returns max 100 per page; fetch in two pages if count > 100
    pages   = []
    fetched = 0
    skip    = 0
    page_size = min(count, 100)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        while fetched < count:
            params = {"count": page_size, "skip": skip}
            try:
                resp = await client.get(f"{BASE_URL}/payments", headers=headers, params=params)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                log.error(f"Razorpay API error {e.response.status_code}: {e.response.text[:200]}")
                break
            except httpx.RequestError as e:
                log.error(f"Razorpay request failed: {e}")
                break

            data  = resp.json()
            items = data.get("items", [])
            if not items:
                break

            pages.extend(items)
            fetched += len(items)
            skip    += len(items)
            if len(items) < page_size:
                break   # last page

    return [p for p in pages if p.get("status") == "failed"]


def _normalise(p: Dict) -> Dict:
    """Convert raw Razorpay payment object → pipeline schema."""
    return {
        "id":                  p.get("id"),
        "amount":              p.get("amount", 0),         # integer paise
        "currency":            p.get("currency", "INR"),
        "method":              p.get("method", "unknown"),
        "status":              p.get("status"),
        "error_code":          p.get("error_code") or "",
        "error_description":   p.get("error_description") or "",
        "error_source":        p.get("error_source") or "",
        "error_step":          p.get("error_step") or "",
        "error_reason":        p.get("error_reason") or "",
        "created_at":          p.get("created_at", 0),
        "order_id":            p.get("order_id"),
        "email":               p.get("email") or "",
        "contact":             p.get("contact") or "",
    }


async def ingest_failures(count: int = 200) -> List[Dict]:
    """
    Main entry point. Returns a list of normalised failed payments.
    Returns [] if keys are not configured or API call fails.
    """
    if not _keys_configured():
        log.info("Razorpay keys not configured — skipping real-data fetch")
        return []

    log.info(f"Fetching up to {count} failed payments from Razorpay test mode...")
    raw = await _fetch_raw(count)
    normalised = [_normalise(p) for p in raw if p.get("id")]
    log.info(f"Ingestor: {len(raw)} total failed, {len(normalised)} normalised")
    return normalised
