"""
Tracks "progress" toward a market's resolution deadline. The rich version
(qualitative reading of whether talks are progressing) needs the paid tier
(ingestion/historical_events.py already covers this as part of its research).
The free tier here is a genuinely useful, honest proxy: time-to-resolution
plus recent volume/price-movement trend, which are real signals about
whether something is heating up - just not a qualitative news read.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="historical_context.negotiation_progress_tracker",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy=(
        "Uses time-to-resolution and recent price/volume momentum (both already "
        "computed in features/market_features.py) as a crude, honest proxy for "
        "'is this heating up.' Does not claim to read qualitative negotiation "
        "progress - that would require the paid tier's open-web research, "
        "already covered separately by historical_context.event_history_search."
    ),
))


def track_progress(market_features: dict) -> dict:
    """
    Returns {"progress_label": str, "days_remaining": float|None,
    "momentum_signal": str}
    """
    days_remaining = market_features.get("time_to_resolution_days")
    momentum = market_features.get("price_momentum", 0.0)

    if days_remaining is None:
        urgency = "unknown"
    elif days_remaining <= 3:
        urgency = "imminent"
    elif days_remaining <= 14:
        urgency = "near-term"
    else:
        urgency = "distant"

    if momentum > 0.05:
        momentum_signal = "price moving up recently - possible new information"
    elif momentum < -0.05:
        momentum_signal = "price moving down recently - possible new information"
    else:
        momentum_signal = "price stable - no strong recent signal of change"

    return {
        "progress_label": urgency,
        "days_remaining": days_remaining,
        "momentum_signal": momentum_signal,
    }
