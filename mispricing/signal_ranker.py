"""Ranks mispricing signals for alert priority. Pure sort, free."""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="mispricing.signal_ranker",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure in-memory sort.",
))


def rank_signals(signals: list, top_n: int = None) -> list:
    ranked = sorted(signals, key=lambda s: (-s["confidence"], -s["edge_size"]))
    return ranked[:top_n] if top_n else ranked
