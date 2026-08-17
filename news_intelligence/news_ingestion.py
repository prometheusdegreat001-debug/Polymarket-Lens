"""
News Ingestion Engine (spec section 9). Fetches from both provided APIs,
normalizes, and stores - deduplication happens at the storage layer (see
storage.db.save_news_article's UNIQUE(url) constraint), not here, so it
stays correct even if the same article comes back from both APIs or
across multiple ingestion cycles.

Deliberately backend-only: no ranking, no market matching, no UI. That's
news_market_matcher.py's job, kept separate so this module can be tested
and reasoned about on its own (see spec section 46: "Layer 3 should not
duplicate Layer 1 or Layer 2 logic" - same modularity principle applied
one level down here too).
"""

from config.loader import news as news_cfg
from config.cost_profile import CostProfile, register
from ingestion.news_sources import fetch_newsdata_latest, fetch_allnews_search
from storage import db

MODULE_COST_PROFILE = register(CostProfile(
    module_name="news_intelligence.news_ingestion",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - uses the user's own provided NewsData.io/AllNewsAPI credentials.",
))


def run_ingestion_cycle(queries: list) -> dict:
    """
    queries: list of search terms to pull news for (e.g. derived from
    active market titles/categories - see main.py's caller for how these
    get chosen). Returns {"fetched": int, "stored": int, "sources_used": [...]}.
    """
    fetched, stored = 0, 0
    sources_used = []

    if news_cfg.newsdata.enabled:
        sources_used.append("newsdata")
        for q in queries:
            articles = fetch_newsdata_latest(query=q, language=news_cfg.newsdata.language,
                                              max_results=news_cfg.newsdata.max_articles_per_fetch)
            fetched += len(articles)
            for a in articles:
                if a.get("url"):
                    before = _article_exists(a["url"])
                    db.save_news_article(a)
                    if not before:
                        stored += 1

    if news_cfg.allnews.enabled:
        sources_used.append("allnews")
        for q in queries:
            articles = fetch_allnews_search(q, max_results=news_cfg.allnews.max_articles_per_fetch)
            fetched += len(articles)
            for a in articles:
                if a.get("url"):
                    before = _article_exists(a["url"])
                    db.save_news_article(a)
                    if not before:
                        stored += 1

    return {"fetched": fetched, "stored": stored, "sources_used": sources_used}


def _article_exists(url: str) -> bool:
    with db.get_conn() as conn:
        row = conn.execute("SELECT 1 FROM news_articles WHERE url = ?", (url,)).fetchone()
    return row is not None
