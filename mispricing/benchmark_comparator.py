"""
Compares a market's implied probability against its benchmark (from
probability_model) and computes edge size + direction. Pure arithmetic.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="mispricing.benchmark_comparator",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure arithmetic once probability_model has already "
                            "resolved a benchmark (which may itself be paid - see that module).",
))


def compare(implied_probability: float, benchmark_probability: float) -> dict:
    if implied_probability is None or benchmark_probability is None:
        return {"edge_size": 0.0, "direction": "HOLD"}

    edge_size = round(benchmark_probability - implied_probability, 4)
    if abs(edge_size) < 0.001:
        direction = "HOLD"
    elif edge_size > 0:
        direction = "YES"  # benchmark thinks Yes is more likely than the market prices -> Yes looks cheap
    else:
        direction = "NO"

    return {"edge_size": abs(edge_size), "raw_edge": edge_size, "direction": direction}
