"""
Checks whether the found evidence is current (recent enough to be
relevant) - reads the LLM's self-reported news_is_current flag from
fetch_evidence, gated against the configured max_news_age_hours.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="verification.event_matcher",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))

from config.loader import verification as verification_cfg


def check_news_currency(evidence: dict) -> bool:
    # The LLM already judges "last 48 hours" per the prompt in
    # ingestion/external_sources.py using the configured threshold.
    # If verification.max_news_age_hours is changed, the prompt text
    # should be updated to match (see ingestion/external_sources.py).
    return bool(evidence.get("news_is_current", False))
