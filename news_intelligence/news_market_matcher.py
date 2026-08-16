"""
News Relevance Engine (spec section 10). Matches stored news articles to
a given market using keyword overlap - reuses
ingestion.free_news_sources' existing keyword extraction/matching (same
utility, not a reimplementation) rather than duplicating that logic here.
"""

from config.loader import news as news_cfg
from config.cost_profile import CostProfile, register
from ingestion.free_news_sources import _extract_keywords, _keyword_match
from storage import db

MODULE_COST_PROFILE = register(CostProfile(
    module_name="news_intelligence.news_market_matcher",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure keyword-overlap matching over already-stored articles.",
))


def match_articles_to_market(market_title: str, market_description: str = None,
                              extra_keywords: list = None, articles: list = None) -> list:
    """
    Searches recently-ingested news for articles relevant to this market,
    using the same keyword-overlap approach as the free RSS matcher.
    extra_keywords lets a caller pass in specific entities (people/orgs/
    countries) already known to be relevant, boosting recall beyond
    what's in the title/description alone.

    articles: pass in an already-fetched list (e.g.
    storage.db.get_recent_news_articles called ONCE per scan cycle) to
    avoid re-querying and re-scanning the same article set from scratch
    for every single market being checked in that cycle - with up to
    MAX_SIGNALS_PROCESSED_PER_RUN signals per cycle, calling this without
    reusing a shared article list means the same full-table read repeats
    that many times for identical data. If omitted, fetches fresh
    (bounded by news.yml's max_article_age_hours) - useful for one-off/
    testing calls where reuse doesn't matter.

    Returns up to news.yml's max_matches_per_market articles, each with an
    added "overlap_score" (0-1) and "matched_keyword_count".
    """
    query_text = " ".join(filter(None, [market_title, market_description, " ".join(extra_keywords or [])]))
    query_keywords = _extract_keywords(query_text)
    if not query_keywords:
        return []

    if articles is None:
        articles = db.get_recent_news_articles(max_age_hours=news_cfg.max_article_age_hours)

    matches = []
    for a in articles:
        candidate_text = " ".join(filter(None, [a.get("headline"), a.get("description")]))
        matched_count, overlap_ratio = _keyword_match(query_keywords, candidate_text)
        if overlap_ratio >= news_cfg.min_relevance_overlap:
            matches.append({**a, "overlap_score": round(overlap_ratio, 4), "matched_keyword_count": matched_count})

    matches.sort(key=lambda m: -m["overlap_score"])
    return matches[:news_cfg.max_matches_per_market]
