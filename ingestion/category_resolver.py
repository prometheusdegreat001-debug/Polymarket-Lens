"""
Resolves desired category names (weather, iran, middle-east, elections,
breaking-news, economy, etc.) to REAL Gamma tag IDs by querying the actual
/tags endpoint at startup - rather than hardcoding numbers we can't
independently verify. Falls back to a small set of already-confirmed
static IDs (politics, crypto, sports, finance, tech, culture, geopolitics)
for speed/reliability, and only hits /tags for anything not in that set.

On "Perps" specifically: Polymarket does not offer true perpetual futures
(that's a different product entirely - see dYdX, GMX, Hyperliquid). The
closest analog on Polymarket is short-horizon crypto up/down markets
(e.g. "btc-updown-15m-..."), which are already covered under the crypto
tag - there is no separate "perps" tag to resolve, and we don't fabricate
one.
"""

from ingestion.polymarket_api import fetch_all_tags
from config.loader import market_categories
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="ingestion.category_resolver",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - one free Gamma /tags API call, cached for the process lifetime.",
))

# Aliases so a category name in config can match Polymarket's actual tag
# label/slug even when the wording differs slightly (e.g. "iran" ->
# Polymarket's tag might be labeled "Iran" with slug "iran", but
# "middle_east" needs to match slug "middle-east" or label "Middle East").
_ALIASES = {
    "middle_east": ["middle east", "middle-east"],
    "breaking_news": ["breaking news", "breaking-news", "breaking"],
}

_cached_tags = None


def resolve_category_tag_ids(desired_categories: list) -> dict:
    """
    Returns {category_name: tag_id} for every category in desired_categories
    that could be resolved. Static, pre-verified IDs are used first; any
    remaining categories are resolved via a single /tags call. Categories
    that genuinely can't be found are omitted (and printed) rather than
    guessed.
    """
    global _cached_tags

    resolved = {}
    unresolved = []
    for cat in desired_categories:
        static_id = getattr(market_categories.tag_ids, cat, None)
        if static_id is not None:
            resolved[cat] = static_id
        else:
            unresolved.append(cat)

    if unresolved:
        if _cached_tags is None:
            _cached_tags = fetch_all_tags()

        for cat in unresolved:
            search_terms = _ALIASES.get(cat, [cat.replace("_", " "), cat.replace("_", "-")])
            match = _find_tag(search_terms)
            if match:
                resolved[cat] = match["id"]
            else:
                print(f"WARNING: could not resolve category '{cat}' to a real Gamma tag - "
                      f"skipping it rather than guessing an ID. Check config/market_categories.yml "
                      f"or Polymarket's current tag list.")

    return resolved


def _find_tag(search_terms: list):
    for tag in _cached_tags:
        label = (tag.get("label") or "").strip().lower()
        slug = (tag.get("slug") or "").strip().lower()
        for term in search_terms:
            if term == label or term == slug:
                return tag
    return None
