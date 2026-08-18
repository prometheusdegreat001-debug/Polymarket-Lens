"""
Polymarket-native, fully free signal source using real measured price
history (storage.db.market_price_observations - the market's own price
now vs. last scan cycle), not a comparison against any external
benchmark. Complements mispricing.wallet_flow_detector as a second free
path for markets that would otherwise never generate any signal
(arbitrage needs a multi-outcome basket, cross-platform needs a Kalshi
match or paid LLM estimate - most simple 2-way daily markets have
neither).

Framed explicitly as a FADE/contrarian signal, matching
intelligence.opportunity_classifier's NOISE_FADE definition: a sharp,
recent, unverified price move is treated as a candidate overreaction to
bet against, not a trend to follow - momentum-following and mean-
reversion are opposite trading theses, and this module deliberately only
generates the fade interpretation, consistent with how NOISE_FADE is
already defined and risk-checked elsewhere in this codebase.
"""

from datetime import datetime, timezone

from config.loader import risk as risk_cfg
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="mispricing.momentum_detector",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - uses only real, already-tracked price history (storage.db.market_price_observations), no external calls.",
))

MOMENTUM_SIGNAL_THRESHOLD = 0.06  # 6+ points of price move since last scan cycle - same bar opportunity_classifier's NOISE_FADE uses


def detect_momentum_signals(events: list, momentum_by_market_id: dict) -> list:
    """
    momentum_by_market_id: {market_id: price_momentum} - precomputed ONCE
    per scan cycle (see main.py) via storage.db.get_and_update_market_price,
    which has a real side effect (it updates the stored "last seen" price)
    - calling it more than once per market per cycle would corrupt the
    second reading, so this function takes the precomputed values rather
    than calling that function itself.
    """
    signals = []
    for event in events:
        for m in event.get("markets", []):
            market_id = m.get("market_id")
            momentum = momentum_by_market_id.get(market_id)
            if momentum is None or abs(momentum) < MOMENTUM_SIGNAL_THRESHOLD:
                continue
            if m.get("liquidity", 0) < risk_cfg.min_liquidity_usd:
                continue

            # Fade the move: price fell -> bet YES is now underpriced
            # relative to where it just was; price rose -> bet NO. This
            # is a CONTRARIAN thesis, not a momentum-following one - see
            # module docstring.
            direction = "YES" if momentum < 0 else "NO"
            implied = m.get("outcome_prices", {}).get(direction.capitalize())
            if implied is None:
                continue

            signals.append({
                "market_id": market_id,
                "market_url": m.get("market_url", event.get("market_url", "")),
                "signal_type": "momentum_fade",
                "implied_probability": implied,
                "benchmark_probability": None,
                "benchmark_source": "price_history",
                "edge_size": round(abs(momentum), 4),  # the measured swing itself, a real quantity - not fabricated
                "direction": direction,
                # Deliberately the LOWEST confidence of any free signal type:
                # a price move with NO verification or wallet corroboration
                # behind it is the weakest evidence this system generates -
                # intelligence.risk_manager.risk_check already applies extra
                # scrutiny to "noise_fade" opportunities on top of this.
                "confidence": 0.4,
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "_event_title": event.get("title", ""),
                "_momentum": round(momentum, 4),
            })
    return signals
