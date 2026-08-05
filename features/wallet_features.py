"""
Core wallet features computed from real trade/position data. These feed
wallet_intel/wallet_classifier.py and wallet_scoring.py - nothing here is
estimated or fabricated, all derived from /activity and /closed-positions.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="features.wallet_features",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))

from collections import Counter
from datetime import datetime, timezone

from config.loader import wallet_scoring as ws_cfg


def compute_wallet_features(wallet_address: str, activity: list, closed_positions: list,
                             open_positions: list, wallet_age_days: float) -> dict:
    trade_count = len(activity)
    trades_per_day = round(trade_count / max(wallet_age_days, 1), 4)

    notionals = [a["notional_usd"] for a in activity if a.get("notional_usd")]
    total_notional = round(sum(notionals), 2)
    avg_notional = round(total_notional / len(notionals), 2) if notionals else 0.0

    wins = [p for p in closed_positions if p["realized_pnl"] > 0]
    losses = [p for p in closed_positions if p["realized_pnl"] <= 0]
    resolved_count = len(wins) + len(losses)
    win_rate = round(len(wins) / resolved_count, 4) if resolved_count else None

    pnl_lifetime = round(sum(p["realized_pnl"] for p in closed_positions), 2)
    max_drawdown = _compute_max_drawdown(closed_positions)

    event_counter = Counter(a.get("event_slug") or a.get("title") for a in activity if a.get("event_slug") or a.get("title"))
    market_breadth = len(event_counter)
    market_hhi = _herfindahl_index(event_counter)

    days_since_last_trade = _compute_recency(activity)

    return {
        "wallet_address": wallet_address,
        "trade_count": trade_count,
        "trades_per_day": trades_per_day,
        "total_notional_usd": total_notional,
        "avg_notional_usd": avg_notional,
        "pnl_lifetime": pnl_lifetime,
        "win_rate": win_rate,
        "resolved_trade_count": resolved_count,
        "max_drawdown_usd": max_drawdown,
        "market_breadth": market_breadth,
        "market_hhi": market_hhi,
        "top_events": [{"event": ev, "trade_count": c} for ev, c in event_counter.most_common(5)],
        "days_since_last_trade": days_since_last_trade,
        **_compute_recent_window(activity, closed_positions, days=14, suffix="_14d"),
        **_compute_recent_window(activity, closed_positions, days=28, suffix="_28d"),
        **_compute_recent_window(activity, closed_positions, days=30, suffix="_30d"),
        "biggest_win_usd": round(max((p["realized_pnl"] for p in wins), default=0.0), 2),
        "biggest_loss_usd": round(min((p["realized_pnl"] for p in losses), default=0.0), 2),
    }


def compute_longshot_pattern(closed_positions: list) -> dict:
    """
    Detects a REPEATED low-entry-price, small-stake trading pattern
    ("longshot specialist"): resolved trades where the wallet bought at a
    crowd-implied price of longshot_pattern.max_entry_price or below, with
    a per-trade stake capped at longshot_pattern.max_stake_usd or below.

    Deliberately distinct from two other modules that look similar:
    - strategy_learning/contrarian_win_finder.py surfaces a single
      standout WIN as a manual case study, not a copy-trade signal.
    - wallet_intel/lucky_wallet_detector.py flags when profit is
      concentrated in one or two trades (i.e. likely a fluke).
    This function instead asks "does this wallet DO this on purpose,
    repeatedly, small and capped?" - requiring several qualifying trades
    (min_qualifying_trades) is what separates a real pattern from a
    single lucky longshot bet.
    """
    cfg = ws_cfg.longshot_pattern
    qualifying = [
        p for p in closed_positions
        if p.get("avg_price") is not None and p["avg_price"] <= cfg.max_entry_price
        and p.get("total_bought") is not None and p["total_bought"] <= cfg.max_stake_usd
    ]
    count = len(qualifying)
    wins = [p for p in qualifying if p["realized_pnl"] > 0]
    win_rate = round(len(wins) / count, 4) if count else None
    total_staked = round(sum(p["total_bought"] for p in qualifying), 2)
    total_pnl = round(sum(p["realized_pnl"] for p in qualifying), 2)

    is_pattern = count >= cfg.min_qualifying_trades and total_pnl >= cfg.min_net_pnl_usd

    return {
        "is_longshot_specialist": is_pattern,
        "longshot_trade_count": count,
        "longshot_win_rate": win_rate,
        "longshot_total_staked_usd": total_staked,
        "longshot_total_pnl_usd": total_pnl,
    }


def _compute_recent_window(activity: list, closed_positions: list, days: int, suffix: str) -> dict:
    """
    Trade count and notional volume come from `activity` timestamps -
    these are real, exact trade-level data. Resolved PnL "in the last N
    days" is approximated using each closed position's market end_date
    (resolution date) as the closest available proxy for when that PnL
    was realized - this is honest and real, but it's a proxy, not the
    exact settlement timestamp, so it's labeled clearly wherever displayed.
    """
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - days * 86400

    recent_trades = [a for a in activity if a.get("timestamp") and a["timestamp"] >= cutoff]
    trade_count = len(recent_trades)
    volume = round(sum(a["notional_usd"] for a in recent_trades if a.get("notional_usd")), 2)

    recent_resolved = [
        p for p in closed_positions
        if p.get("end_date") and _parse_date_ts(p["end_date"]) and _parse_date_ts(p["end_date"]) >= cutoff
    ]
    pnl = round(sum(p["realized_pnl"] for p in recent_resolved), 2) if recent_resolved else 0.0
    wins_recent = sum(1 for p in recent_resolved if p["realized_pnl"] > 0)
    win_rate_recent = round(wins_recent / len(recent_resolved), 4) if recent_resolved else None

    return {
        f"trade_count{suffix}": trade_count,
        f"volume_usd{suffix}": volume,
        f"pnl_resolved{suffix}": pnl,
        f"win_rate{suffix}": win_rate_recent,
        f"resolved_count{suffix}": len(recent_resolved),
    }


def _parse_date_ts(date_str) -> float:
    """Best-effort ISO date string -> unix timestamp. Returns None if unparseable."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _compute_recency(activity: list) -> float:
    """
    Days since this wallet's most recent trade. This is the honest check
    for "is this wallet actually still active right now" - a great
    historical win rate from a wallet that hasn't traded in a month is a
    poor copy-trading candidate regardless of past skill.
    """
    if not activity:
        return float("inf")
    timestamps = [a["timestamp"] for a in activity if a.get("timestamp")]
    if not timestamps:
        return float("inf")
    last_ts = max(timestamps)
    now = datetime.now(timezone.utc).timestamp()
    return round((now - last_ts) / 86400, 1)


def _compute_max_drawdown(closed_positions: list) -> float:
    """
    Running-PnL peak-to-trough drawdown across closed positions in
    chronological order (by end_date where available, otherwise as-given
    order from the API, which is already a reasonable proxy).
    """
    if not closed_positions:
        return 0.0
    ordered = sorted(closed_positions, key=lambda p: p.get("end_date") or "")
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in ordered:
        running += p["realized_pnl"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    return round(max_dd, 2)


def _herfindahl_index(counter: Counter) -> float:
    """
    Market concentration index: sum of squared shares. 1.0 = all trades in
    one event (fully concentrated/specialist), close to 0 = evenly spread
    across many events (diversified).
    """
    total = sum(counter.values())
    if total == 0:
        return 0.0
    return round(sum((c / total) ** 2 for c in counter.values()), 4)


def category_performance(closed_positions: list, event_category_map: dict) -> dict:
    """
    Breaks down win rate / PnL by category (politics/crypto/tech/etc.),
    given a mapping of event_slug -> category built by the caller from
    Gamma event data. Positions whose event isn't in the map are skipped
    (we don't guess a category from title text alone).
    """
    by_category = {}
    for p in closed_positions:
        category = event_category_map.get(p.get("event_slug"))
        if not category:
            continue
        bucket = by_category.setdefault(category, {"wins": 0, "losses": 0, "pnl": 0.0})
        if p["realized_pnl"] > 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["pnl"] += p["realized_pnl"]

    result = {}
    for category, b in by_category.items():
        resolved = b["wins"] + b["losses"]
        result[category] = {
            "win_rate": round(b["wins"] / resolved, 4) if resolved else None,
            "pnl": round(b["pnl"], 2),
            "resolved_count": resolved,
        }
    return result
