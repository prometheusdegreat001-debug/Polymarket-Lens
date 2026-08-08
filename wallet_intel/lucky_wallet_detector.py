"""
Detects when a wallet's profitability is likely luck rather than skill:
concentrated in 1-2 trades, too small a sample, or drawdown too large
relative to gains. Pure arithmetic over real closed_positions data.
"""

from config.loader import wallet_scoring as ws_cfg
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="wallet_intel.lucky_wallet_detector",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure arithmetic over already-fetched closed_positions data.",
))


def detect_luck(closed_positions: list, features: dict) -> dict:
    resolved_count = features.get("resolved_trade_count", 0)
    total_pnl = sum(p["realized_pnl"] for p in closed_positions) if closed_positions else 0.0

    top_trade_pnl_share = 0.0
    if closed_positions and total_pnl > 0:
        top_pnl = max((p["realized_pnl"] for p in closed_positions), default=0.0)
        top_trade_pnl_share = round(top_pnl / total_pnl, 4) if total_pnl else 0.0

    small_sample = resolved_count < ws_cfg.min_sample_size
    concentrated = top_trade_pnl_share > ws_cfg.luck_penalty.max_concentration_share
    heavy_drawdown = features.get("max_drawdown_usd", 0.0) > ws_cfg.max_acceptable_drawdown_usd

    is_luck_dominated = small_sample or concentrated

    reasons = []
    if small_sample:
        reasons.append(f"only {resolved_count} resolved trades (need {ws_cfg.min_sample_size}+)")
    if concentrated:
        reasons.append(f"{top_trade_pnl_share*100:.0f}% of total PnL from a single trade")
    if heavy_drawdown:
        reasons.append(f"max drawdown ${features.get('max_drawdown_usd', 0):,.0f} exceeds threshold")

    return {
        "is_luck_dominated": is_luck_dominated,
        "top_trade_pnl_share": top_trade_pnl_share,
        "small_sample": small_sample,
        "concentrated": concentrated,
        "heavy_drawdown": heavy_drawdown,
        "reasons": reasons,
    }


def compute_penalty(luck_flags: dict) -> float:
    penalty = 0.0
    if luck_flags["small_sample"]:
        penalty += ws_cfg.luck_penalty.penalty_small_sample
    if luck_flags["concentrated"]:
        penalty += ws_cfg.luck_penalty.penalty_concentration
    if luck_flags["heavy_drawdown"]:
        penalty += ws_cfg.luck_penalty.penalty_drawdown
    return min(penalty, 1.0)
