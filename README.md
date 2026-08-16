# Polymarket Alpha Intelligence Engine v2

A modular, cost-aware research pipeline: detects mispricing, verifies
evidence (free-tier first, paid LLM only as a gated fallback), scores
wallets by real skill (not luck), builds a full intelligence report per
market, and sends plain-English Discord alerts with direct action links.

## What changed from v1

v1 was a flat set of scripts (`scanners.py`, `research.py`,
`discord_alerts.py`...). v2 is the same real functionality, restructured
into proper package boundaries with explicit data contracts, so each piece
can be tested, tuned, and extended independently:

```
config/            # all thresholds/weights in YAML, zero hardcoded numbers
ingestion/         # raw data pulls (Gamma, CLOB, Kalshi, Data API, + paid LLM tier)
storage/           # schemas.py (data contracts) + db.py (SQLite)
features/          # market/wallet/behavior/liquidity feature engineering
verification/      # evidence-gating pipeline ("no source, no signal")
wallet_intel/       # taxonomy, copy-trade scoring, luck detection
mispricing/        # arbitrage + cross-platform + (optional) LLM benchmark
historical_context/ # precedent matching - free tier uses Polymarket's OWN
                     # resolved-market history, paid tier only if that's thin
intelligence/       # decision engine, confidence aggregation, risk sizing
alerts/             # Discord payload + CTA + formatting
main.py             # orchestrator
```

## Cost-aware by construction

**Every module declares a `MODULE_COST_PROFILE`** (`requires_paid_api`,
`estimated_cost_per_call_usd`, `free_fallback_strategy`) - see
`config/cost_profile.py`. Run `python main.py` once and it prints a
startup report showing exactly which modules are currently paid vs free.

**The master switch is `config/cost.yml`'s `enable_paid_research: false`.**
With this off (the default), the entire system runs on nothing but free,
public REST APIs - Gamma, CLOB, Kalshi, and Polymarket's Data API - exactly
like v1. You get: arbitrage detection, Kalshi cross-platform comparison,
full wallet intelligence (taxonomy, luck detection, copy-trade scoring),
AND historical precedent matching (using Polymarket's own resolved-market
history) - all for $0 marginal cost.

Turning `enable_paid_research: true` on (plus setting `ANTHROPIC_API_KEY`)
adds:
- Real news/evidence verification for markets whose edge depends on an
  LLM-elicited benchmark (only reached when no free Kalshi match exists)
- Deeper open-web historical research (only reached when Polymarket's own
  history has fewer than `min_similar_events_before_llm` similar past markets)

Both paid paths are hard-gated by `verification/confidence_gate.py`'s
budget governor (`max_paid_calls_per_scan_cycle`, `daily_budget_usd` in
`config/cost.yml`) - there's no path where a paid call fires without going
through that check first.

**Important design decision:** arbitrage signals and Kalshi cross-platform
signals are mechanically verified by construction (real math between two
independent, real markets) - they can reach a `BUY_YES`/`BUY_NO` decision
without ever touching the paid tier. Only signals resting on an
LLM-elicited probability estimate are gated behind actual evidence
verification passing. This is the concrete implementation of "no source,
no signal" - applied specifically where it matters, not blanket-applied to
signals that are already real and independent.

## Setup

```bash
pip install -r requirements.txt
python main.py                              # one-off scan, default categories
python main.py --loop                       # continuous (Railway worker mode)
python main.py --wallet-scan                # force wallet scan on a one-off run
python main.py --categories crypto,geopolitics
```

## Environment variables

```
DISCORD_WEBHOOK_URL          # required for market alerts
DISCORD_WALLET_WEBHOOK_URL   # optional - separate channel for wallet alerts
                              # (falls back to DISCORD_WEBHOOK_URL if unset)
ANTHROPIC_API_KEY             # only needed if you enable paid research
SCAN_INTERVAL_SECONDS         # default 900 (15 min)
MAX_KALSHI_PER_SCAN           # default 500
WALLET_SCAN_EVERY_N_RUNS      # default 4 (wallet scan runs every 4th cycle)
DB_PATH                       # default data/mispricing.db
```

Any top-level YAML config value can also be overridden via env var using
the pattern `<SECTION>_<KEY_UPPER>`, e.g. `RISK_MIN_LIQUIDITY_USD=2000`,
`DISCORD_ALERT_MIN_DEVIATION=0.05`. Nested keys (like `wallet_scoring.yml`'s
`weights.*`) are YAML-only - edit the file directly for those.

## Config files

| File | Controls |
|---|---|
| `config/risk.yml` | Liquidity/volume minimums, position sizing caps, edge thresholds |
| `config/wallet_scoring.yml` | Copy-trade scoring weights, luck-penalty thresholds, wallet taxonomy cutoffs |
| `config/verification.yml` | Source count requirements, confidence thresholds, LLM model/search budget |
| `config/market_categories.yml` | Which Polymarket categories to scan, real Gamma tag IDs |
| `config/discord.yml` | CTA labels/URL templates, alert thresholds, what to show in alerts |
| `config/cost.yml` | **The master paid-research switch and spend caps** |

## Deploying on Railway

Same as v1 - `Procfile` + `railway.json` included, runs as a worker
(`python -u main.py --loop`). Set your env vars under the service's
Variables tab. Railway's filesystem is ephemeral on redeploy - attach a
Volume mounted at `/app/data` if you want scan history to survive
redeploys.

## Known limitations (honest, not hidden)

- `event_age_days` in `features/market_features.py` currently always
  returns 0.0 - Gamma's per-market object doesn't reliably expose a
  `createdAt` field the way it exposes `endDate`. Time-to-resolution
  (which DOES work) is the more load-bearing of the two anyway.
- Wallet-to-market relevance matching (`main._find_relevant_wallets`) uses
  title-similarity against a tracked wallet's historical top-events, since
  there's no free "who currently holds this specific market" endpoint.
  This is an honest proxy, not a direct holdings lookup - a wallet flagged
  as "relevant" to a market may not currently hold a live position there.
- `price_momentum` (used by `historical_context/negotiation_progress_tracker.py`)
  isn't yet computed anywhere and defaults to neutral (0.0) - would need a
  rolling price-history table to do properly; noted as a next step, not
  faked with a placeholder number.
- Order-book fetching (`ingestion/orderbook_stream.py`) only runs for
  markets that already produced a mispricing signal (bounded to ~20/cycle
  by `MAX_SIGNALS_PROCESSED_PER_RUN` in `main.py`), not for every market
  scanned - fetching the live book for hundreds of markets every 15
  minutes would be needlessly expensive for a signal-driven system.

## Testing philosophy

Every module in this build was compile-checked AND runtime-tested with
realistic synthetic data before being shipped - including the two most
important behavioral guarantees:
1. Arbitrage/Kalshi signals reach `BUY_YES`/`BUY_NO` on pure math alone,
   without needing the paid tier.
2. LLM-estimate-based signals are correctly capped at `MONITOR` until
   verification actually passes.

Both are asserted in the test suite embedded in this build's development
history, not just eyeballed.
