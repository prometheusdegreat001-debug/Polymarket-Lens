"""
Historical precedent research - same cost/gating profile as
external_sources.py (Anthropic API + web_search, disabled by default).
Answers: does this event resemble past similar setups, and how did those
resolve?
"""

import json
import requests

from config.loader import verification as verification_cfg, ANTHROPIC_API_KEY
from ingestion.external_sources import evidence_enabled, _ANTHROPIC_API_URL, _ANTHROPIC_VERSION, _parse_json_response
from config.cost_profile import CostProfile, register

_ESTIMATED_SEARCHES = 4
_ESTIMATED_COST_PER_CALL = round(_ESTIMATED_SEARCHES * 0.01 + 0.04, 3)  # slightly higher - reads more sources

MODULE_COST_PROFILE = register(CostProfile(
    module_name="ingestion.historical_events",
    requires_paid_api=True,
    estimated_cost_per_call_usd=_ESTIMATED_COST_PER_CALL,
    free_fallback_strategy=(
        "When disabled, returns precedent_score=0.0 (neutral - neither "
        "supportive nor negative) with an explicit explanation that no "
        "research was performed. decision_engine treats precedent_score=0.0 "
        "as 'no information,' never as 'confirmed neutral precedent' - the "
        "distinction matters and is preserved via raw_ok=False."
    ),
))


def fetch_precedent(market_question: str) -> dict:
    """
    Returns {"similar_events": [{"title":.., "outcome":.., "date":.., "source_url":..}, ...],
    "precedent_score": -1.0 to 1.0, "precedent_summary": str,
    "resembles_failed_setup": bool, "source_urls": [...], "raw_ok": bool}
    """
    if not evidence_enabled():
        return {
            "similar_events": [], "precedent_score": 0.0,
            "precedent_summary": "Historical precedent research is disabled "
                                  "(set verification.enabled: true and ANTHROPIC_API_KEY to turn this on).",
            "resembles_failed_setup": False, "source_urls": [], "raw_ok": False,
        }

    prompt = f"""Research the historical precedent for this Polymarket prediction market question:

Question: {market_question}

Search for similar past events, talks, or situations (e.g. prior negotiations,
prior similar predictions, prior similar deadlines) and how they actually
resolved. Then respond with ONLY a JSON object (no other text, no markdown
fences) with this exact shape:

{{
  "similar_events": [{{"title": "...", "outcome": "...", "date": "...", "source_url": "..."}}],
  "precedent_score": <float -1.0 to 1.0, where -1 means strong negative precedent
    (similar past setups consistently failed/didn't happen), +1 means strong
    supportive precedent, 0 means no clear precedent either way>,
  "precedent_summary": "<2-3 sentence plain-English summary>",
  "resembles_failed_setup": <bool, does this closely resemble a past setup that failed>,
  "source_urls": ["url1", "url2", ...]
}}"""

    try:
        resp = requests.post(
            _ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": verification_cfg.llm_model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": verification_cfg.llm_max_searches_per_check,
                }],
            },
            timeout=60,
        )
    except requests.RequestException as e:
        return _failure_result(f"Precedent request failed: {e}")

    if resp.status_code != 200:
        return _failure_result(f"Precedent API returned {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    full_text = "\n".join(text_blocks).strip()

    parsed = _parse_json_response(full_text)
    if parsed is None:
        return _failure_result("Could not parse precedent response as JSON.")

    parsed["raw_ok"] = True
    return parsed


def _failure_result(reason: str) -> dict:
    return {
        "similar_events": [], "precedent_score": 0.0, "precedent_summary": reason,
        "resembles_failed_setup": False, "source_urls": [], "raw_ok": False,
    }
