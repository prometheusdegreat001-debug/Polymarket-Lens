"""
Polymarket Gamma API ingestion (public, no auth). Pulls events/markets by
category and normalizes them into dicts ready for storage.MarketSnapshot,
always including a real market_url (Polymarket action link) and
market_category, per the v2 spec's "every market carries its own link."
"""

import json
from config.loader import GAMMA_API_BASE, POLYMARKET_WEB_BASE, market_categories
from ingestion.http_utils import get_json
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="ingestion.polymarket_api",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - Gamma API is free and public; this module has no paid path at all.",
))


def _safe_json_list(raw):
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def fetch_active_events(max_events: int = 500, page_size: int = 100, tag_id: int = None):
    """
    Gamma's /events endpoint is OFFSET-based (limit+offset), not cursor
    based, and returns a plain JSON array - it does NOT return a
    {data, next_cursor} wrapper. An earlier version of this function
    looked for a "next_cursor" key that never exists in the real
    response, so `cursor` stayed None and the loop broke after exactly
    one page every time, silently capping every category scan at a
    single ~100-event page no matter how high max_events was set. This
    uses real offset pagination and keeps requesting pages until either
    max_events is hit or Gamma returns a short page (end of results).
    """
    events = []
    offset = 0
    while len(events) < max_events:
        limit = min(page_size, max_events - len(events))
        params = {
            "closed": "false", "active": "true",
            "limit": limit, "offset": offset,
        }
        if tag_id is not None:
            params["tag_id"] = tag_id

        data = get_json(f"{GAMMA_API_BASE}/events", params=params)
        page_events = data if isinstance(data, list) else data.get("data", data.get("events", []))

        if not page_events:
            break
        for ev in page_events:
            events.append(_normalize_event(ev))
        offset += len(page_events)
        if len(page_events) < limit:
            break  # short page = last page
    return events


def fetch_events_by_categories(categories: list, max_per_category: int, tag_map: dict = None):
    """
    Pulls events across MULTIPLE categories, deduplicated by event_id.

    tag_map, if given, should be the output of
    ingestion.category_resolver.resolve_category_tag_ids() - a merged
    {category_name: tag_id} covering both the fast static IDs and anything
    resolved dynamically via the real /tags endpoint. If not given, falls
    back to the static-only map (politics/crypto/sports/finance/tech/
    culture/geopolitics) - any category not in that set is silently
    unavailable, which is exactly the bug this parameter fixes.

    If a category's tag-based pull comes back thin (under 1/4 of what was
    asked for), supplements it with a free-text /public-search pass using
    the category name as the query - catches markets that are genuinely
    relevant but weren't tagged with that category on Polymarket's side.
    Best-effort only: search failures are swallowed since the tag-based
    pull is already the primary, reliable path.
    """
    if tag_map is None:
        tag_map = market_categories.tag_ids.__dict__
    seen = set()
    merged = []
    for category in categories:
        tag_id = tag_map.get(category)
        category_events = []
        if tag_id is not None:
            category_events = fetch_active_events(max_events=max_per_category, tag_id=tag_id)

        if len(category_events) < max_per_category // 4:
            try:
                supplemental = search_markets_by_keyword(category.replace("_", " "), max_results=30)
                category_events.extend(supplemental)
            except Exception:
                pass  # supplementary path only - never let this break the primary pull

        for ev in category_events:
            if ev["event_id"] in seen:
                continue
            seen.add(ev["event_id"])
            ev["category"] = category
            merged.append(ev)
    return merged


def search_markets_by_keyword(query: str, max_results: int = 30) -> list:
    """
    Free-text search via Gamma's /public-search endpoint - a SUPPLEMENTARY
    discovery path, not the primary one. /events itself does NOT support
    free-text search (confirmed: a ?q=/?search= param on /events is
    silently ignored and just returns default ordering), so tag-based
    fetch_events_by_categories stays primary. This exists to catch
    markets that are relevant to a category but weren't tagged with it -
    used as a fallback when a tag-based category pull comes back thin.

    Defensive about response shape since this endpoint isn't as widely
    documented as /events - accepts a plain list, or a dict with an
    "events" and/or "markets" key, and normalizes events found through
    the same _normalize_event() used everywhere else.
    """
    data = get_json(f"{GAMMA_API_BASE}/public-search", params={"q": query, "limit": max_results})
    if isinstance(data, list):
        raw_events = data
    elif isinstance(data, dict):
        raw_events = data.get("events", []) or data.get("data", [])
    else:
        raw_events = []
    return [_normalize_event(ev) for ev in raw_events if isinstance(ev, dict)]


def _normalize_event(ev: dict) -> dict:
    slug = ev.get("slug", "")
    market_url = f"{POLYMARKET_WEB_BASE}/event/{slug}" if slug else ""

    markets = []
    for m in ev.get("markets", []):
        outcomes = _safe_json_list(m.get("outcomes"))
        prices = _safe_json_list(m.get("outcomePrices"))
        clob_token_ids = _safe_json_list(m.get("clobTokenIds"))

        outcome_prices = {}
        for i, name in enumerate(outcomes):
            try:
                outcome_prices[name] = float(prices[i])
            except (IndexError, ValueError, TypeError):
                continue

        market_slug = m.get("slug", slug)
        markets.append({
            "market_id": m.get("id"),
            "condition_id": m.get("conditionId"),
            "question": m.get("question"),
            "group_item_title": m.get("groupItemTitle"),
            "outcomes": outcomes,
            "outcome_prices": outcome_prices,
            "clob_token_ids": clob_token_ids,
            "liquidity": _to_float(m.get("liquidity")),
            "volume_24h": _to_float(m.get("volume24hr") or m.get("volume24hrClob")),
            "neg_risk": bool(m.get("negRisk", False)),
            "closed": bool(m.get("closed", False)),
            "end_date": m.get("endDate"),
            "resolution_rule": m.get("description", "") or ev.get("description", ""),
            "market_url": f"{POLYMARKET_WEB_BASE}/event/{market_slug}" if market_slug else market_url,
        })

    return {
        "event_id": ev.get("id"),
        "title": ev.get("title"),
        "slug": slug,
        "market_url": market_url,
        "neg_risk": bool(ev.get("negRisk", False)),
        "markets": markets,
    }


def fetch_event_by_slug(slug: str):
    """
    Looks up a single event by its slug - used by the transaction explainer
    (Phase 3) to pull real resolution criteria and current liquidity for a
    specific past trade's market. Returns None if not found rather than
    guessing.
    """
    if not slug:
        return None
    data = get_json(f"{GAMMA_API_BASE}/events", params={"slug": slug})
    events = data if isinstance(data, list) else data.get("data", [])
    if not events:
        return None
    return _normalize_event(events[0])


def fetch_closed_events(tag_id: int = None, max_events: int = 200, page_size: int = 100):
    """
    Pulls CLOSED/resolved events - the free-tier data source for historical
    precedent matching (historical_context/similar_event_finder.py). Real
    Polymarket resolution history, zero cost, no LLM required.

    Same offset-pagination fix as fetch_active_events above - see its
    docstring.
    """
    events = []
    offset = 0
    while len(events) < max_events:
        limit = min(page_size, max_events - len(events))
        params = {
            "closed": "true",
            "limit": limit, "offset": offset,
        }
        if tag_id is not None:
            params["tag_id"] = tag_id

        data = get_json(f"{GAMMA_API_BASE}/events", params=params)
        page_events = data if isinstance(data, list) else data.get("data", data.get("events", []))

        if not page_events:
            break
        for ev in page_events:
            events.append(_normalize_closed_event(ev))
        offset += len(page_events)
        if len(page_events) < limit:
            break
    return events


def _normalize_closed_event(ev: dict) -> dict:
    """Lighter-weight normalization for closed events - we only need title,
    resolution outcome, and dates for precedent matching, not live prices."""
    slug = ev.get("slug", "")
    markets = ev.get("markets", [])

    # For a resolved binary/neg-risk market, the winning outcome is whichever
    # market's outcomePrices settled near 1.0 - Gamma keeps this in outcomePrices
    # even after close.
    resolved_outcome = None
    for m in markets:
        prices = _safe_json_list(m.get("outcomePrices"))
        outcomes = _safe_json_list(m.get("outcomes"))
        for i, p in enumerate(prices):
            try:
                if float(p) >= 0.95 and i < len(outcomes):
                    resolved_outcome = outcomes[i] if len(outcomes) == 2 else m.get("groupItemTitle") or outcomes[i]
                    break
            except (ValueError, TypeError):
                continue
        if resolved_outcome:
            break

    return {
        "event_id": ev.get("id"),
        "title": ev.get("title"),
        "slug": slug,
        "market_url": f"{POLYMARKET_WEB_BASE}/event/{slug}" if slug else "",
        "resolved_outcome": resolved_outcome,
        "end_date": ev.get("endDate"),
        "closed_at": ev.get("closedTime") or ev.get("endDate"),
    }


def fetch_all_tags(max_tags: int = 500):
    """
    Pulls all tags from Gamma's real /tags endpoint - {id, label, slug} for
    each. This is what lets us resolve category names (weather, iran,
    middle-east, elections, breaking-news, economy, etc.) to real tag IDs
    at runtime instead of hardcoding numbers we can't independently verify.
    """
    tags = []
    offset = 0
    page_size = 100
    while len(tags) < max_tags:
        params = {"limit": min(page_size, max_tags - len(tags)), "offset": offset}
        page = get_json(f"{GAMMA_API_BASE}/tags", params=params)
        if not page:
            break
        for t in page:
            tags.append({"id": t.get("id"), "label": t.get("label"), "slug": t.get("slug")})
        offset += page_size
        if len(page) < page_size:
            break
    return tags


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
