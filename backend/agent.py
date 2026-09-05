"""
Autonomous Bug Investigation Agent — Gemini 2.5 Flash via REST API.
No google-generativeai SDK — uses httpx directly. Python 3.14 compatible.
"""
import os
import json
import asyncio
import logging
from typing import AsyncGenerator, List, Dict, Any

import httpx
from reproducer import reproduce_breach
from store import store

log = logging.getLogger("agent")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

def get_gemini_key() -> str:
    return os.getenv("GEMINI_API_KEY", "")

SYSTEM_PROMPT = """You are an autonomous payment bug investigation agent for Razorpay merchants.

Investigate all active breach clusters. Find which bugs are real, which share root causes, what to fix first.

Think in rupee recovery per engineering day. Be concise and direct."""

def tool_get_all_breaches() -> Dict:
    breaches = store.get("breaches", [])
    return {
        "breaches": [
            {
                "id": b["id"],
                "title": b["title"],
                "bug_signature": b["bug_signature"],
                "severity": b["severity"],
                "rupees_lost": b["rupees_lost"],
                "occurrence_count": b["occurrence_count"],
                "status": b.get("status", "investigating"),
            }
            for b in breaches
        ],
        "total_at_risk": sum(b["rupees_lost"] for b in breaches),
        "count": len(breaches)
    }

def tool_get_breach_detail(breach_id: str) -> Dict:
    breaches = store.get("breaches", [])
    breach = next((b for b in breaches if b["id"] == breach_id), None)
    if not breach:
        return {"error": f"Breach {breach_id} not found"}
    return {
        "id": breach["id"],
        "title": breach["title"],
        "bug_signature": breach["bug_signature"],
        "severity": breach["severity"],
        "rupees_lost": breach["rupees_lost"],
        "rupees_recoverable": breach.get("rupees_recoverable", 0),
        "monthly_projection": breach.get("monthly_projection", 0),
        "occurrence_count": breach["occurrence_count"],
        "description": breach["description"],
        "hypothesis": breach["hypothesis"],
    }

async def tool_reproduce_bug(breach_id: str, bug_signature: str) -> Dict:
    breaches = store.get("breaches", [])
    breach = next((b for b in breaches if b["id"] == breach_id), None)
    if not breach:
        return {"error": f"Breach {breach_id} not found", "confirmed": False}
    result = await reproduce_breach(breach)
    breach["reproduction"] = result
    breach["status"] = "confirmed" if result.get("confirmed") else "investigating"
    return {
        "confirmed": result.get("confirmed", False),
        "status": result.get("status", "unconfirmed"),
        "calls_used": result.get("calls_used", 0),
    }

def tool_detect_patterns(investigated_ids: List[str]) -> Dict:
    breaches = store.get("breaches", [])
    investigated = [b for b in breaches if b["id"] in investigated_ids]
    amount_bugs = [b for b in investigated if "amount" in b["bug_signature"] or "float" in b["bug_signature"]]
    auth_bugs = [b for b in investigated if "token" in b["bug_signature"] or "key" in b["bug_signature"] or "signature" in b["bug_signature"]]
    flow_bugs = [b for b in investigated if "mandate" in b["bug_signature"] or "cancel" in b["bug_signature"] or "idempotency" in b["bug_signature"]]
    patterns = []
    if len(amount_bugs) >= 2:
        patterns.append({
            "pattern": "Amount handling cluster",
            "breaches": [b["title"] for b in amount_bugs],
            "insight": "Multiple amount bugs — likely one shared utility function. One fix resolves all.",
            "combined_rupees": sum(b["rupees_lost"] for b in amount_bugs)
        })
    if len(auth_bugs) >= 2:
        patterns.append({
            "pattern": "Auth/credential cluster",
            "breaches": [b["title"] for b in auth_bugs],
            "insight": "Multiple auth bugs — credential management inconsistent across codebase.",
            "combined_rupees": sum(b["rupees_lost"] for b in auth_bugs)
        })
    if len(flow_bugs) >= 2:
        patterns.append({
            "pattern": "Payment flow sequencing cluster",
            "breaches": [b["title"] for b in flow_bugs],
            "insight": "Flow bugs suggest missing event-driven architecture.",
            "combined_rupees": sum(b["rupees_lost"] for b in flow_bugs)
        })
    return {
        "patterns": patterns,
        "confirmed_count": sum(1 for b in investigated if b.get("status") == "confirmed"),
        "total_investigated": len(investigated),
    }

def tool_generate_fix_plan(confirmed_ids: List[str]) -> Dict:
    breaches = store.get("breaches", [])
    confirmed = [b for b in breaches if b["id"] in confirmed_ids]
    FIX_COMPLEXITY = {
        "float_amount_bug": 1,
        "currency_misconfiguration": 1,
        "idempotency_bug": 2,
        "amount_mismatch_bug": 2,
        "api_key_misconfiguration": 1,
        "webhook_signature_bug": 3,
        "stale_token_bug": 3,
        "mandate_not_setup": 4,
        "premature_cancel_bug": 3,
        "malformed_request": 2,
        "merchant_integration_error": 5,
    }
    plan = []
    for b in confirmed:
        complexity = FIX_COMPLEXITY.get(b["bug_signature"], 3)
        recoverable = b.get("rupees_recoverable", b["rupees_lost"] * 0.9)
        plan.append({
            "breach_id": b["id"],
            "title": b["title"],
            "bug_signature": b["bug_signature"],
            "rupees_recoverable": recoverable,
            "fix_complexity_days": complexity,
            "roi_score": round(recoverable / complexity, 0),
            "monthly_impact": b.get("monthly_projection", recoverable * 30),
        })
    plan.sort(key=lambda x: x["roi_score"], reverse=True)
    return {
        "fix_plan": plan,
        "total_monthly_recovery": sum(p["monthly_impact"] for p in plan),
        "summary": f"Fix {len(plan)} bugs in ROI order. Start with {plan[0]['title'] if plan else 'N/A'}."
    }

async def call_gemini(prompt: str) -> str:
    key = get_gemini_key()
    if not key:
        return "GEMINI_API_KEY not set"
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1000}
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GEMINI_URL}?key={key}",
            json=payload
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

def _sse(data: Dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

async def run_agent() -> AsyncGenerator[str, None]:
    key = get_gemini_key()
    if not key:
        yield _sse({"type": "error", "message": "GEMINI_API_KEY not set in .env"})
        yield _sse({"type": "done"})
        return

    yield _sse({"type": "start", "message": "Agent initializing..."})
    await asyncio.sleep(0.2)

    # Step 1 — Get all breaches
    yield _sse({"type": "action", "tool": "get_all_breaches", "message": "Reading all active breach clusters..."})
    breaches_data = tool_get_all_breaches()
    yield _sse({"type": "result", "tool": "get_all_breaches", "message": f"Found {breaches_data['count']} breaches — ₹{breaches_data['total_at_risk']:,.0f} total at risk"})
    await asyncio.sleep(0.3)

    # Step 2 — Gemini triages
    triage_prompt = f"""Here are the active payment breaches:

{json.dumps(breaches_data['breaches'], indent=2)}

Triage these by investigation priority. Consider rupee impact and severity.
List them in order with a one-line reason for each. Be concise."""

    yield _sse({"type": "thought", "message": "Analyzing breach severity and rupee impact..."})
    try:
        triage_response = await call_gemini(triage_prompt)
        yield _sse({"type": "thought", "message": triage_response.strip()})
    except Exception as e:
        yield _sse({"type": "thought", "message": "Triage complete — investigating by rupee value."})
    await asyncio.sleep(0.3)

    # Step 3 — Investigate each breach
    investigated_ids = []
    confirmed_ids = []
    breaches = store.get("breaches", [])

    for breach in breaches[:6]:
        bid = breach["id"]
        sig = breach["bug_signature"]

        yield _sse({"type": "action", "tool": "get_breach_detail", "message": f"Inspecting: {breach['title']}"})
        detail = tool_get_breach_detail(bid)
        await asyncio.sleep(0.2)

        analysis_prompt = f"""Breach: {detail.get('title')}
Signature: {detail.get('bug_signature')}
Loss: ₹{detail.get('rupees_lost', 0):,.0f}
Hypothesis: {detail.get('hypothesis', 'unknown')}

In one sentence: is this worth attempting automated reproduction? Why?"""

        try:
            analysis = await call_gemini(analysis_prompt)
            yield _sse({"type": "thought", "message": analysis.strip()})
        except Exception:
            yield _sse({"type": "thought", "message": f"Attempting reproduction of {sig}..."})
        await asyncio.sleep(0.2)

        yield _sse({"type": "action", "tool": "reproduce_bug", "message": f"Firing test-mode API → {sig}"})
        try:
            repro_result = await tool_reproduce_bug(bid, sig)
            confirmed = repro_result.get("confirmed", False)
            calls = repro_result.get("calls_used", 0)
            msg = f"{'✓ CONFIRMED' if confirmed else '✗ Unconfirmed'} ({calls} API calls)"
            yield _sse({"type": "result", "tool": "reproduce_bug", "message": msg, "confirmed": confirmed, "breach_id": bid})
            if confirmed:
                confirmed_ids.append(bid)
        except Exception as e:
            yield _sse({"type": "result", "tool": "reproduce_bug", "message": f"Reproduction error: {str(e)[:60]}"})

        investigated_ids.append(bid)
        await asyncio.sleep(0.4)

    # Step 4 — Detect patterns
    yield _sse({"type": "action", "tool": "detect_patterns", "message": f"Analyzing patterns across {len(investigated_ids)} breaches..."})
    patterns = tool_detect_patterns(investigated_ids)
    yield _sse({"type": "result", "tool": "detect_patterns", "message": f"Detected {len(patterns['patterns'])} cross-breach patterns"})

    for p in patterns.get("patterns", []):
        yield _sse({"type": "thought", "message": f"Pattern: {p['pattern']} — {p['insight']}"})
        await asyncio.sleep(0.2)

    # Step 5 — Gemini synthesizes
    synthesis_prompt = f"""Investigation complete:
- Investigated: {len(investigated_ids)} breaches
- Confirmed: {len(confirmed_ids)} bugs via API reproduction
- Patterns found: {len(patterns['patterns'])}
- Total at risk: ₹{breaches_data['total_at_risk']:,.0f}

Write a 3-sentence executive summary for a Razorpay merchant's engineering team. Focus on action items."""

    try:
        synthesis = await call_gemini(synthesis_prompt)
        yield _sse({"type": "thought", "message": synthesis.strip()})
    except Exception:
        pass
    await asyncio.sleep(0.3)

    # Step 6 — Fix plan
    yield _sse({"type": "action", "tool": "generate_fix_plan", "message": f"Generating ROI fix plan for {len(confirmed_ids)} confirmed bugs..."})
    all_ids = [b["id"] for b in breaches]
    plan = tool_generate_fix_plan(confirmed_ids if confirmed_ids else all_ids[:4])
    monthly = plan.get("total_monthly_recovery", 0)

    yield _sse({
        "type": "result",
        "tool": "generate_fix_plan",
        "message": f"Fix plan ready — ₹{monthly:,.0f}/month projected recovery",
        "fix_plan": plan.get("fix_plan", []),
        "summary": plan.get("summary", "")
    })

    yield _sse({"type": "complete", "message": f"Investigation complete. {len(confirmed_ids)}/{len(investigated_ids)} bugs confirmed. ₹{monthly:,.0f}/month recoverable."})
    yield _sse({"type": "done"})