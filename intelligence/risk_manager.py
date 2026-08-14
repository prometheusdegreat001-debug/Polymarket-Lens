"""
Risk/position-sizing logic. Purely formulaic, transparent, and configured
by the user's own risk.yml - never a confident dollar recommendation.

If risk.default_bankroll_usd is null (the default), this ONLY ever
produces a percentage of risk budget, never a dollar figure - keeps this
a research tool, not something that looks like financial advice.
"""

from config.loader import risk as risk_cfg
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="intelligence.risk_manager",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure formulaic sizing, no external calls.",
))


def suggested_size(edge_size: float, confidence_tier: str, liquidity_usd: float) -> dict:
    """
    Simple fractional-edge sizing, capped by risk.yml's
    max_position_size_pct_of_bankroll. This is NOT Kelly-optimal or a
    trading recommendation - it's a transparent, conservative starting
    point you can override entirely.

    Returns {"suggested_size_pct_of_risk_budget": float,
             "max_loss_tolerance_usd": float|None}
    """
    tier_multiplier = {"low": 0.25, "medium": 0.6, "high": 1.0}.get(confidence_tier, 0.25)

    # Scale with edge size but never exceed the configured cap.
    raw_pct = min(edge_size * 100, risk_cfg.max_position_size_pct_of_bankroll)
    sized_pct = round(raw_pct * tier_multiplier, 2)

    max_loss_usd = None
    if risk_cfg.default_bankroll_usd:
        max_loss_usd = round(risk_cfg.default_bankroll_usd * (sized_pct / 100), 2)

    return {
        "suggested_size_pct_of_risk_budget": sized_pct,
        "max_loss_tolerance_usd": max_loss_usd,
    }


def risk_check(opportunity_type: str, mispricing_signal: dict, verification: dict, historical: dict,
                market_features: dict, wallet_agreement_score: float) -> dict:
    """
    STEP 2 of the decision pipeline, run AFTER opportunity classification
    and BEFORE the final verdict. Two kinds of checks:
    - Universal checks that apply no matter what type of opportunity this
      is (liquidity floor, explicit verification failure, historical
      precedent conflict).
    - Opportunity-TYPE-SPECIFIC checks - a NOISE_FADE and a DEEP_VALUE
      opportunity carry genuinely different risk profiles even at the
      same confidence score, and should be flagged differently.

    Returns {"risk_ok": bool, "risk_level": "low"/"medium"/"high", "risk_flags": [str]}.
    risk_ok=False is a HARD block (routes straight to IGNORE downstream);
    risk_flags without risk_ok=False are WARNINGS that inform the verdict
    (e.g. can push a TRADE down to WATCH) without being an automatic kill.
    """
    flags = []

    liquidity_usd = market_features.get("liquidity_usd", 0.0)
    liquidity_ok = liquidity_usd >= risk_cfg.min_liquidity_usd
    if not liquidity_ok:
        flags.append(
            f"Liquidity (${liquidity_usd:,.0f}) is below the configured minimum "
            f"(${risk_cfg.min_liquidity_usd:,.0f}) - too thin to size into safely."
        )

    verification_failed = bool(verification and verification.get("status") == "FAIL")
    if verification_failed:
        flags.append(f"Verification explicitly FAILED: {verification.get('explanation', '')}")

    if historical and historical.get("resembles_failed_setup"):
        flags.append(f"Resembles past similar setups that did NOT resolve as hoped: {historical.get('precedent_summary', '')}")

    if market_features.get("regime_tag") == "illiquid":
        flags.append("Regime tag flags this market as illiquid - fills may be worse than the quoted price suggests.")

    # Opportunity-type-specific risk framing - the SAME confidence score
    # means different things depending on what kind of story is behind it.
    if opportunity_type == "noise_fade":
        flags.append(
            "NOISE_FADE risk: the price move itself is the primary evidence here - if the move "
            "was actually information-driven rather than an overreaction, fading it is wrong. "
            "Inherently higher-variance than a verified structural edge."
        )
    elif opportunity_type == "flow_scalp" and wallet_agreement_score < 0.5:
        flags.append(
            f"FLOW_SCALP risk: wallet agreement ({wallet_agreement_score:.2f}) is positive but not "
            f"strong - flow conviction here is moderate, not a clear consensus."
        )
    elif opportunity_type == "unclassified":
        flags.append("UNCLASSIFIED: no coherent opportunity-type story - treat any apparent edge with extra caution.")

    hard_fail = (not liquidity_ok) or verification_failed
    if hard_fail:
        risk_level = "high"
    elif len(flags) >= 2:
        risk_level = "high"
    elif flags:
        risk_level = "medium"
    else:
        risk_level = "low"

    return {"risk_ok": not hard_fail, "risk_level": risk_level, "risk_flags": flags}
