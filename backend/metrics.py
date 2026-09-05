"""
Metrics: compute honest precision, recall, F1, false-positive cost, and ablation.

Ground truth comes from the synthetic answer key.
All numbers are computed against the real classifier output — not hand-tuned.
"""
import logging
from collections import defaultdict
from typing import List, Dict

log = logging.getLogger("metrics")

ATTRIBUTION_LABELS = ["MERCHANT", "BANK", "CUSTOMER"]


def compute_metrics(
    classified: List[Dict],
    answer_key: Dict[str, Dict],   # payment_id → {true_label, true_sig}
    breaches:   List[Dict],
) -> Dict:
    """
    Returns the full metrics dict. All keys are documented inline.
    Never raises — returns {error: ...} on failure.
    """
    try:
        return _compute(classified, answer_key, breaches)
    except Exception as e:
        log.exception(f"Metrics computation failed: {e}")
        return {"error": str(e)}


def _compute(classified, answer_key, breaches):
    # Only score failures we have ground truth for
    scored = [f for f in classified if f["id"] in answer_key]
    if not scored:
        return {"error": "no ground truth to score against — run synthetic eval first"}

    # ── Attribution-level (MERCHANT / BANK / CUSTOMER) ────────────────────────
    label_tp: Dict[str, int] = defaultdict(int)
    label_fp: Dict[str, int] = defaultdict(int)
    label_fn: Dict[str, int] = defaultdict(int)
    fp_cost_rupees = 0.0

    for f in scored:
        truth      = answer_key[f["id"]]
        true_label = truth["true_label"]
        pred_label = f.get("attribution", "AMBIGUOUS")

        if pred_label == true_label:
            label_tp[true_label] += 1
        else:
            label_fp[pred_label] += 1
            label_fn[true_label] += 1
            # FP cost: cases where we wrongly blame the merchant
            if pred_label == "MERCHANT" and true_label != "MERCHANT":
                fp_cost_rupees += f.get("amount", 0) / 100

    per_class = {}
    macro_precisions = []
    macro_recalls    = []

    for label in ATTRIBUTION_LABELS:
        tp = label_tp[label]
        fp = label_fp[label]
        fn = label_fn[label]
        p  = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        r  = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        per_class[label] = {
            "precision": round(p, 4),
            "recall":    round(r, 4),
            "f1":        round(f1, 4),
            "tp": tp, "fp": fp, "fn": fn,
            "count": tp + fn,   # total true positives of this class
        }
        if (tp + fn) > 0:   # only include classes with real examples
            macro_precisions.append(p)
            macro_recalls.append(r)

    overall_precision = sum(macro_precisions) / len(macro_precisions) if macro_precisions else 0.0
    overall_recall    = sum(macro_recalls)    / len(macro_recalls)    if macro_recalls    else 0.0
    overall_f1 = (
        2 * overall_precision * overall_recall / (overall_precision + overall_recall)
        if (overall_precision + overall_recall) > 0 else 0.0
    )

    # ── Bug-signature level (within MERCHANT class) ───────────────────────────
    sig_tp: Dict[str, int] = defaultdict(int)
    sig_fp: Dict[str, int] = defaultdict(int)
    sig_fn: Dict[str, int] = defaultdict(int)

    merchant_scored = [f for f in scored if answer_key[f["id"]]["true_label"] == "MERCHANT"]
    for f in merchant_scored:
        true_sig = answer_key[f["id"]]["true_sig"]
        pred_sig = f.get("bug_signature") or "none"
        if pred_sig == true_sig:
            sig_tp[true_sig] += 1
        else:
            sig_fp[pred_sig] += 1
            sig_fn[true_sig] += 1

    per_sig = {}
    for sig in set(list(sig_tp) + list(sig_fn)):
        tp = sig_tp[sig]
        fp = sig_fp.get(sig, 0)
        fn = sig_fn[sig]
        p  = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        r  = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        per_sig[sig] = {
            "precision": round(p, 4),
            "recall":    round(r, 4),
            "f1":        round(f1, 4),
            "tp": tp, "fp": fp, "fn": fn,
        }

    # ── Reproduction rate ─────────────────────────────────────────────────────
    confirmed  = sum(1 for b in breaches if b.get("reproduction", {}).get("confirmed"))
    attempted  = sum(1 for b in breaches if b.get("reproduction", {}).get("status") not in ("pending", None))
    repro_rate = confirmed / attempted if attempted > 0 else 0.0

    # ── Ablation: deterministic-only vs with LLM fallback ────────────────────
    # confidence == 1.0 → deterministic rule fired
    # confidence < 1.0  → heuristic or ambiguous
    det_cases = [f for f in scored if f.get("classification_confidence", 0) >= 1.0]
    heu_cases = [f for f in scored if f.get("classification_confidence", 0) < 1.0]

    def _accuracy(cases):
        if not cases:
            return 0.0
        correct = sum(1 for f in cases if f.get("attribution") == answer_key[f["id"]]["true_label"])
        return round(correct / len(cases), 4)

    ablation = {
        "deterministic_rules": {
            "count":    len(det_cases),
            "accuracy": _accuracy(det_cases),
            "note":     "source + reason matched a hard rule — no LLM needed",
        },
        "heuristic_fallback": {
            "count":    len(heu_cases),
            "accuracy": _accuracy(heu_cases),
            "note":     "source or reason absent — heuristic or ambiguous path",
        },
    }

    # ── Financial summary ─────────────────────────────────────────────────────
    rupees_identified = sum(
        f.get("amount", 0) / 100
        for f in classified
        if f.get("attribution") == "MERCHANT"
    )
    total    = len(classified)
    merchant = sum(1 for f in classified if f.get("attribution") == "MERCHANT")

    return {
        # Top-line
        "overall_precision":          round(overall_precision, 4),
        "overall_recall":             round(overall_recall, 4),
        "overall_f1":                 round(overall_f1, 4),
        "false_positive_cost_rupees": round(fp_cost_rupees, 2),
        # Breakdown
        "per_class":                  per_class,
        "per_sig":                    per_sig,
        # Reproduction
        "reproduction_rate":          round(repro_rate, 4),
        "reproductions_confirmed":    confirmed,
        "reproductions_attempted":    attempted,
        # Ablation
        "ablation":                   ablation,
        # Financial
        "rupees_identified":          round(rupees_identified, 2),
        "total_failures":             total,
        "total_failures_scored":      len(scored),
        "merchant_caused_count":      merchant,
        "merchant_caused_pct":        round(merchant / total * 100, 1) if total > 0 else 0.0,
        "batch_size":                 total,
    }
