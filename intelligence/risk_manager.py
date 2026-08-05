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
