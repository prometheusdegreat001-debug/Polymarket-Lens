"""
Raw fetchers for the two provided news APIs. Both normalize into the SAME
shape (see _normalize_newsdata_article / _normalize_allnews_article) so
downstream code (news_intelligence/news_ingestion.py) never needs to know
which source an article came from.

SECURITY: API keys are read from config.loader (which reads them from
environment variables only) and are NEVER logged, printed, or included in
any stored record. If a key is missing, the fetcher returns an empty list
and prints a plain warning - it never silently fabricates articles.

NewsData.io (/latest): response shape confirmed against NewsData.io's own
published documentation and multiple independent sources - {"status",
"totalResults", "results": [{"article_id", "title", "link", "description",
"pubDate", "source_id", "source_name", "category", "country", "language",
"keywords", ...}], "nextPage"}.

AllNewsAPI (api.allnewsapi.com): the exact response schema for THIS
specific service could NOT be confirmed - every search for "AllNewsAPI"
surfaced OTHER, differently-branded news APIs (newsapi.org, thenewsapi.com,
newscatcherapi.com) instead of documentation for api.allnewsapi.com
itself. Per the explicit instruction not to guess service mappings, this
fetcher is written DEFENSIVELY: it tries several plausible response
shapes/field names, and if none match, it logs exactly what came back
(structure only, never the raw content indiscriminately) rather than
silently returning nothing or fabricating a shape that fits.
"""

from datetime import datetime, timezone

from config.loader import (
    NEWSDATA_API_KEY, ALLNEWS_API_KEY, NEWSDATA_API_BASE, ALLNEWS_API_BASE,
)
from ingestion.http_utils import get_json, ApiError
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="ingestion.news_sources",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - both NewsData.io and AllNewsAPI are the user's own provided credentials, treated as free/included for this system's purposes.",
))


def fetch_newsdata_latest(query: str = None, category: str = None, country: str = None,
                           language: str = "en", max_results: int = 50) -> list:
    if not NEWSDATA_API_KEY:
        print("News: NEWSDATA_API_KEY not set - skipping NewsData.io fetch.")
        return []

    params = {"apikey": NEWSDATA_API_KEY, "language": language}
    if query:
        params["q"] = query[:500]  # documented max query length
    if category:
        params["category"] = category
    if country:
        params["country"] = country

    try:
        data = get_json(f"{NEWSDATA_API_BASE}/latest", params=params)
    except ApiError as e:
        print(f"WARNING: NewsData.io fetch failed: {e}")
        return []

    if data.get("status") != "success":
        print(f"WARNING: NewsData.io returned status={data.get('status')}: {data.get('results', 'no detail')}")
        return []

    results = data.get("results") or []
    return [_normalize_newsdata_article(a) for a in results[:max_results]]


def fetch_allnews_search(query: str, max_results: int = 30) -> list:
    if not ALLNEWS_API_KEY:
        print("News: ALLNEWS_API_KEY not set - skipping AllNewsAPI fetch.")
        return []

    params = {"apikey": ALLNEWS_API_KEY, "q": query}
    try:
        data = get_json(f"{ALLNEWS_API_BASE}/search", params=params)
    except ApiError as e:
        print(f"WARNING: AllNewsAPI fetch failed: {e}")
        return []

    # Defensive multi-shape handling - see module docstring for why.
    raw_articles = None
    for key in ("articles", "results", "data", "news"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            raw_articles = data[key]
            break
    if raw_articles is None and isinstance(data, list):
        raw_articles = data

    if raw_articles is None:
        print(f"WARNING: AllNewsAPI response didn't match any expected shape. "
              f"Top-level keys found: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}. "
              f"Schema for this specific API could not be confirmed ahead of time - please share a sample "
              f"response so the fetcher can be corrected.")
        return []

    return [_normalize_allnews_article(a) for a in raw_articles[:max_results] if isinstance(a, dict)]


def _normalize_newsdata_article(a: dict) -> dict:
    return {
        "source_api": "newsdata",
        "external_id": a.get("article_id"),
        "headline": a.get("title"),
        "description": a.get("description"),
        "url": a.get("link"),
        "source_name": a.get("source_name") or a.get("source_id"),
        "published_at": _parse_newsdata_pubdate(a.get("pubDate")),
        "country": a.get("country") or [],
        "category": a.get("category") or [],
        "keywords": a.get("keywords") or [],
        "language": a.get("language"),
    }


def _normalize_allnews_article(a: dict) -> dict:
    # Field names are best-effort guesses across common conventions
    # (title/headline, url/link, description/summary/content,
    # publishedAt/pubDate/date) since the exact schema is unconfirmed -
    # see module docstring. Whichever key is actually present gets used;
    # nothing is fabricated if a field is genuinely absent.
    headline = a.get("title") or a.get("headline")
    url = a.get("url") or a.get("link")
    description = a.get("description") or a.get("summary") or a.get("content")
    published_raw = a.get("publishedAt") or a.get("pubDate") or a.get("date") or a.get("published_at")
    source = a.get("source")
    source_name = source.get("name") if isinstance(source, dict) else source

    return {
        "source_api": "allnews",
        "external_id": a.get("id") or url,
        "headline": headline,
        "description": description,
        "url": url,
        "source_name": source_name,
        "published_at": _parse_generic_timestamp(published_raw),
        "country": [],
        "category": [],
        "keywords": [],
        "language": a.get("language"),
    }


def _parse_newsdata_pubdate(pubdate_str):
    """NewsData.io format: 'YYYY-MM-DD HH:MM:SS', UTC per their documented convention."""
    if not pubdate_str:
        return None
    try:
        dt = datetime.strptime(pubdate_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return _parse_generic_timestamp(pubdate_str)


def _parse_generic_timestamp(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            continue
    return None
