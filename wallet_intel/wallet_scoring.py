"""
CopyTradingSuitabilityScore (0-100) - matches the exact 5-factor weighting
requested in the Wallet Intelligence Layer v2.4 spec:
  Address Type (25%) | Activity Consistency (25%) | Behavioral Quality (20%)
  | Risk Management (15%) | Evidence of Edge (15%)
Pure arithmetic, no external calls.
"""

from config.loader import wallet_scoring as ws_cfg
from wallet_intel.lucky_wallet_detector import compute_penalty
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="wallet_intel.wallet_scoring",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - weighted arithmetic scoring over precomputed features.",
))


def compute_copy_trade_score(features: dict, luck_flags: dict) -> int:
    w = ws_cfg.weights

    # --- Component 1: Address Type (25%) ---
    # Reinterpreted from a naive EOA-vs-contract check (which would
    # incorrectly flag EVERY Polymarket trader, since all user funds sit
    # in per-user proxy wallet contracts by design) into: is this a known
    # SHARED SYSTEM contract (relay hub, factory, pUSD, CTF)? Those get
    # zero credit here; any real user proxy wallet gets full credit.
    address_type_score = 0.0 if features.get("is_system_contract") else 1.0

    # --- Component 2: Activity Consistency (25%) ---
    # Blends real day-of-week spread (are trades spread across >=2-3
    # distinct days/week, not bursty) with the recency-weighted engagement
    # score built last session (days since last trade, trades in the last
    # 14 days, recent 30-day PnL sign) - both genuinely measure "is this
    # wallet consistently, currently active," just from different angles.
    day_spread_score = _day_spread_score(features)
    recent_activity_score = _recent_activity_score(features)
    activity_consistency_score = 0.6 * day_spread_score + 0.4 * recent_activity_score

    # --- Component 3: Behavioral Quality (20%) ---
    behavioral_quality_score = _behavioral_quality_score(features.get("activity_pattern_label"))

    # --- Component 4: Risk Management & Discipline (15%) ---
    max_drawdown = features.get("max_drawdown_usd", 0.0)
    risk_score = max(0.0, 1.0 - (max_drawdown / max(ws_cfg.max_acceptable_drawdown_usd, 1)))

    # --- Component 5: Evidence of Edge (15%) ---
    win_rate = features.get("win_rate") or 0.0
    resolved_count = features.get("resolved_trade_count", 0)
    breadth = features.get("market_breadth", 0)
    sample_confidence = min(1.0, resolved_count / max(ws_cfg.min_sample_size * 3, 1))
    breadth_score = min(1.0, breadth / 5.0)
    category_consistency = _category_consistency_score(features.get("category_performance", {}))
    edge_score = 0.5 * win_rate + 0.25 * sample_confidence + 0.15 * breadth_score + 0.10 * category_consistency

    raw_score = (
        w.address_type * address_type_score
        + w.activity_consistency * activity_consistency_score
        + w.behavioral_quality * behavioral_quality_score
        + w.risk_management * risk_score
        + w.evidence_of_edge * edge_score
    )

    penalty = compute_penalty(luck_flags)
    final_score = raw_score * (1 - penalty)

    return round(max(0.0, min(1.0, final_score)) * 100)


def _day_spread_score(features: dict) -> float:
    """1.0 at >=3 active days/week (top of the '2-3 days' requirement), scaled down below that, zeroed if bursty."""
    active_days = features.get("avg_active_days_per_week", 0.0)
    score = min(1.0, active_days / 3.0)
    if features.get("is_bursty"):
        score *= 0.3  # heavy penalty for burst-then-silent patterns even if raw day count looks OK
    return score


def _behavioral_quality_score(pattern_label: str) -> float:
    return {
        "active_human_trader": 1.0,
        "consistent_semi_automated": 0.65,
        "high_frequency_bot": 0.15,
        "inconsistent_activity": 0.10,
    }.get(pattern_label, 0.4)  # unknown/not-yet-classified - neutral-low, not assumed good


def _recent_activity_score(features: dict) -> float:
    """
    Blends how recently the wallet last traded, how many trades it's made
    in the last 14 days, and whether its last-30-day RESOLVED PnL is
    positive - recent engagement and recent skill, not just lifetime stats.
    """
    days_inactive = features.get("days_since_last_trade")
    if days_inactive is None or days_inactive == float("inf"):
        return 0.0

    ideal = ws_cfg.activity_recency.ideal_days_inactive
    max_allowed = ws_cfg.activity_recency.max_days_inactive
    if days_inactive <= ideal:
        recency_component = 1.0 - 0.5 * (days_inactive / max(ideal, 1))
    else:
        recency_component = max(0.0, 0.5 * (1 - (days_inactive - ideal) / max(max_allowed - ideal, 1)))

    trades_14d = features.get("trade_count_14d", 0)
    engagement_component = min(1.0, trades_14d / 3.0)

    pnl_30d = features.get("pnl_resolved_30d")
    if pnl_30d is None or features.get("resolved_count_30d", 0) == 0:
        pnl_component = 0.5
    else:
        pnl_component = 1.0 if pnl_30d > 0 else 0.0

    return round(0.5 * recency_component + 0.25 * engagement_component + 0.25 * pnl_component, 4)


def _category_consistency_score(category_performance: dict) -> float:
    """
    1.0 if the wallet performs well (positive PnL) across ALL categories it
    has resolved trades in; lower if performance swings between strongly
    positive and strongly negative across categories (inconsistent edge).
    """
    if not category_performance:
        return 0.5  # neutral - no category breakdown available
    win_rates = [v["win_rate"] for v in category_performance.values() if v.get("win_rate") is not None]
    if not win_rates:
        return 0.5
    avg = sum(win_rates) / len(win_rates)
    variance = sum((wr - avg) ** 2 for wr in win_rates) / len(win_rates)
    return round(max(0.0, 1.0 - variance * 2), 4)


def recommendation_from_score(score: int) -> str:
    if score >= 65:
        return "copy"
    if score >= 40:
        return "watch"
    return "avoid"
