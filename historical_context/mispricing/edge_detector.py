"""
Runs both FREE mispricing detectors and returns unified MispricingSignal-shaped
dicts:
  1. Internal arbitrage consistency check (neg-risk outcome sums) - free
  2. Cross-platform benchmark comparison (Kalshi, or LLM estimate as last
     resort - see probability_model.py for that ordering)
"""

from datetime import datetime, timezone

from config.loader import risk
from mispricing.probability_model import get_benchmark_probability
from mispricing.benchmark_comparator import compare
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="mispricing.edge_detector",
    requires_paid_api=False,  # arbitrage path is always free; cross-platform inherits probability_model's conditional cost
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="Arbitrage detection (detect_arbitrage) is 100% free always. "
                            "Cross-platform detection (detect_cross_platform_edges) inherits "
                            "whatever cost profile probability_model.get_benchmark_probability "
                            "resolves to for each market (free Kalshi match, or paid LLM fallback "
                            "only if you've enabled it).",
))


def detect_arbitrage(events: list) -> list:
    """Free, always-on. Neg-risk outcome-sum consistency check."""
    signals = []
    for event in events:
        neg_risk_markets = [m for m in event["markets"] if m.get("neg_risk")]
        if len(neg_risk_markets) < 2:
            continue

        yes_prices, min_liquidity = [], None
        for m in neg_risk_markets:
            yes_price = m["outcome_prices"].get("Yes")
            if yes_price is None:
                continue
            yes_prices.append(yes_price)
            liq = m.get("liquidity", 0)
            min_liquidity = liq if min_liquidity is None else min(min_liquidity, liq)

        if len(yes_prices) < 2 or (min_liquidity is not None and min_liquidity < risk.min_liquidity_usd):
            continue

        outcome_sum = sum(yes_prices)
        deviation = abs(outcome_sum - 1.0)
        if deviation >= risk.edge_threshold_low:
            signals.append({
                "market_id": event["event_id"],
                "market_url": event.get("market_url", ""),
                "signal_type": "arbitrage",
                "implied_probability": outcome_sum,
                "benchmark_probability": 1.0,
                "benchmark_source": "internal_consistency",
                "edge_size": round(deviation, 4),
                "direction": "YES" if outcome_sum < 1.0 else "HOLD",
                "confidence": 0.9,  # mechanical check, high confidence by construction
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "_event_title": event["title"], "_num_outcomes": len(yes_prices),
                "_min_liquidity": min_liquidity, "_outcome_sum": round(outcome_sum, 4),
            })
    return signals


def detect_cross_platform_edges(events: list, kalshi_markets: list) -> list:
    """Free-first (Kalshi match), conditionally paid (LLM fallback) per market."""
    signals = []
    for event in events:
        for m in event["markets"]:
            if m.get("liquidity", 0) < risk.min_liquidity_usd or m.get("volume_24h", 0) < risk.min_volume_24h_usd:
                continue

            implied = m["outcome_prices"].get("Yes")
            if implied is None:
                continue

            benchmark = get_benchmark_probability(m, kalshi_markets)
            if benchmark["benchmark_probability"] is None:
                continue

            comparison = compare(implied, benchmark["benchmark_probability"])
            if comparison["edge_size"] < risk.edge_threshold_low:
                continue

            signals.append({
                "market_id": m["market_id"],
                "market_url": m.get("market_url", ""),
                "signal_type": "cross_platform",
                "implied_probability": implied,
                "benchmark_probability": benchmark["benchmark_probability"],
                "benchmark_source": benchmark["benchmark_source"],
                "edge_size": comparison["edge_size"],
                "direction": comparison["direction"],
                "confidence": 0.8 if benchmark["benchmark_source"] == "kalshi" else 0.5,
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "_question": m.get("question"), "_kalshi_match": benchmark.get("kalshi_match"),
                "_similarity": benchmark.get("similarity"),
            })
    return signals
