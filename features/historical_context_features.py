"""
Thin feature-layer wrapper around historical_context module output, so
intelligence/confidence_aggregator.py has consistent numeric fields to
combine regardless of how precedent research was computed internally.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="features.historical_context_features",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))


def compute_historical_context_features(historical_event_record: dict) -> dict:
    return {
        "precedent_score": historical_event_record.get("precedent_score", 0.0),
        "resembles_failed_setup": historical_event_record.get("resembles_failed_setup", False),
        "similar_event_count": len(historical_event_record.get("similar_events", [])),
        "has_precedent_data": bool(historical_event_record.get("raw_ok", False)),
    }
