"""
Free-tier news/evidence ingestion via real, public RSS/Atom feeds. Zero
cost, no LLM, no API key. This is checked BEFORE the paid LLM evidence
tier (ingestion/external_sources.py) - if enough real, current, relevant
coverage is found here, verification can PASS without ever touching the
paid tier.

Honesty notes on feed selection (checked against current sources, not
assumed from memory):
  - Reuters and AP News both discontinued their direct public RSS feeds
    around 2020. The real, still-working free alternative is Google
    News' RSS search, scoped to their domain - a genuine, documented,
    free Google endpoint, not a workaround of questionable legitimacy.
  - Anthropic does not currently publish a public RSS feed - omitted
    rather than guessed. Anthropic-relevant coverage is still reachable
    via the AI-news aggregator feeds and arXiv below.
  - Government feeds vary enormously by agency; only the Federal
    Register (a genuinely stable, official, documented feed) is included
    by default. Add more via GOV_FEEDS below or google_alerts_rss_urls
    in config/verification.yml if you have specific agencies you track.
  - Google Alerts feeds are inherently personal (Google generates a
    private RSS URL per alert you configure at google.com/alerts) - they
    can't be hardcoded here. Add yours via config/verification.yml's
    google_alerts_rss_urls list.
"""

import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests

from config.loader import verification as verification_cfg
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="ingestion.free_news_sources",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - this module IS the free tier. All feeds are public RSS/Atom, no auth, no cost.",
))

_REQUEST_TIMEOUT = 10

# --- News wires (Reuters/AP have no direct RSS since ~2020 - Google News
# RSS search scoped to their domain is the real, working free substitute) ---
NEWSWIRE_FEEDS = [
    {"name": "Reuters (via Google News)", "url": "https://news.google.com/rss/search?q=when:2d+allinurl:reuters.com&hl=en-US&gl=US&ceid=US:en", "tier": "primary"},
    {"name": "AP News (via Google News)", "url": "https://news.google.com/rss/search?q=when:2d+allinurl:apnews.com&hl=en-US&gl=US&ceid=US:en", "tier": "primary"},
]

# --- Government / official (only genuinely stable official feeds by default) ---
GOV_FEEDS = [
    {"name": "Federal Register", "url": "https://www.federalregister.gov/documents.rss", "tier": "primary"},
]

# --- AI labs / research (official blogs + arXiv, real confirmed feed paths) ---
AI_FEEDS = [
    {"name": "OpenAI News", "url": "https://openai.com/news/rss.xml", "tier": "primary"},
    {"name": "Google Research/DeepMind", "url": "https://research.google/blog/rss", "tier": "primary"},
    {"name": "Hugging Face Blog", "url": "https://huggingface.co/blog/feed.xml", "tier": "primary"},
    {"name": "MIT Technology Review - AI", "url": "https://www.technologyreview.com/feed/", "tier": "secondary"},
    {"name": "arXiv cs.AI", "url": "https://export.arxiv.org/rss/cs.AI", "tier": "secondary"},
    {"name": "arXiv cs.LG", "url": "https://export.arxiv.org/rss/cs.LG", "tier": "secondary"},
    {"name": "arXiv cs.CL", "url": "https://export.arxiv.org/rss/cs.CL", "tier": "secondary"},
    {"name": "arXiv cs.CV", "url": "https://export.arxiv.org/rss/cs.CV", "tier": "secondary"},
]

# --- General tech news (standard, long-stable feed paths) ---
TECH_FEEDS = [
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "tier": "secondary"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "tier": "secondary"},
    {"name": "Wired", "url": "https://www.wired.com/feed/rss", "tier": "secondary"},
    {"name": "VentureBeat", "url": "https://venturebeat.com/feed/", "tier": "secondary"},
]

CATEGORY_FEED_MAP = {
    "politics": NEWSWIRE_FEEDS + GOV_FEEDS,
    "geopolitics": NEWSWIRE_FEEDS + GOV_FEEDS,
    "finance": NEWSWIRE_FEEDS,
    "crypto": NEWSWIRE_FEEDS + TECH_FEEDS,
    "tech": AI_FEEDS + TECH_FEEDS,
    "culture": TECH_FEEDS,
}

_STOPWORDS = {
    "the", "a", "an", "will", "be", "to", "in", "on", "of", "by", "for",
    "at", "is", "are", "this", "that", "and", "or", "does", "do", "did",
    "has", "have", "had", "with", "than", "reach", "reaches",
}


def user_configured_feeds() -> list:
    """Personal Google Alerts RSS URLs, if the user has added any."""
    urls = getattr(verification_cfg, "google_alerts_rss_urls", []) or []
    return [{"name": "Google Alert (user-configured)", "url": u, "tier": "secondary"} for u in urls]


def search_feeds(query_text: str, market_category: str = None, max_age_hours: int = None, max_results: int = 8) -> list:
    """
    Searches relevant free RSS feeds for entries matching query_text
    (typically the market question). Returns a list of matches:
    {"title", "link", "published_iso", "source_name", "tier", "hours_old"}

    This is real keyword matching against real, freshly-fetched RSS
    content - not a guess and not an LLM call.
    """
    max_age_hours = max_age_hours or verification_cfg.max_news_age_hours
    feeds = CATEGORY_FEED_MAP.get(market_category, NEWSWIRE_FEEDS + TECH_FEEDS) + user_configured_feeds()

    keywords = _extract_keywords(query_text)
    if not keywords:
        return []

    now = datetime.now(timezone.utc)
    matches = []

    for feed in feeds:
        try:
            entries = _fetch_feed(feed["url"])
        except Exception:
            continue  # one dead/slow feed shouldn't break the whole search

        for entry in entries:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            combined = f"{title} {summary}"
            matched_count, overlap = _keyword_match(keywords, combined)
            # Accept if either a strong ratio OR at least 2 absolute keyword
            # matches - a ratio-only cutoff unfairly penalizes short,
            # specific queries (e.g. "Iran", "agreement" matching is a
            # strong signal even if it's only 2 of 6 query keywords).
            if overlap < 0.4 and matched_count < 2:
                continue

            published = entry.get("published")
            hours_old = None
            if published:
                hours_old = (now - published).total_seconds() / 3600
                if hours_old > max_age_hours * 6:
                    # Way outside freshness window even generously - skip.
                    # (6x multiplier since some feeds, like arXiv, are
                    # naturally lower-frequency than daily news wires.)
                    continue

            matches.append({
                "title": title,
                "link": entry.get("link", ""),
                "published_iso": published.isoformat() if published else None,
                "source_name": feed["name"],
                "tier": feed["tier"],
                "hours_old": round(hours_old, 1) if hours_old is not None else None,
                "overlap_score": round(overlap, 3),
            })

    matches.sort(key=lambda m: -m["overlap_score"])
    return matches[:max_results]


def _extract_keywords(text: str) -> set:
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
    words = [w for w in text.split() if len(w) > 2 and w.lower() not in _STOPWORDS]
    return set(w.lower() for w in words)


def _keyword_match(query_keywords: set, candidate_text: str) -> tuple:
    """Returns (matched_count, overlap_ratio)."""
    candidate_keywords = _extract_keywords(candidate_text)
    if not query_keywords or not candidate_keywords:
        return 0, 0.0
    matched = query_keywords & candidate_keywords
    return len(matched), len(matched) / len(query_keywords)


def _fetch_feed(url: str) -> list:
    """
    Fetches and parses an RSS or Atom feed using stdlib XML parsing (no
    feedparser dependency - keeps requirements.txt minimal). Handles both
    RSS 2.0 (<item>) and Atom (<entry>) formats.
    """
    raw = _fetch_raw(url)
    root = ET.fromstring(raw)

    entries = []
    # RSS 2.0
    for item in root.findall(".//item"):
        entries.append({
            "title": _text(item, "title"),
            "link": _text(item, "link"),
            "summary": _text(item, "description"),
            "published": _parse_date(_text(item, "pubDate")),
        })
    # Atom
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//atom:entry", ns):
        link_el = entry.find("atom:link", ns)
        entries.append({
            "title": _text(entry, "atom:title", ns),
            "link": link_el.get("href") if link_el is not None else "",
            "summary": _text(entry, "atom:summary", ns),
            "published": _parse_date(_text(entry, "atom:updated", ns) or _text(entry, "atom:published", ns)),
        })

    return entries


def _fetch_raw(url: str) -> bytes:
    resp = requests.get(url, timeout=_REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (compatible; PolymarketIntelBot/1.0)"})
    resp.raise_for_status()
    return resp.content


def _text(el, tag, ns=None):
    found = el.find(tag, ns) if ns else el.find(tag)
    return found.text.strip() if found is not None and found.text else ""


def _parse_date(date_str: str):
    if not date_str:
        return None
    formats = [
        "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
