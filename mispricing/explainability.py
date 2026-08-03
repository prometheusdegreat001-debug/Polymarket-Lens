"""
Turns a MispricingSignal dict into a plain-English explanation any
non-technical reader can follow. Pure string templating, free.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="mispricing.explainability",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure string templating over already-computed signal fields.",
))


def explain_signal(signal: dict) -> str:
    edge_pp = signal["edge_size"] * 100

    if signal["signal_type"] == "arbitrage":
        underpriced = signal["_outcome_sum"] < 1.0
        direction_word = "CHEAP" if underpriced else "EXPENSIVE"
        action = (
            "buying \"Yes\" on every single outcome in this group would cost less than "
            "$1 total, even though exactly one is guaranteed to pay out $1"
            if underpriced else
            "this group is hard to arbitrage fresh (would require shorting multiple legs at once)"
        )
        return (
            f"\"{signal['_event_title']}\" — all the possible outcomes together are priced "
            f"{edge_pp:.1f} cents too {direction_word}. In theory, {action}."
        )

    # cross_platform
    source_label = "an independent market (Kalshi)" if signal["benchmark_source"] == "kalshi" else "an AI research estimate"
    cheaper_side = "This market's Yes" if signal["direction"] == "YES" else "The benchmark's Yes"
    return (
        f"\"{signal.get('_question', 'This market')}\" is priced at {signal['implied_probability']:.2f} "
        f"here vs {signal['benchmark_probability']:.2f} according to {source_label} — "
        f"a {edge_pp:.1f} cent gap. {cheaper_side} looks relatively cheap."
    )
