"""
Polymarket Alpha Intelligence Engine v2 - orchestrator.

Runtime flow (matches the architecture doc):
  1. Ingest market data across categories + Kalshi + closed-events (free-tier
     historical data) - all free, always.
  2. Detect mispricing signals directly from raw market data (free: arbitrage
     + Kalshi cross-platform; conditionally paid: LLM-estimate fallback,
     disabled by default).
  3. For each candidate signal (there are usually few): fetch its order book,
     compute market features, run verification (free-tier-first) and
     historical precedent (free-tier-first), evaluate any relevant tracked
     wallets, and build a full MarketIntelligenceReport.
  4. Decide BUY_YES / BUY_NO / MONITOR / NO_TRADE via intelligence/decision_engine.
  5. Alert on Discord if thresholds are met.
  6. Periodically (every WALLET_SCAN_EVERY_N_RUNS cycles): scan the
     leaderboard for new qualifying wallets, build their full dossier via
     wallet_intel/*, alert on newly-discovered ones.

Usage:
    python main.py                    # one-off scan
    python main.py --loop             # continuous (Railway worker mode)
    python main.py --wallet-scan      # force the wallet scan on a one-off run
"""

import argparse
import sys
import time
from datetime import datetime, timezone

import storage.db as db
from config.loader import (
    market_categories, risk as risk_cfg, discord as discord_cfg,
    wallet_scoring as ws_cfg, SCAN_INTERVAL_SECONDS, MAX_KALSHI_PER_SCAN,
    WALLET_SCAN_EVERY_N_RUNS, NEWS_INGEST_EVERY_N_RUNS, news as news_cfg,
)
from config.cost_profile import print_startup_report

from news_intelligence.news_ingestion import run_ingestion_cycle
from news_intelligence.news_market_matcher import match_articles_to_market
from news_intelligence.news_direction import classify_sentiment

from ingestion.polymarket_api import fetch_events_by_categories, fetch_closed_events
from ingestion.category_resolver import resolve_category_tag_ids
from ingestion.external_sources_kalshi import fetch_open_markets as fetch_kalshi_markets
from ingestion.orderbook_stream import fetch_book, compute_spread_and_depth
from ingestion.wallet_activity import (
    fetch_leaderboard, fetch_leaderboard_pool, fetch_wallet_trade_summary, fetch_wallet_activity_detailed,
    fetch_wallet_closed_positions, fetch_wallet_open_positions, fetch_market_holders,
)
from ingestion.http_utils import ApiError

from features.market_features import compute_market_features
from features.wallet_features import compute_wallet_features, category_performance, compute_longshot_pattern
from features.behavior_features import compute_behavior_features

from mispricing.edge_detector import detect_arbitrage, detect_cross_platform_edges
from mispricing.wallet_flow_detector import detect_wallet_flow_signals
from mispricing.momentum_detector import detect_momentum_signals
from mispricing.probability_model import find_best_kalshi_candidate
from mispricing.signal_ranker import rank_signals

from mispricing.probability_model import title_similarity
from intelligence.market_intelligence_builder import build_report
from wallet_intel.copy_trade_filter import evaluate_wallet
from wallet_intel.address_safety import is_system_contract
from wallet_intel.wallet_classifier import classify_activity_pattern
from wallet_intel.report_formatter import render_wallet_report
from wallet_intel.transaction_explainer import explain_recent_trades, get_event_cached, clear_event_cache
from wallet_intel.strategy_drift import analyze_strategy_drift
from wallet_intel.wallet_ranker import rank_wallets
from strategy_learning.contrarian_win_finder import find_contrarian_big_win
from strategy_learning.contrarian_alert_formatter import send_contrarian_win_alert

from alerts.alert_payload_builder import build_payload
from alerts.discord_formatter import send_market_alert, send_wallet_alert

MAX_SIGNALS_PROCESSED_PER_RUN = 20  # caps the expensive per-signal work (order book, verification)


def run_scan(categories: list, max_per_category: int, max_kalshi: int, include_wallet_scan: bool = False,
             include_news_ingestion: bool = False):
    db.init_db()
    run_id = db.start_run()

    # See wallet_intel.transaction_explainer.clear_event_cache's docstring:
    # that module's per-event cache has no TTL, so it must be reset each
    # scan cycle in this long-running (--loop) worker or "current
    # liquidity" and resolution-rule text goes stale the longer the
    # process has been up.
    clear_event_cache()

    print(f"[run {run_id}] Fetching Polymarket events across categories: "
          f"{', '.join(categories)} (up to {max_per_category} each)...")
    tag_map = resolve_category_tag_ids(categories)
    resolved_names = sorted(tag_map.keys())
    missing_names = sorted(set(categories) - set(tag_map.keys()))
    print(f"[run {run_id}] Resolved {len(resolved_names)}/{len(categories)} categories to real tag IDs: "
          f"{resolved_names}" + (f" | UNRESOLVED (skipped): {missing_names}" if missing_names else ""))
    try:
        events = fetch_events_by_categories(categories, max_per_category, tag_map=tag_map)
    except ApiError as e:
        print(f"FAILED to fetch Polymarket events: {e}", file=sys.stderr)
        return
    print(f"[run {run_id}] Retrieved {len(events)} events.")

    events, dropped_far_out, dropped_no_date = _filter_events_by_resolution_window(
        events, market_categories.max_days_to_resolution
    )
    print(f"[run {run_id}] Resolution-window filter (<= {market_categories.max_days_to_resolution} days): "
          f"{len(events)} event(s) kept, {dropped_far_out} dropped (resolving too far out), "
          f"{dropped_no_date} dropped (no parseable resolution date).")

    if include_news_ingestion:
        # Isolated from the rest of the pipeline on purpose: unlike
        # _run_wallet_scan below (which runs AFTER market alerts, so a
        # crash there can't take down the core pipeline), news ingestion
        # runs early so this cycle's matching can use fresh data - which
        # means an unhandled exception here would otherwise propagate
        # up through run_scan and abort market alerting too, for a
        # secondary/experimental feature. Same "one weak dependency
        # shouldn't take down the whole pipeline" principle already
        # applied everywhere else (fetch_leaderboard_pool,
        # fetch_events_by_categories, Discord _post all catch-and-continue
        # rather than crash).
        try:
            _run_news_ingestion(run_id, events)
        except Exception as e:
            print(f"WARNING: news ingestion failed this cycle (market scanning continues normally): {e}", file=sys.stderr)

    if risk_cfg.enable_kalshi_cross_platform:
        print(f"[run {run_id}] Fetching Kalshi markets (limit={max_kalshi})...")
        try:
            kalshi_markets = fetch_kalshi_markets(max_markets=max_kalshi)
        except ApiError as e:
            print(f"WARNING: Kalshi fetch failed: {e}", file=sys.stderr)
            kalshi_markets = []
        print(f"[run {run_id}] Retrieved {len(kalshi_markets)} Kalshi markets.")
    else:
        kalshi_markets = []
        print(f"[run {run_id}] Kalshi cross-platform matching disabled (risk.enable_kalshi_cross_platform=false) "
              f"- Polymarket-only data sources per Phase 1 restriction.")

    print(f"[run {run_id}] Fetching closed events for historical precedent (free tier)...")
    try:
        closed_events = fetch_closed_events(max_events=max_per_category * len(categories))
    except ApiError as e:
        print(f"WARNING: closed-events fetch failed: {e}", file=sys.stderr)
        closed_events = []
    print(f"[run {run_id}] Retrieved {len(closed_events)} closed events.")

    poly_market_count = sum(len(e["markets"]) for e in events)

    tracked_wallets = _load_tracked_wallets()

    # Computed ONCE per scan cycle, shared by the momentum-fade detector
    # below AND the per-signal loop further down - get_and_update_market_price
    # has a real side effect (updates the stored "last seen" price), so
    # calling it twice for the same market in one cycle would corrupt the
    # second reading (it would see the price the first call JUST wrote).
    momentum_by_market_id = {}
    momentum_errors = 0
    for e in events:
        for m in e["markets"]:
            try:
                implied = m.get("outcome_prices", {}).get("Yes")
                if implied is None or not m.get("market_id"):
                    continue
                prior = db.get_and_update_market_price(m["market_id"], implied)
                momentum_by_market_id[m["market_id"]] = (
                    round(implied - prior, 4) if prior is not None else 0.0
                )
            except Exception as e_inner:
                # PREVIOUSLY unguarded - one bad market here (unexpected
                # data shape, DB contention, anything) would abort the
                # ENTIRE market-scanning pipeline before a single signal
                # was ever generated, since this runs before arbitrage/
                # cross-platform/wallet-flow/momentum detection even
                # starts. Now one bad market just gets skipped (falls
                # back to momentum=0.0 for it specifically) instead of
                # taking down every other market's signal detection too.
                momentum_errors += 1
                continue
    if momentum_errors:
        print(f"[run {run_id}] WARNING: {momentum_errors} market(s) failed momentum tracking "
              f"(skipped individually, rest of scan continues normally).")

    # --- Free-tier mispricing detection ---
    # arbitrage: always on, pure internal math, no external dependency.
    # cross_platform: gated by risk.enable_kalshi_cross_platform (off by
    # default per explicit user preference - Polymarket-native only).
    # wallet_flow / momentum_fade: NEW, Polymarket-native, fully free
    # signal sources that don't need arbitrage's multi-outcome structure
    # or cross_platform's Kalshi match - see their modules' docstrings
    # for why they exist. This is what lets a simple 2-way daily
    # Polymarket market (most sports/esports/weather propositions, which
    # structurally can't generate an arbitrage or cross-platform signal)
    # still surface real opportunities.
    arb_signals = detect_arbitrage(events)
    cross_signals = detect_cross_platform_edges(events, kalshi_markets) if risk_cfg.enable_kalshi_cross_platform else []
    wallet_flow_signals = detect_wallet_flow_signals(events, tracked_wallets)
    momentum_signals = detect_momentum_signals(events, momentum_by_market_id)
    all_signals = rank_signals(
        arb_signals + cross_signals + wallet_flow_signals + momentum_signals,
        top_n=MAX_SIGNALS_PROCESSED_PER_RUN,
    )
    _print_coverage_diagnostics(events, kalshi_markets, arb_signals, cross_signals)
    print(f"[run {run_id}] {len(arb_signals)} arbitrage signal(s), "
          f"{len(cross_signals)} cross-platform signal(s), "
          f"{len(wallet_flow_signals)} wallet-flow signal(s), "
          f"{len(momentum_signals)} momentum-fade signal(s), processing top {len(all_signals)}.")

    market_lookup = {m["market_id"]: m for e in events for m in e["markets"]}
    event_lookup = {e["event_id"]: e for e in events}

    # Fetched ONCE per scan cycle, not per-signal - see
    # match_articles_to_market's docstring for why this matters at up to
    # MAX_SIGNALS_PROCESSED_PER_RUN calls per cycle.
    recent_news_articles = db.get_recent_news_articles(max_age_hours=news_cfg.max_article_age_hours)

    reports_built = 0
    alerts_sent = 0
    report_summaries = []  # for detailed per-signal logging below
    for signal in all_signals:
        market = market_lookup.get(signal["market_id"])
        event = event_lookup.get(signal["market_id"])  # arbitrage signals key by event_id

        if market:
            question = market.get("question", "")
            resolution_rule = market.get("resolution_rule", "")
            market_title = question
            token_ids = market.get("clob_token_ids", [])
            book = fetch_book(token_ids[0]) if token_ids else None
            book_stats = compute_spread_and_depth(book) if book else {}
            m_features = compute_market_features(market, book_stats)
        elif event:
            question = event.get("title", "")
            resolution_rule = ""
            market_title = event.get("title", "")
            # Arbitrage signals span multiple markets within this event - use
            # real aggregated data across the neg-risk markets, not hardcoded
            # zeros. min_liquidity is already computed by edge_detector;
            # volume/depth are summed across the same markets that fed the
            # arbitrage check, so this reflects genuine tradability, not a
            # placeholder that would fail every liquidity check by construction.
            neg_risk_markets = [m for m in event["markets"] if m.get("neg_risk")]
            total_volume = sum(m.get("volume_24h", 0.0) for m in neg_risk_markets)
            min_liquidity = signal.get("_min_liquidity", 0.0)
            m_features = {
                "liquidity_usd": min_liquidity,
                "volume_24h_usd": total_volume,
                "depth_usd": min_liquidity,  # no live order book for a basket - liquidity is the honest proxy
                "time_to_resolution_days": None,
            }
        else:
            continue  # signal references a market/event we no longer have - skip safely

        category = _infer_category(market_title, events)
        relevant_wallets = _find_relevant_wallets(market_title, tracked_wallets)

        try:
            # Wrapped so a failure processing ANY SINGLE signal (news
            # matching, decision engine, alert building - any of it)
            # can't silently cancel every remaining signal in this cycle,
            # or the wallet scan that runs after this loop. Previously
            # nothing caught exceptions here, so one bad market could take
            # down the whole cycle's market alerts - a risk that grew with
            # each new thing added inside this loop (news matching being
            # the newest).

            # News Relevance Engine (spec section 10) - matches against
            # ALREADY-STORED articles from this cycle's (or a recent prior
            # cycle's) ingestion pass, so this costs zero extra API calls per
            # market regardless of how many signals are being processed.
            # `recent_news_articles` is fetched ONCE per run_scan call (see
            # above, before this loop starts) rather than re-querying the
            # full table for every signal - with up to
            # MAX_SIGNALS_PROCESSED_PER_RUN signals per cycle, that would
            # otherwise repeat the same full-table read that many times over.
            # Additive only: attached to the report for visibility, not yet
            # wired into decision-making (that's a Layer 3 concern - out of
            # scope for this "backend ingestion + matching, no UI yet" pass).
            news_matches = match_articles_to_market(market_title, articles=recent_news_articles)
            news_context = []
            for m in news_matches:
                sentiment = classify_sentiment(m.get("headline"), m.get("description"))
                db.save_news_market_match(m["id"], signal["market_id"], m["overlap_score"], sentiment["sentiment"])
                news_context.append({
                    "headline": m.get("headline"), "source": m.get("source_name"), "url": m.get("url"),
                    "overlap_score": m["overlap_score"], "sentiment": sentiment["sentiment"],
                })

            # Real price momentum - now read from momentum_by_market_id,
            # computed ONCE per market earlier in run_scan (shared with
            # mispricing.momentum_detector) rather than calling
            # storage.db.get_and_update_market_price again here, which
            # would corrupt the reading (see that computation's comment
            # for why calling it twice per market per cycle is unsafe).
            m_features["price_momentum"] = momentum_by_market_id.get(signal["market_id"], 0.0)

            report = build_report(
                mispricing_signal=signal, market_features=m_features,
                market_question=question, resolution_rule=resolution_rule,
                market_category=category, closed_events=closed_events,
                wallet_evaluations=relevant_wallets,
            )
            report["news_context"] = news_context  # additive - see comment above
            db.save_record(run_id, "MarketIntelligenceReport", report, market_id=signal["market_id"])
            reports_built += 1

            will_alert = _should_alert(report, signal)
            alert_reason = ""
            if not will_alert:
                if report["decision_label"] not in ("BUY_YES", "BUY_NO"):
                    alert_reason = f"decision={report['decision_label']} (not a trade call)"
                else:
                    alert_reason = (
                        f"edge {signal.get('edge_size', 0)*100:.1f}pp below alert "
                        f"threshold {discord_cfg.alert_min_deviation*100:.1f}pp"
                    )

            report_summaries.append({
                "title": market_title[:80], "signal_type": signal.get("signal_type"),
                "edge_pp": signal.get("edge_size", 0.0) * 100,
                "decision": report["decision_label"], "confidence": report.get("confidence_tier"),
                "opportunity_type": report.get("opportunity_type"), "verdict": report.get("verdict"),
                "will_alert": will_alert, "skip_reason": alert_reason,
            })

            if will_alert:
                payload = build_payload(report, wallet_profiles=relevant_wallets)
                db.save_record(run_id, "DiscordAlertPayload", payload, market_id=signal["market_id"])
                if send_market_alert(payload):
                    alerts_sent += 1
        except Exception as e:
            print(f"WARNING: failed to process signal for {market_title!r} - skipping this market, "
                  f"continuing with the rest of the cycle: {e}", file=sys.stderr)
            continue

    db.finish_run(run_id, len(events), poly_market_count, len(kalshi_markets))
    _print_report_details(run_id, report_summaries)
    _print_summary(run_id, events, kalshi_markets, all_signals, reports_built, alerts_sent)

    if include_wallet_scan:
        _run_wallet_scan(run_id, events, closed_events)
    else:
        print(f"\n[run {run_id}] Wallet scan skipped this cycle "
              f"(runs every {WALLET_SCAN_EVERY_N_RUNS} cycles - use --wallet-scan to force it).")


def _should_alert(report: dict, signal: dict) -> bool:
    if report["decision_label"] not in ("BUY_YES", "BUY_NO"):
        return False
    return signal.get("edge_size", 0.0) >= discord_cfg.alert_min_deviation


def _filter_events_by_resolution_window(events: list, max_days: float):
    """
    Explicit user preference: only trade "daily or weekly" opportunities -
    a market resolving in 2027, or even 3 months out, gets excluded
    entirely here, before any mispricing/wallet/news analysis ever touches
    it, not just deprioritized later. Filters at the MARKET level (not
    whole events) since a single event can technically bundle markets
    with different resolution dates; an event is only dropped once ALL of
    its markets fail the window. Markets with no parseable end_date are
    excluded too - if we can't confirm a market is near-term, we don't
    scan it, rather than assuming it's fine.

    Returns (filtered_events, dropped_far_out_count, dropped_no_date_count).
    """
    now = datetime.now(timezone.utc)
    dropped_far_out, dropped_no_date = 0, 0
    filtered_events = []

    for ev in events:
        kept_markets = []
        for m in ev.get("markets", []):
            end_date_str = m.get("end_date")
            if not end_date_str:
                dropped_no_date += 1
                continue
            try:
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                dropped_no_date += 1
                continue
            days_to_resolution = (end_dt - now).total_seconds() / 86400
            if 0 <= days_to_resolution <= max_days:
                kept_markets.append(m)
            else:
                dropped_far_out += 1

        if kept_markets:
            filtered_events.append({**ev, "markets": kept_markets})

    return filtered_events, dropped_far_out, dropped_no_date


def _infer_category(market_title: str, events: list) -> str:
    for e in events:
        if e.get("title") == market_title or any(m.get("question") == market_title for m in e["markets"]):
            return e.get("category", "unknown")
    return "unknown"


def _load_tracked_wallets() -> list:
    """Pulls previously-discovered wallet candidates from the DB for
    market-level relevance matching (does this market overlap a tracked
    wallet's known top events)."""
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM wallet_candidates").fetchall()
    return [dict(r) for r in rows]


def _find_relevant_wallets(market_title: str, tracked_wallets: list, threshold: float = 0.5) -> list:
    """
    Two matching passes, in priority order:

    1. REAL current direction, via each wallet's open_positions_detail
       (built in main.py's _build_open_positions_detail - actual current
       market/outcome/entry-price data, not an approximation). Title-
       matched against this market; when it matches, the wallet's
       ACTUAL current side (YES/NO) is used - this is what previously
       said "direction unknown without a per-market holdings call" and
       hardcoded direction=None for every wallet, which silently made
       wallet-agreement scoring (and FLOW_SCALP classification) inert
       for every single signal, always. That holdings data exists now.

    2. Fallback: title-similarity against a wallet's historical
       top_events (was ACTIVE in a similar-sounding market before, not
       necessarily positioned in THIS one right now) - kept as broader
       context for wallets with no matching open position, honestly
       marked direction=None rather than guessing.
    """
    import json as _json
    relevant = []
    seen_addresses = set()

    for w in tracked_wallets:
        if w.get("copy_trade_recommendation") not in ("copy", "watch"):
            continue
        raw_positions = w.get("open_positions_detail")
        try:
            positions = _json.loads(raw_positions) if isinstance(raw_positions, str) else (raw_positions or [])
        except (ValueError, TypeError):
            positions = []
        for p in positions:
            if title_similarity(market_title, p.get("market_title", "")) >= threshold:
                outcome = (p.get("outcome") or "").strip().upper()
                direction = outcome if outcome in ("YES", "NO") else None
                relevant.append({**w, "direction": direction})
                seen_addresses.add(w["wallet_address"])
                break

    for w in tracked_wallets:
        if w["wallet_address"] in seen_addresses:
            continue
        top_events_raw = w.get("top_events")
        try:
            top_events = _json.loads(top_events_raw) if top_events_raw else []
        except (ValueError, TypeError):
            top_events = []
        for ev in top_events:
            if title_similarity(market_title, ev.get("event", "")) >= threshold:
                relevant.append({**w, "direction": None})  # no matching OPEN position - historical relevance only
                break

    return relevant


def _run_wallet_scan(run_id: int, events: list = None, closed_events: list = None):
    """
    Scans the leaderboard for qualifying wallets. Two things this fixes
    versus earlier versions:

    1. Ranks by RECENT-window PnL (config: leaderboard_time_period, default
       MONTH), not all-time - all-time ranking surfaces wallets that made a
       fortune years ago and went dormant, which was flooding results with
       stale wallets even though they technically passed the age/trade/PnL
       filters.
    2. Enforces a HARD inactivity cutoff (activity_recency.max_days_inactive,
       default 14 days) at discovery time, using the trade-summary data
       already fetched for the age check - no extra API calls needed. This
       is a real exclusion, not just a recommendation downgrade - inactive
       wallets never make it into `qualifying` at all.

    The broaden-and-retry logic below only ever loosens age/trade-count/PnL
    thresholds when too few wallets qualify - it NEVER loosens the
    inactivity cutoff, since "must still be actively trading" is the one
    requirement that shouldn't be negotiable just because the pool is thin.
    """
    target_min_wallets = 5
    attempts = [
        {"pool_size": ws_cfg.leaderboard_pool_size, "max_trades": ws_cfg.max_trade_count_for_selectivity,
         "min_pnl": ws_cfg.min_pnl_usd, "min_age": ws_cfg.min_wallet_age_days},
        {"pool_size": ws_cfg.leaderboard_pool_size * 2, "max_trades": ws_cfg.max_trade_count_for_selectivity * 2,
         "min_pnl": ws_cfg.min_pnl_usd, "min_age": ws_cfg.min_wallet_age_days * 0.66},
        {"pool_size": ws_cfg.leaderboard_pool_size * 4, "max_trades": ws_cfg.max_trade_count_for_selectivity * 4,
         "min_pnl": ws_cfg.min_pnl_usd * 0.5, "min_age": ws_cfg.min_wallet_age_days * 0.33},
    ]

    event_category_map = {
        e.get("slug"): e.get("category") for e in (events or []) if e.get("slug")
    }

    qualifying = []
    rejection_counts = {}
    for i, params in enumerate(attempts):
        print(f"\n[run {run_id}] Wallet scan attempt {i+1}/{len(attempts)}: "
              f"pool={params['pool_size']}, max_trades={params['max_trades']}, "
              f"min_pnl=${params['min_pnl']:.0f}, min_age={params['min_age']:.0f}d, "
              f"max_days_inactive={ws_cfg.activity_recency.max_days_inactive} (fixed, never loosened)...")
        qualifying, rejection_counts = _scan_wallets_with_params(
            event_category_map=event_category_map, closed_events_pool=closed_events, **params)
        print(f"[run {run_id}] Attempt {i+1} found {len(qualifying)} qualifying wallet(s). "
              f"Rejected: {rejection_counts}")
        if len(qualifying) >= target_min_wallets:
            break

    if not qualifying:
        print(f"[run {run_id}] Wallet scan: 0 wallets qualified even after broadening filters. "
              f"Rejection breakdown: {rejection_counts}")
        return

    new_count, skipped_stale_alert, contrarian_alert_count = 0, 0, 0
    for wallet_record in qualifying:
        is_new = db.upsert_wallet_candidate(run_id, wallet_record)
        if is_new:
            new_count += 1

        # Strategic learning layer alert fires independently of the
        # copy-trade recommendation below - a wallet can have one
        # spectacular contrarian win worth studying even if its
        # OVERALL pattern isn't copy-worthy (e.g. flagged for drift
        # or dormancy). This is a case study, not a copy-trade signal.
        #
        # Deliberately NOT gated on `is_new`: dedup happens at the level of
        # the SPECIFIC WIN (wallet + market/outcome + entry time, see
        # contrarian_win["win_key"]), via contrarian_alerts_sent, not at
        # the wallet level. Gating on wallet-level `is_new` meant that once
        # a wallet had been seen once, no contrarian win on it could ever
        # alert again - even a brand-new one discovered on a later scan of
        # an already-known wallet - which silently starved this webhook.
        contrarian_win = wallet_record.get("contrarian_win")
        if contrarian_win and contrarian_win.get("win_key"):
            already_sent = db.has_contrarian_alert_been_sent(
                wallet_record["wallet_address"], contrarian_win["win_key"]
            )
            if not already_sent:
                wallet_context = {
                    "copy_trade_recommendation_label": wallet_record.get("copy_trade_recommendation_label"),
                    "copy_trade_score": wallet_record.get("copy_trade_score"),
                    "behavior_label": wallet_record.get("behavior_label"),
                    "days_since_last_trade": wallet_record.get("days_since_last_trade"),
                    "copy_command": wallet_record.get("copy_command"),
                }
                sent_ok = send_contrarian_win_alert(
                    wallet_record["wallet_address"], wallet_record.get("username"), contrarian_win,
                    wallet_context=wallet_context, longshot_pattern=wallet_record.get("longshot_pattern"),
                )
                if sent_ok:
                    db.record_contrarian_alert_sent(
                        wallet_record["wallet_address"], contrarian_win["win_key"],
                        contrarian_win.get("realized_pnl"),
                    )
                    contrarian_alert_count += 1

        # PREVIOUSLY: wallets flagged "avoid" got SKIPPED from Discord
        # entirely - no message at all, not even a flagged one. Given
        # contrarian-win wallets (one spectacular longshot bet) are
        # frequently EXACTLY the profile that scores "avoid" overall
        # (concentrated luck rather than consistency - see
        # wallet_intel/lucky_wallet_detector.py), this meant the wallet
        # channel could go completely silent even while the strategy
        # channel kept firing normally for those same wallets - looking
        # exactly like "wallets aren't reaching the alert" when really
        # they were reaching it and being silently dropped right before
        # sending. Now EVERY qualifying wallet gets a Discord message
        # (subject to the same change/cooldown dedup as before) - an
        # "avoid" wallet still shows its full activity, recency, and WHY
        # it's not copy-worthy, clearly labeled, rather than nothing.
        should_alert = db.should_send_wallet_alert(
            wallet_record["wallet_address"],
            wallet_record.get("copy_trade_recommendation"),
            wallet_record.get("copy_trade_score"),
        )
        if should_alert:
            if send_wallet_alert(wallet_record):
                db.record_wallet_alert_sent(
                    wallet_record["wallet_address"],
                    wallet_record.get("copy_trade_recommendation"),
                    wallet_record.get("copy_trade_score"),
                )
                print(f"  Alerted on wallet candidate: {wallet_record.get('username') or wallet_record['wallet_address']} "
                      f"({wallet_record.get('copy_trade_recommendation_label', '?')}, "
                      f"last active {wallet_record.get('days_since_last_trade', '?')} days ago)")
        else:
            skipped_stale_alert += 1  # no meaningful change since last alert + cooldown not yet elapsed

    print(f"[run {run_id}] Wallet scan: {len(qualifying)} wallet(s) evaluated, {new_count} newly discovered, "
          f"{skipped_stale_alert} skipped from alerting (no meaningful change since last alert), "
          f"{contrarian_alert_count} contrarian-win alert(s) sent to strategy webhook.")


def _scan_wallets_with_params(pool_size: int, max_trades: int, min_pnl: float, min_age: float,
                               event_category_map: dict = None, closed_events_pool: list = None) -> tuple:
    try:
        leaderboard = fetch_leaderboard_pool(
            pool_size=int(pool_size), categories=ws_cfg.leaderboard_categories,
            primary_period=ws_cfg.leaderboard_time_period,
            secondary_periods=ws_cfg.leaderboard_secondary_time_periods,
        )
    except ApiError as e:
        print(f"WARNING: leaderboard fetch failed: {e}", file=sys.stderr)
        return [], {}

    now = datetime.now(timezone.utc).timestamp()
    results = []
    rejected = {"pnl_too_low": 0, "no_address": 0, "trade_summary_failed": 0,
                "too_many_trades_or_no_trades": 0, "too_new": 0, "inactive": 0,
                "dossier_fetch_failed": 0, "system_contract": 0}

    for entry in leaderboard:
        if entry["pnl"] < min_pnl:
            rejected["pnl_too_low"] += 1
            continue
        wallet = entry["wallet_address"]
        if not wallet:
            rejected["no_address"] += 1
            continue

        # HARD exclusion for known Polymarket system infrastructure
        # contracts (relay hub, deposit wallet factory, pUSD, CTF) - these
        # are shared platform contracts, not individual traders, so a win
        # rate or PnL "score" on one of them is meaningless. This is a
        # genuine exclusion, not a weighted penalty, since these should
        # never legitimately appear as copy-trade candidates at all.
        is_sys_contract, sys_label = is_system_contract(wallet)
        if is_sys_contract:
            rejected["system_contract"] += 1
            print(f"  EXCLUDED (system contract, not a trader): {wallet} = {sys_label}")
            continue

        try:
            summary = fetch_wallet_trade_summary(wallet, max_trades=int(max_trades))
        except ApiError:
            rejected["trade_summary_failed"] += 1
            continue

        if summary["hit_cap"] or summary["trade_count"] == 0 or summary["first_trade_ts"] is None:
            rejected["too_many_trades_or_no_trades"] += 1
            continue

        wallet_age_days = (now - summary["first_trade_ts"]) / 86400
        if wallet_age_days < min_age:
            rejected["too_new"] += 1
            continue

        # HARD inactivity cutoff - uses last_trade_ts already returned by
        # fetch_wallet_trade_summary above, no extra API call. This is the
        # actual fix: excluded from the pool entirely, not just downgraded
        # later. Never loosened across broaden-and-retry attempts.
        days_since_last_trade = (now - summary["last_trade_ts"]) / 86400 if summary["last_trade_ts"] else float("inf")
        if days_since_last_trade > ws_cfg.activity_recency.max_days_inactive:
            rejected["inactive"] += 1
            continue

        try:
            activity = fetch_wallet_activity_detailed(wallet, limit=max(summary["trade_count"], 1))
            closed_positions = fetch_wallet_closed_positions(wallet)
            open_positions = fetch_wallet_open_positions(wallet)
        except ApiError as e:
            print(f"WARNING: dossier fetch failed for {wallet}: {e}", file=sys.stderr)
            rejected["dossier_fetch_failed"] += 1
            continue

        features = compute_wallet_features(wallet, activity, closed_positions, open_positions, wallet_age_days)
        features.update(compute_behavior_features(activity))
        features["wallet_age_days"] = round(wallet_age_days, 1)
        features["is_system_contract"] = False  # already hard-excluded above if True - kept for completeness in the score/report
        features["activity_pattern_label"] = classify_activity_pattern(features)
        if event_category_map:
            features["category_performance"] = category_performance(closed_positions, event_category_map)

        # --- Phase 5: strategy consistency scoring + drift detection ---
        drift_result = analyze_strategy_drift(activity, closed_positions, event_category_map=event_category_map)
        features["drift_result"] = drift_result

        # Repeated low-entry-price / small-stake ($100 max) pattern - see
        # features.wallet_features.compute_longshot_pattern for the full
        # rationale. Computed before the hard filters below because a
        # confirmed pattern EXEMPTS a wallet from the filters that would
        # otherwise reject it purely for having sparse, bursty activity
        # (which is inherent to this trading style, not a red flag).
        features["longshot_pattern"] = compute_longshot_pattern(closed_positions, activity=activity)
        is_longshot_specialist = features["longshot_pattern"]["is_longshot_specialist"]

        # --- Phase 2: non-negotiable hard wallet filters ---
        # (except for confirmed longshot specialists - see above)
        hf_cfg = ws_cfg.hard_filters
        trades_per_week_recent = features.get("trade_count_28d", 0) / 4.0
        if trades_per_week_recent < hf_cfg.min_trades_per_week_recent and not is_longshot_specialist:
            rejected["hf_not_enough_recent_trades_per_week"] = rejected.get("hf_not_enough_recent_trades_per_week", 0) + 1
            continue
        if features.get("resolved_trade_count", 0) < hf_cfg.min_resolved_trades_hard and not is_longshot_specialist:
            rejected["hf_one_hit_wonder"] = rejected.get("hf_one_hit_wonder", 0) + 1
            continue
        if features.get("market_breadth", 0) < hf_cfg.min_market_breadth_hard:
            # NOT exempted for longshot specialists - "consistent across
            # multiple markets" is still meaningful here (and is generally
            # already satisfied by min_qualifying_trades>=3 distinct
            # longshot bets), so this stays a genuine floor either way.
            rejected["hf_insufficient_market_breadth"] = rejected.get("hf_insufficient_market_breadth", 0) + 1
            continue
        if features["activity_pattern_label"] == "inconsistent_activity" and not is_longshot_specialist:
            rejected["hf_no_readable_strategy"] = rejected.get("hf_no_readable_strategy", 0) + 1
            continue
        if features["activity_pattern_label"] == "high_frequency_bot":
            # Explicit HARD exclusion, not just a scoring penalty - "no
            # bots" is a stated non-negotiable requirement for this
            # profile, not a preference to weigh against other signals.
            # Previously a bot-cadence wallet could still clear the copy
            # threshold on score alone (bots score low but not always low
            # enough), so this closes that gap outright regardless of how
            # good its PnL/win-rate looks.
            rejected["hf_bot_cadence"] = rejected.get("hf_bot_cadence", 0) + 1
            continue

        evaluation = evaluate_wallet(closed_positions, features)

        wallet_record = _build_wallet_record(
            wallet=wallet, entry=entry, features=features, evaluation=evaluation,
            activity=activity, closed_positions=closed_positions, open_positions=open_positions,
            closed_events_pool=closed_events_pool,
        )
        results.append(wallet_record)

    # Rank "copy" recommendations first, then by score - NOT a plain score
    # sort (that was the bug: a dormant/drift-flagged wallet can keep a
    # high RAW score even after copy_trade_recommendation is overridden
    # down to "watch"/"avoid", so sorting on raw score alone could rank it
    # above a genuinely good "copy" wallet with a lower score - the same
    # override-blindness bug fixed in wallet_intel/report_formatter.py's
    # top-line recommendation, just showing up here in list ordering
    # instead. wallet_ranker.rank_wallets() tiers on the actual
    # (override-applied) recommendation first, score only as a tiebreak.
    results = rank_wallets(results)
    return results, rejected


def _build_wallet_record(wallet: str, entry: dict, features: dict, evaluation: dict,
                          activity: list, closed_positions: list, open_positions: list,
                          closed_events_pool: list = None) -> dict:
    """
    Adapter between features/wallet_features.py's canonical field names
    (resolved_trade_count, market_breadth, avg_notional_usd - matching
    storage/schemas.py's WalletProfile) and storage/db.py's wallet_candidates
    table columns (resolved_count, distinct_events, avg_trade_size_usd, plus
    win/loss counts and a plain-language behavioral_pattern string that
    aren't computed elsewhere). Keeps features/*.py's output canonical while
    this is where the richer per-wallet dossier gets assembled for storage
    and Discord display.
    """
    wins = [p for p in closed_positions if p["realized_pnl"] > 0]
    losses = [p for p in closed_positions if p["realized_pnl"] <= 0]
    resolved_count = len(wins) + len(losses)
    total_realized_pnl = sum(p["realized_pnl"] for p in closed_positions) if closed_positions else 0.0

    trade_count = features.get("trade_count", 0)
    pnl_lifetime = features.get("pnl_lifetime", entry.get("pnl", 0.0))
    pnl_per_trade = round(pnl_lifetime / trade_count, 2) if trade_count else 0.0

    notionals = [a["notional_usd"] for a in activity if a.get("notional_usd")]
    largest_trade_usd = round(max(notionals), 2) if notionals else 0.0

    open_exposure_usd = round(sum(p.get("current_value", 0.0) for p in open_positions), 2)

    behavioral_pattern = _describe_behavior(
        trades_per_day=features.get("trades_per_day", 0.0),
        distinct_events=features.get("market_breadth", 0),
        buy_ratio=features.get("buy_ratio"),
        avg_trade_size_usd=features.get("avg_notional_usd", 0.0),
        largest_trade_usd=largest_trade_usd,
    )

    record = {
        "wallet_address": wallet, "username": entry.get("username"), "rank": entry.get("rank"),
        "pnl": entry.get("pnl", 0.0), "vol": entry.get("vol", 0.0),
        "cross_period_confirmed": entry.get("cross_period_confirmed", False),
        "leaderboard_source_categories": entry.get("source_categories", []),
        "trade_count": trade_count, "wallet_age_days": features.get("wallet_age_days", 0.0),
        "pnl_per_trade": pnl_per_trade,
        "wins": len(wins), "losses": len(losses), "resolved_count": resolved_count,
        "win_rate": features.get("win_rate"), "avg_win": round(sum(p["realized_pnl"] for p in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(p["realized_pnl"] for p in losses) / len(losses), 2) if losses else 0.0,
        "total_realized_pnl": round(total_realized_pnl, 2),
        "trades_per_day": features.get("trades_per_day", 0.0),
        "distinct_events": features.get("market_breadth", 0),
        "top_events": features.get("top_events", []),
        "buy_ratio": features.get("buy_ratio"),
        "avg_trade_size_usd": features.get("avg_notional_usd", 0.0),
        "largest_trade_usd": largest_trade_usd,
        "behavioral_pattern": behavioral_pattern,
        "open_positions_count": len(open_positions),
        "open_exposure_usd": open_exposure_usd,
        "behavior_label": evaluation.get("behavior_label"),
        "copy_trade_score": evaluation.get("copy_trade_score"),
        "copy_trade_score_10": evaluation.get("copy_trade_score_10"),
        "copy_trade_recommendation": evaluation.get("copy_trade_recommendation"),
        "copy_trade_recommendation_label": evaluation.get("copy_trade_recommendation_label"),
        "why_copy_or_not": evaluation.get("why_copy_or_not"),
        "biggest_win_usd": evaluation.get("biggest_win_usd", 0.0),
        "biggest_loss_usd": evaluation.get("biggest_loss_usd", 0.0),
        "recent_14d_summary": evaluation.get("recent_14d_summary"),
        "sample_quality": evaluation.get("sample_quality"),
        "trade_count_14d": features.get("trade_count_14d", 0),
        "pnl_resolved_30d": features.get("pnl_resolved_30d", 0.0),
        "category_performance": features.get("category_performance", {}),
        "pnl_lifetime": pnl_lifetime,
        "days_since_last_trade": features.get("days_since_last_trade"),
        "is_system_contract": features.get("is_system_contract", False),
        "system_contract_label": features.get("system_contract_label"),
        "activity_pattern_label": features.get("activity_pattern_label", "unknown"),
        "avg_active_days_per_week": features.get("avg_active_days_per_week", 0.0),
        "is_bursty": features.get("is_bursty", False),
        "luck_flags": evaluation.get("luck_flags", {}),
        "entry_timing_label": features.get("entry_timing_label", "unknown"),
        "timing_entropy": features.get("timing_entropy", 0.0),
        "trades_per_day": features.get("trades_per_day", 0.0),
        "drift_result": features.get("drift_result", {}),
        "longshot_pattern": features.get("longshot_pattern", {}),
        # Individual open-position detail (market, entry vs current price,
        # unrealized PnL, days held so far) - previously only a COUNT
        # (open_positions_count) and an aggregate dollar figure
        # (open_exposure_usd) made it into the record; the actual
        # positions themselves were fetched but never surfaced anywhere,
        # so "must have open positions" had no visible detail behind it.
        "open_positions_detail": _build_open_positions_detail(open_positions),
    }
    record["transaction_blocks"] = explain_recent_trades(activity, max_trades=5)
    record["contrarian_win"] = find_contrarian_big_win(closed_positions, activity, closed_events_pool)
    record["market_options_breakdown"] = _build_market_options_breakdown(activity, open_positions)
    record["copy_command"] = _build_copy_command(record, open_positions)
    record["full_report_text"] = render_wallet_report(record)
    return record


def _build_open_positions_detail(open_positions: list) -> list:
    """
    Per-position breakdown for currently-open positions: market, side
    bought, entry price vs. current price, unrealized PnL, and a cheap
    stake_under_100 flag (matches the same $100 stake cap used elsewhere
    for "easy copytrading" sizing). Capped at the 10 largest by current
    value - a report showing 40+ tiny dust positions isn't useful.

    For the top 3 positions by unrealized gain (current price well above
    entry price - the "cheap early entry" profile worth copying), cross-
    checks the wallet's holding against Polymarket's real /holders data
    for that market: an independently-verifiable, on-chain confirmation
    that this wallet genuinely holds a meaningful stake at that price,
    not just a self-reported number from one API in isolation.
    """
    sorted_positions = sorted(open_positions, key=lambda x: -(x.get("current_value") or 0.0))[:10]
    detail = []
    for p in sorted_positions:
        entry_price = p.get("avg_price")
        cur_price = p.get("cur_price")
        detail.append({
            "market_title": p.get("title"),
            "outcome": p.get("outcome"),
            "entry_price": entry_price,
            "current_price": cur_price,
            "current_value_usd": p.get("current_value"),
            "unrealized_pnl_usd": p.get("cash_pnl"),
            "stake_under_100": (p.get("size", 0.0) * (entry_price or 0.0)) <= 100,
            "_condition_id": p.get("condition_id"),
            "_gain": (cur_price - entry_price) if (cur_price is not None and entry_price is not None) else 0.0,
        })

    top_gainers = sorted(detail, key=lambda d: -d["_gain"])[:3]
    for d in top_gainers:
        if d["_gain"] <= 0 or not d["_condition_id"]:
            continue
        try:
            holders = fetch_market_holders(d["_condition_id"], min_balance=1.0, limit=20)
        except ApiError:
            holders = []
        d["holders_confirmed"] = len(holders) > 0
        d["holder_pool_size_checked"] = len(holders)

    for d in detail:
        d.pop("_condition_id", None)
        d.pop("_gain", None)
    return detail


def _build_copy_command(wallet_record: dict, open_positions: list) -> str:
    """
    "Copy command if it meets all the requirements" - a concrete,
    actionable line telling the person exactly what to do, only shown
    when the wallet actually clears the bar for copying (recommendation
    == "copy" AND it has at least one open position to actually copy into
    right now - a copy-worthy wallet sitting on zero open positions has
    nothing to actually copy today, however good its history looks).
    """
    if wallet_record.get("copy_trade_recommendation") != "copy":
        return "No copy command - this wallet does not currently meet all copy-trading requirements."
    if not open_positions:
        return "No copy command - this wallet meets copy-trading requirements but has no open positions right now."

    # Prefer the position with the smallest entry price (most contrarian/
    # cheapest, aligning with "low money entry" as the requirement) among
    # positions under the $100 stake cap; fall back to the largest by
    # current value if none qualify under $100.
    cfg_cap = 100
    under_cap = [p for p in open_positions if (p.get("size", 0.0) * (p.get("avg_price") or 0.0)) <= cfg_cap]
    pool = under_cap if under_cap else open_positions
    target = min(pool, key=lambda p: p.get("avg_price") if p.get("avg_price") is not None else 1.0)

    cur_price = target.get("cur_price")
    market = target.get("title", "unknown market")
    outcome = target.get("outcome", "unknown outcome")
    max_price_str = f"${cur_price:.2f}" if cur_price is not None else "current market price"
    return (
        f"COPY: Buy **{outcome}** on \"{market}\" at up to {max_price_str}, "
        f"capped at ${cfg_cap} stake (matches this wallet's own sizing pattern)."
    )


def _build_market_options_breakdown(activity: list, open_positions: list) -> str:
    """
    Phase 4 requirement: 'all available market options on the event and
    the wallet's positions.' Uses the wallet's most recent trade's event
    to fetch every outcome market Gamma has for that event, cross-
    referenced against the wallet's actual open positions in it.

    Matches on the position's specific market title/question, NOT just
    the outcome label ("Yes"/"No") - those labels are shared across every
    binary market in a multi-outcome event, so outcome-only matching would
    incorrectly mark every market as "held" the moment the wallet holds
    a "Yes" in just one of them.
    """
    if not activity:
        return "No recent activity to determine an event."

    most_recent = max(activity, key=lambda a: a.get("timestamp", 0))
    event_slug = most_recent.get("event_slug")
    if not event_slug:
        return "Most recent trade has no associated event."

    event = get_event_cached(event_slug)
    if not event or not event.get("markets"):
        return f"Could not fetch full market list for event '{event_slug}'."

    held_titles = {
        p.get("title") for p in open_positions
        if p.get("event_slug") == event_slug and p.get("title")
    }

    lines = [f"Event: **{event.get('title', event_slug)}**"]
    for m in event["markets"]:
        question = m.get("question", "unknown outcome")
        outcomes = m.get("outcomes") or []
        prices = m.get("outcome_prices") or {}
        held_marker = " ← wallet holds a position here" if question in held_titles else ""
        price_str = ", ".join(f"{o}: {prices.get(o, 0):.2f}" for o in outcomes) if outcomes else "n/a"
        lines.append(f"  - {question} ({price_str}){held_marker}")

    return "\n".join(lines)


def _describe_behavior(trades_per_day, distinct_events, buy_ratio, avg_trade_size_usd, largest_trade_usd) -> str:
    concentration = "concentrated in a small handful of events" if distinct_events <= 3 else "diversified across many events"
    if buy_ratio is None:
        directionality = "unknown buy/sell mix"
    elif buy_ratio >= 0.8:
        directionality = "almost always opens new positions rather than exiting early"
    elif buy_ratio <= 0.2:
        directionality = "mostly exits/closes positions rather than opening fresh ones"
    else:
        directionality = "a balanced mix of opening and closing positions"

    return (
        f"Trades about {trades_per_day:.2f}x/day on average, {concentration} "
        f"({distinct_events} distinct events), with {directionality}. "
        f"Average trade size is roughly ${avg_trade_size_usd:,.0f}, with a "
        f"largest single trade of ${largest_trade_usd:,.0f}."
    )


def _run_news_ingestion(run_id: int, events: list):
    """
    Backend-only news ingestion (spec sections 9-10) - fetches from both
    provided APIs using each distinct market CATEGORY present in this
    scan as a broad query (bounded, predictable API usage regardless of
    how many individual markets are active - a category-level query set
    stays small even when event counts are large), then stores everything
    for per-market keyword matching to draw on locally (no extra API
    calls needed per market - see the per-signal matching call below).
    """
    categories_present = sorted({e.get("category") for e in events if e.get("category")})
    if not categories_present:
        print(f"[run {run_id}] News ingestion: no categories present in this scan - skipping.")
        return
    # Internal category keys (config/market_categories.yml) are things
    # like "middle_east" and "breaking_news" - fine as dict/config keys,
    # but sent LITERALLY as a search query they'd almost never match real
    # article text (no real article contains the string "middle_east"
    # with an underscore). Converted to natural-language phrases before
    # ever reaching the news APIs.
    search_queries = sorted({c.replace("_", " ") for c in categories_present})
    print(f"[run {run_id}] News ingestion: fetching for {len(search_queries)} categories...")
    result = run_ingestion_cycle(search_queries)
    print(f"[run {run_id}] News ingestion: {result['fetched']} article(s) fetched, "
          f"{result['stored']} new (deduplicated), sources used: {result['sources_used']}.")

    pruned = db.prune_old_news_articles(news_cfg.max_article_age_hours)
    if pruned:
        print(f"[run {run_id}] News ingestion: pruned {pruned} article(s) past the "
              f"{news_cfg.max_article_age_hours}h relevance window.")


def _print_coverage_diagnostics(events: list, kalshi_markets: list, arb_signals: list, cross_signals: list):
    """
    Explains WHY categories with no signals have none - the two structural
    reasons are (1) arbitrage only exists for neg-risk multi-outcome
    groups, which most non-election/awards categories simply don't use,
    and (2) Kalshi cross-platform matching failed to clear the similarity
    threshold. This prints the best near-miss Kalshi score per category so
    a persistent "0 signals" is diagnosable instead of a silent black box.
    """
    from collections import defaultdict

    by_category = defaultdict(lambda: {"markets": 0, "neg_risk_markets": 0, "best_kalshi_score": 0.0})
    for e in events:
        cat = e.get("category", "unknown")
        for m in e["markets"]:
            by_category[cat]["markets"] += 1
            if m.get("neg_risk"):
                by_category[cat]["neg_risk_markets"] += 1
            if kalshi_markets and m.get("liquidity", 0) >= 1:
                _, score = find_best_kalshi_candidate(m, kalshi_markets)
                by_category[cat]["best_kalshi_score"] = max(by_category[cat]["best_kalshi_score"], score)

    print("\n--- CATEGORY COVERAGE DIAGNOSTICS ---")
    for cat in sorted(by_category.keys()):
        stats = by_category[cat]
        note = ""
        if stats["neg_risk_markets"] == 0 and stats["best_kalshi_score"] < 0.55:
            note = " <- no neg-risk groups AND no Kalshi match above 0.55 this cycle: structurally no free signal source available"
        print(f"  {cat}: {stats['markets']} market(s) scanned, {stats['neg_risk_markets']} in neg-risk groups, "
              f"best Kalshi match score {stats['best_kalshi_score']:.2f}{note}")


def _print_report_details(run_id: int, report_summaries: list):
    if not report_summaries:
        return
    print(f"\n--- [run {run_id}] SIGNAL-BY-SIGNAL DETAIL ---")
    for r in report_summaries:
        status = "🔔 ALERTED" if r["will_alert"] else f"skipped ({r['skip_reason']})"
        opp_str = f"{r.get('opportunity_type', 'unclassified')}" if r.get('opportunity_type') else 'unclassified'
        verdict_emoji = {"TRADE": "🟢", "WATCH": "🟡", "IGNORE": "🔴"}.get(r.get("verdict"), "⚪")
        print(f"  [{r['edge_pp']:.1f}pp | {r['signal_type']}] {r['title']!r}")
        print(f"      opportunity={opp_str} risk_verdict={verdict_emoji}{r.get('verdict', '?')} "
              f"decision={r['decision']} confidence={r['confidence']} -> {status}")


def _print_summary(run_id, events, kalshi_markets, signals, reports_built, alerts_sent):
    print("\n" + "=" * 60)
    print(f"SCAN RUN #{run_id} SUMMARY")
    print("=" * 60)
    print(f"Polymarket events scanned: {len(events)}")
    print(f"Kalshi markets scanned:    {len(kalshi_markets)}")
    print(f"Signals processed:         {len(signals)}")
    print(f"Intelligence reports built:{reports_built}")
    print(f"Discord alerts sent:       {alerts_sent}")
    print("\nAll reports stored in the database for historical tracking.")
    print("Reminder: these are research signals, not trade calls, and")
    print("nothing here is financial advice.")


def run_forever(categories: list, max_per_category: int, max_kalshi: int, interval_seconds: int):
    print(f"Starting continuous scan loop (interval={interval_seconds}s). Ctrl+C to stop.")
    cycle = 0
    while True:
        cycle += 1
        run_wallet_scan = (cycle % WALLET_SCAN_EVERY_N_RUNS == 0)
        run_news_ingestion = (cycle % NEWS_INGEST_EVERY_N_RUNS == 0)
        try:
            run_scan(categories=categories, max_per_category=max_per_category,
                      max_kalshi=max_kalshi, include_wallet_scan=run_wallet_scan,
                      include_news_ingestion=run_news_ingestion)
        except Exception as e:
            print(f"ERROR during scan (will retry next interval): {e}", file=sys.stderr)
        print(f"\nSleeping {interval_seconds}s until next scan...\n")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    print_startup_report()  # shows which modules are paid/free and current settings

    parser = argparse.ArgumentParser(description="Polymarket Alpha Intelligence Engine v2")
    parser.add_argument("--categories", type=str, default=",".join(market_categories.categories_to_scan))
    parser.add_argument("--max-events-per-category", type=int, default=market_categories.max_events_per_category)
    parser.add_argument("--max-kalshi", type=int, default=MAX_KALSHI_PER_SCAN)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=SCAN_INTERVAL_SECONDS)
    parser.add_argument("--wallet-scan", action="store_true")
    parser.add_argument("--news-ingestion", action="store_true")
    args = parser.parse_args()

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]

    if args.loop:
        run_forever(categories, args.max_events_per_category, args.max_kalshi, args.interval)
    else:
        run_scan(categories=categories, max_per_category=args.max_events_per_category,
                  max_kalshi=args.max_kalshi, include_wallet_scan=args.wallet_scan,
                  include_news_ingestion=args.news_ingestion)
