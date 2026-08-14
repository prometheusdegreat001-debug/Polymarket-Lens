"""
Strategy Consistency Score + Drift Detection (Phase 5).

Honesty note on approach: we don't store per-wallet snapshots across scan
cycles, so "drift" here is measured WITHIN a single wallet's own trade
history - split into a BASELINE period (everything before the recent
window) and a RECENT period (last ~6 weeks / 20-30 trades, whichever the
data supports). This is a real, defensible way to detect drift without
needing weeks of accumulated scan history first.

The CUSUM component is explicitly a LIGHTWEIGHT proxy (bucketed HHI trend
across the recent period), not a rigorous statistical control chart -
labeled as such everywhere it's surfaced, matching the spec's own
"lightweight CUSUM" framing.
"""

from collections import Counter
from datetime import datetime, timezone

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="wallet_intel.strategy_drift",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure arithmetic over already-fetched activity/closed-position data.",
))

RECENT_WINDOW_DAYS = 42     # upper end of the "4-6 weeks" spec range
RECENT_MIN_TRADES = 20      # lower end of "20-30 trades"


def analyze_strategy_drift(activity: list, closed_positions: list, event_category_map: dict = None) -> dict:
    """
    Returns {consistency_score, drift_status, adherence_rate_pct,
    cusum_alarm, cusum_strength, core_category, core_pnl, peripheral_pnl,
    peripheral_dominance, reasons, insufficient_history}.

    event_category_map ({event_slug: category_name}), if given, groups
    trades by their REAL category (crypto/politics/etc.) rather than
    individual event_slug - this matters a lot: a wallet diversifying
    across many DIFFERENT crypto events is normal, stable behavior within
    one strategy, not "drift." Without a category map, falls back to
    event_slug-level grouping (finer-grained, more conservative).
    """
    if len(activity) < RECENT_MIN_TRADES + 5:
        return _insufficient_history_result()

    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - RECENT_WINDOW_DAYS * 86400
    recent = [a for a in activity if a.get("timestamp", 0) >= cutoff]
    baseline = [a for a in activity if a.get("timestamp", 0) < cutoff]

    # Time-based split too thin - fall back to count-based split.
    if len(recent) < RECENT_MIN_TRADES or len(baseline) < 5:
        sorted_activity = sorted(activity, key=lambda a: a.get("timestamp", 0))
        split_idx = max(len(sorted_activity) - RECENT_MIN_TRADES, 1)
        baseline = sorted_activity[:split_idx]
        recent = sorted_activity[split_idx:]

    if not baseline or not recent:
        return _insufficient_history_result()

    event_category_map = event_category_map or {}

    baseline_cat = _category_distribution(baseline, event_category_map)
    recent_cat = _category_distribution(recent, event_category_map)

    # CONFIRMED BUG this fixes: a wallet that legitimately trades MULTIPLE
    # established patterns/categories (e.g. crypto AND sports AND politics,
    # by design) was getting flagged as "drifting" every time the MIX
    # between those same established categories shifted period to period -
    # even though it never left its own established repertoire. Trading
    # more crypto this month and more sports last month isn't drift for a
    # wallet that has always done both - it's normal variation in which
    # markets looked good when. A single-pattern wallet whose recent
    # activity swings toward one dominant category IS meaningfully
    # different from one that's always spread across several.
    #
    # Fix: a category only counts as part of the wallet's ESTABLISHED
    # repertoire if it was a meaningful (>=5%) share of the BASELINE
    # period. Adherence and peripheral-dominance are then judged by
    # whether recent activity stays WITHIN that established repertoire,
    # not by how the exact percentage weighting shifts within it. A
    # wallet only gets flagged for genuinely expanding into NEW,
    # previously-untested categories - a real signal, not noise.
    established_categories = {c for c, share in baseline_cat.items() if share >= 0.05}
    is_multi_pattern_baseline = len(established_categories) >= 2
    novel_share = sum(share for c, share in recent_cat.items() if c not in established_categories)

    if is_multi_pattern_baseline:
        adherence_rate = round(max(0.0, 1.0 - novel_share), 4)
    else:
        adherence_rate = _distribution_similarity(baseline_cat, recent_cat)

    baseline_cv = _size_cv(baseline)
    recent_cv = _size_cv(recent)
    sizing_score = max(0.0, 1.0 - abs(recent_cv - baseline_cv))

    baseline_timing = _timing_stats(baseline)
    recent_timing = _timing_stats(recent)
    timing_score = _timing_stability_score(baseline_timing, recent_timing)

    baseline_hhi = _herfindahl(baseline_cat)
    recent_hhi = _herfindahl(recent_cat)
    category_focus_score = max(0.0, 1.0 - abs(recent_hhi - baseline_hhi))

    baseline_win_rate = _win_rate_for_period(closed_positions, baseline, event_category_map)
    recent_win_rate = _win_rate_for_period(closed_positions, recent, event_category_map)
    performance_score = (
        max(0.0, 1.0 - abs(recent_win_rate - baseline_win_rate))
        if baseline_win_rate is not None and recent_win_rate is not None
        else 0.5
    )

    consistency_score = round(
        100 * (
            0.40 * adherence_rate
            + 0.20 * sizing_score
            + 0.20 * timing_score
            + 0.10 * category_focus_score
            + 0.10 * performance_score
        )
    )

    cusum_alarm, cusum_strength = _lightweight_cusum(recent, event_category_map)

    # Peripheral dominance: same established-repertoire fix as adherence_rate
    # above. For a multi-pattern wallet, "peripheral" means genuinely NEW,
    # untested categories dominating recent activity - not "anything other
    # than whichever single category happened to be largest in baseline,"
    # which would falsely flag a wallet that's always split its activity
    # fairly evenly across several established patterns.
    core_category = max(baseline_cat.items(), key=lambda kv: kv[1])[0] if baseline_cat else None
    core_pnl, peripheral_pnl = _core_peripheral_pnl(closed_positions, core_category, event_category_map)

    if is_multi_pattern_baseline:
        peripheral_dominance = novel_share > 0.5
    else:
        core_share_recent = recent_cat.get(core_category, 0.0) if core_category else 0.0
        peripheral_dominance = (1 - core_share_recent) > 0.5

    adherence_pct = round(adherence_rate * 100, 1)

    if adherence_pct < 65 or (cusum_strength == "strong" and peripheral_dominance):
        drift_status = "confirmed_drift"
    elif adherence_pct < 80 or cusum_strength == "mild":
        drift_status = "mild_drift"
    else:
        drift_status = "stable"

    reasons = _build_reasons(adherence_pct, cusum_alarm, cusum_strength, peripheral_dominance,
                              drift_status, is_multi_pattern_baseline, established_categories)

    return {
        "consistency_score": consistency_score,
        "drift_status": drift_status,
        "adherence_rate_pct": adherence_pct,
        "cusum_alarm": cusum_alarm,
        "cusum_strength": cusum_strength,
        "core_category": core_category,
        "core_pnl": round(core_pnl, 2),
        "peripheral_pnl": round(peripheral_pnl, 2),
        "peripheral_dominance": peripheral_dominance,
        "is_multi_pattern_baseline": is_multi_pattern_baseline,
        "established_categories": sorted(established_categories),
        "reasons": reasons,
        "insufficient_history": False,
    }


def _insufficient_history_result() -> dict:
    return {
        "consistency_score": None,
        "drift_status": "insufficient_history",
        "adherence_rate_pct": None,
        "cusum_alarm": False,
        "cusum_strength": "none",
        "core_category": None,
        "core_pnl": 0.0,
        "peripheral_pnl": 0.0,
        "peripheral_dominance": False,
        "reasons": ["Not enough trade history yet to split into baseline vs. recent periods for drift analysis."],
        "insufficient_history": True,
    }


def _category_distribution(trades: list, event_category_map: dict) -> dict:
    def cat_of(t):
        slug = t.get("event_slug")
        return event_category_map.get(slug, slug or t.get("title"))
    counter = Counter(cat_of(t) for t in trades if t.get("event_slug") or t.get("title"))
    total = sum(counter.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in counter.items()}


def _distribution_similarity(a: dict, b: dict) -> float:
    """1.0 = identical distributions, 0.0 = totally disjoint. 1 - total variation distance."""
    if not a or not b:
        return 0.5  # neutral - can't compare
    keys = set(a.keys()) | set(b.keys())
    tvd = sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys) / 2
    return round(max(0.0, 1.0 - tvd), 4)


def _size_cv(trades: list) -> float:
    sizes = [t["notional_usd"] for t in trades if t.get("notional_usd")]
    if len(sizes) < 2:
        return 0.0
    mean = sum(sizes) / len(sizes)
    if mean == 0:
        return 0.0
    variance = sum((s - mean) ** 2 for s in sizes) / len(sizes)
    return (variance ** 0.5) / mean


def _timing_stats(trades: list) -> dict:
    if len(trades) < 2:
        return {"avg_active_days_per_week": 0.0}
    timestamps = sorted(t["timestamp"] for t in trades if t.get("timestamp"))
    if len(timestamps) < 2:
        return {"avg_active_days_per_week": 0.0}
    span_days = max((timestamps[-1] - timestamps[0]) / 86400, 1)
    distinct_days = {datetime.fromtimestamp(ts, tz=timezone.utc).date() for ts in timestamps}
    return {"avg_active_days_per_week": min(len(distinct_days) / max(span_days / 7, 1), 7.0)}


def _timing_stability_score(baseline_timing: dict, recent_timing: dict) -> float:
    diff = abs(recent_timing["avg_active_days_per_week"] - baseline_timing["avg_active_days_per_week"])
    return max(0.0, 1.0 - diff / 7.0)


def _herfindahl(distribution: dict) -> float:
    return sum(p ** 2 for p in distribution.values()) if distribution else 0.0


def _win_rate_for_period(closed_positions: list, period_trades: list, event_category_map: dict) -> float:
    def cat_of_slug(slug):
        return event_category_map.get(slug, slug)
    period_categories = {cat_of_slug(t.get("event_slug")) for t in period_trades if t.get("event_slug")}
    if not period_categories:
        return None
    relevant = [p for p in closed_positions if cat_of_slug(p.get("event_slug")) in period_categories]
    if not relevant:
        return None
    wins = sum(1 for p in relevant if p["realized_pnl"] > 0)
    return wins / len(relevant)


def _lightweight_cusum(recent_trades: list, event_category_map: dict) -> tuple:
    """
    Practical proxy for CUSUM (not a rigorous statistical control chart,
    per the spec's own "lightweight" framing): splits the recent period
    into up to 4 chronological buckets, computes HHI per bucket, and
    checks whether concentration is trending consistently in one direction
    with a total swing beyond a threshold.
    """
    sorted_trades = sorted(recent_trades, key=lambda t: t.get("timestamp", 0))
    n_buckets = min(4, max(2, len(sorted_trades) // 5))
    if n_buckets < 2:
        return False, "none"

    bucket_size = max(len(sorted_trades) // n_buckets, 1)
    buckets = [sorted_trades[i:i + bucket_size] for i in range(0, len(sorted_trades), bucket_size)][:n_buckets]
    hhis = [_herfindahl(_category_distribution(b, event_category_map)) for b in buckets if b]

    if len(hhis) < 2:
        return False, "none"

    total_swing = hhis[-1] - hhis[0]
    monotonic = all(hhis[i] <= hhis[i + 1] for i in range(len(hhis) - 1)) or \
                all(hhis[i] >= hhis[i + 1] for i in range(len(hhis) - 1))

    if monotonic and abs(total_swing) > 0.30:
        return True, "strong"
    if monotonic and abs(total_swing) > 0.15:
        return True, "mild"
    return False, "none"


def _core_peripheral_pnl(closed_positions: list, core_category, event_category_map: dict) -> tuple:
    def cat_of_slug(slug):
        return event_category_map.get(slug, slug)
    if not core_category:
        return 0.0, sum(p["realized_pnl"] for p in closed_positions)
    core_pnl = sum(p["realized_pnl"] for p in closed_positions if cat_of_slug(p.get("event_slug")) == core_category)
    peripheral_pnl = sum(p["realized_pnl"] for p in closed_positions if cat_of_slug(p.get("event_slug")) != core_category)
    return core_pnl, peripheral_pnl


def _build_reasons(adherence_pct, cusum_alarm, cusum_strength, peripheral_dominance, drift_status,
                    is_multi_pattern_baseline=False, established_categories=None) -> list:
    reasons = [f"Strategy adherence rate: {adherence_pct}% (recent behavior vs. established baseline)."]
    if is_multi_pattern_baseline and established_categories:
        reasons.append(
            f"This wallet has an established multi-pattern repertoire ({', '.join(sorted(established_categories))}) - "
            f"shifting the MIX between these is normal and not counted as drift; only genuinely new, "
            f"untested categories are."
        )
    if cusum_alarm:
        reasons.append(f"Lightweight CUSUM flagged a {cusum_strength} directional shift in category concentration.")
    if peripheral_dominance:
        if is_multi_pattern_baseline:
            reasons.append("Most of this wallet's RECENT trading activity is in categories OUTSIDE its established multi-pattern repertoire - genuinely new territory.")
        else:
            reasons.append("Most of this wallet's RECENT trading activity (by trade share, not PnL) is outside its established core category.")
    if drift_status == "confirmed_drift":
        reasons.append("This wallet's current behavior no longer matches its established strategy closely enough to trust for copy-trading.")
    elif drift_status == "mild_drift":
        reasons.append("Some drift detected - worth monitoring, not yet a hard rejection.")
    else:
        reasons.append("Behavior remains consistent with this wallet's established strategy.")
    return reasons
