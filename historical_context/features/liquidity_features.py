"""
Liquidity/depth checks - a thin, explicit layer so verification's
confidence_gate.py has a single, readable place to check "is this market
even tradeable" rather than re-deriving it inline.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="features.liquidity_features",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))

from config.loader import risk


def liquidity_sufficient(market_features: dict) -> bool:
    return (
        market_features.get("liquidity_usd", 0.0) >= risk.min_liquidity_usd
        and market_features.get("volume_24h_usd", 0.0) >= risk.min_volume_24h_usd
        and market_features.get("depth_usd", 0.0) >= risk.min_depth_usd
    )


def liquidity_report(market_features: dict) -> dict:
    """Explains WHY liquidity passed/failed - used in VerificationRecord.explanation."""
    checks = {
        "liquidity_usd": (market_features.get("liquidity_usd", 0.0), risk.min_liquidity_usd),
        "volume_24h_usd": (market_features.get("volume_24h_usd", 0.0), risk.min_volume_24h_usd),
        "depth_usd": (market_features.get("depth_usd", 0.0), risk.min_depth_usd),
    }
    failures = [
        f"{name}=${actual:,.0f} is below minimum ${minimum:,.0f}"
        for name, (actual, minimum) in checks.items() if actual < minimum
    ]
    return {"passed": len(failures) == 0, "failures": failures}
