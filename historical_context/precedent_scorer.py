"""
Scores precedent from matched historical events (free tier) - pure
arithmetic over how similar past markets actually resolved. No LLM.

precedent_score: -1.0 (past similar setups consistently resolved NO /
failed) to +1.0 (consistently resolved YES / succeeded), 0.0 = mixed or
no clear pattern.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="historical_context.precedent_scorer",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - rule-based scoring over already-matched historical events.",
))

# "Yes-like" outcome strings we can recognize from Gamma's outcome labels.
_YES_LIKE = {"yes", "y"}


def score_precedent(similar_events: list) -> dict:
    """
    Returns {"precedent_score": float, "precedent_summary": str,
    "resembles_failed_setup": bool}
    """
    if not similar_events:
        return {
            "precedent_score": 0.0,
            "precedent_summary": "No sufficiently similar past resolved markets found on Polymarket.",
            "resembles_failed_setup": False,
        }

    yes_count, no_count, unknown_count = 0, 0, 0
    for ev in similar_events:
        outcome = (ev.get("resolved_outcome") or "").strip().lower()
        if outcome in _YES_LIKE:
            yes_count += 1
        elif outcome and outcome not in _YES_LIKE:
            no_count += 1
        else:
            unknown_count += 1

    decided = yes_count + no_count
    if decided == 0:
        return {
            "precedent_score": 0.0,
            "precedent_summary": (
                f"Found {len(similar_events)} similar past market(s) but couldn't "
                f"determine how they resolved."
            ),
            "resembles_failed_setup": False,
        }

    # +1 if all resolved Yes, -1 if all resolved No, scaled in between.
    precedent_score = round((yes_count - no_count) / decided, 3)
    resembles_failed_setup = no_count >= 3 and no_count > yes_count

    top_titles = ", ".join(f'"{ev["title"]}"' for ev in similar_events[:3])
    precedent_summary = (
        f"Of {decided} similar past market(s) with a known resolution, "
        f"{yes_count} resolved YES and {no_count} resolved NO "
        f"(e.g. {top_titles}). "
        + (
            "This closely resembles a pattern of past setups that did NOT happen."
            if resembles_failed_setup
            else "Precedent leans toward YES." if precedent_score > 0.3
            else "Precedent leans toward NO." if precedent_score < -0.3
            else "Precedent is mixed - no strong signal either way."
        )
    )

    return {
        "precedent_score": precedent_score,
        "precedent_summary": precedent_summary,
        "resembles_failed_setup": resembles_failed_setup,
    }
