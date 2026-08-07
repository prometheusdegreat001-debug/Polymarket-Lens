"""
Turns a MarketIntelligenceReport into a DiscordAlertPayload-shaped dict.
All plain-language, non-technical explanations live here - technical
detail (raw edge numbers, similarity scores) is available in the report
but summarized in plain English for the alert itself.
"""

from datetime import datetime, timezone

from alerts.cta_builder import build_ctas
from config.loader import discord as discord_cfg
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="alerts.alert_payload_builder",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure formatting over an already-built MarketIntelligenceReport.",
))

_DECISION_EMOJI = {"BUY_YES": "🟢", "BUY_NO": "🔴", "MONITOR": "🟡", "NO_TRADE": "⚪"}


def build_payload(report: dict, wallet_profiles: list) -> dict:
    mispricing = report.get("mispricing") or {}
    verification = report.get("verification") or {}
    historical = report.get("historical_context") or {}

    market_title = mispricing.get("_event_title") or mispricing.get("_question") or report["market_id"]
    emoji = _DECISION_EMOJI.get(report["decision_label"], "⚪")

    plain_explanation = _plain_explanation(mispricing, report)
    evidence_summary = _evidence_summary(verification)
    historical_summary = historical.get("precedent_summary", "No historical context available.")
    wallet_summary = _wallet_summary(wallet_profiles)
    main_risks, failure_conditions = _risks_and_failure(report, historical)

    decision_statement = _decision_statement(mispricing, report, market_title)

    # Phase 6: full multi-paragraph deep-dive, built for every BUY_YES/BUY_NO
    # decision. This is a NEW, additive field - it does NOT replace or
    # restructure any existing payload field, and (per the explicit
    # instruction not to touch Discord alert format/structure/delivery in
    # any way) is NOT wired into the live Discord embed. It's attached to
    # the payload/DB record so it's available for other use (a dashboard,
    # a query, a future decision) without altering what actually gets sent.
    deep_dive_explanation = (
        _build_deep_dive_explanation(mispricing, report, historical, wallet_profiles)
        if report["decision_label"] in ("BUY_YES", "BUY_NO") else None
    )

    ctas = build_ctas(
        market_url=report.get("market_url", ""),
        source_urls=verification.get("source_urls", []) or historical.get("source_urls", []),
        wallet_addresses=report.get("influential_wallets", []),
    )

    return {
        "title": f"{emoji} {market_title}",
        "market_url": report.get("market_url", ""),
        "decision_statement": decision_statement,
        "plain_explanation": plain_explanation,
        "evidence_summary": evidence_summary,
        "historical_summary": historical_summary,
        "wallet_summary": wallet_summary,
        "decision_label": report["decision_label"],
        "suggested_size_pct": report.get("suggested_size_pct_of_risk_budget", 0.0),
        "confidence": report.get("confidence_tier", "low"),
        "main_risks": main_risks,
        "failure_conditions": failure_conditions,
        "cta_buttons": ctas,
        "wallet_addresses": report.get("influential_wallets", []) if discord_cfg.show_wallet_addresses else [],
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "deep_dive_explanation": deep_dive_explanation,  # NOT sent to Discord embed - see note above
    }


def _decision_statement(mispricing: dict, report: dict, market_title: str) -> str:
    """
    Builds the explicit, contract-level decision line required for every
    alert - names the exact market, the exact side, and the exact price,
    rather than a bare "Buy YES"/"Buy NO".
    """
    label = report["decision_label"]
    signal_type = mispricing.get("signal_type", "")
    implied_prob = mispricing.get("implied_probability", 0.0)

    if label == "MONITOR":
        return f"Monitor only — \"{market_title}\": edge detected but not yet confirmed enough to act."
    if label == "NO_TRADE":
        return f"No trade — \"{market_title}\": {report.get('why_this_side', 'insufficient edge or evidence')}"

    side = "YES" if label == "BUY_YES" else "NO"

    if signal_type == "arbitrage":
        num_outcomes = mispricing.get("_num_outcomes", "several")
        outcome_sum = mispricing.get("implied_probability", 0.0)
        return (
            f"Buy YES on ALL {num_outcomes} outcomes in \"{market_title}\" "
            f"(combined cost {outcome_sum:.3f} per $1.00 guaranteed payout — "
            f"buy the full basket, not a single outcome)."
        )

    # Cross-platform / single binary market - name the exact contract
    return f"Buy {side} on \"{market_title}\" at {implied_prob:.2f} ({implied_prob*100:.0f}% implied probability)."


def _build_deep_dive_explanation(mispricing: dict, report: dict, historical: dict, wallet_profiles: list) -> str:
    """
    Phase 6 item 3: thorough multi-paragraph explanation for every advised
    buy, covering: fundamental thesis, current odds vs. estimated true
    probability, key catalysts, risks of being wrong, liquidity
    considerations, and why the side has positive expected value.

    Phase 6 item 2 (crypto/perp depth): note up front - Polymarket does
    NOT offer true perpetual futures or funding rates (that's a different
    product category entirely, e.g. dYdX/Hyperliquid). For crypto-category
    markets specifically, this adds real available depth instead of
    fabricating a "funding context" that doesn't exist on this platform:
    liquidity/depth detail, historical resolution accuracy (via the
    historical_context precedent data already on the report), and
    smart-wallet concentration (via wallets we've already tracked and
    scored as influential on this market).
    """
    direction = mispricing.get("direction", "HOLD")
    edge_pp = mispricing.get("edge_size", 0.0) * 100
    implied_prob = mispricing.get("implied_probability", 0.0)
    benchmark_prob = mispricing.get("benchmark_probability")
    signal_type = mispricing.get("signal_type", "")
    category = report.get("market_category", "unknown")

    # --- Fundamental thesis ---
    if signal_type == "arbitrage":
        thesis = (
            f"This is a pure internal-consistency arbitrage: the outcomes in this neg-risk group "
            f"sum to {implied_prob:.3f} rather than the 1.00 they mathematically should, given exactly "
            f"one outcome can resolve YES. This doesn't depend on any prediction about the real-world "
            f"event - it's a pricing inconsistency on Polymarket's own order books."
        )
    else:
        source = "Kalshi (an independent, real second market)" if mispricing.get("benchmark_source") == "kalshi" else "an AI-researched probability estimate"
        benchmark_str = f"{benchmark_prob:.2f}" if benchmark_prob is not None else "n/a"
        thesis = (
            f"This market's implied probability ({implied_prob:.2f}) diverges from {source}'s "
            f"estimate ({benchmark_str}) by {edge_pp:.1f} "
            f"percentage points, suggesting the {direction} side is mispriced relative to that benchmark."
        )

    # --- Current odds vs estimated true probability ---
    odds_paragraph = (
        f"Current market-implied probability: {implied_prob*100:.1f}%. "
        + (f"Benchmark/estimated true probability: {benchmark_prob*100:.1f}%. " if benchmark_prob is not None else "")
        + f"Gap: {edge_pp:.1f} percentage points in favor of {direction}."
    )

    # --- Key catalysts ---
    catalysts = "No specific news catalyst identified beyond the pricing signal itself."
    if report.get("verification", {}).get("status") == "PASS":
        catalysts = report["verification"].get("explanation", catalysts)

    neg_progress = report.get("negotiation_progress") or {}
    if neg_progress.get("progress_label"):
        days_left = neg_progress.get("days_remaining")
        days_str = f"{days_left:.1f} days" if days_left is not None else "an unknown timeframe"
        catalysts = (
            f"{catalysts} Resolution window: {neg_progress['progress_label']} ({days_str} remaining). "
            f"{neg_progress.get('momentum_signal', '')}"
        )

    # --- Risks of being wrong ---
    risks = ["The benchmark or internal-consistency assumption itself could be wrong."]
    if historical.get("resembles_failed_setup"):
        risks.append("This setup resembles past similar markets that did NOT resolve as hoped - real historical precedent working against this thesis.")
    if report.get("verification", {}).get("status") != "PASS":
        risks.append("This has not been independently news-verified - the edge could be closing for a real reason not yet reflected in our data.")

    # --- Liquidity considerations ---
    liquidity_usd = mispricing.get("_min_liquidity") or report.get("market_features", {}).get("liquidity_usd", 0)
    liquidity_para = (
        f"Minimum liquidity across the relevant leg(s): ${liquidity_usd:,.0f}. "
        f"Always check the live order book before sizing - a flagged deviation on a thin book can be "
        f"mostly slippage, not real captureable edge."
    )

    # --- Why positive expected value ---
    ev_para = (
        f"If the benchmark/internal-consistency check is correct, buying {direction} at the current "
        f"price captures the {edge_pp:.1f}pp gap as edge. Expected value is positive as long as the "
        f"true probability is closer to the benchmark than to the current market price - which is "
        f"exactly the bet this signal is making, not a guarantee."
    )

    sections = [
        f"**Fundamental thesis:** {thesis}",
        f"**Current odds vs. estimated true probability:** {odds_paragraph}",
        f"**Key catalysts:** {catalysts}",
        f"**Risks of being wrong:** {' '.join(risks)}",
        f"**Liquidity considerations:** {liquidity_para}",
        f"**Why this has positive expected value:** {ev_para}",
    ]

    if category == "crypto":
        sections.append(_build_crypto_deep_dive(report, historical, wallet_profiles))

    return "\n\n".join(sections)


def _build_crypto_deep_dive(report: dict, historical: dict, wallet_profiles: list) -> str:
    """
    Real available depth for crypto-category markets. Explicitly does NOT
    include "funding context" - Polymarket has no perpetual futures or
    funding rate mechanism; that concept doesn't apply here and isn't
    fabricated to look like it does.
    """
    similar_events = historical.get("similar_events", [])
    if similar_events:
        resolved_summary = ", ".join(
            f"\"{e.get('title')}\" → {e.get('resolved_outcome', 'unknown')}" for e in similar_events[:3]
        )
        resolution_accuracy_note = f"Similar past crypto markets on Polymarket resolved: {resolved_summary}."
    else:
        resolution_accuracy_note = "No sufficiently similar past crypto markets found on Polymarket for a resolution-accuracy comparison."

    smart_wallets_here = [
        w for w in wallet_profiles
        if w.get("copy_trade_recommendation") == "copy"
    ]
    concentration_note = (
        f"{len(smart_wallets_here)} wallet(s) we've independently scored as 'copy'-worthy are currently "
        f"tracked as active in this market."
        if smart_wallets_here else
        "No wallets we've independently scored as 'copy'-worthy are currently tracked as active in this market."
    )

    return (
        "**Crypto market deep-dive:** Note: Polymarket does not offer true perpetual futures or funding "
        "rates - that concept doesn't apply here and isn't fabricated to look like it does. "
        f"{resolution_accuracy_note} {concentration_note} "
        "For real on-chain flow (whale transfers, exchange in/outflows), cross-check a blockchain "
        "explorer directly - that data isn't currently ingested by this system."
    )


def _plain_explanation(mispricing: dict, report: dict) -> str:
    edge_pp = mispricing.get("edge_size", 0.0) * 100
    signal_type = mispricing.get("signal_type", "")
    direction = mispricing.get("direction", "HOLD")

    if signal_type == "arbitrage":
        outcome_sum = mispricing.get("implied_probability", 0.0)
        cheap_or_rich = "too CHEAP" if outcome_sum < 1.0 else "too EXPENSIVE"
        return (
            f"All the possible outcomes in this market together are priced "
            f"{edge_pp:.1f} cents {cheap_or_rich} relative to the $1.00 they "
            f"should add up to."
        )

    benchmark_source = mispricing.get("benchmark_source", "")
    source_label = "Kalshi (a real second market)" if benchmark_source == "kalshi" else "an AI-researched estimate"
    return (
        f"This market's price looks {edge_pp:.1f} cents off compared to {source_label}, "
        f"suggesting the {direction} side may be underpriced."
    )


def _evidence_summary(verification: dict) -> str:
    status = verification.get("status", "INSUFFICIENT_EVIDENCE")
    tier = verification.get("verification_tier")
    tier_label = {
        "free_rss": "✅ Verified via free RSS feeds (Reuters/AP/gov/AI-lab/tech news)",
        "paid_llm": "✅ Verified via AI-researched web search",
    }.get(tier, "")

    if status == "PASS":
        prefix = f"{tier_label}\n" if tier_label else ""
        return prefix + verification.get("explanation", "Evidence verified.")
    if status == "FAIL":
        return f"⚠️ Evidence check FAILED: {verification.get('explanation', '')}"
    return verification.get("explanation") or "Not enough evidence was found to verify this independently yet."


def _wallet_summary(wallet_profiles: list) -> str:
    if not wallet_profiles:
        return "No notable wallets currently tracked in this market."

    lines = []
    for w in wallet_profiles[:3]:
        name = w.get("username") or w.get("wallet_address", "")[:10] + "…"
        lines.append(
            f"{name}: {w.get('behavior_label', 'unknown')}, "
            f"copy-trade score {w.get('copy_trade_score', 0)}/100 "
            f"({w.get('copy_trade_recommendation', 'watch')})"
        )
    return "\n".join(lines)


def _risks_and_failure(report: dict, historical: dict) -> tuple:
    risks = ["Prediction markets can move suddenly on new information not yet reflected here."]
    if historical.get("resembles_failed_setup"):
        risks.append("This setup resembles past similar markets that did NOT resolve as hoped.")
    if report.get("verification", {}).get("status") != "PASS":
        risks.append("This has not been independently news-verified.")

    failure = report.get("invalidation_conditions", "Re-evaluate if the underlying edge closes.")
    return " ".join(risks), failure
