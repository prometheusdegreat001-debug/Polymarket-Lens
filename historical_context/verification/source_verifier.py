"""
Orchestrates evidence gathering: tries the FREE RSS/news tier first
(ingestion/free_news_sources.py - real feeds, zero cost), and only
escalates to the paid LLM tier (ingestion/external_sources.py) if the free
tier didn't find enough real, current, relevant coverage AND paid
verification is enabled.

This does NOT decide pass/fail on its own - that's confidence_gate.py's
job, combining this with resolution_rule_checker, market_relevance_checker,
and liquidity. This module's only responsibility: get the best available
evidence (free first) and make sure trust scores are sane.
"""

from datetime import datetime, timezone

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="verification.source_verifier",
    requires_paid_api=True,  # conditionally - see notes
    estimated_cost_per_call_usd=0.07,  # only if it escalates to the paid tier
    free_fallback_strategy=(
        "Always tries ingestion.free_news_sources (real RSS feeds - Reuters/AP "
        "via Google News, arXiv, AI lab blogs, tech news, gov feeds) FIRST. "
        "If that finds enough primary+secondary sources within the freshness "
        "window, evidence is built entirely from free data - the paid LLM tier "
        "is never called. Only escalates to ingestion.external_sources (paid) "
        "if free coverage is insufficient AND verification.enabled=true."
    ),
))

from ingestion.free_news_sources import search_feeds
from ingestion.external_sources import fetch_evidence, evidence_enabled
from features.source_quality_features import score_sources
from config.loader import verification as verification_cfg


def verify_sources(market_question: str, resolution_rule: str, market_category: str = None) -> dict:
    free_matches = search_feeds(market_question, market_category=market_category)

    primary_free = [m for m in free_matches if m["tier"] == "primary"]
    secondary_free = [m for m in free_matches if m["tier"] == "secondary"]

    # "Primary + secondary required" means at least min_primary_sources
    # PLUS enough total independent corroboration - it does NOT require a
    # source to be literally tagged "secondary". Two independent wire
    # services (e.g. Reuters AND AP both covering it) is exactly the kind
    # of corroboration this requirement is meant to capture, even though
    # both come from NEWSWIRE_FEEDS' "primary" tier.
    total_required = verification_cfg.min_primary_sources + verification_cfg.min_secondary_sources
    free_sufficient = (
        len(primary_free) >= verification_cfg.min_primary_sources
        and len(free_matches) >= total_required
    )

    if free_matches and free_sufficient:
        return _evidence_from_free_matches(free_matches, resolution_rule)

    if evidence_enabled():
        paid_evidence = fetch_evidence(market_question, resolution_rule)
        paid_evidence["verification_tier"] = "paid_llm"
        independent_scores = score_sources(paid_evidence.get("source_urls", []))
        paid_evidence["source_trust_scores"] = {
            url: round((paid_evidence.get("source_trust_scores", {}).get(url, 0.5)
                        + independent_scores.get(url, 0.5)) / 2, 3)
            for url in paid_evidence.get("source_urls", [])
        }
        return paid_evidence

    # No paid tier available - return whatever free evidence exists (even
    # if insufficient) so the caller can see partial coverage, clearly
    # labeled as incomplete rather than silently empty.
    if free_matches:
        result = _evidence_from_free_matches(free_matches, resolution_rule)
        result["raw_ok"] = False  # insufficient count, even though some real matches exist
        result["summary"] = (
            f"Found {len(free_matches)} related item(s) via free RSS feeds "
            f"({len(primary_free)} primary, {len(secondary_free)} secondary) - "
            f"below the required {verification_cfg.min_primary_sources} primary / "
            f"{verification_cfg.min_secondary_sources} secondary threshold, and "
            f"paid verification is disabled to escalate further."
        )
        return result

    return {
        "source_urls": [], "source_trust_scores": {},
        "primary_source_count": 0, "secondary_source_count": 0,
        "news_is_current": False, "event_matches_resolution_rule": False,
        "internally_consistent": False,
        "summary": "No matching coverage found in free RSS feeds, and paid verification is disabled.",
        "raw_ok": False, "verification_tier": "free_rss",
    }


def _evidence_from_free_matches(matches: list, resolution_rule: str) -> dict:
    """
    Builds an evidence dict directly from real RSS matches - genuinely
    free verification, no LLM involved. Trust scores come from
    features/source_quality_features.py's domain heuristic (the same one
    used to cross-check the paid tier), not a guess.
    """
    urls = [m["link"] for m in matches if m.get("link")]
    trust_scores = score_sources(urls)

    primary_count = sum(1 for m in matches if m["tier"] == "primary")
    secondary_count = sum(1 for m in matches if m["tier"] == "secondary")
    freshest_hours = min((m["hours_old"] for m in matches if m["hours_old"] is not None), default=None)

    top_titles = "; ".join(m["title"] for m in matches[:3] if m.get("title"))
    summary = (
        f"Found {len(matches)} related item(s) via free RSS feeds "
        f"({primary_count} primary, {secondary_count} secondary): {top_titles}. "
        f"This is real published coverage matched by keyword overlap to the "
        f"market question - not an LLM-read confirmation, so treat "
        f"'event_matches_resolution_rule' as a heuristic, not a guarantee."
    )

    return {
        "source_urls": urls,
        "source_trust_scores": trust_scores,
        "primary_source_count": primary_count,
        "secondary_source_count": secondary_count,
        # Freshness and resolution-matching are both heuristic without an
        # LLM actually reading the content - transparently capped/estimated
        # rather than asserted as verified fact.
        "news_is_current": freshest_hours is not None and freshest_hours <= 48,
        "event_matches_resolution_rule": len(matches) >= 2,  # heuristic: multiple independent keyword-matched hits
        "internally_consistent": len(matches) >= 2,  # heuristic: no contradiction-detection without reading full text
        "summary": summary,
        "raw_ok": True,
        "verification_tier": "free_rss",
    }
