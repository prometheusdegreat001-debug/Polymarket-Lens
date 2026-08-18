"""
Produces an "external benchmark probability" for a market, trying sources
in strict cost order:
  1. Kalshi cross-platform match (FREE, real, independent market price)
  2. LLM-elicited estimate (PAID - only if Kalshi has no match AND
     verification is enabled) - and even then, labeled as an estimate,
     never as ground truth.

If neither is available, returns None rather than fabricating a number -
"no benchmark" is an honest result; a guessed one is not.
"""

import re
from difflib import SequenceMatcher

from config.loader import verification as verification_cfg
from ingestion.external_sources import evidence_enabled, _ANTHROPIC_API_URL, _ANTHROPIC_VERSION, _parse_json_response
from config.cost_profile import CostProfile, register
import requests

_TITLE_MATCH_THRESHOLD = 0.55
_STOPWORDS = {"the", "a", "an", "will", "be", "to", "in", "on", "of", "by", "for", "at", "is", "are", "this", "that"}

MODULE_COST_PROFILE = register(CostProfile(
    module_name="mispricing.probability_model",
    requires_paid_api=True,  # conditionally - see notes
    estimated_cost_per_call_usd=0.05,
    free_fallback_strategy=(
        "Kalshi title-matching (FREE) is always attempted FIRST. The paid "
        "LLM-elicited estimate is a LAST RESORT, only reached if: (1) no "
        "Kalshi match was found, AND (2) verification.enabled=true. If "
        "verification is disabled (the default), a market with no Kalshi "
        "match simply gets NO external benchmark at all - never a fabricated "
        "or guessed number."
    ),
    notes="This is the module where 'rule-based before LLM' matters most - "
          "get_benchmark_probability()'s early-return structure enforces it.",
))


def get_benchmark_probability(market: dict, kalshi_markets: list) -> dict:
    """
    Returns {"benchmark_probability": float|None, "benchmark_source": str,
    "kalshi_match": dict|None, "similarity": float|None}
    """
    kalshi_match, similarity = _find_kalshi_match(market, kalshi_markets)
    if kalshi_match:
        return {
            "benchmark_probability": kalshi_match.get("implied_prob"),
            "benchmark_source": "kalshi",
            "kalshi_match": kalshi_match,
            "similarity": similarity,
        }

    # No free match found. Only fall through to the paid path if it's
    # explicitly enabled - never silently spend money.
    if verification_cfg.enabled and evidence_enabled():
        estimate = _llm_elicited_estimate(market.get("question", ""))
        if estimate is not None:
            return {
                "benchmark_probability": estimate,
                "benchmark_source": "llm_estimate",
                "kalshi_match": None,
                "similarity": None,
            }

    return {"benchmark_probability": None, "benchmark_source": "none", "kalshi_match": None, "similarity": None}


def _find_kalshi_match(market: dict, kalshi_markets: list):
    best_match, best_score = find_best_kalshi_candidate(market, kalshi_markets)
    if best_match and best_score >= _TITLE_MATCH_THRESHOLD:
        return best_match, round(best_score, 3)
    return None, None


def find_best_kalshi_candidate(market: dict, kalshi_markets: list):
    """
    Returns the single best Kalshi candidate and its score REGARDLESS of
    whether it clears the match threshold - used by main.py to log
    near-misses, so "0 cross-platform signals" is diagnosable (bad
    phrasing match vs. genuinely no equivalent Kalshi market) instead of
    a silent black box.
    """
    best_match, best_score = None, 0.0
    for km in kalshi_markets:
        score = _title_similarity(market.get("question", ""), km.get("title", ""))
        if score > best_score:
            best_score = score
            best_match = km
    return best_match, round(best_score, 3) if best_match else 0.0


def _title_similarity(a: str, b: str) -> float:
    def tokens(t):
        t = _normalize_numbers(t)
        t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
        return set(w for w in t.split() if w not in _STOPWORDS and len(w) > 1)

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    seq = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    return 0.6 * jaccard + 0.4 * seq


def _normalize_numbers(text: str) -> str:
    """
    Polymarket and Kalshi phrase the same price level very differently -
    "$150,000", "150k", "150000" all mean the same thing but would tokenize
    to completely different words. This normalizes common numeric formats
    to a consistent form BEFORE tokenizing, which is what actually lets
    crypto/finance markets match across platforms - without this, almost
    every crypto market fails to match purely on phrasing, not because no
    equivalent market exists.
    """
    def expand_k(match):
        return str(int(float(match.group(1)) * 1000))

    text = re.sub(r"\$?([\d.]+)\s*k\b", expand_k, text, flags=re.IGNORECASE)
    text = re.sub(r"[\$,]", "", text)
    return text


# Public alias - this is the one title-similarity function used consistently
# across Kalshi matching (this module), historical precedent matching
# (historical_context/similar_event_finder.py), and anywhere else in the
# system that needs "are these two market titles about the same thing."
title_similarity = _title_similarity


def _llm_elicited_estimate(market_question: str):
    """
    PAID. Only called after the free Kalshi path has already failed and
    verification is explicitly enabled. Returns a probability estimate
    (0-1) or None if the call fails/can't be parsed - never a guess.
    """
    from config.loader import ANTHROPIC_API_KEY

    prompt = f"""Estimate the true probability (0.0 to 1.0) that this event resolves YES,
using web search to inform your estimate:

Question: {market_question}

Respond with ONLY a JSON object (no other text): {{"probability": 0.0-1.0, "reasoning": "1-2 sentences"}}"""

    try:
        resp = requests.post(
            _ANTHROPIC_API_URL,
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": _ANTHROPIC_VERSION,
                     "content-type": "application/json"},
            json={
                "model": verification_cfg.llm_model, "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [{"type": "web_search_20250305", "name": "web_search",
                           "max_uses": verification_cfg.llm_max_searches_per_check}],
            },
            timeout=45,
        )
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        return None

    data = resp.json()
    text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    parsed = _parse_json_response(text)
    if not parsed:
        return None
    try:
        return float(parsed["probability"])
    except (KeyError, ValueError, TypeError):
        return None
