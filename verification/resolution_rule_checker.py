"""
Checks whether the evidence found actually addresses the market's
resolution rule (not just a related-but-different question). The LLM
already self-reports this in fetch_evidence's JSON contract
(event_matches_resolution_rule) - this module is the single place that
reads that field, so if we ever want to add an independent second check
later, there's one obvious place to add it.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="verification.resolution_rule_checker",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))


def check_resolution_match(evidence: dict) -> bool:
    return bool(evidence.get("event_matches_resolution_rule", False))
