"""
Fetches fuller resolution metadata for a specific market when the
verification pipeline needs to check "does this event match the
settlement rule" in more detail than the summary already captured during
the main event pull.
"""

from config.loader import GAMMA_API_BASE
from ingestion.http_utils import get_json, ApiError
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="ingestion.resolution_metadata",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - Gamma market detail endpoint is free and public.",
))


def fetch_market_detail(market_id: str):
    """
    Returns the full Gamma market object for one market_id, including its
    complete resolution/description text - used by
    verification/resolution_rule_checker.py.
    """
    try:
        data = get_json(f"{GAMMA_API_BASE}/markets/{market_id}")
    except ApiError:
        return None
    if not data:
        return None
    return {
        "market_id": data.get("id"),
        "question": data.get("question"),
        "resolution_rule": data.get("description", ""),
        "end_date": data.get("endDate"),
        "closed": bool(data.get("closed", False)),
        "uma_resolution_status": data.get("umaResolutionStatus"),
    }
