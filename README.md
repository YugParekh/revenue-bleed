# Revenue Bleed — Merchant War Room
live link:https://revenue-bleed.vercel.app/

> Merchants blame their payment gateway for failures their own code caused.  
> This agent proves which is which — by reproducing the bug.

Built for Razorpay AI Buildathon 2026 — Track 03: AI Revenue Recovery.

---

## What it does

Razorpay's error responses tag every failure with `source`, `step`, and `reason`.  
A significant share of failures tagged `source: business` are merchant integration bugs —  
but merchants see a failure count, blame the PSP, and file a support ticket.

Revenue Bleed:
1. **Ingests** failed payments from Razorpay test-mode API
2. **Classifies** each failure: MERCHANT / BANK / CUSTOMER — deterministic rules first, LLM for ambiguous
3. **Clusters** merchant-caused failures into named bug signatures
4. **Reproduces** each hypothesis by firing a minimal test-mode API call — confirms or discards
5. **Quantifies** the rupee value, monthly projection, and recovery rate per bug
6. **Reports** precision, recall, false-positive cost, and reproduction rate on a planted ground-truth dataset

---

## Setup

```bash
# 1. Clone and enter
cd revenue-bleed/backend

# 2. Create env file
cp .env.example .env
# Edit .env — add your Razorpay test keys

# 3. Install
pip install -r requirements.txt

# 4. Run
uvicorn main:app --reload --port 8000
```

Then open `frontend/index.html` in your browser (or serve with `python -m http.server 3000` from the frontend folder).

---

## Architecture

```
frontend/index.html          → War room UI (vanilla JS, no build step)
backend/
  main.py                    → FastAPI app, routes
  ingestor.py                → Razorpay test-mode API → normalised failures
  classifier.py              → Deterministic triage + LLM fallback
  diagnoser.py               → Cluster → breach cards
  reproducer.py              → Hypothesis → test-mode call → confirm/discard
  quantifier.py              → Rupee projections per breach
  synthetic.py               → Planted-bug population + eval pipeline
  metrics.py                 → Precision, recall, FP cost, ablation
  store.py                   → In-memory state (swap for Redis in prod)
```

---

## Metrics (synthetic eval, 358 failures, 9 planted bug classes)

| | Precision | Recall | F1 |
|---|---|---|---|
| MERCHANT | 100% | 100% | 100% |
| BANK | 100% | 100% | 100% |
| CUSTOMER | 100% | 100% | 100% |

- **False-positive cost**: ₹0 (no bank/customer failures mis-attributed to merchant)
- **Merchant-caused share**: 42.6% of all failures
- **Revenue identified**: ₹3,42,179 in 24h window
- **Reproduction rate**: Updates once real test keys are configured

> Metrics are 100% on synthetic data because the classification rules are deterministic
> and the planted bugs map exactly to the rule table. Real-world accuracy will be lower
> (ambiguous cases, missing error fields). The ablation table shows which cases the LLM
> tier handles vs deterministic rules.

---

## Bug signatures detected

| Bug | Severity | Reproduction |
|---|---|---|
| Float amount (sent 999.00 not 99900) | CRITICAL | Automated |
| Order/payment amount mismatch | HIGH | Automated |
| Webhook signature verification failing | HIGH | Manual |
| Stale/expired token reuse | HIGH | Automated |
| Duplicate order / idempotency failure | MEDIUM | Automated |
| API key misconfiguration | CRITICAL | Automated |
| Subscription mandate not authenticated | HIGH | Manual |
| Currency code misconfiguration | MEDIUM | Automated |

---

## Bounded actions

- Reproducer only calls endpoints in a hard allowlist (POST /v1/orders, GET /v1/payments)
- Max 10 API calls per reproduction run
- All calls go to test-mode only — no live API access
- Full audit log of every call made, with inputs, outputs, and confidence
- Fixes emitted as patch suggestions only — no automated code modification
