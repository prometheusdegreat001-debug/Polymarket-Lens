"""
Behavioral features - the patterns wallet_intel/wallet_classifier.py uses
to tell "smart money" from "emotional trader" from "noise wallet". All
computed from real /activity timestamps and side (BUY/SELL), no fabricated
psychology.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="features.behavior_features",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))

import math
from collections import Counter
from datetime import datetime, timezone


def compute_behavior_features(activity: list) -> dict:
    if not activity:
        return {
            "directional_bias": 0.0, "timing_entropy": 0.0,
            "avg_holding_duration_days": None, "entry_timing_label": "unknown",
            "buy_ratio": None,
        }

    buy_count = sum(1 for a in activity if a["side"] == "BUY")
    sell_count = len(activity) - buy_count
    buy_ratio = round(buy_count / len(activity), 4)
    # directional_bias: -1 (always sells) to +1 (always buys)
    directional_bias = round((buy_count - sell_count) / len(activity), 4)

    timing_entropy = _compute_timing_entropy(activity)
    entry_timing_label = _classify_entry_timing(activity)
    activity_consistency = _compute_activity_consistency(activity)

    return {
        "directional_bias": directional_bias,
        "buy_ratio": buy_ratio,
        "timing_entropy": timing_entropy,
        "avg_holding_duration_days": _estimate_avg_holding_duration(activity),
        "entry_timing_label": entry_timing_label,
        **activity_consistency,
    }


def _compute_activity_consistency(activity: list) -> dict:
    """
    Real "active days per week" and burst-vs-spread analysis from actual
    trade timestamps - this is what distinguishes a consistent trader from
    a bot that fires 50 trades in one hour then vanishes for a month, or
    from a wallet that's genuinely active but only in short intense bursts.

    Measured against NOW (not just the min/max of the activity list itself)
    - a wallet whose only data is a 3-day burst 90 days ago would otherwise
    look "perfectly consistent" over its own tiny internal span, when the
    real picture (burst, then 90 days of silence) only shows up once you
    compare against the present.

    Returns:
      avg_active_days_per_week: mean distinct calendar days with >=1 trade,
        per 7-day window, across the span from first trade to now.
      is_bursty: True if activity is concentrated in a small fraction of
        that full span rather than spread out.
      active_weeks_ratio: fraction of weeks (first-trade-to-now) that had
        ANY activity at all.
    """
    timestamps = sorted(a["timestamp"] for a in activity if a.get("timestamp"))
    if len(timestamps) < 2:
        return {"avg_active_days_per_week": 0.0, "is_bursty": False, "active_weeks_ratio": 0.0}

    now = datetime.now(timezone.utc).timestamp()
    span_days = (now - timestamps[0]) / 86400
    if span_days < 1:
        # First trade was today - too little history to judge consistency either way.
        return {"avg_active_days_per_week": 7.0, "is_bursty": False, "active_weeks_ratio": 1.0}

    distinct_days = {
        datetime.fromtimestamp(ts, tz=timezone.utc).date() for ts in timestamps
    }
    total_weeks = max(span_days / 7, 1)
    avg_active_days_per_week = round(len(distinct_days) / total_weeks, 2)

    # Burst detection: what fraction of the wallet's full first-trade-to-now
    # lifespan actually had trading activity, at the WEEK level. A wallet
    # trading in 2 weeks out of 30 is bursty even if those 2 weeks were
    # individually busy every day.
    active_week_buckets = {int((ts - timestamps[0]) // (7 * 86400)) for ts in timestamps}
    active_weeks_ratio = round(len(active_week_buckets) / total_weeks, 4)
    is_bursty = active_weeks_ratio < 0.25 and span_days > 21  # only meaningful over a long-enough span

    return {
        "avg_active_days_per_week": min(avg_active_days_per_week, 7.0),
        "is_bursty": is_bursty,
        "active_weeks_ratio": min(active_weeks_ratio, 1.0),
    }


def _compute_timing_entropy(activity: list) -> float:
    """
    Shannon entropy of trade-hour-of-day distribution, normalized to 0-1.
    0 = every trade happens at the exact same hour (highly mechanical/bot-like
    or a person with one fixed routine); 1 = trades spread uniformly across
    all 24 hours (no discernible timing pattern).
    """
    hours = []
    for a in activity:
        ts = a.get("timestamp")
        if ts is None:
            continue
        hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        hours.append(hour)

    if not hours:
        return 0.0

    counts = Counter(hours)
    total = len(hours)
    entropy = -sum((c / total) * math.log2(c / total) for c in counts.values())
    max_entropy = math.log2(24)
    return round(entropy / max_entropy, 4) if max_entropy else 0.0


def _classify_entry_timing(activity: list) -> str:
    """
    Placeholder heuristic pending real news-timestamp correlation (that
    requires the verification/historical_context LLM layer to know when
    news actually broke for each trade's market). Without that, we can
    only observe trade cadence, not true "early vs reactive vs late"
    relative to external events - so we report 'unknown' honestly rather
    than guessing.
    """
    return "unknown"


def _estimate_avg_holding_duration(activity: list) -> float:
    """
    Rough proxy: average gap between consecutive trades on the SAME event
    (a BUY followed later by a SELL on the same event_slug approximates a
    round-trip hold). This undercounts positions still open (no matching
    SELL yet) - those are excluded, which biases toward shorter *closed*
    holds only. Flagged as an estimate, not exact.
    """
    by_event = {}
    for a in sorted(activity, key=lambda x: x.get("timestamp") or 0):
        key = a.get("event_slug") or a.get("title")
        if not key:
            continue
        by_event.setdefault(key, []).append(a)

    durations_days = []
    for trades in by_event.values():
        buys = [t for t in trades if t["side"] == "BUY"]
        sells = [t for t in trades if t["side"] == "SELL"]
        if buys and sells:
            first_buy_ts = buys[0]["timestamp"]
            first_sell_after = next((s["timestamp"] for s in sells if s["timestamp"] > first_buy_ts), None)
            if first_sell_after:
                durations_days.append((first_sell_after - first_buy_ts) / 86400)

    if not durations_days:
        return None
    return round(sum(durations_days) / len(durations_days), 2)
