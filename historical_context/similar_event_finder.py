"""
Finds past RESOLVED Polymarket events similar to the current market, using
title-similarity matching against Gamma's closed-events data. This is the
free tier of historical_context/ - real Polymarket resolution history,
zero cost, zero LLM.

Reuses the same title-similarity function as mispricing/probability_model.py
(Kalshi matching) and the original v1 matcher.py - one similarity function,
used consistently everywhere in the system.
"""

from mispricing.probability_model import title_similarity
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="historical_context.similar_event_finder",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - matches against already-fetched Gamma closed-events data, no external calls.",
))

_SIMILARITY_THRESHOLD = 0.35  # looser than Kalshi matching (0.55) since we want
                              # "structurally similar past markets," not "the same event"


def find_similar_resolved_events(current_title: str, closed_events: list, top_n: int = 5,
                                  exclude_slug: str = None) -> list:
    """
    Returns up to top_n past resolved events similar to current_title,
    each with {title, resolved_outcome, end_date, market_url, similarity}.

    exclude_slug: when the market being analyzed is itself a resolved
    event (e.g. strategy_learning/contrarian_win_finder.py looking for
    precedent on a trade that already closed), it can legitimately show
    up inside closed_events (which is a general population pull, not
    scoped to exclude it) and would then "match itself" at similarity
    1.0 - a trivially true, useless result that displaces a real
    precedent. Pass its slug here to skip it.
    """
    scored = []
    for ev in closed_events:
        if exclude_slug and ev.get("slug") == exclude_slug:
            continue
        score = title_similarity(current_title, ev.get("title", ""))
        if score >= _SIMILARITY_THRESHOLD:
            scored.append({
                "title": ev.get("title"),
                "resolved_outcome": ev.get("resolved_outcome"),
                "end_date": ev.get("end_date"),
                "market_url": ev.get("market_url"),
                "similarity": round(score, 3),
            })

    scored.sort(key=lambda x: -x["similarity"])
    return scored[:top_n]
