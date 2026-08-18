"""
Ranks a list of evaluated wallets for display/alert priority. Pure sort,
no external calls.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="wallet_intel.wallet_ranker",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure in-memory sort.",
))


def rank_wallets(wallets: list) -> list:
    """
    wallets: list of dicts each containing at least copy_trade_score.
    Ranks "copy" recommendations first, then by score descending.
    """
    order = {"copy": 0, "watch": 1, "avoid": 2}
    return sorted(
        wallets,
        key=lambda w: (order.get(w.get("copy_trade_recommendation", "avoid"), 3), -w.get("copy_trade_score", 0)),
    )
