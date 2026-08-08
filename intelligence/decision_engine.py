"""
The decision engine. Turns (mispricing signal + verification + historical
context + wallet agreement) into one of: BUY_YES, BUY_NO, MONITOR, NO_TRADE.

Key design decision (documented here since it's not obvious from the
original spec): mispricing signals differ in whether they NEED news
verification to be trustworthy:

  - "arbitrage" signals are pure math (internal consistency across
    mutually-exclusive outcomes on the SAME platform) - mechanically
    verified by construction, no news dependency. These can reach
    BUY_YES without the LLM verification tier.
  - "cross_platform" signals with benchmark_source="kalshi" compare two
    real independent markets - also mechanical, no news dependency needed.
  - "cross_platform" signals with benchmark_source="llm_estimate" rest on
    an LLM's probability guess, which is NOT independently verified by
    construction - these are capped at MONITOR unless verification.status
    == "PASS" (i.e. the paid evidence-check tier actually ran and passed).

This is the concrete implementation of "no source, no signal" - applied
specifically where a signal actually depends on an unverified external
claim, not blanket-applied to signals that are already real, independent,
mechanical comparisons.
"""

from intelligence.confidence_aggregator import aggregate_confidence
from intelligence.risk_manager import suggested_size
from config.loader import verification as verification_cfg, risk as risk_cfg
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="intelligence.decision_engine",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure rule-based decision logic over already-computed inputs.",
))


def decide(mispricing_signal: dict, verification: dict, historical: dict,
           wallet_evaluations: list, market_features: dict) -> dict:
    """
    Returns a MarketIntelligenceReport-shaped dict (minus the identity
    fields market_id/market_url/market_category, added by the caller).
    """
    liquidity_ok = market_features.get("liquidity_usd", 0.0) >= risk_cfg.min_liquidity_usd

    if not liquidity_ok:
        return _no_trade_report(
            "Market liquidity is below the configured minimum "
            f"(${risk_cfg.min_liquidity_usd:,.0f}) - too thin to size into safely."
        )

    needs_verification = (
        mispricing_signal.get("signal_type") == "cross_platform"
        and mispricing_signal.get("benchmark_source") == "llm_estimate"
    )

    if needs_verification and verification.get("status") != "PASS":
        return _monitor_report(
            mispricing_signal, verification, historical,
            reason=(
                "This signal relies on an LLM-elicited probability estimate rather "
                "than a real independent market, and hasn't passed evidence "
                "verification yet - capped at MONITOR until it does. "
                f"Verification status: {verification.get('status', 'not run')}."
            ),
        )

    if verification and verification.get("status") == "FAIL":
        return _no_trade_report(
            f"Verification explicitly FAILED: {verification.get('explanation', '')}"
        )

    wallet_agreement_score = _compute_wallet_agreement(mispricing_signal, wallet_evaluations)

    confidence = aggregate_confidence(mispricing_signal, verification, historical, wallet_agreement_score)

    if historical and historical.get("resembles_failed_setup") and confidence["confidence_tier"] != "high":
        return _monitor_report(
            mispricing_signal, verification, historical,
            reason=(
                f"{historical.get('precedent_summary', '')} This edge is real by the "
                f"numbers, but the historical precedent is a real warning - downgraded "
                f"to MONITOR rather than a trade call."
            ),
        )

    direction = mispricing_signal.get("direction", "HOLD")
    if direction not in ("YES", "NO") or confidence["confidence_score"] < verification_cfg.min_confidence_to_alert:
        return _monitor_report(
            mispricing_signal, verification, historical,
            reason=(
                f"Edge detected ({mispricing_signal.get('edge_size', 0)*100:.1f}pp) but "
                f"combined confidence ({confidence['confidence_score']:.2f}) is below the "
                f"alert threshold ({verification_cfg.min_confidence_to_alert}) - worth "
                f"watching, not yet a trade call."
            ),
        )

    decision_label = "BUY_YES" if direction == "YES" else "BUY_NO"
    sizing = suggested_size(mispricing_signal.get("edge_size", 0.0), confidence["confidence_tier"],
                             market_features.get("liquidity_usd", 0.0))

    why_this_side = _build_why_this_side(mispricing_signal, verification, historical, wallet_agreement_score, direction)
    why_not_opposite = _build_why_not_opposite(mispricing_signal, direction, historical)
    invalidation = _build_invalidation(mispricing_signal, historical)

    return {
        "decision_label": decision_label,
        "confidence_tier": confidence["confidence_tier"],
        "confidence_score": confidence["confidence_score"],
        "suggested_size_pct_of_risk_budget": sizing["suggested_size_pct_of_risk_budget"],
        "max_loss_tolerance_usd": sizing["max_loss_tolerance_usd"],
        "why_this_side": why_this_side,
        "why_not_opposite": why_not_opposite,
        "invalidation_conditions": invalidation,
    }


def _compute_wallet_agreement(mispricing_signal: dict, wallet_evaluations: list) -> float:
    """
    +1.0 if copy-worthy ("copy" recommendation) wallets are positioned on
    the same side as the signal's direction, -1.0 if they're positioned
    against it, 0.0 if no relevant wallet data or mixed signal.
    Requires wallet_evaluations entries to include a "direction" field
    (the side that wallet is currently positioned on for this market) -
    if that's absent, this stays neutral rather than guessing.
    """
    signal_direction = mispricing_signal.get("direction")
    if signal_direction not in ("YES", "NO") or not wallet_evaluations:
        return 0.0

    copy_worthy = [w for w in wallet_evaluations if w.get("copy_trade_recommendation") == "copy"]
    if not copy_worthy:
        return 0.0

    agree = sum(1 for w in copy_worthy if w.get("direction") == signal_direction)
    disagree = sum(1 for w in copy_worthy if w.get("direction") not in (None, signal_direction))
    total = agree + disagree
    if total == 0:
        return 0.0
    return round((agree - disagree) / total, 3)


def _build_why_this_side(mispricing_signal, verification, historical, wallet_agreement_score, direction) -> str:
    parts = [
        f"{mispricing_signal.get('edge_size', 0)*100:.1f}pp edge from "
        f"{mispricing_signal.get('benchmark_source', 'internal check')}."
    ]
    if verification and verification.get("status") == "PASS":
        parts.append(f"Verified: {verification.get('explanation', '')}")

    precedent_note = _precedent_direction_note(historical, direction)
    if precedent_note:
        parts.append(precedent_note)

    if wallet_agreement_score > 0.3:
        parts.append("Smart-money wallets tracked by this system are positioned the same way.")
    return " ".join(parts)


def _precedent_direction_note(historical: dict, direction: str) -> str:
    """
    Explicitly states whether real past-resolved-market precedent AGREES
    or CONFLICTS with the direction this signal is suggesting - surfaced
    for every decision, not just when precedent happens to be strongly
    supportive. This is the concrete "look at how similar past markets
    actually resolved, then decide which buy side is best" analysis.
    """
    if not historical or historical.get("precedent_score") is None:
        return ""

    score = historical["precedent_score"]
    summary = historical.get("precedent_summary", "")

    # precedent_score: +1 = past similar setups mostly resolved YES-like,
    # -1 = mostly resolved NO-like.
    precedent_leans_yes = score > 0.15
    precedent_leans_no = score < -0.15

    if direction == "YES":
        if precedent_leans_yes:
            return f"Historical precedent REINFORCES this YES call: {summary}"
        if precedent_leans_no:
            return f"⚠️ Historical precedent CONFLICTS with this YES call: {summary} Weigh this against the pricing edge before acting."
    elif direction == "NO":
        if precedent_leans_no:
            return f"Historical precedent REINFORCES this NO call: {summary}"
        if precedent_leans_yes:
            return f"⚠️ Historical precedent CONFLICTS with this NO call: {summary} Weigh this against the pricing edge before acting."

    return f"Historical precedent is mixed/neutral on this direction: {summary}" if summary else ""


def _build_why_not_opposite(mispricing_signal, direction, historical) -> str:
    opposite = "NO" if direction == "YES" else "YES"
    base = (
        f"The opposite side ({opposite}) would require the market to be "
        f"correctly priced already, which the detected "
        f"{mispricing_signal.get('edge_size', 0)*100:.1f}pp edge argues against."
    )
    precedent_note = _precedent_direction_note(historical, direction)
    if precedent_note.startswith("⚠️"):
        base += (
            f" That said, historical precedent leans toward {opposite}, not {direction} - "
            f"this is a real reason the opposite side isn't as weak as the pricing edge alone suggests."
        )
    return base


def _build_invalidation(mispricing_signal, historical) -> str:
    base = (
        "This setup is invalidated if the edge closes (price moves toward "
        "the benchmark) or if the underlying benchmark itself was wrong."
    )
    if historical and historical.get("resembles_failed_setup"):
        base += " Also watch for this resembling the same pattern as past similar setups that failed."
    return base


def _no_trade_report(reason: str) -> dict:
    return {
        "decision_label": "NO_TRADE", "confidence_tier": "low", "confidence_score": 0.0,
        "suggested_size_pct_of_risk_budget": 0.0, "max_loss_tolerance_usd": None,
        "why_this_side": "N/A", "why_not_opposite": "N/A",
        "invalidation_conditions": reason,
    }


def _monitor_report(mispricing_signal, verification, historical, reason: str) -> dict:
    return {
        "decision_label": "MONITOR", "confidence_tier": "low", "confidence_score": 0.0,
        "suggested_size_pct_of_risk_budget": 0.0, "max_loss_tolerance_usd": None,
        "why_this_side": reason, "why_not_opposite": "N/A - not yet a trade call.",
        "invalidation_conditions": "Revisit once verification/precedent picture improves.",
    }
