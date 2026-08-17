"""
STEP 1 of the decision pipeline: "WHAT TYPE OF OPPORTUNITY IS THIS?"

This runs BEFORE any BUY_YES/BUY_NO logic. The old decision engine went
straight from "is there a pricing edge?" to a buy/no-buy call - it never
asked what KIND of edge it was looking at, so a genuine structural
mispricing, a fleeting overreaction, and smart-money order flow all got
evaluated through the exact same lens. That's the wrong lens for at least
two of the three: a noise overreaction needs to be checked differently
than a deep structural mispricing does.

Every opportunity gets classified into exactly one of four types, using
only signals this system already computes for real (no fabricated
inputs):

- NOISE_FADE: a sharp, recent, UNVERIFIED price move (real
  price_momentum data, see storage.db.market_price_observations) with no
  news or precedent support behind it - the classic "crowd overreacted,
  this should mean-revert" setup. Inherently higher-variance: the edge
  IS the overreaction, and overreactions can also just be right.
- FLOW_SCALP: copy-worthy tracked wallets are actively positioned on one
  side (real wallet_agreement_score, not a guess) in a market resolving
  soon - a short-horizon, wallet-driven signal rather than a structural
  pricing thesis.
- DEEP_VALUE: a large, well-verified, structurally-grounded mispricing
  (real cross-platform/arbitrage math or passed news verification) with
  enough time to resolution that this isn't a rushed trade.
- SPECIALIST_EDGE: corroborated specifically by wallets who are
  proven category specialists (real leaderboard_source_categories data
  from the wallet-discovery leaderboard pool, see
  ingestion.wallet_activity.fetch_leaderboard_pool) in THIS market's own
  category - domain expertise, not a generic pricing signal.
- UNCLASSIFIED: none of the above patterns clearly fit. This is not a
  trade call by default - see intelligence.decision_engine, which routes
  UNCLASSIFIED to WATCH at best, never TRADE, since we can't tell a
  coherent story about why the edge exists.
"""

import json

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="intelligence.opportunity_classifier",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure rule-based classification over already-computed signals.",
))

_NOISE_FADE_MOMENTUM_THRESHOLD = 0.06   # 6+ points of price move since last scan cycle
_DEEP_VALUE_MIN_EDGE = 0.08             # matches verification.yml's min_edge_for_paid_verification
_DEEP_VALUE_MIN_DAYS_TO_RESOLUTION = 2.0
_FLOW_SCALP_MAX_DAYS_TO_RESOLUTION = 2.0
_FLOW_SCALP_MIN_AGREEMENT = 0.3


def classify_opportunity(mispricing_signal: dict, verification: dict, historical: dict,
                          wallet_evaluations: list, wallet_agreement_score: float,
                          market_features: dict, market_category: str = None) -> dict:
    """
    Returns {"opportunity_type": ..., "opportunity_label": ..., "classification_reason": ...}.
    Checked in a deliberate priority order (see each branch's comment for
    why it's ranked where it is) - a market can technically show traits of
    more than one type, but the FIRST clear match is the most useful
    single story to tell about why this might be worth trading.
    """
    edge_size = mispricing_signal.get("edge_size", 0.0) or 0.0
    direction = mispricing_signal.get("direction")
    time_to_resolution = market_features.get("time_to_resolution_days")
    momentum = market_features.get("price_momentum", 0.0) or 0.0

    # --- SPECIALIST_EDGE checked FIRST: domain-specific corroboration is
    # the strongest, most specific story available when it's real, and
    # shouldn't be masked by a more generic DEEP_VALUE/FLOW_SCALP match
    # that happens to also technically apply. ---
    specialist_wallets = _find_category_specialist_wallets(wallet_evaluations, market_category)
    if specialist_wallets and direction in ("YES", "NO"):
        names = ", ".join(w.get("username") or w["wallet_address"][:10] for w in specialist_wallets[:3])
        return {
            "opportunity_type": "specialist_edge",
            "opportunity_label": "Specialist Edge",
            "classification_reason": (
                f"{len(specialist_wallets)} wallet(s) with a PROVEN track record specifically in "
                f"{market_category or 'this'} markets ({names}) are positioned in this market - "
                f"domain expertise, not just a generic pricing signal."
            ),
        }

    # --- DEEP_VALUE: a real, large, structurally-grounded edge with room
    # to be patient. Checked before FLOW_SCALP/NOISE_FADE since a
    # genuinely large, verified edge is the strongest kind of story and
    # shouldn't be downgraded to a shorter-horizon label just because
    # wallet flow or momentum also happen to be present. ---
    is_mechanically_verified = (
        mispricing_signal.get("signal_type") == "arbitrage"
        or (mispricing_signal.get("signal_type") == "cross_platform"
            and mispricing_signal.get("benchmark_source") == "kalshi")
    )
    is_news_verified = verification and verification.get("status") == "PASS"
    if (edge_size >= _DEEP_VALUE_MIN_EDGE and (is_mechanically_verified or is_news_verified)
            and (time_to_resolution is None or time_to_resolution >= _DEEP_VALUE_MIN_DAYS_TO_RESOLUTION)):
        basis = "mechanical cross-market/arbitrage math" if is_mechanically_verified else "independent news verification"
        return {
            "opportunity_type": "deep_value",
            "opportunity_label": "Deep Value",
            "classification_reason": (
                f"{edge_size*100:.1f}pp structural edge backed by {basis}, with "
                f"{'no imminent deadline' if time_to_resolution is None else f'{time_to_resolution:.1f} days'} "
                f"to resolution - room to be patient rather than rushed."
            ),
        }

    # --- FLOW_SCALP: short-horizon, wallet-driven. Real
    # wallet_agreement_score (see decision_engine._compute_wallet_agreement),
    # not a guess - requires actual "copy"-tier wallets on the same side. ---
    if (wallet_agreement_score >= _FLOW_SCALP_MIN_AGREEMENT and direction in ("YES", "NO")
            and time_to_resolution is not None and time_to_resolution <= _FLOW_SCALP_MAX_DAYS_TO_RESOLUTION):
        return {
            "opportunity_type": "flow_scalp",
            "opportunity_label": "Flow Scalp",
            "classification_reason": (
                f"Copy-worthy tracked wallets are positioned {direction} (agreement score "
                f"{wallet_agreement_score:.2f}) with only {time_to_resolution:.1f} days to resolution - "
                f"a short-horizon, order-flow-driven setup rather than a structural thesis."
            ),
        }

    # --- NOISE_FADE: a real, measured price swing (see
    # storage.db.get_and_update_market_price) without verification or
    # precedent support behind it. Checked last since it's the
    # highest-variance, least-evidenced category - only reached once
    # nothing more structurally grounded matched. ---
    lacks_support = (not verification or verification.get("status") in (None, "FAIL", "INSUFFICIENT_EVIDENCE"))
    if abs(momentum) >= _NOISE_FADE_MOMENTUM_THRESHOLD and lacks_support:
        fade_direction = "YES" if momentum < 0 else "NO"  # price fell -> fade toward YES being underpriced now, and vice versa
        return {
            "opportunity_type": "noise_fade",
            "opportunity_label": "Noise Fade",
            "classification_reason": (
                f"Price moved {momentum*100:+.1f}pp recently with no verification or precedent "
                f"support behind the move - looks like an overreaction worth fading toward "
                f"{fade_direction}, not a confirmed structural edge."
            ),
        }

    return {
        "opportunity_type": "unclassified",
        "opportunity_label": "Unclassified",
        "classification_reason": (
            "This doesn't clearly fit a specialist, deep-value, flow, or noise-fade pattern - "
            "there may be a pricing edge, but no coherent story for WHY it exists."
        ),
    }


def _find_category_specialist_wallets(wallet_evaluations: list, market_category: str) -> list:
    """
    Real check, not a guess: does this market's category appear in a
    wallet's leaderboard_source_categories (which real category-specific
    leaderboard pulls it was actually found in - see
    ingestion.wallet_activity.fetch_leaderboard_pool)? OVERALL doesn't
    count as a "specialist" category - only a real niche category match
    does, and only for wallets with an actual "copy" or "watch"
    recommendation (a wallet with a bad track record showing up in a
    category leaderboard isn't a meaningful specialist signal).
    """
    if not market_category or not wallet_evaluations:
        return []
    target = _MARKET_CATEGORY_TO_LEADERBOARD_CATEGORY.get(market_category.strip().lower(), market_category.strip().upper())
    specialists = []
    for w in wallet_evaluations:
        if w.get("copy_trade_recommendation") not in ("copy", "watch"):
            continue
        raw_categories = w.get("leaderboard_source_categories")
        if isinstance(raw_categories, str):
            try:
                raw_categories = json.loads(raw_categories)
            except (ValueError, TypeError):
                raw_categories = []
        categories = {str(c).upper() for c in (raw_categories or [])} - {"OVERALL"}
        if target in categories:
            specialists.append(w)
    return specialists


# Market-scanning categories (config/market_categories.yml, e.g. "iran",
# "elections") use a different, more granular taxonomy than the
# leaderboard's real category enum (OVERALL/SPORTS/ESPORTS/CRYPTO/
# POLITICS/CULTURE/WEATHER/ECONOMICS/TECH/FINANCE - confirmed against
# Polymarket's own API docs). Without this mapping, a market tagged
# "iran" would never match ANY wallet's leaderboard categories, since no
# such leaderboard category exists - it would just silently never find a
# specialist for anything outside the categories that happen to share a
# name. Categories not listed here (e.g. "breaking_news") have no
# reasonable leaderboard equivalent and are left unmapped on purpose,
# rather than force-mapped to something misleading.
_MARKET_CATEGORY_TO_LEADERBOARD_CATEGORY = {
    "politics": "POLITICS", "elections": "POLITICS", "geopolitics": "POLITICS",
    "iran": "POLITICS", "middle_east": "POLITICS",
    "crypto": "CRYPTO", "sports": "SPORTS", "esports": "ESPORTS",
    "tech": "TECH", "finance": "FINANCE", "culture": "CULTURE",
    "weather": "WEATHER", "economy": "ECONOMICS",
}
