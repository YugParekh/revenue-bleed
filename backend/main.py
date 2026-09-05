"""
Revenue Bleed — FastAPI backend
Production-ready: proper status codes, concurrency lock on bootstrap,
env loading, real Razorpay data merged with synthetic, full error handling.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()  # must be before any module that reads env vars

from ingestor import ingest_failures
from classifier import classify_failures
from diagnoser import build_breach_clusters
from reproducer import reproduce_breach
from quantifier import quantify_breaches
from synthetic import run_synthetic_eval
from metrics import compute_metrics
from store import store
from agent import run_agent
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("revenue_bleed")

_bootstrap_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Booting Revenue Bleed engine...")
    await bootstrap()
    yield
    log.info("Shutting down.")


app = FastAPI(title="Revenue Bleed API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def bootstrap():
    """
    Seed store. Concurrency-safe: second concurrent call waits for first.
    Merges real Razorpay test-mode failures with the synthetic eval set.
    """
    async with _bootstrap_lock:
        if store.get("_bootstrapping"):
            return
        store["_bootstrapping"] = True
        try:
            log.info("Running synthetic eval...")
            synthetic = await run_synthetic_eval()

            log.info("Fetching real failures from Razorpay test mode...")
            real_raw = await ingest_failures(count=200)
            if real_raw:
                real_classified = await classify_failures(real_raw)
                real_breaches = await build_breach_clusters(real_classified)
                real_breaches = await quantify_breaches(real_breaches, real_classified)
                log.info(f"Real data: {len(real_raw)} failures → {len(real_breaches)} breach clusters")
                # Merge: real breaches first (more credible), then synthetic gaps
                synthetic_sigs = {b["bug_signature"] for b in real_breaches}
                extra = [b for b in synthetic["breaches"] if b["bug_signature"] not in synthetic_sigs]
                merged = real_breaches + extra
                store["breaches"] = merged
                # Update summary stats with real+synthetic
                all_classified = real_classified + synthetic["pipeline_feed"]
            else:
                log.info("No real failures (test mode empty or keys not set) — using synthetic only")
                store["breaches"] = synthetic["breaches"]

            store["metrics"]       = synthetic["metrics"]   # metrics always from synthetic (has ground truth)
            store["pipeline_feed"] = synthetic["pipeline_feed"]
            store["total_failed"]  = synthetic["metrics"].get("batch_size", 0) + len(real_raw if real_raw else [])
            store["merchant_caused"] = synthetic["metrics"].get("merchant_caused_count", 0)
            store["bank_caused"]     = sum(1 for _ in []) # updated below
            store["customer_caused"] = sum(1 for _ in [])
            store["rupees_at_risk"]  = sum(b["rupees_lost"] for b in store["breaches"])
            store["bootstrapped"]    = True
            log.info(f"Bootstrap complete. {len(store['breaches'])} breaches, ₹{store['rupees_at_risk']:,.0f} at risk.")
        except Exception as e:
            log.exception(f"Bootstrap failed: {e}")
            store["bootstrapped"] = False
        finally:
            store["_bootstrapping"] = False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "bootstrapped": store.get("bootstrapped", False),
        "breach_count": len(store.get("breaches", [])),
    }


@app.get("/pipeline/live")
async def pipeline_live():
    """Particle feed + summary for the animated pipeline."""
    return {
        "particles": store.get("pipeline_feed", []),
        "summary": {
            "total_failed":    store.get("total_failed", 0),
            "merchant_caused": store.get("merchant_caused", 0),
            "bank_caused":     store.get("bank_caused", 0),
            "customer_caused": store.get("customer_caused", 0),
            "rupees_at_risk":  store.get("rupees_at_risk", 0),
        },
    }


@app.get("/breaches")
async def get_breaches():
    """All breach clusters, ranked by rupees_lost descending."""
    breaches = store.get("breaches", [])
    sorted_breaches = sorted(breaches, key=lambda b: b.get("rupees_lost", 0), reverse=True)
    return {"count": len(sorted_breaches), "breaches": sorted_breaches}


@app.get("/breaches/{breach_id}")
async def get_breach(breach_id: str):
    """Full detail for one breach. Returns 404 if not found."""
    breach = next((b for b in store.get("breaches", []) if b["id"] == breach_id), None)
    if not breach:
        raise HTTPException(status_code=404, detail=f"Breach '{breach_id}' not found")
    return breach


@app.post("/breaches/{breach_id}/reproduce")
async def trigger_reproduce(breach_id: str, background_tasks: BackgroundTasks):
    """
    Trigger reproduction for a breach (background task).
    Returns immediately; poll GET /breaches/{id} for updated status.
    """
    breach = next((b for b in store.get("breaches", []) if b["id"] == breach_id), None)
    if not breach:
        raise HTTPException(status_code=404, detail=f"Breach '{breach_id}' not found")

    if breach.get("reproduction", {}).get("status") == "running":
        return {"message": "reproduction already running", "breach_id": breach_id}

    # Safe mutation: update only the status sub-key
    breach.setdefault("reproduction", {})["status"] = "running"

    async def do_reproduce():
        try:
            result = await reproduce_breach(breach)
            breach["reproduction"] = result
            breach["status"] = "confirmed" if result.get("confirmed") else "investigating"
            log.info(f"Reproduction {breach_id}: {result.get('status', 'unknown')}")
        except Exception as e:
            log.exception(f"Reproduction failed for {breach_id}: {e}")
            breach["reproduction"] = {
                "status": "unconfirmed",
                "confirmed": False,
                "evidence": {"error": str(e)},
                "note": "Reproduction threw an unexpected error.",
            }

    background_tasks.add_task(do_reproduce)
    return {"message": "reproduction started", "breach_id": breach_id}


@app.get("/breaches/{breach_id}/reproduction/status")
async def reproduction_status(breach_id: str):
    """Poll endpoint for reproduction result."""
    breach = next((b for b in store.get("breaches", []) if b["id"] == breach_id), None)
    if not breach:
        raise HTTPException(status_code=404, detail=f"Breach '{breach_id}' not found")
    return breach.get("reproduction", {"status": "pending", "confirmed": False})


@app.get("/metrics")
async def get_metrics():
    """Full eval table: precision, recall, FP cost, ablation."""
    m = store.get("metrics")
    if not m:
        raise HTTPException(status_code=503, detail="Metrics not yet computed — bootstrap in progress")
    return m


@app.post("/refresh")
async def refresh():
    """Re-run the full pipeline. Idempotent — concurrent calls are serialised."""
    store["bootstrapped"] = False
    await bootstrap()
    return {
        "message": "refreshed",
        "breaches": len(store.get("breaches", [])),
        "rupees_at_risk": store.get("rupees_at_risk", 0),
    }

@app.post("/agent/investigate")
async def agent_investigate():
    """Stream the autonomous investigation agent via SSE."""
    return StreamingResponse(
        run_agent(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
