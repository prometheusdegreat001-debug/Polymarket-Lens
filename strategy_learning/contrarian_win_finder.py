"""
Contrarian Big-Win Finder - the Manual/Strategic Trade Learning Layer.

Explicitly separate from wallet_intel/ (which scores wallets for
copy-trading suitability). This module has a different goal: surface
individual standout trades where a wallet won big by taking a position
AGAINST what the crowd was pricing at the time - genuinely unique,
contrarian behavior - as case studies for the USER's own manual strategic
learning, not as a copy-trade signal.

The key honest insight this relies on: a prediction market's PRICE AT THE
MOMENT OF A TRADE *is* the crowd's aggregated consensus at that instant.
If a wallet bought "Yes" at $0.08, that means the market (i.e., most
money/traders) was pricing only an 8% chance of Yes - overwhelmingly
betting against it. If that trade later resolved Yes, it's a real,
verifiable contrarian win - no external "what did everyone else do" data
is needed, because the price itself already encodes that.
"""

from datetime import datetime, timezone

from historical_context.similar_event_finder import find_similar_resolved_events
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="strategy_learning.contrarian_win_finder",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure arithmetic over already-fetched closed-position and activity data, reuses the free historical precedent matcher.",
))

# A trade counts as "contrarian" if the crowd (market price) was pricing
# the eventual winning outcome at or below this probability - i.e. most
# money was betting against it.
CONTRARIAN_ENTRY_PRICE_MAX = 0.25

# Minimum realized profit to bother featuring as a "big win" case study.
MIN_BIG_WIN_PNL_USD = 500


def find_contrarian_big_win(closed_positions: list, activity: list, closed_events_pool: list = None) -> dict:
    """
    Scans a wallet's resolved positions for the single most notable
    contrarian big win (lowest entry price + biggest payout), and builds
    a full case-study dict around it: when bought, how long held, a
    clearly-labeled SPECULATED rationale (never presented as fact), and
    similar past events for context.

    Returns None if no qualifying contrarian win is found - this is not
    forced to always return something.
    """
    candidates = [
        p for p in closed_positions
        if p.get("realized_pnl", 0) >= MIN_BIG_WIN_PNL_USD
        and p.get("avg_price") is not None
        and p["avg_price"] <= CONTRARIAN_ENTRY_PRICE_MAX
    ]
    if not candidates:
        return None

    # Feature the single biggest one - most useful as a standout case study.
    win = max(candidates, key=lambda p: p["realized_pnl"])

    entry_info = _find_entry_details(win, activity)
    holding_days, was_live_entry = _compute_holding_duration(win, entry_info)
    rationale = _speculate_rationale(win, entry_info, holding_days, was_live_entry)
    similar_events = (
        find_similar_resolved_events(win.get("title", ""), closed_events_pool,
                                      exclude_slug=win.get("event_slug"))
        if closed_events_pool else []
    )

    # Stable identity for THIS specific win, used by storage/db.py to dedup
    # the Discord alert at the trade level rather than the wallet level -
    # see contrarian_alerts_sent. Falls back to market_title when
    # event_slug/entry_timestamp are missing so we still get a usable key
    # rather than crashing or silently colliding two different wins.
    win_key = "|".join(str(x) for x in (
        win.get("event_slug") or win.get("title") or "unknown_event",
        win.get("outcome") or "unknown_outcome",
        entry_info.get("entry_timestamp") or "unknown_entry_ts",
    ))

    return {
        "win_key": win_key,
        "market_title": win.get("title"),
        "event_slug": win.get("event_slug"),
        "outcome_bought": win.get("outcome"),
        "entry_price": win.get("avg_price"),
        "crowd_implied_probability_pct": round(win.get("avg_price", 0) * 100, 1),
        "realized_pnl": win.get("realized_pnl"),
        "total_bought_usd": win.get("total_bought"),
        "payout_multiple": round(win["realized_pnl"] / win["total_bought"], 1) if win.get("total_bought") else None,
        "entry_timestamp": entry_info.get("entry_timestamp"),
        "entry_time_display": _format_time(entry_info.get("entry_timestamp")),
        "resolution_date": win.get("end_date"),
        "holding_days": holding_days,
        "was_live_entry": was_live_entry,
        "speculated_rationale": rationale,
        "similar_past_events": similar_events,
    }


def _find_entry_details(win: dict, activity: list) -> dict:
    """Finds the earliest matching BUY trade for this same market/outcome - the actual entry point."""
    matches = [
        a for a in activity
        if a.get("event_slug") == win.get("event_slug")
        and a.get("outcome") == win.get("outcome")
        and a.get("side") == "BUY"
    ]
    if not matches:
        return {"entry_timestamp": None}
    earliest = min(matches, key=lambda a: a.get("timestamp", float("inf")))
    return {"entry_timestamp": earliest.get("timestamp")}


def _compute_holding_duration(win: dict, entry_info: dict):
    """
    Days between entry and resolution. Assumes held to resolution (the
    realized_pnl came from the market resolving, not an interim sale) -
    stated as an assumption, not asserted with false precision if we
    can't fully verify no interim trading occurred.

    Can legitimately come out negative: `end_date` is the market's
    LISTED/scheduled close time, but live/in-play markets (esports BO3s,
    in-game sports markets) often keep accepting trades after that
    listed time and resolve later - so an entry can be timestamped after
    the nominal end_date without anything being wrong. Rather than
    silently showing a nonsensical "-0.5 days," this is clamped to 0 for
    display and the caller is told a flag was raised, so
    _speculate_rationale can say what actually happened instead of
    guessing an "informational edge" story that doesn't fit a negative
    number.
    """
    entry_ts = entry_info.get("entry_timestamp")
    end_date_str = win.get("end_date")
    if not entry_ts or not end_date_str:
        return None, False
    try:
        end_ts = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None, False
    raw_days = (end_ts - entry_ts) / 86400
    if raw_days < 0:
        return 0.0, True
    return round(raw_days, 1), False


def _speculate_rationale(win: dict, entry_info: dict, holding_days, was_live_entry: bool = False) -> str:
    """
    A clearly-labeled SPECULATION about why this trade might have been
    made - never presented as confirmed fact, since we cannot know the
    wallet's actual reasoning. Offers plausible, data-grounded
    possibilities rather than asserting one.
    """
    price = win.get("avg_price", 0)
    multiple = round(win["realized_pnl"] / win["total_bought"], 1) if win.get("total_bought") else None

    possibilities = []
    if was_live_entry:
        possibilities.append(
            "a live/in-play entry - the trade timestamp is after the market's LISTED close time, which "
            "happens on markets (esports BO3s, in-game sports lines) that keep accepting trades past their "
            "scheduled time and resolve later; this makes an 'informational edge' story less likely and a "
            "live-odds/momentum read more likely"
        )
    if price <= 0.10:
        possibilities.append(
            "a high-conviction, low-probability 'longshot' bet (Barbell/Tail-style) - small stake, "
            "asymmetric payoff if right"
        )
    if not was_live_entry and holding_days is not None and holding_days <= 3:
        possibilities.append(
            "a fast-moving informational edge - entering shortly before resolution suggests they may have "
            "acted on information not yet reflected in the price"
        )
    elif holding_days is not None and holding_days >= 30:
        possibilities.append(
            "a long-held independent conviction position, entered early and held through resolution "
            "regardless of interim price moves"
        )
    if not possibilities:
        possibilities.append("an independent probability estimate that diverged sharply from the market's pricing")

    base = (
        f"SPECULATED (not confirmed) rationale - the entry price of {price:.2f} means the market was "
        f"pricing this outcome at only {price*100:.0f}% likely, so most money was betting against it. "
        f"Possible explanations: " + "; or ".join(possibilities) + "."
    )
    if multiple is not None:
        base += f" This produced a {multiple}x return on capital deployed."
    return base


def _format_time(timestamp) -> str:
    if not timestamp:
        return "unknown"
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")
