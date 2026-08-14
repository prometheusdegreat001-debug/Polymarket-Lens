"""
Market-level features. Combines the Gamma snapshot (price, liquidity,
volume) with a live CLOB order book (spread, depth) where available.
Everything here is a real, computed number - no external model probability
lives in this file (that's mispricing/benchmark_comparator.py's job).
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="features.market_features",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))

from datetime import datetime, timezone


def compute_market_features(market: dict, book_stats: dict = None) -> dict:
    """
    `market` is a normalized market dict from ingestion/polymarket_api.py.
    `book_stats` is the output of ingestion/orderbook_stream.compute_spread_and_depth,
    or None if the book couldn't be fetched (falls back to Gamma-only fields).
    """
    yes_price = market["outcome_prices"].get("Yes")
    book_stats = book_stats or {}

    event_age_days = _compute_event_age_days(market)
    time_to_resolution_days = _compute_time_to_resolution(market)

    return {
        "market_id": market.get("market_id"),
        "market_url": market.get("market_url", ""),
        "implied_probability": yes_price,
        "bid": book_stats.get("bid"),
        "ask": book_stats.get("ask"),
        "spread": book_stats.get("spread"),
        "depth_usd": book_stats.get("depth_usd", 0.0),
        "liquidity_usd": market.get("liquidity", 0.0),
        "volume_24h_usd": market.get("volume_24h", 0.0),
        "event_age_days": event_age_days,
        "time_to_resolution_days": time_to_resolution_days,
        "regime_tag": _classify_regime(market, time_to_resolution_days),
    }


def _compute_event_age_days(market: dict) -> float:
    # Gamma doesn't always return a createdAt on the market sub-object; if
    # missing, we report 0.0 rather than guessing.
    created = market.get("created_at")
    if not created:
        return 0.0
    try:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - created_dt).days, 1)
    except (ValueError, AttributeError):
        return 0.0


def _compute_time_to_resolution(market: dict):
    end_date = market.get("end_date")
    if not end_date:
        return None
    try:
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        delta_days = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return round(delta_days, 2)
    except (ValueError, AttributeError):
        return None


def _classify_regime(market: dict, time_to_resolution_days) -> str:
    """
    Simple, transparent regime tag - not a volatility model, just a
    liquidity/time-based label useful for filtering in the decision engine.
    """
    liquidity = market.get("liquidity", 0.0)
    if time_to_resolution_days is not None and time_to_resolution_days < 1:
        return "resolving_imminently"
    if liquidity < 1000:
        return "illiquid"
    if liquidity > 100000:
        return "high_liquidity"
    return "normal"
