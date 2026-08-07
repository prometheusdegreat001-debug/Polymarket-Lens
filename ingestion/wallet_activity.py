"""
Polymarket Data API ingestion (public, no auth) - leaderboard, per-wallet
trade activity, open positions, closed (resolved) positions. This is the
real data source behind all wallet intelligence - win/loss record comes
from realizedPnl on closed positions, not an estimate.
"""

from config.loader import DATA_API_BASE
from ingestion.http_utils import get_json
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="ingestion.wallet_activity",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - Polymarket Data API (leaderboard/activity/positions) is free and public.",
))


def fetch_leaderboard(pool_size: int, category: str = "OVERALL",
                       time_period: str = "ALL", order_by: str = "PNL"):
    pool_size = min(pool_size, 1050)
    entries = []
    offset = 0
    page_size = 50

    while len(entries) < pool_size:
        params = {
            "category": category, "timePeriod": time_period, "orderBy": order_by,
            "limit": min(page_size, pool_size - len(entries)), "offset": offset,
        }
        page = get_json(f"{DATA_API_BASE}/v1/leaderboard", params=params)
        if not page:
            break
        for entry in page:
            entries.append({
                "rank": entry.get("rank"),
                "wallet_address": entry.get("proxyWallet"),
                "username": entry.get("userName"),
                "vol": _to_float(entry.get("vol")),
                "pnl": _to_float(entry.get("pnl")),
                "verified_badge": bool(entry.get("verifiedBadge", False)),
            })
        offset += page_size
        if len(page) < page_size:
            break
    return entries


def fetch_leaderboard_pool(pool_size: int, categories: list, primary_period: str,
                            secondary_period: str = None) -> list:
    """
    Merges the leaderboard across MULTIPLE categories and (optionally) TWO
    time periods into one deduplicated candidate pool, instead of a single
    OVERALL/one-period pull.

    Why this exists: a single OVERALL, one-period leaderboard pull
    structurally biases discovery toward whales - a wallet that
    specializes in one category (e.g. an esports specialist) may never
    crack the OVERALL top-N even while dominating its own category, and a
    wallet with a real but recent edge may not have accumulated enough
    PnL over a full MONTH to appear yet even though it's already visible
    over WEEK. Merging both fixes real, confirmed gaps rather than just
    tuning one threshold.

    Each returned entry gets `source_periods`: the set of periods
    (primary/secondary) it appeared in, independent of category - a
    wallet appearing in BOTH periods (i.e. consistently ranking, not just
    spiking once) is genuinely stronger evidence of a repeatable pattern
    than either one alone. This is exposed for scoring/reporting to use
    as corroborating evidence, not as an additional hard gate - adding
    more hard gates would only shrink the pool further, and the real
    goal here is to widen it.
    """
    merged = {}  # wallet_address -> entry dict (best pnl/rank kept, periods unioned)

    periods = [primary_period]
    if secondary_period and secondary_period != primary_period:
        periods.append(secondary_period)

    for category in categories:
        for period in periods:
            try:
                page = fetch_leaderboard(pool_size=pool_size, category=category, time_period=period)
            except Exception:
                continue  # one bad category/period pull shouldn't sink the whole scan
            for entry in page:
                wallet = entry.get("wallet_address")
                if not wallet:
                    continue
                if wallet not in merged:
                    merged[wallet] = dict(entry)
                    merged[wallet]["source_periods"] = {period}
                    merged[wallet]["source_categories"] = {category}
                else:
                    merged[wallet]["source_periods"].add(period)
                    merged[wallet]["source_categories"].add(category)
                    # Keep the higher PnL figure seen across pulls (different
                    # category/period calls can report slightly different
                    # numbers for the same wallet).
                    if entry.get("pnl", 0.0) > merged[wallet].get("pnl", 0.0):
                        merged[wallet]["pnl"] = entry["pnl"]

    results = list(merged.values())
    for r in results:
        r["cross_period_confirmed"] = len(r["source_periods"]) > 1
        r["source_periods"] = sorted(r["source_periods"])
        r["source_categories"] = sorted(r["source_categories"])
    # Best PnL first, so downstream min_pnl filtering and pool_size caps
    # keep the strongest candidates when the merged pool is larger than
    # what a single pull would have returned.
    results.sort(key=lambda r: -(r.get("pnl") or 0.0))
    return results


def fetch_wallet_trade_summary(wallet_address: str, max_trades: int):
    """Cheap qualifying check: trade count + wallet age from earliest trade."""
    params = {
        "user": wallet_address, "type": "TRADE", "limit": max_trades,
        "sortBy": "TIMESTAMP", "sortDirection": "ASC",
    }
    activity = get_json(f"{DATA_API_BASE}/activity", params=params)
    if not activity:
        return {"trade_count": 0, "hit_cap": False, "first_trade_ts": None, "last_trade_ts": None}
    hit_cap = len(activity) >= max_trades
    return {
        "trade_count": len(activity), "hit_cap": hit_cap,
        "first_trade_ts": activity[0].get("timestamp"),
        "last_trade_ts": activity[-1].get("timestamp"),
    }


def fetch_wallet_activity_detailed(wallet_address: str, limit: int = 500):
    """Full trade activity list for behavioral analysis (frequency, sizing, events)."""
    params = {
        "user": wallet_address, "type": "TRADE", "limit": limit,
        "sortBy": "TIMESTAMP", "sortDirection": "ASC",
    }
    activity = get_json(f"{DATA_API_BASE}/activity", params=params)
    if not activity:
        return []
    return [
        {
            "wallet_address": wallet_address,
            "side": a.get("side"),
            "size": _to_float(a.get("size")),
            "price": _to_float(a.get("price")),
            "notional_usd": _to_float(a.get("size")) * _to_float(a.get("price")),
            "timestamp": a.get("timestamp"),
            "title": a.get("title"),
            "event_slug": a.get("eventSlug"),
            "outcome": a.get("outcome"),
        }
        for a in activity
    ]


def fetch_wallet_closed_positions(wallet_address: str, limit: int = 500):
    """Resolved positions - the real win/loss source (realizedPnl per position)."""
    params = {"user": wallet_address, "limit": limit, "sortBy": "REALIZEDPNL", "sortDirection": "DESC"}
    positions = get_json(f"{DATA_API_BASE}/closed-positions", params=params)
    if not positions:
        return []
    return [
        {
            "wallet_address": wallet_address,
            "title": p.get("title"), "event_slug": p.get("eventSlug"), "outcome": p.get("outcome"),
            "realized_pnl": _to_float(p.get("realizedPnl")),
            "avg_price": _to_float(p.get("avgPrice")),
            "total_bought": _to_float(p.get("totalBought")),
            "end_date": p.get("endDate"),
        }
        for p in positions
    ]


def fetch_wallet_open_positions(wallet_address: str, limit: int = 500):
    """Current open positions - live exposure check."""
    params = {"user": wallet_address, "limit": limit, "sortBy": "CURRENT", "sortDirection": "DESC"}
    positions = get_json(f"{DATA_API_BASE}/positions", params=params)
    if not positions:
        return []
    return [
        {
            "wallet_address": wallet_address,
            "title": p.get("title"), "event_slug": p.get("eventSlug"), "outcome": p.get("outcome"),
            "size": _to_float(p.get("size")), "avg_price": _to_float(p.get("avgPrice")),
            "cur_price": _to_float(p.get("curPrice")), "current_value": _to_float(p.get("currentValue")),
            "cash_pnl": _to_float(p.get("cashPnl")),
            "condition_id": p.get("conditionId"),  # needed for fetch_market_holders below
        }
        for p in positions
    ]


def fetch_market_holders(condition_id: str, min_balance: float = 1.0, limit: int = 20):
    """
    Top current holders of a market's outcome tokens - used to CONFIRM a
    "cheap/early entry" claim on a wallet's OPEN position independently
    of the wallet's own self-reported avgPrice: if a wallet holds a large
    share of a market at a price well below the market's current price,
    that's a real, on-chain-verifiable cheap entry, not just a number
    from one API pulled in isolation.
    """
    if not condition_id:
        return []
    params = {"market": condition_id, "minBalance": min_balance, "limit": limit}
    holders = get_json(f"{DATA_API_BASE}/holders", params=params)
    if not holders:
        return []
    return [
        {
            "wallet_address": h.get("proxyWallet") or h.get("wallet"),
            "amount": _to_float(h.get("amount")),
            "outcome_index": h.get("outcomeIndex"),
        }
        for h in holders
    ]


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
