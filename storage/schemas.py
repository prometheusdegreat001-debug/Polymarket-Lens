"""
Versioned data contracts for the whole pipeline. Every object that moves
between modules is one of these - no ad-hoc dicts passed around once data
leaves ingestion. `schema_version` lets us evolve fields later without
silently breaking old stored records.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="storage.schemas",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure local computation over already-fetched data, no external calls of any kind.",
))

from dataclasses import dataclass, field
from typing import Optional, Literal

SCHEMA_VERSION = 1


@dataclass
class MarketSnapshot:
    schema_version: int = SCHEMA_VERSION
    market_id: str = ""
    condition_id: str = ""
    event_id: str = ""
    title: str = ""
    market_category: str = ""
    market_url: str = ""
    implied_probability: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread: Optional[float] = None
    depth_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    resolution_rule: str = ""
    event_age_days: float = 0.0
    time_to_resolution_days: Optional[float] = None
    captured_at: str = ""


@dataclass
class TradeFill:
    schema_version: int = SCHEMA_VERSION
    wallet_address: str = ""
    market_id: str = ""
    side: Literal["BUY", "SELL"] = "BUY"
    size: float = 0.0
    price: float = 0.0
    notional_usd: float = 0.0
    timestamp: str = ""
    event_slug: str = ""


@dataclass
class WalletProfile:
    schema_version: int = SCHEMA_VERSION
    wallet_address: str = ""
    username: Optional[str] = None
    wallet_age_days: float = 0.0
    trade_count: int = 0
    trades_per_day: float = 0.0
    total_notional_usd: float = 0.0
    avg_notional_usd: float = 0.0
    pnl_lifetime: float = 0.0
    pnl_recent_30d: Optional[float] = None
    win_rate: Optional[float] = None
    resolved_trade_count: int = 0
    max_drawdown_usd: float = 0.0
    market_breadth: int = 0
    market_hhi: float = 0.0
    directional_bias: float = 0.0
    timing_entropy: float = 0.0
    avg_holding_duration_days: Optional[float] = None
    entry_timing_label: Literal["early", "reactive", "late", "unknown"] = "unknown"
    category_performance: dict = field(default_factory=dict)
    behavior_label: str = "unknown"
    copy_trade_score: int = 0
    copy_trade_recommendation: Literal["copy", "watch", "avoid"] = "watch"
    why_copy_or_not: str = ""
    top_events: list = field(default_factory=list)
    last_updated_at: str = ""


@dataclass
class WalletFeatureVector:
    schema_version: int = SCHEMA_VERSION
    wallet_address: str = ""
    features: dict = field(default_factory=dict)
    computed_at: str = ""


@dataclass
class VerificationRecord:
    schema_version: int = SCHEMA_VERSION
    market_id: str = ""
    market_url: str = ""
    status: Literal["PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "DISABLED"] = "INSUFFICIENT_EVIDENCE"
    source_urls: list = field(default_factory=list)
    source_trust_scores: dict = field(default_factory=dict)
    primary_source_count: int = 0
    secondary_source_count: int = 0
    event_matches_resolution_rule: bool = False
    news_is_current: bool = False
    liquidity_sufficient: bool = False
    internally_consistent: bool = False
    confidence: float = 0.0
    explanation: str = ""
    verified_at: str = ""


@dataclass
class HistoricalEventRecord:
    schema_version: int = SCHEMA_VERSION
    market_id: str = ""
    similar_events: list = field(default_factory=list)
    precedent_score: float = 0.0
    precedent_summary: str = ""
    resembles_failed_setup: bool = False
    source_urls: list = field(default_factory=list)
    researched_at: str = ""


@dataclass
class MispricingSignal:
    schema_version: int = SCHEMA_VERSION
    market_id: str = ""
    market_url: str = ""
    signal_type: Literal["arbitrage", "cross_platform", "benchmark_llm"] = "arbitrage"
    implied_probability: float = 0.0
    benchmark_probability: Optional[float] = None
    benchmark_source: str = ""
    edge_size: float = 0.0
    direction: Literal["YES", "NO", "HOLD"] = "HOLD"
    confidence: float = 0.0
    explanation_plain: str = ""
    detected_at: str = ""


@dataclass
class MarketIntelligenceReport:
    schema_version: int = SCHEMA_VERSION
    market_id: str = ""
    market_url: str = ""
    market_category: str = ""
    mispricing: Optional[dict] = None            # serialized MispricingSignal
    verification: Optional[dict] = None            # serialized VerificationRecord
    historical_context: Optional[dict] = None      # serialized HistoricalEventRecord
    influential_wallets: list = field(default_factory=list)
    decision_label: Literal["BUY_YES", "BUY_NO", "MONITOR", "NO_TRADE"] = "NO_TRADE"
    confidence_tier: Literal["low", "medium", "high"] = "low"
    suggested_size_pct_of_risk_budget: float = 0.0
    max_loss_tolerance_usd: Optional[float] = None
    why_this_side: str = ""
    why_not_opposite: str = ""
    invalidation_conditions: str = ""
    built_at: str = ""


@dataclass
class DiscordAlertPayload:
    schema_version: int = SCHEMA_VERSION
    title: str = ""
    market_url: str = ""
    plain_explanation: str = ""
    evidence_summary: str = ""
    historical_summary: str = ""
    wallet_summary: str = ""
    decision_label: str = ""
    suggested_size_pct: float = 0.0
    confidence: str = ""
    main_risks: str = ""
    failure_conditions: str = ""
    cta_buttons: list = field(default_factory=list)
    wallet_addresses: list = field(default_factory=list)
    sent_at: str = ""
