"""
Source quality/trust scoring - a transparent, editable domain reputation
heuristic, not a black-box model. Used to sanity-check the LLM's own
self-reported source_trust_scores from ingestion/external_sources.py
against an independent, hardcoded reference list.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="features.source_quality_features",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))

from urllib.parse import urlparse

# Reference trust scores by domain - deliberately conservative and editable.
# This is NOT exhaustive; unknown domains get a neutral default score.
_KNOWN_DOMAIN_TRUST = {
    "reuters.com": 0.95, "apnews.com": 0.95, "bloomberg.com": 0.90,
    "wsj.com": 0.90, "ft.com": 0.90, "bbc.com": 0.88, "npr.org": 0.85,
    "nytimes.com": 0.85, "washingtonpost.com": 0.85,
    "coindesk.com": 0.75, "theblock.co": 0.75,
    "twitter.com": 0.35, "x.com": 0.35, "reddit.com": 0.30,
    "medium.com": 0.40, "substack.com": 0.40,
}
_DEFAULT_TRUST = 0.5


def score_source(url: str) -> float:
    domain = _extract_domain(url)
    return _KNOWN_DOMAIN_TRUST.get(domain, _DEFAULT_TRUST)


def score_sources(urls: list) -> dict:
    return {url: score_source(url) for url in urls}


def is_primary_source(url: str) -> bool:
    """
    Primary = wire services and major outlets with direct reporting.
    Secondary = everything else with a reasonable trust score. This is a
    simplification - true primary/secondary classification would need to
    read the article, not just the domain - flagged as a known limitation.
    """
    return score_source(url) >= 0.80


def _extract_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""
