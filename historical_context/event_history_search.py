"""
Orchestrates historical_context: tries the FREE tier (Polymarket's own
resolved-event history) first, and only escalates to the PAID tier
(open-web research via ingestion.historical_events) if the free tier
found too few similar events AND paid research is enabled.

This is the module main.py actually calls - it owns the free-vs-paid
decision, so callers never need to know which tier answered.
"""

from datetime import datetime, timezone

from config.loader import verification as verification_cfg
from historical_context.similar_event_finder import find_similar_resolved_events
from historical_context.precedent_scorer import score_precedent
from ingestion.external_sources import evidence_enabled
from ingestion.historical_events import fetch_precedent
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="historical_context.event_history_search",
    requires_paid_api=True,  # conditionally - see notes
    estimated_cost_per_call_usd=0.06,  # inherits ingestion.historical_events' per-call cost
    free_fallback_strategy=(
        "Always tries historical_context.similar_event_finder (free, Polymarket's "
        "own closed-events history) FIRST. Only escalates to the paid "
        "ingestion.historical_events.fetch_precedent (open-web research) if: "
        "(1) fewer than verification.min_similar_events_before_llm similar events "
        "were found for free, AND (2) verification.enabled=true. If disabled, a "
        "market with thin in-platform precedent simply gets a precedent_score "
        "based on whatever was found for free, clearly labeled as such."
    ),
    notes="This module's escalation logic is the concrete implementation of "
          "'free tier first, paid only if genuinely insufficient.'",
))


def research_precedent(market_id: str, market_title: str, closed_events: list) -> dict:
    """
    Returns a HistoricalEventRecord-shaped dict.
    """
    similar = find_similar_resolved_events(market_title, closed_events)
    free_result = score_precedent(similar)

    enough_free_precedent = len(similar) >= verification_cfg.min_similar_events_before_llm

    if enough_free_precedent or not (verification_cfg.enabled and evidence_enabled()):
        return {
            "market_id": market_id,
            "similar_events": similar,
            "precedent_score": free_result["precedent_score"],
            "precedent_summary": free_result["precedent_summary"] + (
                "" if enough_free_precedent
                else " (Paid historical research is disabled - this reflects "
                     "in-platform precedent only.)"
            ),
            "resembles_failed_setup": free_result["resembles_failed_setup"],
            "source_urls": [ev["market_url"] for ev in similar if ev.get("market_url")],
            "source": "free_tier_only",
            "researched_at": datetime.now(timezone.utc).isoformat(),
        }

    # Free tier found too few similar events - escalate to paid open-web research.
    paid_result = fetch_precedent(market_title)
    if not paid_result.get("raw_ok"):
        # Paid call failed - fall back to whatever the free tier found rather
        # than silently returning nothing.
        return {
            "market_id": market_id,
            "similar_events": similar,
            "precedent_score": free_result["precedent_score"],
            "precedent_summary": free_result["precedent_summary"] + (
                f" (Paid research was attempted but failed: {paid_result.get('precedent_summary', '')})"
            ),
            "resembles_failed_setup": free_result["resembles_failed_setup"],
            "source_urls": [ev["market_url"] for ev in similar if ev.get("market_url")],
            "source": "free_tier_paid_failed",
            "researched_at": datetime.now(timezone.utc).isoformat(),
        }

    return {
        "market_id": market_id,
        "similar_events": similar + paid_result.get("similar_events", []),
        "precedent_score": paid_result.get("precedent_score", 0.0),
        "precedent_summary": paid_result.get("precedent_summary", ""),
        "resembles_failed_setup": paid_result.get("resembles_failed_setup", False),
        "source_urls": paid_result.get("source_urls", []),
        "source": "paid_tier",
        "researched_at": datetime.now(timezone.utc).isoformat(),
    }
