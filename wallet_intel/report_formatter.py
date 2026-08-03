"""
Renders a wallet evaluation into the EXACT output format specified by the
Polymarket Wallet Intelligence Layer v2.4 spec - used for the detailed
per-wallet text block (in addition to, not instead of, the structured
Discord embed fields already built by alerts/discord_formatter.py).
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="wallet_intel.report_formatter",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure text templating over already-computed wallet data.",
))

_ACTIVITY_PATTERN_DISPLAY = {
    "active_human_trader": "Active Human Trader",
    "consistent_semi_automated": "Consistent Semi-Automated",
    "high_frequency_bot": "High-Frequency Bot/Relayer",
    "inconsistent_activity": "Inconsistent Activity",
}


def _build_drift_section(w: dict) -> str:
    drift = w.get("drift_result") or {}
    if isinstance(drift, str):
        import json
        try:
            drift = json.loads(drift)
        except (ValueError, TypeError):
            drift = {}

    if not drift or drift.get("insufficient_history"):
        return "Not enough trade history yet to assess strategy consistency/drift."

    status_display = {
        "stable": "✅ Stable", "mild_drift": "🟡 Mild Drift", "confirmed_drift": "🔴 Confirmed Drift",
    }.get(drift.get("drift_status"), "Unknown")

    lines = [
        f"Consistency Score: {drift.get('consistency_score')}/100",
        f"Drift Status: {status_display}",
        f"Strategy Adherence Rate: {drift.get('adherence_rate_pct')}%",
        f"Lightweight CUSUM: {'⚠️ ' + drift.get('cusum_strength', 'none') + ' alarm' if drift.get('cusum_alarm') else 'no alarm'}",
        f"Core category: {drift.get('core_category', 'unknown')} (${drift.get('core_pnl', 0):,.0f} core PnL vs ${drift.get('peripheral_pnl', 0):,.0f} peripheral)"
        + (" — peripheral-dominant" if drift.get("peripheral_dominance") else ""),
    ]
    return "\n".join(lines)


def _build_fork_analysis(w: dict) -> str:
    """
    Phase 4 item 9, upgraded: classifies this wallet's REAL observed
    behavior against a set of known Polymarket trading archetypes, then
    gives concrete "fork and improve" advice framed for the USER's OWN
    MANUAL trading (not auto-copying) - the goal is loss-minimization
    through better-informed manual decisions, using this wallet's
    demonstrated pattern as a reference point, not a signal to blindly follow.

    Honesty note: only archetypes with real, detectable signals in our
    data are matched (category concentration, breadth, sizing, frequency,
    average entry price). Archetypes needing data we don't have - order
    type/maker-vs-taker flags, post-news timing, or fine-print/resolution-
    criteria targeting - are NOT guessed at; the report says so plainly
    when nothing matches confidently.
    """
    cat_perf = w.get("category_performance") or {}
    if isinstance(cat_perf, str):
        import json
        try:
            cat_perf = json.loads(cat_perf)
        except (ValueError, TypeError):
            cat_perf = {}

    archetype, archetype_evidence = _classify_strategy_archetype(w, cat_perf)
    archetype_advice = _archetype_fork_advice(archetype, w, cat_perf)

    return f"**Detected archetype: {archetype}**\n{archetype_evidence}\n\n{archetype_advice}"


def _classify_strategy_archetype(w: dict, cat_perf: dict) -> tuple:
    breadth = w.get("distinct_events", 0)
    trades_per_day = w.get("trades_per_day", 0.0)
    win_rate = w.get("win_rate")
    resolved = w.get("resolved_count", 0)
    avg_size = w.get("avg_trade_size_usd", 0.0)
    buy_ratio = w.get("buy_ratio")

    top_category_share = 0.0
    top_category = None
    if cat_perf:
        total_trades_in_cats = sum(c.get("resolved_count", 0) for c in cat_perf.values())
        if total_trades_in_cats > 0:
            top_category, top_stats = max(cat_perf.items(), key=lambda kv: kv[1].get("resolved_count", 0))
            top_category_share = top_stats.get("resolved_count", 0) / total_trades_in_cats

    # Specialist Mirroring: one category dominates 60%+ of volume, decent
    # sample, and a real edge in it.
    if top_category and top_category_share >= 0.6 and resolved >= 10 and win_rate is not None and win_rate >= 0.55:
        return (
            "Specialist Mirroring",
            f"{top_category_share*100:.0f}% of this wallet's resolved trades are in **{top_category}** "
            f"({cat_perf[top_category].get('win_rate', 0)*100:.0f}% win rate there) - a real, sustained "
            f"concentration in one niche, not a spread bet."
        )

    # Conviction Long-Dated: low frequency, larger-than-average size,
    # narrow breadth - few, bigger, more deliberate bets.
    if trades_per_day < 0.3 and avg_size > 0 and breadth <= 5:
        return (
            "Conviction Long-Dated Bets",
            f"Low trade frequency ({trades_per_day:.2f}/day) across only {breadth} distinct events with "
            f"an average size of ${avg_size:,.0f} - consistent with a small number of deliberate, "
            f"higher-conviction positions rather than frequent trading."
        )

    # Calibration / Value Betting: diversified across many categories with
    # a consistently decent win rate everywhere, not concentrated in one.
    if breadth >= 5 and win_rate is not None and win_rate >= 0.55 and top_category_share < 0.5:
        return (
            "Calibration / Value Betting",
            f"Diversified across {breadth} distinct events with no single category dominating "
            f"(top category is only {top_category_share*100:.0f}% of volume) and a {win_rate*100:.0f}% "
            f"overall win rate - consistent with broadly better-calibrated probability estimates "
            f"rather than one specific niche edge."
        )

    return (
        "Unclassified / Mixed",
        "This wallet's pattern doesn't clearly match one of the detectable archetypes "
        "(Specialist Mirroring, Conviction Long-Dated, or Calibration/Value Betting) - "
        "could be early-stage, genuinely mixed, or following a style (e.g. Rules/Resolution "
        "Edge, Overreaction Fade, Market Making) that isn't reliably detectable from trade "
        "timestamps and sizes alone."
    )


def _archetype_fork_advice(archetype: str, w: dict, cat_perf: dict) -> str:
    """
    Concrete manual-trading upgrades, paraphrased and adapted from known
    Polymarket strategy write-ups, personalized to this specific wallet's
    real numbers - framed for YOUR manual decisions, not for auto-copying.
    """
    if archetype == "Specialist Mirroring":
        return (
            "**For your own manual trading:** rather than copying every trade this wallet makes, use it as "
            "a reference for which category might reward specialization for YOU. If you build your own edge "
            "in this same category, consider: (1) only acting on markets you've personally read the full "
            "resolution criteria for, not just the headline; (2) capping any single position at 1-5% of your "
            "own capital regardless of how confident this wallet looks; (3) tracking a small basket of your "
            "own trades in this category for a few weeks before sizing up, the same way this wallet appears "
            "to have built consistency over time rather than betting big immediately."
        )

    if archetype == "Conviction Long-Dated Bets":
        return (
            "**For your own manual trading:** this style trades sparingly and holds through resolution - "
            "the manual-trading lesson isn't to mimic the specific bets, but the discipline: form your own "
            "independent view on a longer-horizon market (30-90+ days), size small (this is historically the "
            "lowest-Sharpe of the positive-performing styles, so don't over-allocate), and avoid checking "
            "the position daily, since long-dated markets are especially prone to short-term noise that "
            "tempts early, unnecessary exits."
        )

    if archetype == "Calibration / Value Betting":
        return (
            "**For your own manual trading:** the lesson here is breadth with discipline, not chasing one "
            "hot category. Before you place a manual trade, write down your own probability estimate BEFORE "
            "looking at the market price, then compare - only act if your independent estimate diverges "
            "meaningfully from the market's implied probability. Size using a fractional-Kelly approach "
            "against your own edge estimate, not a fixed bet size, and diversify across uncorrelated "
            "categories the way this wallet does."
        )

    return (
        "**For your own manual trading:** without a confidently-detected archetype, the safest generic "
        "loss-minimizing habits are: always read the full resolution criteria before entering (titles "
        "mislead more often than you'd expect), cap any single position at a small fixed % of your capital, "
        "check order-book depth before sizing so you're not just paying slippage, and treat markets that "
        "already sit at 90%+ or 5%- implied probability with extra scrutiny - the edge there is usually "
        "thin and already priced by faster participants."
    )


def build_full_activity_breakdown(wallet_record: dict) -> str:
    """
    Phase 4 mandatory structure, items 3-9 (items 1-2 - what was bought/sold
    and precise trade time - are already covered by the Phase 3 transaction
    explainer section above this one in the final report).
    """
    behavior_label = wallet_record.get("behavior_label", "unknown")
    pattern_label = wallet_record.get("activity_pattern_label", "unknown")
    pattern_display = _ACTIVITY_PATTERN_DISPLAY.get(pattern_label, "Unknown")
    timing_entropy = wallet_record.get("timing_entropy", 0.0)
    active_days = wallet_record.get("avg_active_days_per_week", 0.0)
    is_bursty = wallet_record.get("is_bursty", False)
    score_10 = wallet_record.get("copy_trade_score_10", 0)
    rec_label = wallet_record.get("copy_trade_recommendation_label", "Conditional")
    behavioral_pattern_text = wallet_record.get("behavioral_pattern", "No pattern description available.")
    market_options = wallet_record.get("market_options_breakdown", "Not available.")
    why = wallet_record.get("why_copy_or_not", "")

    return f"""**Wallet Behavior Classification:** {behavior_label}

**Human vs Bot Determination:** {pattern_display}
  On-chain evidence: timing entropy {timing_entropy:.2f} (0=mechanical/scripted, 1=highly variable/human-like), {active_days:.1f} active days/week, {"bursty (concentrated then silent)" if is_bursty else "not bursty (spread activity)"}.

**Copy-Trading Fitness Verdict:** {rec_label} ({score_10}/10)

**Strategy Consistency & Drift:**
{_build_drift_section(wallet_record)}

**Wallet Trade Pattern:**
{behavioral_pattern_text}

**All Available Market Options on the Event (and this wallet's positions):**
{market_options}

**Readable Trading Strategy (plain language):**
{why}

**Fork Analysis (ways to adapt this for your own trading):**
{_build_fork_analysis(wallet_record)}
"""


def build_quality_scorecard(w: dict) -> str:
    """
    Phase 6 item 1: clean, scannable markdown table with the exact 8
    columns specified: Win Rate | Recent PnL (30d) | Sample Size |
    Activity Recency | Category Edge | Strategy Consistency | Drift
    Status | Overall Copy Fitness.
    """
    win_rate = w.get("win_rate")
    win_rate_str = f"{win_rate*100:.0f}%" if win_rate is not None else "N/A"

    pnl_30d = w.get("pnl_resolved_30d", 0.0)
    pnl_30d_str = f"${pnl_30d:,.0f}"

    resolved_count = w.get("resolved_count", 0)
    sample_str = f"{resolved_count} trades" + (" ✅" if resolved_count >= 15 else " ⚠️ small")

    days_inactive = w.get("days_since_last_trade")
    recency_str = f"{days_inactive:.0f}d ago" if days_inactive is not None else "unknown"

    cat_perf = w.get("category_performance") or {}
    if isinstance(cat_perf, str):
        import json
        try:
            cat_perf = json.loads(cat_perf)
        except (ValueError, TypeError):
            cat_perf = {}
    if cat_perf:
        best = max(cat_perf.items(), key=lambda kv: kv[1].get("pnl", 0))
        category_edge_str = f"{best[0]}: {best[1].get('win_rate', 0)*100:.0f}%" if best[1].get("win_rate") is not None else "N/A"
    else:
        category_edge_str = "N/A"

    drift = w.get("drift_result") or {}
    if isinstance(drift, str):
        import json
        try:
            drift = json.loads(drift)
        except (ValueError, TypeError):
            drift = {}
    consistency_str = f"{drift.get('consistency_score')}/100" if drift.get("consistency_score") is not None else "N/A"
    drift_status_display = {
        "stable": "✅ Stable", "mild_drift": "🟡 Mild", "confirmed_drift": "🔴 Confirmed",
        "insufficient_history": "N/A",
    }.get(drift.get("drift_status"), "N/A")

    fitness = f"{w.get('copy_trade_recommendation_label', 'Conditional')} ({w.get('copy_trade_score_10', 0)}/10)"

    header = "| Win Rate | Recent PnL (30d) | Sample Size | Activity Recency | Category Edge | Strategy Consistency | Drift Status | Overall Copy Fitness |"
    sep = "|---|---|---|---|---|---|---|---|"
    row = f"| {win_rate_str} | {pnl_30d_str} | {sample_str} | {recency_str} | {category_edge_str} | {consistency_str} | {drift_status_display} | {fitness} |"

    return f"{header}\n{sep}\n{row}"


def render_wallet_report(wallet_record: dict) -> str:
    """
    wallet_record: the full dict assembled by main.py's _build_wallet_record
    (features + evaluation + entry data merged), already containing
    is_system_contract / activity_pattern_label / avg_active_days_per_week
    / is_bursty / copy_trade_score etc.
    """
    address = wallet_record.get("wallet_address", "unknown")
    is_sys_contract = wallet_record.get("is_system_contract", False)
    sys_contract_label = wallet_record.get("system_contract_label")

    address_type = (
        f"Smart Contract (Deposit Wallet) - {sys_contract_label}" if is_sys_contract
        else "Wallet (EOA-equivalent user proxy)"
    )

    score = wallet_record.get("copy_trade_score", 0)
    active_days = wallet_record.get("avg_active_days_per_week", 0.0)
    pattern_label = wallet_record.get("activity_pattern_label", "unknown")
    pattern_display = _ACTIVITY_PATTERN_DISPLAY.get(pattern_label, "Unknown")
    recommendation = _score_to_recommendation_label(score)
    risk_level = _score_to_risk_level(score)

    meets_requirement = active_days >= 2.0 and not wallet_record.get("is_bursty", False)
    requirement_note = (
        "meets the 2-3 active days/week requirement" if meets_requirement
        else "does NOT meet the 2-3 active days/week requirement"
    )

    positive_signals = _build_positive_signals(wallet_record)
    red_flags = _build_red_flags(wallet_record, is_sys_contract)
    summary = _build_summary(wallet_record, pattern_display, meets_requirement)

    transaction_blocks = wallet_record.get("transaction_blocks", [])
    transaction_section = (
        "**Recent Transactions (most recent first):**\n" + "\n\n".join(transaction_blocks)
        if transaction_blocks else "**Recent Transactions:** No recent trade data available."
    )

    return f"""**Wallet Address:** `{address}`
**Address Type:** {address_type}
**Score:** {score}/100
**Activity Pattern:** Active on {active_days:.1f} days per week (average)
**Classification:** {pattern_display}
**Recommendation:** {recommendation}

**Quality Scorecard:**
{build_quality_scorecard(wallet_record)}

{transaction_section}

**Summary:**
{summary}

**Positive Signals:**
{positive_signals}

**Red Flags:**
{red_flags}

**Risk Level:** {risk_level}
**Copytrading Advice:**
{_build_copytrading_advice(wallet_record, recommendation, meets_requirement, requirement_note, is_sys_contract)}

---
**FULL ACTIVITY BREAKDOWN**

{build_full_activity_breakdown(wallet_record)}
"""


def _score_to_recommendation_label(score: int) -> str:
    if score >= 85:
        return "Strong Buy"
    if score >= 72:
        return "Buy"
    if score >= 60:
        return "Neutral"
    return "Avoid"


def _score_to_risk_level(score: int) -> str:
    if score >= 72:
        return "Low"
    if score >= 60:
        return "Medium"
    return "High"


def _build_positive_signals(w: dict) -> str:
    signals = []
    win_rate = w.get("win_rate")
    if win_rate is not None and win_rate >= 0.6:
        signals.append(f"- {win_rate*100:.0f}% win rate across {w.get('resolved_count', 0)} resolved trades")
    if not w.get("is_bursty", True) and w.get("avg_active_days_per_week", 0) >= 2:
        signals.append(f"- Consistent activity: {w.get('avg_active_days_per_week', 0):.1f} active days/week, not bursty")
    if w.get("days_since_last_trade", 999) <= 7:
        signals.append(f"- Recently active: last trade {w.get('days_since_last_trade')} day(s) ago")
    if w.get("distinct_events", 0) >= 3:
        signals.append(f"- Diversified across {w.get('distinct_events')} distinct events, not a one-hit wonder")
    if not signals:
        signals.append("- None strong enough to highlight")
    return "\n".join(signals)


def _build_red_flags(w: dict, is_sys_contract: bool) -> str:
    flags = []
    if is_sys_contract:
        flags.append(f"- ⚠️ This is a known Polymarket SYSTEM contract ({w.get('system_contract_label')}), not an individual trader - should never be copy-traded")
    if w.get("is_bursty"):
        flags.append("- Bursty activity pattern: concentrated trading then long silent gaps")
    if w.get("activity_pattern_label") == "high_frequency_bot":
        flags.append("- High-frequency, mechanically-timed pattern consistent with a bot/relayer, not a human trader")
    if w.get("resolved_count", 0) < 15:
        flags.append(f"- Small sample: only {w.get('resolved_count', 0)} resolved trades (15-20+ preferred for statistical relevance)")
    if w.get("luck_flags", {}).get("is_luck_dominated"):
        flags.append("- Flagged as luck-dominated: " + "; ".join(w.get("luck_flags", {}).get("reasons", [])))
    if w.get("days_since_last_trade", 0) and w.get("days_since_last_trade", 0) > 14:
        flags.append(f"- Inactive for {w.get('days_since_last_trade')} days - may no longer be actively trading")
    if not flags:
        flags.append("- None identified")
    return "\n".join(flags)


def _build_summary(w: dict, pattern_display: str, meets_requirement: bool) -> str:
    name = w.get("username") or w.get("wallet_address", "")[:10] + "…"
    win_rate = w.get("win_rate")
    win_rate_str = f"{win_rate*100:.0f}%" if win_rate is not None else "an unknown"
    req_phrase = "meets" if meets_requirement else "does not meet"
    return (
        f"{name} shows a {win_rate_str} win rate across {w.get('resolved_count', 0)} resolved trades, "
        f"classified as {pattern_display}. This wallet {req_phrase} the minimum activity-frequency bar "
        f"for a reliable copy-trading signal. {w.get('why_copy_or_not', '')}"
    )


def _build_copytrading_advice(w: dict, recommendation: str, meets_requirement: bool,
                               requirement_note: str, is_sys_contract: bool) -> str:
    if is_sys_contract:
        return "Do not copy-trade - this is shared platform infrastructure, not an individual trader."
    if recommendation == "Avoid":
        return f"Avoid copying this wallet. It {requirement_note}."
    size_pct = {"Strong Buy": "3-5%", "Buy": "1-3%", "Neutral": "0.5-1%"}.get(recommendation, "0%")
    return (
        f"If copying, size at roughly {size_pct} of risk budget per trade. "
        f"This wallet {requirement_note}. Address type: "
        f"{'Smart Contract' if is_sys_contract else 'Wallet (EOA-equivalent user proxy)'}."
    )
