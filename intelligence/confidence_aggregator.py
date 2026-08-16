"""
Combines mispricing confidence + verification confidence + historical
precedent + wallet agreement into one confidence_tier
("low"/"medium"/"high"). Pure arithmetic - every input is already a
number produced elsewhere in the pipeline, this just combines them
transparently.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="intelligence.confidence_aggregator",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure arithmetic combination of already-computed confidence scores.",
))


def aggregate_confidence(mispricing_signal: dict, verification: dict,
                          historical: dict, wallet_agreement_score: float) -> dict:
    """
    wallet_agreement_score: -1.0 (smart-money wallets disagree with the
    signal direction) to +1.0 (they agree), 0.0 if no relevant wallet data.

    Returns {"confidence_tier": "low"|"medium"|"high", "confidence_score": float}
    """
    signal_confidence = mispricing_signal.get("confidence", 0.0)
    verification_confidence = verification.get("confidence", 0.0) if verification else 0.0
    precedent = historical.get("precedent_score", 0.0) if historical else 0.0

    # Historical precedent only ever adjusts DOWN (a bad precedent is a real
    # warning), never up beyond what the mispricing signal itself supports -
    # good precedent doesn't manufacture edge that isn't there.
    precedent_adjustment = min(0.0, precedent) * 0.15

    wallet_adjustment = wallet_agreement_score * 0.1

    combined = (
        signal_confidence * 0.55
        + verification_confidence * 0.25
        + precedent_adjustment
        + wallet_adjustment
    )
    combined = round(max(0.0, min(1.0, combined + 0.2)), 3)  # +0.2 baseline so a pure-math arbitrage signal isn't unfairly capped low

    if combined >= 0.7:
        tier = "high"
    elif combined >= 0.45:
        tier = "medium"
    else:
        tier = "low"

    return {"confidence_tier": tier, "confidence_score": combined}
