"""
Polymarket-native, fully free signal source. Doesn't need a Kalshi match
or a paid LLM estimate - real tracked wallets with a proven track record
(wallet_intel's "copy"/"watch" tier) being meaningfully positioned on ONE
side of a market IS the signal, using their REAL current open positions
(main.py's _build_open_positions_detail), not an approximation.

Why this exists: arbitrage signals only fire on multi-outcome baskets
(rare), and cross-platform signals need a Kalshi match (narrow coverage)
or a paid LLM estimate (off by default) - simple 2-way daily Polymarket
markets (most sports, esports, weather propositions) structurally can't
generate a signal through either path. This gives them a real, free
third path.
"""

import json
from datetime import datetime, timezone

from config.loader import risk as risk_cfg
from config.cost_profile import CostProfile, register
from mispricing.probability_model import title_similarity

MODULE_COST_PROFILE = register(CostProfile(
    module_name="mispricing.wallet_flow_detector",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - uses already-fetched wallet open-position data, no external calls of any kind.",
))

MIN_WALLETS_AGREEING = 2
TITLE_MATCH_THRESHOLD = 0.5


def detect_wallet_flow_signals(events: list, tracked_wallets: list) -> list:
    """
    For each event, checks whether MULTIPLE "copy"/"watch" tier tracked
    wallets hold a REAL open position (same title-matching approach used
    everywhere else in this codebase, e.g. main.py's _find_relevant_wallets)
    on the SAME side. Requires agreement, not just presence - one wallet
    holding a position isn't a flow signal, several good wallets
    independently agreeing is.
    """
    signals = []
    qualifying_wallets = [w for w in tracked_wallets if w.get("copy_trade_recommendation") in ("copy", "watch")]
    if len(qualifying_wallets) < MIN_WALLETS_AGREEING:
        return signals

    for event in events:
        market_title = event.get("title", "")
        if not market_title or not event.get("markets"):
            continue

        yes_wallets, no_wallets = [], []
        for w in qualifying_wallets:
            raw_positions = w.get("open_positions_detail")
            try:
                positions = json.loads(raw_positions) if isinstance(raw_positions, str) else (raw_positions or [])
            except (ValueError, TypeError):
                positions = []
            for p in positions:
                if title_similarity(market_title, p.get("market_title", "")) >= TITLE_MATCH_THRESHOLD:
                    outcome = (p.get("outcome") or "").strip().upper()
                    if outcome == "YES":
                        yes_wallets.append(w)
                    elif outcome == "NO":
                        no_wallets.append(w)
                    break

        if len(yes_wallets) >= MIN_WALLETS_AGREEING and len(yes_wallets) > len(no_wallets):
            direction, agreeing = "YES", yes_wallets
        elif len(no_wallets) >= MIN_WALLETS_AGREEING and len(no_wallets) > len(yes_wallets):
            direction, agreeing = "NO", no_wallets
        else:
            continue

        market = event["markets"][0]
        if market.get("liquidity", 0) < risk_cfg.min_liquidity_usd:
            continue
        implied = market.get("outcome_prices", {}).get(direction.capitalize())
        if implied is None:
            continue

        # No price-vs-benchmark comparison exists here by construction -
        # the signal IS the wallet consensus. edge_size is a real,
        # transparent function of HOW MANY good wallets agree and how
        # strong their own track records are (not fabricated), used the
        # same way a price-edge would be for alert-threshold/sizing
        # purposes downstream.
        avg_score = sum(w.get("copy_trade_score", 50) for w in agreeing) / len(agreeing)
        edge_size = round(min(0.03 * len(agreeing) * (avg_score / 100), 0.15), 4)
        confidence = round(min(0.4 + 0.08 * len(agreeing), 0.75), 3)  # capped below arbitrage's mechanical 0.9 - this is inherently softer evidence

        signals.append({
            "market_id": market["market_id"],
            "market_url": market.get("market_url", event.get("market_url", "")),
            "signal_type": "wallet_flow",
            "implied_probability": implied,
            "benchmark_probability": None,
            "benchmark_source": "wallet_consensus",
            "edge_size": edge_size,
            "direction": direction,
            "confidence": confidence,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "_event_title": market_title,
            "_agreeing_wallet_count": len(agreeing),
            "_agreeing_wallets": [w["wallet_address"] for w in agreeing],
        })

    return signals
