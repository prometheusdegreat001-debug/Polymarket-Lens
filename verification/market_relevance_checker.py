"""
Checks whether the market itself is even worth verifying - sufficient
liquidity/depth/volume. A perfectly verified news story is useless if you
can't actually trade the market without moving the price past your edge.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="verification.market_relevance_checker",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))

from features.liquidity_features import liquidity_sufficient, liquidity_report


def check_market_relevance(market_features: dict) -> dict:
    passed = liquidity_sufficient(market_features)
    report = liquidity_report(market_features)
    return {"liquidity_sufficient": passed, "report": report}
