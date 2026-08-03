"""
Transaction Explainer (Phase 3) - produces a clean, human-readable summary
for each of a wallet's selected recent trades.

Honesty note on scope: two of the requested fields need a real caveat.

1. "Full resolution criteria" - real, pulled live from Gamma per the
   trade's event (one call per DISTINCT event among the selected trades,
   deduplicated and cached for the process lifetime so repeat trades in
   the same event don't refetch).

2. "Estimated market impact (price move, liquidity consumed, % of open
   interest)" - there is no free (or paid, within this system's scope)
   source for HISTORICAL order-book depth at the exact time of a past
   trade. What IS real and available is the market's CURRENT liquidity.
   This module computes the trade's notional size as a % of CURRENT
   liquidity as an honest proxy for "how big was this trade relative to
   the market," explicitly labeled as a current-liquidity estimate, not a
   true historical price-impact measurement. We do not fabricate a
   historical price-move number we have no way to verify.
"""

from datetime import datetime, timezone

from ingestion.polymarket_api import fetch_event_by_slug
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="wallet_intel.transaction_explainer",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy=(
        "Uses free Gamma event lookups, deduplicated per distinct event "
        "among the selected trades (not per-trade) to keep calls bounded. "
        "If a lookup fails, resolution criteria and liquidity context are "
        "shown as 'not available' rather than guessed."
    ),
))

_event_cache = {}


def explain_recent_trades(activity: list, max_trades: int = 5) -> list:
    """
    Returns a list of formatted markdown blocks, one per selected recent
    trade (most recent first), each a self-contained human-readable
    summary.
    """
    if not activity:
        return []

    recent = sorted(activity, key=lambda a: a.get("timestamp", 0), reverse=True)[:max_trades]
    blocks = []
    for trade in recent:
        blocks.append(_explain_trade(trade))
    return blocks


def _explain_trade(trade: dict) -> str:
    event_slug = trade.get("event_slug")
    event = _get_event_cached(event_slug) if event_slug else None

    market_title = trade.get("title") or (event.get("title") if event else "Unknown market")
    resolution_rule = _extract_resolution_rule(event)
    side = trade.get("outcome") or "Unknown"
    action = trade.get("side", "BUY")
    shares = trade.get("size", 0.0)
    price = trade.get("price", 0.0)
    notional = trade.get("notional_usd", shares * price)
    timestamp = trade.get("timestamp")

    time_str = _format_time(timestamp)
    liquidity_note = _estimate_liquidity_context(notional, event)

    return (
        f"- **{market_title}**\n"
        f"  Resolution criteria: {resolution_rule}\n"
        f"  Action: {action} \"{side}\" | {shares:,.2f} shares @ ${price:.3f} = ${notional:,.2f} USDC\n"
        f"  {liquidity_note}\n"
        f"  Time: {time_str}"
    )


def get_event_cached(event_slug: str):
    """Public wrapper around the internal cache - used by main.py's
    market-options breakdown (Phase 4) to reuse the same dedup cache as
    the transaction explainer, avoiding a duplicate fetch for the same event."""
    return _get_event_cached(event_slug)


def _get_event_cached(event_slug: str):
    if event_slug in _event_cache:
        return _event_cache[event_slug]
    try:
        event = fetch_event_by_slug(event_slug)
    except Exception:
        event = None
    _event_cache[event_slug] = event
    return event


def _extract_resolution_rule(event) -> str:
    if not event:
        return "not available (event lookup failed)"
    for m in event.get("markets", []):
        rule = m.get("resolution_rule")
        if rule:
            return rule
    return "not published / not found in market data"


def _estimate_liquidity_context(notional: float, event) -> str:
    if not event or not event.get("markets"):
        return "Market impact: not available (current liquidity lookup failed)."
    current_liquidity = max((m.get("liquidity", 0.0) for m in event["markets"]), default=0.0)
    if current_liquidity <= 0:
        return "Market impact: not available (no current liquidity data)."
    pct = (notional / current_liquidity) * 100
    return (
        f"Market impact (estimate): this trade's ${notional:,.0f} is ~{pct:.1f}% of the market's "
        f"CURRENT liquidity (${current_liquidity:,.0f}) - this is a present-day reference point, "
        f"NOT the actual historical depth at the moment this trade executed, which isn't available "
        f"from free data sources."
    )


def _format_time(timestamp) -> str:
    if not timestamp:
        return "unknown time"
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    delta_days = (now - dt).days
    if delta_days == 0:
        relative = "today"
    elif delta_days == 1:
        relative = "yesterday"
    else:
        relative = f"{delta_days} days ago"
    return f"{dt.strftime('%Y-%m-%d %H:%M UTC')} ({relative})"
