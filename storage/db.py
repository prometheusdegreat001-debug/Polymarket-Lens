"""
SQLite storage for the full pipeline. Same engine as v1 (single file,
zero server setup - appropriate for a periodic-polling research tool),
extended schema to store every object in storage/schemas.py.

All records beyond the simplest ones are stored as JSON blobs in a
generic `records` table with a `record_type` discriminator - this avoids
hand-writing 9 separate CREATE TABLE statements that all need to evolve
in lockstep with schemas.py, at the cost of losing per-field SQL queries
(fine for a research tool; query by record_type +市 filter in Python).
Wallet candidates keep their own richer table since we query/sort on
specific columns (win_rate, copytrade score) often enough to want real columns.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="storage.db",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))

import sqlite3
import json
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from config.loader import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    events_scanned INTEGER,
    poly_markets_scanned INTEGER,
    kalshi_markets_scanned INTEGER
);

-- Generic JSON-blob store for MarketSnapshot, VerificationRecord,
-- HistoricalEventRecord, MispricingSignal, MarketIntelligenceReport,
-- DiscordAlertPayload, TradeFill, WalletFeatureVector.
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    record_type TEXT NOT NULL,
    market_id TEXT,
    wallet_address TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_type ON records(record_type);
CREATE INDEX IF NOT EXISTS idx_records_market ON records(market_id);

-- Verification cache: avoid re-paying for the same market's evidence
-- check every scan cycle.
CREATE TABLE IF NOT EXISTS verification_cache (
    market_id TEXT PRIMARY KEY,
    verification_json TEXT NOT NULL,
    cached_at TEXT NOT NULL
);

-- Trade-level dedup for the Strategic Trade Learning Layer's contrarian
-- big-win Discord alert. Previously this alert only fired when a wallet
-- was newly discovered (see upsert_wallet_candidate's is_new flag), which
-- meant that after a wallet's first scan, no contrarian win on it - not
-- even a brand new, bigger, more recent one - could ever trigger another
-- alert, since the wallet itself was no longer "new." That silently
-- starved the whole strategy webhook once the initial batch of wallets
-- had been seen once. This table keys dedup on the SPECIFIC WIN (wallet +
-- market/outcome + entry time), not the wallet, so a genuinely new
-- contrarian win alerts even on a wallet we've seen many times before.
CREATE TABLE IF NOT EXISTS contrarian_alerts_sent (
    wallet_address TEXT NOT NULL,
    win_key TEXT NOT NULL,
    realized_pnl REAL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (wallet_address, win_key)
);

-- Tracks the main wallet-intelligence Discord alert, SEPARATELY from
-- contrarian_alerts_sent above. CONFIRMED BUG this fixes: that alert was
-- previously gated on wallet_candidates' is_new flag (send_wallet_alert
-- only ever called "if is_new:"), meaning it fired EXACTLY ONCE per
-- wallet for the wallet's entire lifetime in the database - after the
-- first alert, no matter how many times the wallet re-qualified, how
-- much its score changed, or how good its current open positions looked,
-- it could never be alerted on again. This silently starved the wallet
-- intelligence channel down to zero new alerts once the first small
-- batch of wallets had been seen once, while the strategy-learning
-- channel (correctly keyed on the specific win, not the wallet) kept
-- firing normally for those same wallets - exactly the "strategy layer
-- works, wallet layer doesn't" symptom reported. Re-alerts now fire when
-- the recommendation changes, the score moves meaningfully, or enough
-- time has passed - see storage.db.should_send_wallet_alert.
CREATE TABLE IF NOT EXISTS wallet_alerts_sent (
    wallet_address TEXT PRIMARY KEY,
    last_recommendation TEXT,
    last_score INTEGER,
    last_alerted_at TEXT NOT NULL
);

-- Last-observed implied_probability per market, updated every scan cycle
-- a market appears in. This is what makes
-- historical_context/negotiation_progress_tracker.py's price_momentum
-- signal real instead of a permanent placeholder: features/market_features.py
-- only ever has a live Gamma SNAPSHOT (no history of its own - Gamma
-- doesn't return one), so without persisting the previous observation
-- here, there is nothing to diff against and momentum would silently be
-- 0.0 ("price stable") on every single call, forever, regardless of what
-- the market actually did.
CREATE TABLE IF NOT EXISTS market_price_observations (
    market_id TEXT PRIMARY KEY,
    implied_probability REAL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_candidates (
    wallet_address TEXT PRIMARY KEY,
    username TEXT,
    rank TEXT,
    pnl REAL,
    vol REAL,
    cross_period_confirmed INTEGER,
    leaderboard_source_categories TEXT,
    trade_count INTEGER,
    wallet_age_days REAL,
    pnl_per_trade REAL,
    wins INTEGER,
    losses INTEGER,
    resolved_count INTEGER,
    win_rate REAL,
    avg_win REAL,
    avg_loss REAL,
    total_realized_pnl REAL,
    trades_per_day REAL,
    distinct_events INTEGER,
    top_events TEXT,
    buy_ratio REAL,
    avg_trade_size_usd REAL,
    largest_trade_usd REAL,
    behavioral_pattern TEXT,
    open_positions_count INTEGER,
    open_exposure_usd REAL,
    behavior_label TEXT,
    copy_trade_score INTEGER,
    copy_trade_recommendation TEXT,
    why_copy_or_not TEXT,
    days_since_last_trade REAL,
    copy_trade_score_10 REAL,
    copy_trade_recommendation_label TEXT,
    biggest_win_usd REAL,
    biggest_loss_usd REAL,
    recent_14d_summary TEXT,
    sample_quality TEXT,
    trade_count_14d INTEGER,
    pnl_resolved_30d REAL,
    category_performance TEXT,
    is_system_contract INTEGER,
    system_contract_label TEXT,
    activity_pattern_label TEXT,
    avg_active_days_per_week REAL,
    is_bursty INTEGER,
    full_report_text TEXT,
    drift_result TEXT,
    contrarian_win TEXT,
    longshot_pattern TEXT,
    open_positions_detail TEXT,
    first_seen_run_id INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_run_id INTEGER,
    last_seen_at TEXT NOT NULL
);
"""

# Columns added after the original table definition - migrated in on
# startup so an existing database (e.g. from before this field existed)
# upgrades cleanly instead of crashing on a missing column.
_WALLET_MIGRATION_COLUMNS = [
    ("days_since_last_trade", "REAL"),
    ("copy_trade_score_10", "REAL"),
    ("copy_trade_recommendation_label", "TEXT"),
    ("biggest_win_usd", "REAL"),
    ("cross_period_confirmed", "INTEGER"),
    ("leaderboard_source_categories", "TEXT"),
    ("biggest_loss_usd", "REAL"),
    ("recent_14d_summary", "TEXT"),
    ("sample_quality", "TEXT"),
    ("trade_count_14d", "INTEGER"),
    ("pnl_resolved_30d", "REAL"),
    ("category_performance", "TEXT"),
    ("is_system_contract", "INTEGER"),
    ("system_contract_label", "TEXT"),
    ("activity_pattern_label", "TEXT"),
    ("avg_active_days_per_week", "REAL"),
    ("is_bursty", "INTEGER"),
    ("full_report_text", "TEXT"),
    ("drift_result", "TEXT"),
    ("contrarian_win", "TEXT"),
    ("longshot_pattern", "TEXT"),
    ("open_positions_detail", "TEXT"),
]


@contextmanager
def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_wallet_columns(conn)


def _migrate_wallet_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(wallet_candidates)")}
    for col_name, col_type in _WALLET_MIGRATION_COLUMNS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE wallet_candidates ADD COLUMN {col_name} {col_type}")


def start_run() -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO scan_runs (started_at) VALUES (?)", (_now(),))
        return cur.lastrowid


def finish_run(run_id: int, events_scanned: int, poly_count: int, kalshi_count: int):
    with get_conn() as conn:
        conn.execute(
            """UPDATE scan_runs SET finished_at=?, events_scanned=?, poly_markets_scanned=?,
               kalshi_markets_scanned=? WHERE id=?""",
            (_now(), events_scanned, poly_count, kalshi_count, run_id),
        )


def save_record(run_id, record_type: str, payload: dict, market_id: str = None, wallet_address: str = None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO records (run_id, record_type, market_id, wallet_address, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, record_type, market_id, wallet_address, json.dumps(payload), _now()),
        )


def get_cached_verification(market_id: str, max_age_hours: float):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT verification_json, cached_at FROM verification_cache WHERE market_id=?",
            (market_id,),
        ).fetchone()
    if not row:
        return None
    cached_at = datetime.fromisoformat(row["cached_at"])
    age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
    if age_hours > max_age_hours:
        return None
    return json.loads(row["verification_json"])


def set_cached_verification(market_id: str, verification: dict):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO verification_cache (market_id, verification_json, cached_at)
               VALUES (?, ?, ?)
               ON CONFLICT(market_id) DO UPDATE SET verification_json=excluded.verification_json,
                   cached_at=excluded.cached_at""",
            (market_id, json.dumps(verification), _now()),
        )


_WALLET_COLUMNS = [
    "wallet_address", "username", "rank", "pnl", "vol", "cross_period_confirmed",
    "leaderboard_source_categories", "trade_count", "wallet_age_days",
    "pnl_per_trade", "wins", "losses", "resolved_count", "win_rate", "avg_win", "avg_loss",
    "total_realized_pnl", "trades_per_day", "distinct_events", "top_events", "buy_ratio",
    "avg_trade_size_usd", "largest_trade_usd", "behavioral_pattern", "open_positions_count",
    "open_exposure_usd", "behavior_label", "copy_trade_score", "copy_trade_recommendation",
    "why_copy_or_not", "days_since_last_trade", "copy_trade_score_10",
    "copy_trade_recommendation_label", "biggest_win_usd", "biggest_loss_usd",
    "recent_14d_summary", "sample_quality", "trade_count_14d", "pnl_resolved_30d",
    "category_performance", "is_system_contract", "system_contract_label",
    "activity_pattern_label", "avg_active_days_per_week", "is_bursty", "full_report_text",
    "drift_result", "contrarian_win", "longshot_pattern", "open_positions_detail",
]


def upsert_wallet_candidate(run_id, wallet: dict) -> bool:
    """Insert or update; returns True if newly discovered (drives Discord dedup)."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT wallet_address FROM wallet_candidates WHERE wallet_address = ?",
            (wallet["wallet_address"],),
        ).fetchone()
        is_new = existing is None
        now = _now()

        values = {c: wallet.get(c) for c in _WALLET_COLUMNS}
        if "top_events" in values and isinstance(values["top_events"], list):
            values["top_events"] = json.dumps(values["top_events"])
        if "category_performance" in values and isinstance(values["category_performance"], dict):
            values["category_performance"] = json.dumps(values["category_performance"])
        if "drift_result" in values and isinstance(values["drift_result"], dict):
            values["drift_result"] = json.dumps(values["drift_result"])
        if "contrarian_win" in values and isinstance(values["contrarian_win"], dict):
            values["contrarian_win"] = json.dumps(values["contrarian_win"])
        if "longshot_pattern" in values and isinstance(values["longshot_pattern"], dict):
            values["longshot_pattern"] = json.dumps(values["longshot_pattern"])
        if "open_positions_detail" in values and isinstance(values["open_positions_detail"], list):
            values["open_positions_detail"] = json.dumps(values["open_positions_detail"])
        if "leaderboard_source_categories" in values and isinstance(values["leaderboard_source_categories"], list):
            values["leaderboard_source_categories"] = json.dumps(values["leaderboard_source_categories"])
        if "cross_period_confirmed" in values and isinstance(values["cross_period_confirmed"], bool):
            values["cross_period_confirmed"] = int(values["cross_period_confirmed"])

        if is_new:
            cols = _WALLET_COLUMNS + ["first_seen_run_id", "first_seen_at", "last_seen_run_id", "last_seen_at"]
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO wallet_candidates ({', '.join(cols)}) VALUES ({placeholders})",
                [values.get(c) for c in _WALLET_COLUMNS] + [run_id, now, run_id, now],
            )
        else:
            set_clause = ", ".join(f"{c}=?" for c in _WALLET_COLUMNS if c != "wallet_address")
            conn.execute(
                f"UPDATE wallet_candidates SET {set_clause}, last_seen_run_id=?, last_seen_at=? WHERE wallet_address=?",
                [values.get(c) for c in _WALLET_COLUMNS if c != "wallet_address"]
                + [run_id, now, wallet["wallet_address"]],
            )
        return is_new


def get_and_update_market_price(market_id: str, current_price):
    """
    Returns the PREVIOUSLY recorded implied_probability for market_id (or
    None if we've never seen this market before), then stores current_price
    as the new latest observation for next scan cycle. See
    market_price_observations' docstring above - this is what powers a
    real price_momentum signal instead of a permanent placeholder.
    """
    if market_id is None or current_price is None:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT implied_probability FROM market_price_observations WHERE market_id=?",
            (market_id,),
        ).fetchone()
        prior = row["implied_probability"] if row else None
        conn.execute(
            """INSERT INTO market_price_observations (market_id, implied_probability, observed_at)
               VALUES (?, ?, ?)
               ON CONFLICT(market_id) DO UPDATE SET
                   implied_probability=excluded.implied_probability,
                   observed_at=excluded.observed_at""",
            (market_id, current_price, _now()),
        )
    return prior


def should_send_wallet_alert(wallet_address: str, recommendation: str, score,
                              cooldown_hours: float = 24.0) -> bool:
    """
    Decides whether the MAIN wallet-intelligence alert should fire for
    this wallet right now. Fires when:
    - Never alerted on before, OR
    - The recommendation changed (e.g. watch -> copy, or copy -> avoid -
      this is exactly the kind of change someone needs to know about), OR
    - The score moved by 10+ points (a meaningful shift, not just noise
      from one extra trade), OR
    - It's been at least `cooldown_hours` since the last alert (so a
      genuinely stable "copy" wallet still resurfaces periodically
      instead of going silent forever after one alert - the bug this
      whole table exists to fix).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_recommendation, last_score, last_alerted_at FROM wallet_alerts_sent WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()

    if row is None:
        return True
    if row["last_recommendation"] != recommendation:
        return True
    if score is not None and row["last_score"] is not None and abs(score - row["last_score"]) >= 10:
        return True
    try:
        last_alerted = datetime.fromisoformat(row["last_alerted_at"])
        hours_since = (datetime.now(timezone.utc) - last_alerted).total_seconds() / 3600
    except (ValueError, TypeError):
        return True  # malformed timestamp - safer to re-alert than to stay silent
    return hours_since >= cooldown_hours


def record_wallet_alert_sent(wallet_address: str, recommendation: str, score):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO wallet_alerts_sent (wallet_address, last_recommendation, last_score, last_alerted_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(wallet_address) DO UPDATE SET
                   last_recommendation=excluded.last_recommendation,
                   last_score=excluded.last_score,
                   last_alerted_at=excluded.last_alerted_at""",
            (wallet_address, recommendation, score, _now()),
        )


def has_contrarian_alert_been_sent(wallet_address: str, win_key: str) -> bool:
    """True if THIS specific contrarian win (identified by win_key, not just
    the wallet) has already been alerted on the strategy learning webhook."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM contrarian_alerts_sent WHERE wallet_address=? AND win_key=?",
            (wallet_address, win_key),
        ).fetchone()
    return row is not None


def record_contrarian_alert_sent(wallet_address: str, win_key: str, realized_pnl: float = None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO contrarian_alerts_sent (wallet_address, win_key, realized_pnl, sent_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(wallet_address, win_key) DO NOTHING""",
            (wallet_address, win_key, realized_pnl, _now()),
        )


def _now():
    return datetime.now(timezone.utc).isoformat()
