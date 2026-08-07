"""
External evidence ingestion - the ONE module in this whole system that
costs real money per call. It calls the Anthropic Messages API with the
web_search tool to find and read real news about a specific market
question, and returns a structured, cited summary.

HARD GATED: this makes zero API calls unless BOTH:
  1. config/verification.yml has enabled: true
  2. ANTHROPIC_API_KEY is set as an env var

If either is missing, fetch_evidence() returns an INSUFFICIENT_EVIDENCE
stub immediately with no network call and no cost - this is intentional,
not a bug, so the rest of the pipeline (arbitrage, cross-platform, wallet
intel) keeps working today even before you decide to turn this on.
"""

import json
import requests

from config.loader import verification as verification_cfg, ANTHROPIC_API_KEY
from config.cost_profile import CostProfile, register

# Real cost estimate (see README): $0.01/search x up to
# llm_max_searches_per_check searches, plus ~5-10k tokens of reasoning at
# Sonnet rates. Conservative per-call estimate below assumes the configured
# search cap is used.
_ESTIMATED_SEARCHES = 4  # matches default verification.llm_max_searches_per_check
_ESTIMATED_COST_PER_CALL = round(_ESTIMATED_SEARCHES * 0.01 + 0.03, 3)  # searches + token estimate

MODULE_COST_PROFILE = register(CostProfile(
    module_name="ingestion.external_sources",
    requires_paid_api=True,
    estimated_cost_per_call_usd=_ESTIMATED_COST_PER_CALL,
    free_fallback_strategy=(
        "When disabled (verification.enabled: false or no ANTHROPIC_API_KEY), "
        "this returns an explicit INSUFFICIENT_EVIDENCE result immediately with "
        "NO network call and NO cost. The pipeline still runs on the free "
        "arbitrage + Kalshi cross-platform signals; it simply never reaches "
        "PASS-verified status for a BUY_YES/BUY_NO decision, and decision_engine "
        "caps output at MONITOR/NO_TRADE instead. This is the intended safe "
        "default, not a degraded mode."
    ),
    notes="The only module in the entire pipeline with a non-zero cost per call.",
))

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


def evidence_enabled() -> bool:
    return bool(verification_cfg.enabled) and bool(ANTHROPIC_API_KEY)


def fetch_evidence(market_question: str, resolution_rule: str) -> dict:
    """
    Returns a dict matching the shape verification/source_verifier.py
    expects: {"source_urls": [...], "source_trust_scores": {...},
    "primary_source_count": int, "secondary_source_count": int,
    "news_is_current": bool, "summary": str, "raw_ok": bool}

    If evidence checking is disabled/unconfigured, returns immediately
    with raw_ok=False and an explanation - no API call is made.
    """
    if not evidence_enabled():
        return {
            "source_urls": [], "source_trust_scores": {},
            "primary_source_count": 0, "secondary_source_count": 0,
            "news_is_current": False, "event_matches_resolution_rule": False,
            "internally_consistent": False,
            "summary": "Evidence checking is disabled (set verification.enabled: true "
                       "and ANTHROPIC_API_KEY to turn this on).",
            "raw_ok": False,
        }

    prompt = f"""Research this Polymarket prediction market question using web search:

Question: {market_question}
Resolution rule: {resolution_rule}

Search for current, relevant news about this specific question. Then respond
with ONLY a JSON object (no other text, no markdown fences) with this exact shape:

{{
  "source_urls": ["url1", "url2", ...],
  "source_trust_scores": {{"url1": 0.0-1.0, ...}},
  "primary_source_count": <int, count of primary sources like official statements/direct reporting>,
  "secondary_source_count": <int, count of secondary sources like analysis/commentary>,
  "news_is_current": <bool, is the most relevant news from the last 48 hours>,
  "event_matches_resolution_rule": <bool, does the news you found actually address
    what the resolution rule requires, or is it about a related-but-different question>,
  "internally_consistent": <bool, do your found sources agree with each other,
    or do they contradict each other on the key facts>,
  "summary": "<2-3 sentence plain-English summary of what the evidence shows, written for a non-technical reader>"
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
        return _failure_result(f"Evidence request failed: {e}")

    if resp.status_code != 200:
        return _failure_result(f"Evidence API returned {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    full_text = "\n".join(text_blocks).strip()

    parsed = _parse_json_response(full_text)
    if parsed is None:
        return _failure_result("Could not parse evidence response as JSON.")

    parsed["raw_ok"] = True
    return parsed


def _parse_json_response(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _failure_result(reason: str) -> dict:
    return {
        "source_urls": [], "source_trust_scores": {},
        "primary_source_count": 0, "secondary_source_count": 0,
        "news_is_current": False, "event_matches_resolution_rule": False,
        "internally_consistent": False, "summary": reason, "raw_ok": False,
    }
