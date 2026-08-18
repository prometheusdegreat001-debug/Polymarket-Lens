"""
Combines classification + score + luck detection into the final
copy_trade_recommendation and why_copy_or_not explanation - the piece that
turns numbers into an actual sentence a human reads.
"""

from wallet_intel.wallet_classifier import classify_wallet
from wallet_intel.lucky_wallet_detector import detect_luck
from wallet_intel.wallet_scoring import compute_copy_trade_score, recommendation_from_score
from config.loader import wallet_scoring as ws_cfg
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="wallet_intel.copy_trade_filter",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - combines already-computed free scores into a final verdict.",
))


_RECOMMENDATION_LABELS = {"copy": "Yes", "watch": "Conditional", "avoid": "No"}


def evaluate_wallet(closed_positions: list, features: dict) -> dict:
    luck_flags = detect_luck(closed_positions, features)
    behavior_label = classify_wallet(features, luck_flags)
    score = compute_copy_trade_score(features, luck_flags)
    recommendation = recommendation_from_score(score)

    days_dormant = features.get("days_since_last_trade")
    recommendation, dormancy_note = _apply_dormancy_override(recommendation, days_dormant)

    drift_result = features.get("drift_result") or {}
    recommendation, drift_note = _apply_drift_override(recommendation, drift_result)

    sample_quality_note = _sample_quality_note(features.get("resolved_trade_count", 0))
    longshot = features.get("longshot_pattern") or {}
    why = _build_explanation(features, luck_flags, behavior_label, score, recommendation,
                              dormancy_note, sample_quality_note, drift_note, longshot)

    return {
        "behavior_label": behavior_label,
        "longshot_pattern": longshot,
        "copy_trade_score": score,
        "copy_trade_score_10": round(score / 10, 1),
        "copy_trade_recommendation": recommendation,
        "copy_trade_recommendation_label": _RECOMMENDATION_LABELS.get(recommendation, "Conditional"),
        "why_copy_or_not": why,
        "luck_flags": luck_flags,
        "days_since_last_trade": days_dormant,
        "biggest_win_usd": features.get("biggest_win_usd", 0.0),
        "biggest_loss_usd": features.get("biggest_loss_usd", 0.0),
        "recent_14d_summary": _recent_summary(features),
        "sample_quality": sample_quality_note,
        "activity_pattern_label": features.get("activity_pattern_label", "unknown"),
        "avg_active_days_per_week": features.get("avg_active_days_per_week", 0.0),
        "is_bursty": features.get("is_bursty", False),
    }


def _sample_quality_note(resolved_count: int) -> str:
    if resolved_count >= ws_cfg.preferred_min_resolved_trades:
        return f"Statistically meaningful sample ({resolved_count} resolved trades)."
    return (
        f"Small sample ({resolved_count} resolved trades, below the "
        f"{ws_cfg.preferred_min_resolved_trades}-trade preferred bar) - treat with extra caution, "
        f"a handful of trades can look like skill by chance."
    )


def _recent_summary(features: dict) -> str:
    trades_14d = features.get("trade_count_14d", 0)
    volume_14d = features.get("volume_usd_14d", 0.0)
    pnl_14d = features.get("pnl_resolved_14d", 0.0)
    resolved_14d = features.get("resolved_count_14d", 0)
    win_rate_14d = features.get("win_rate_14d")

    if trades_14d == 0:
        return "No trades in the last 14 days."

    win_rate_str = f"{win_rate_14d*100:.0f}%" if win_rate_14d is not None else "N/A (nothing resolved yet)"
    return (
        f"Last 14 days: {trades_14d} trade(s), ~${volume_14d:,.0f} volume, "
        f"{resolved_14d} resolved (win rate {win_rate_str}), "
        f"${pnl_14d:,.0f} PnL from trades resolved in that window."
    )


def _apply_dormancy_override(recommendation: str, days_dormant) -> tuple:
    """
    A wallet's historical score doesn't matter if it's gone quiet - this
    downgrades the recommendation regardless of how good the numbers look,
    since a copy-trade candidate needs to actually still be trading.
    """
    cfg = ws_cfg.activity_recency
    if days_dormant is None or days_dormant == float("inf"):
        return recommendation, None

    if days_dormant > cfg.max_days_dormant_for_watch:
        return "avoid", (
            f"⚠️ DORMANT: no trades in {days_dormant:.0f} days (over "
            f"{cfg.max_days_dormant_for_watch:.0f}-day cutoff) - likely inactive or abandoned wallet."
        )
    if days_dormant > cfg.max_days_dormant_for_copy:
        if recommendation == "copy":
            return "watch", (
                f"⚠️ Downgraded from 'copy' to 'watch': no trades in {days_dormant:.0f} days "
                f"(over the {cfg.max_days_dormant_for_copy:.0f}-day active-trading cutoff). "
                f"Historical record looks good, but this wallet isn't currently active."
            )
        return recommendation, (
            f"Note: no trades in {days_dormant:.0f} days - not currently active."
        )
    return recommendation, None


def _apply_drift_override(recommendation: str, drift_result: dict) -> tuple:
    """
    Phase 5 decision rules, applied as a hard override regardless of the
    base score - a wallet whose current behavior has confirmed-drifted
    away from its own established strategy should not be trusted for
    copy-trading even if its historical numbers still look good.
    """
    if not drift_result or drift_result.get("insufficient_history"):
        return recommendation, None

    status = drift_result.get("drift_status")
    adherence = drift_result.get("adherence_rate_pct")

    if status == "confirmed_drift":
        return "avoid", (
            f"⚠️ CONFIRMED STRATEGY DRIFT: adherence rate {adherence}% "
            f"(below the 65% threshold) - this wallet's current behavior no longer "
            f"matches its own established strategy closely enough to trust for copy-trading, "
            f"regardless of its historical score."
        )
    if status == "mild_drift":
        if recommendation == "copy":
            return "watch", (
                f"⚠️ Downgraded from 'copy' to 'watch': mild strategy drift detected "
                f"(adherence rate {adherence}%) - worth monitoring before committing capital."
            )
        return recommendation, f"Mild strategy drift detected (adherence rate {adherence}%) - worth monitoring."

    return recommendation, None


def _build_explanation(features, luck_flags, behavior_label, score, recommendation,
                        dormancy_note, sample_quality_note, drift_note=None, longshot=None) -> str:
    win_rate = features.get("win_rate")
    resolved = features.get("resolved_trade_count", 0)
    breadth = features.get("market_breadth", 0)

    longshot_note = None
    if longshot and longshot.get("is_longshot_specialist"):
        longshot_note = (
            f"🎯 Longshot specialist pattern: {longshot['longshot_trade_count']} trades at "
            f"low entry prices with stakes capped near $100, net "
            f"${longshot['longshot_total_pnl_usd']:,.0f} PnL on those trades. This is a real, "
            f"repeated small-stake style, not the same as a one-off lucky win - but it's still "
            f"a higher-variance style than a consistent broad trader, worth weighing accordingly."
        )

    if luck_flags["is_luck_dominated"]:
        base = (
            f"Classified as {behavior_label} (score {score}/100, {round(score/10,1)}/10): "
            + "; ".join(luck_flags["reasons"])
            + ". Not enough evidence of a repeatable edge yet."
        )
    else:
        win_rate_str = f"{win_rate*100:.0f}%" if win_rate is not None else "unknown"
        base = (
            f"Classified as {behavior_label} (score {score}/100, {round(score/10,1)}/10): {win_rate_str} win rate "
            f"across {resolved} resolved trades spanning {breadth} distinct events, "
            f"with no luck-concentration flags triggered. {sample_quality_note}"
        )

    prefix = " ".join(n for n in [dormancy_note, drift_note] if n)
    if prefix:
        base = f"{prefix} {base}"
    if longshot_note:
        base = f"{base} {longshot_note}"
    return base
