"""
Polymarket CLOB API ingestion (public reads, no auth) - live order book,
spread, and midpoint for a given token ID. This gives us real bid/ask/depth
rather than only the Gamma snapshot price, feeding features/liquidity_features.py.
"""

from config.loader import CLOB_API_BASE
from ingestion.http_utils import get_json, ApiError
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="ingestion.orderbook_stream",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - CLOB order book reads are free and public.",
))


def fetch_book(token_id: str):
    """
    Returns {"bids": [[price, size], ...], "asks": [[price, size], ...]}
    or None if the book can't be fetched (e.g. market not on CLOB yet).
    """
    try:
        data = get_json(f"{CLOB_API_BASE}/book", params={"token_id": token_id})
    except ApiError:
        return None

    if not data:
        return None

    return {
        "bids": [[float(b["price"]), float(b["size"])] for b in data.get("bids", [])],
        "asks": [[float(a["price"]), float(a["size"])] for a in data.get("asks", [])],
    }


def compute_spread_and_depth(book: dict, depth_levels: int = 5):
    """
    From a raw book, compute best bid/ask, spread, and total depth (USD)
    within the top `depth_levels` price levels on each side.
    """
    if not book or not book.get("bids") or not book.get("asks"):
        return {"bid": None, "ask": None, "spread": None, "depth_usd": 0.0}

    best_bid = max(book["bids"], key=lambda x: x[0])[0]
    best_ask = min(book["asks"], key=lambda x: x[0])[0]
    spread = round(best_ask - best_bid, 4)

    bid_depth = sum(price * size for price, size in sorted(book["bids"], key=lambda x: -x[0])[:depth_levels])
    ask_depth = sum(price * size for price, size in sorted(book["asks"], key=lambda x: x[0])[:depth_levels])

    return {
        "bid": best_bid, "ask": best_ask, "spread": spread,
        "depth_usd": round(bid_depth + ask_depth, 2),
    }
