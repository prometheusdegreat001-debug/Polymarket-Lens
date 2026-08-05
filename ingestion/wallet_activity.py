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
        }
        for p in positions
    ]


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
