"""
The glue module: for a single mispricing signal, pulls in verification,
historical context, and any relevant wallet evaluations, then calls
decision_engine to produce a full MarketIntelligenceReport dict.

This is deliberately the ONLY module that knows how to assemble a full
report end-to-end - main.py calls this once per candidate signal, not the
individual sub-modules directly, so the assembly order (free tier first,
paid tier only if warranted) lives in exactly one place.
"""

from datetime import datetime, timezone

from verification.confidence_gate import run_verification
from historical_context.event_history_search import research_precedent
from intelligence.decision_engine import decide
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="intelligence.market_intelligence_builder",
    requires_paid_api=False,  # inherits whatever cost profile its called modules resolve to
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy=(
        "This module itself makes no calls of its own - it orchestrates "
        "verification.confidence_gate and historical_context.event_history_search, "
        "both of which independently enforce free-tier-first, paid-only-if-warranted."
    ),
))


def build_report(mispricing_signal: dict, market_features: dict, market_question: str,
                  resolution_rule: str, market_category: str, closed_events: list,
                  wallet_evaluations: list) -> dict:
    market_id = mispricing_signal["market_id"]
    market_url = mispricing_signal.get("market_url", "")
    market_title = mispricing_signal.get("_event_title") or mispricing_signal.get("_question") or market_question

    verification = run_verification(
        market_id=market_id, market_url=market_url, market_question=market_question,
        resolution_rule=resolution_rule, market_features=market_features,
        market_category=market_category,
    )

    historical = research_precedent(market_id, market_title, closed_events)

    decision = decide(mispricing_signal, verification, historical, wallet_evaluations, market_features)

    influential_wallets = [
        w["wallet_address"] for w in wallet_evaluations
        if w.get("copy_trade_recommendation") == "copy"
    ]

    return {
        "market_id": market_id,
        "market_url": market_url,
        "market_category": market_category,
        "mispricing": mispricing_signal,
        "verification": verification,
        "historical_context": historical,
        "influential_wallets": influential_wallets,
        **decision,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
